import base64
import contextlib
import hashlib
import io
import json
import re
import sqlite3
import struct
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import wxbak


class ExportError(RuntimeError):
    pass


class SnapshotChangedError(ExportError):
    pass


def wal_checksum(data, previous, byte_order):
    checksum_first, checksum_second = previous
    words = struct.unpack(byte_order + "I" * (len(data) // 4), data)
    for index in range(0, len(words), 2):
        checksum_first = (checksum_first + words[index] + checksum_second) & 0xFFFFFFFF
        checksum_second = (checksum_second + words[index + 1] + checksum_first) & 0xFFFFFFFF
    return checksum_first, checksum_second


def decrypt_wal(key, source, destination):
    data = source.read_bytes()
    if len(data) < 32:
        return {"frames": 0, "commit_pages": None}
    magic, version, page_size = struct.unpack(">III", data[:12])
    if magic not in (0x377F0682, 0x377F0683) or page_size != wxbak.PAGE_SZ:
        raise RuntimeError("Unsupported WAL header")
    byte_order = "<" if magic == 0x377F0682 else ">"
    checksum = wal_checksum(data[:24], (0, 0), byte_order)
    if checksum != struct.unpack(">II", data[24:32]):
        raise RuntimeError("Invalid WAL header checksum")
    original_checksum = checksum
    output = bytearray(data[:32])
    committed_length = 32
    committed_frames = 0
    committed_pages = None
    frame_size = 24 + page_size
    for frame_index, offset in enumerate(range(32, len(data) - frame_size + 1, frame_size)):
        frame_header = data[offset:offset + 24]
        if frame_header[8:16] != data[16:24]:
            break
        encrypted_page = data[offset + 24:offset + frame_size]
        original_checksum = wal_checksum(frame_header[:8] + encrypted_page, original_checksum, byte_order)
        if original_checksum != struct.unpack(">II", frame_header[16:24]):
            if frame_index == 0:
                raise RuntimeError("Invalid first WAL frame checksum")
            break
        page_number, commit_pages = struct.unpack(">II", frame_header[:8])
        if not page_number:
            break
        plain_page = wxbak.dec_page(key, encrypted_page, page_number)
        checksum = wal_checksum(frame_header[:8] + plain_page, checksum, byte_order)
        output.extend(frame_header[:16] + struct.pack(">II", *checksum) + plain_page)
        if commit_pages:
            committed_length = len(output)
            committed_frames = frame_index + 1
            committed_pages = commit_pages
    if committed_frames:
        destination.write_bytes(output[:committed_length])
    return {"frames": committed_frames, "commit_pages": committed_pages}


def file_signature(path):
    if not path.exists():
        return None
    metadata = path.stat()
    return metadata.st_size, metadata.st_mtime_ns


def snapshot_database(source, relative, key, working, report=True):
    source_wal = source.with_name(source.name + "-wal")
    before = file_signature(source), file_signature(source_wal)
    encrypted = working / "encrypted" / relative
    encrypted.parent.mkdir(parents=True, exist_ok=True)
    encrypted.write_bytes(source.read_bytes())
    encrypted_wal = encrypted.with_name(encrypted.name + "-wal")
    if before[1]:
        encrypted_wal.write_bytes(source_wal.read_bytes())
    after = file_signature(source), file_signature(source_wal)
    if before != after:
        raise SnapshotChangedError("微信正在更新数据库，未能取得一致快照，请稍后重试。")
    with encrypted.open("rb") as database_file:
        if not wxbak.verify_key(key, database_file.read(wxbak.PAGE_SZ)):
            raise RuntimeError("Database key verification failed")
    destination = working / "decrypted" / relative
    wxbak.dec_db(key, encrypted, destination)
    wal_info = {"frames": 0, "commit_pages": None}
    if before[1] and before[1][0] > 32:
        wal_info = decrypt_wal(key, encrypted_wal, destination.with_name(destination.name + "-wal"))
    with contextlib.closing(sqlite3.connect(str(destination))) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed")
        if wal_info["commit_pages"] is not None:
            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            if page_count != wal_info["commit_pages"]:
                raise RuntimeError("WAL was not replayed correctly")
    if report:
        print(json.dumps({"database": relative.as_posix(), "integrity": "ok", **wal_info}), flush=True)
    return destination, wal_info


def decode_content(content, compression_flag):
    if not isinstance(content, bytes):
        return content or "", None
    decoded = content
    if compression_flag == 4:
        with wxbak.zstandard.ZstdDecompressor().stream_reader(io.BytesIO(content)) as reader:
            decoded = reader.read()
    try:
        return decoded.decode("utf-8"), None
    except UnicodeDecodeError:
        return decoded.decode("utf-8", "replace"), base64.b64encode(decoded).decode("ascii")


def date_bounds(start=None, end=None):
    def parse(value, is_end):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ExportError("日期格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。") from error
        if is_end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed += timedelta(days=1)
            return parsed.timestamp() - 1
        return parsed.timestamp()

    start_timestamp, end_timestamp = parse(start, False), parse(end, True)
    if start_timestamp is not None and end_timestamp is not None and start_timestamp > end_timestamp:
        raise ExportError("开始日期不能晚于结束日期。")
    return start_timestamp, end_timestamp


def conversation_record(username, contact=None):
    contact = contact or {}
    return {"username": username, "remark": contact.get("remark", ""),
            "nick": contact.get("nick", ""), "alias": contact.get("alias", ""),
            "display": contact.get("remark") or contact.get("nick") or username,
            "kind": "群聊" if username.endswith("@chatroom") else "单聊"}


def matches_kind(conversation, kind):
    return kind == "all" or conversation["kind"] == {"group": "群聊", "person": "单聊"}[kind]


def search_conversations(contacts, query="", kind="all"):
    keyword = query.casefold()
    matches = []
    for username, contact in contacts.items():
        conversation = conversation_record(username, contact)
        if matches_kind(conversation, kind) and any(keyword in str(conversation[field]).casefold()
                                                  for field in ("username", "remark", "nick", "alias")):
            matches.append(conversation)
    return sorted(matches, key=lambda conversation: (conversation["kind"], conversation["display"], conversation["username"]))


def resolve_targets(contacts, selectors, kind="all"):
    targets, seen = [], set()
    for selector in selectors:
        if not selector.strip():
            raise ExportError("会话名称不能为空。")
        candidates = search_conversations(contacts, selector, kind)
        exact = [conversation for conversation in candidates
                 if any(selector.casefold() == str(conversation[field]).casefold()
                        for field in ("username", "remark", "nick", "alias"))]
        candidates = exact or candidates
        if not candidates and (selector.startswith("wxid_") or selector.endswith("@chatroom")):
            conversation = conversation_record(selector)
            if matches_kind(conversation, kind):
                candidates = [conversation]
        if not candidates:
            raise ExportError(f"找不到会话「{selector}」，请用 list 搜索备注名、昵称、群名或 ID。")
        if len(candidates) != 1:
            details = "\n".join(f"  {candidate['display']} [{candidate['kind']}] {candidate['username']}"
                                for candidate in candidates)
            raise ExportError(f"「{selector}」匹配到多个会话，请改用下列唯一 ID：\n{details}")
        target = candidates[0]
        if target["username"] not in seen:
            targets.append(target)
            seen.add(target["username"])
    return targets


def load_member_names(contact_path):
    members = {}
    with contextlib.closing(sqlite3.connect(str(contact_path))) as connection:
        columns = [column[1] for column in connection.execute("PRAGMA table_info(chatroom_member)")]
        room_column = next((name for name in ("room_id", "chatroom_id", "chat_room_name") if name in columns), None)
        user_column = next((name for name in ("member_id", "username", "user_name") if name in columns), None)
        name_column = next((name for name in ("display_name", "room_nick_name", "nick_name", "member_name") if name in columns), None)
        if room_column and user_column and name_column:
            for room, username, name in connection.execute(f'SELECT "{room_column}","{user_column}","{name_column}" FROM chatroom_member'):
                if name:
                    members[(wxbak.ds(room), wxbak.ds(username))] = wxbak.ds(name)
    return members


def conversation_counts(decrypted_root):
    counts = {}
    for database in message_shards(decrypted_root):
        with contextlib.closing(sqlite3.connect(str(database))) as connection:
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
                if re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                    counts[table[4:]] = counts.get(table[4:], 0) + connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return counts


def message_shards(database_root):
    return sorted((path for path in (Path(database_root) / "message").glob("message_*.db")
                   if re.fullmatch(r"message_\d+\.db", path.name)),
                  key=lambda path: int(path.stem.rsplit("_", 1)[1]))


def read_conversation(account, decrypted_root, target, contacts, member_names=None,
                      start_timestamp=None, end_timestamp=None, self_name="我"):
    table = "Msg_" + hashlib.md5(target["username"].encode("utf-8")).hexdigest()
    members = member_names or {}
    messages, database_counts = [], []

    def resolve_sender(username):
        if username == account["wxid"]:
            return self_name
        member_display = members.get((target["username"], username))
        contact = contacts.get(username, {})
        return member_display or contact.get("remark") or contact.get("nick") or username

    emoji_context = {"cap": {}, "pk": {}, "md2pk": {}, "resolve": resolve_sender}
    clauses, parameters = [], []
    if start_timestamp is not None:
        clauses.append("messages.create_time >= ?")
        parameters.append(start_timestamp)
    if end_timestamp is not None:
        clauses.append("messages.create_time <= ?")
        parameters.append(end_timestamp)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    for database in message_shards(decrypted_root):
        with contextlib.closing(sqlite3.connect(str(database))) as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                database_counts.append({"database": database.name, "source_messages": 0, "messages": 0})
                continue
            connection.text_factory = bytes
            source_count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            expected_count = connection.execute(f'SELECT COUNT(*) FROM "{table}" AS messages' + where, parameters).fetchone()[0]
            cursor = connection.execute(f'SELECT messages.*,senders.user_name AS export_sender '
                                        f'FROM "{table}" AS messages LEFT JOIN Name2Id AS senders '
                                        'ON messages.real_sender_id=senders.rowid' + where, parameters)
            columns = [column[0] for column in cursor.description]
            shard_count = 0
            for row in cursor:
                record = dict(zip(columns, row))
                timestamp = record["create_time"] or 0
                sender = wxbak.ds(record["export_sender"])
                local_type = record["local_type"]
                raw_content, content_base64 = decode_content(record["message_content"], record.get("WCDB_CT_message_content", 0))
                render_content = raw_content
                if target["kind"] == "群聊" and ":\n" in raw_content:
                    prefix, _, content = raw_content.partition(":\n")
                    if prefix == sender or (not sender or sender == target["username"]) and re.fullmatch(r"[^\s:]{1,256}", prefix):
                        sender = prefix
                        render_content = content
                is_system = not sender or local_type == 10000
                speaker = "系统" if is_system else resolve_sender(sender)
                text = render_content if local_type == 1 else (wxbak.render(local_type, render_content, emoji_context) or "")
                message = {"time": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"),
                           "timestamp": timestamp, "speaker": speaker, "is_self": sender == account["wxid"] and not is_system,
                           "sender_id": sender, "type": local_type, "text": text,
                           "raw_content": raw_content, "source_database": database.name,
                           "sort_seq": record.get("sort_seq", 0) or 0}
                for identifier in ("local_id", "server_id", "msg_svr_id", "status"):
                    if identifier in record:
                        value = record[identifier]
                        message[identifier] = wxbak.ds(value) if isinstance(value, bytes) else value
                if content_base64:
                    message["raw_content_base64"] = content_base64
                messages.append(message)
                shard_count += 1
            if shard_count != expected_count:
                raise ExportError("导出条数与消息库不一致：" + database.name)
            database_counts.append({"database": database.name, "source_messages": source_count, "messages": shard_count})
    messages.sort(key=lambda message: (message["timestamp"], message["sort_seq"]))
    return messages, database_counts


def safe_folder_name(target):
    display = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "_", target["display"]).strip(" .")[:64] or "会话"
    suffix = hashlib.sha256(target["username"].encode("utf-8")).hexdigest()[:8]
    return display + "_" + suffix


def write_conversation(output_root, target, messages, database_counts, wal_records,
                       start=None, end=None, output_format="both"):
    if len(messages) != sum(database["messages"] for database in database_counts):
        raise ExportError("导出条数校验失败。")
    directory = Path(output_root) / safe_folder_name(target)
    directory.mkdir(parents=True, exist_ok=False)
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")
    first = messages[0]["time"] if messages else None
    last = messages[-1]["time"] if messages else None
    metadata = {"schema_version": 1, "conversation": target["display"], "kind": target["kind"],
                "username": target["username"], "remark": target["remark"],
                "nick": target["nick"], "alias": target["alias"], "count": len(messages),
                "exported_at": exported_at, "timezone": str(datetime.now().astimezone().tzinfo),
                "first_message": first, "last_message": last, "start": start, "end": end,
                "scope": "本机可用记录，按指定日期筛选" if start or end else "全部本机可用记录，未限制日期",
                "attachments_exported": False, "raw_content_preserved": output_format in ("both", "json")}
    paths = []
    if output_format in ("both", "json"):
        json_path = directory / "消息.json"
        json_path.write_text(json.dumps({**metadata, "messages": messages}, ensure_ascii=False, indent=2), encoding="utf-8")
        verified = json.loads(json_path.read_text(encoding="utf-8"))
        if verified["count"] != len(verified["messages"]):
            raise ExportError("JSON 文件条数校验失败。")
        paths.append(json_path)
    if output_format in ("both", "txt"):
        text_path = directory / "消息.txt"
        header = [f"{target['kind']}：{target['display']}", f"消息数：{len(messages)}",
                  f"时间：{first or '无记录'} 至 {last or '无记录'}",
                  f"筛选：{start or '不限'} 至 {end or '不限'}",
                  "图片、语音、视频、文件以消息标记保留；附件二进制未导出。", ""]
        body = [f"[{message['time']}] {message['speaker']}：{message['text']}" for message in messages]
        text_path.write_text("\n".join(header + body), encoding="utf-8")
        paths.append(text_path)
    manifest = {**metadata, "database_counts": database_counts, "wal": wal_records,
                "message_types": dict(Counter(str(message["type"]) for message in messages)),
                "speaker_counts": dict(Counter(message["speaker"] for message in messages)),
                "files": {path.name: {"bytes": path.stat().st_size,
                                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                          for path in paths}}
    (directory / "导出说明.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory, manifest


if __name__ == "__main__":
    from wechat_export import main
    raise SystemExit(main(["export", *sys.argv[1:]]))
