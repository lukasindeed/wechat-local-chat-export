import contextlib
import hashlib
import io
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import export_selected as core
import wechat_export as cli


def timestamp(value):
    return int(datetime.fromisoformat(value).timestamp())


def reserved_database(path):
    data = bytearray(path.read_bytes())
    for offset in range(0, len(data), core.wxbak.PAGE_SZ):
        page = bytearray(data[offset:offset + core.wxbak.PAGE_SZ])
        header = 100 if offset == 0 else 0
        if page[header] != 13 or struct.unpack(">H", page[header + 1:header + 3])[0]:
            raise AssertionError("Fixture must use leaf pages without free blocks")
        cell_count, content_start = struct.unpack(">HH", page[header + 3:header + 7])
        if content_start - 80 < header + 8 + cell_count * 2:
            raise AssertionError("Fixture page has insufficient free space")
        page[content_start - 80:4016] = page[content_start:4096]
        page[4016:] = bytes(80)
        page[header + 5:header + 7] = struct.pack(">H", content_start - 80)
        for index in range(cell_count):
            position = header + 8 + index * 2
            pointer = struct.unpack(">H", page[position:position + 2])[0]
            page[position:position + 2] = struct.pack(">H", pointer - 80)
        data[offset:offset + 4096] = page
    data[20] = 80
    path.write_bytes(data)
    with contextlib.closing(sqlite3.connect(str(path))) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def encrypt_database(plain, encrypted, key, salt):
    data = plain.read_bytes()
    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    pages = [core.wxbak.enc_page(key, data[offset:offset + 4096], offset // 4096 + 1, salt, mac_key)
             for offset in range(0, len(data), 4096)]
    encrypted.parent.mkdir(parents=True, exist_ok=True)
    encrypted.write_bytes(b"".join(pages))


def encrypt_wal(plain, encrypted, key, salt):
    data = plain.read_bytes()
    byte_order = "<" if struct.unpack(">I", data[:4])[0] == 0x377F0682 else ">"
    checksum = core.wal_checksum(data[:24], (0, 0), byte_order)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(value ^ 0x3A for value in salt), 2, dklen=32)
    output = bytearray(data[:32])
    for offset in range(32, len(data) - 4119, 4120):
        header = data[offset:offset + 24]
        page_number = struct.unpack(">I", header[:4])[0]
        sealed = core.wxbak.enc_page(key, data[offset + 24:offset + 4120], page_number, salt, mac_key)
        checksum = core.wal_checksum(header[:8] + sealed, checksum, byte_order)
        output.extend(header[:16] + struct.pack(">II", *checksum) + sealed)
    encrypted.write_bytes(output)


class ExportChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wechat-export-selftest-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.tool_patch = patch.object(cli, "TOOL_ROOT", self.root)
        self.tool_patch.start()
        self.addCleanup(self.tool_patch.stop)
        self.account_dir = self.root / "xwechat_files" / "wxid_self_abcd"
        self.database_root = self.account_dir / "db_storage"
        self.account = {"dir": self.account_dir, "db_storage": self.database_root,
                        "name": "wxid_self_abcd", "wxid": "wxid_self"}
        self.keys = {}
        contact_path = self.root / "contacts-plain.db"
        with contextlib.closing(sqlite3.connect(str(contact_path))) as connection, connection:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT,alias TEXT)")
            connection.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
                ("wxid_self", "", "自己", "self"), ("wxid_alice", "好友A", "Alice", "alice"),
                ("wxid_other", "好友A", "Bob", "bob"), ("12345@chatroom", "", "工作群", ""),
                ("67890@chatroom", "", "工作群", "")])
            connection.execute("CREATE TABLE chatroom_member(room_id TEXT,member_id TEXT,display_name TEXT)")
            connection.execute("INSERT INTO chatroom_member VALUES ('12345@chatroom','wxid_member','小王')")
        self.seal(contact_path, Path("contact") / "contact.db")
        self.friend_table = "Msg_" + hashlib.md5(b"wxid_alice").hexdigest()
        self.group_table = "Msg_" + hashlib.md5(b"12345@chatroom").hexdigest()
        self.plain_messages = self.root / "messages-plain.db"
        with contextlib.closing(sqlite3.connect(str(self.plain_messages))) as connection, connection:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
            connection.executemany("INSERT INTO Name2Id VALUES (?)", [("wxid_self",), ("wxid_alice",), ("wxid_member",), (None,)])
            schema = "(local_id INTEGER PRIMARY KEY,server_id INTEGER,sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,message_content BLOB,WCDB_CT_message_content INTEGER,status INTEGER)"
            connection.execute(f'CREATE TABLE "{self.friend_table}" ' + schema)
            connection.execute(f'CREATE TABLE "{self.group_table}" ' + schema)
            compressed = core.wxbak.zstandard.ZstdCompressor().compress("literal &lt; 保持原文".encode("utf-8"))
            connection.executemany(f'INSERT INTO "{self.friend_table}" VALUES (?,?,?,?,?,?,?,?,?)', [
                (1, 101, 2, 2, timestamp("2026-10-05 00:00:00"), 1, compressed, 4, 0),
                (2, 102, 1, 1, timestamp("2026-10-05 00:00:00"), 1, "先发送", 0, 0),
                (3, 103, 3, 2, timestamp("2026-10-05 23:59:59"), 3, "<msg>image</msg>", 0, 0),
                (4, 104, 4, 1, timestamp("2026-10-06 00:00:00"), 1, "次日消息", 0, 0)])
            connection.executemany(f'INSERT INTO "{self.group_table}" VALUES (?,?,?,?,?,?,?,?,?)', [
                (1, 201, 1, 3, timestamp("2026-10-05 10:00:00"), 1, "群成员消息", 0, 0),
                (2, 202, 2, 1, timestamp("2026-10-05 10:01:00"), 1, "自己消息", 0, 0),
                (3, 203, 3, 4, timestamp("2026-10-05 10:02:00"), 1, "wxid_member:\n旧格式消息", 0, 0),
                (4, 204, 4, 3, timestamp("2026-10-05 10:03:00"), 10000, "<content>系统事件</content>", 0, 0)])
        self.seal(self.plain_messages, Path("message") / "message_0.db")
        self.keyfile = self.root / "fixture-keys.json"
        self.keyfile.write_text(json.dumps(self.keys), encoding="utf-8")

    def seal(self, plain, relative):
        reserved_database(plain)
        key, salt = os.urandom(32), os.urandom(16)
        encrypt_database(plain, self.database_root / relative, key, salt)
        self.keys[str(relative)] = {"enc_key": key.hex(), "salt": salt.hex()}

    def session(self):
        return cli.SnapshotSession(self.account, self.keyfile, progress=lambda message: None)

    def test_ambiguous_names_require_ids_and_groups_are_identified(self):
        with self.session() as session:
            with self.assertRaises(core.ExportError):
                core.resolve_targets(session.contacts, ["好友A"])
            with self.assertRaises(core.ExportError):
                core.resolve_targets(session.contacts, ["工作群"])
            target = core.resolve_targets(session.contacts, ["12345@chatroom"], "group")[0]
            self.assertEqual(target["kind"], "群聊")
            with self.assertRaises(core.ExportError):
                core.resolve_targets(session.contacts, ["wxid_alice"], "group")

    def test_search_filters_groups_and_deduplicates_selections(self):
        with self.session() as session:
            groups = session.list_conversations("工作", "group")
            self.assertEqual(len(groups), 2)
            self.assertEqual(sum(group["count"] for group in groups), 4)
            targets = core.resolve_targets(session.contacts, ["Alice", "wxid_alice"])
            self.assertEqual(len(targets), 1)

    def test_batch_exports_member_names_raw_content_and_matching_counts(self):
        with self.session() as session:
            targets = core.resolve_targets(session.contacts, ["Alice", "12345@chatroom"])
            directory, results = cli.export_targets(session, targets, self.root / "exports")
        self.assertEqual([result["count"] for result in results], [4, 4])
        group = json.loads((directory / results[1]["directory"] / "消息.json").read_text(encoding="utf-8"))
        self.assertEqual(group["kind"], "群聊")
        self.assertEqual([message["speaker"] for message in group["messages"]], ["小王", "我", "小王", "系统"])
        self.assertEqual(group["messages"][2]["text"], "旧格式消息")
        self.assertEqual(group["messages"][2]["raw_content"], "wxid_member:\n旧格式消息")
        friend = json.loads((directory / results[0]["directory"] / "消息.json").read_text(encoding="utf-8"))
        self.assertEqual(friend["messages"][0]["server_id"], 102)
        self.assertEqual(friend["messages"][1]["text"], "literal &lt; 保持原文")
        self.assertEqual(list((self.root / ".work").iterdir()), [])

    def test_end_date_includes_last_second_and_excludes_next_day(self):
        with self.session() as session:
            targets = core.resolve_targets(session.contacts, ["Alice"])
            _, results = cli.export_targets(session, targets, self.root / "exports", "2026-10-05", "2026-10-05")
        self.assertEqual(results[0]["count"], 3)
        self.assertEqual(results[0]["last_message"], "2026-10-05 23:59:59")

    def test_all_message_shards_are_merged_and_search_indexes_are_skipped(self):
        plain = self.root / "second-shard.db"
        with contextlib.closing(sqlite3.connect(str(plain))) as connection, connection:
            connection.execute("PRAGMA page_size=4096")
            connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
            connection.execute("INSERT INTO Name2Id VALUES ('wxid_alice')")
            connection.execute(f'CREATE TABLE "{self.friend_table}" '
                               '(local_id INTEGER PRIMARY KEY,sort_seq INTEGER,real_sender_id INTEGER,'
                               'create_time INTEGER,local_type INTEGER,message_content TEXT)')
            connection.execute(f'INSERT INTO "{self.friend_table}" VALUES (1,10,1,?,1,?)',
                               (timestamp("2026-10-06 02:00:00"), "第二分片消息"))
        self.seal(plain, Path("message") / "message_10.db")
        (self.database_root / "message" / "message_fts.db").write_bytes(b"not a message shard")
        self.keyfile.write_text(json.dumps(self.keys), encoding="utf-8")
        with self.session() as session:
            targets = core.resolve_targets(session.contacts, ["Alice"])
            _, results = cli.export_targets(session, targets, self.root / "exports")
        self.assertEqual(results[0]["count"], 5)
        self.assertEqual(results[0]["last_message"], "2026-10-06 02:00:00")

    def test_empty_range_is_a_valid_empty_export(self):
        with self.session() as session:
            targets = core.resolve_targets(session.contacts, ["Alice"])
            _, results = cli.export_targets(session, targets, self.root / "exports", "2020-01-01", "2020-01-02")
        self.assertEqual(results[0]["count"], 0)
        self.assertIsNone(results[0]["first_message"])

    def test_invalid_date_range_is_rejected(self):
        with self.assertRaises(core.ExportError):
            core.date_bounds("2026-10-06", "2026-10-05")
        with self.assertRaises(core.ExportError):
            core.date_bounds("invalid", None)

    def test_repeated_exports_use_new_directories_and_preserve_sources(self):
        before = {path: path.read_bytes() for path in self.database_root.rglob("*.db")}
        with self.session() as session:
            targets = core.resolve_targets(session.contacts, ["Alice"])
            first, _ = cli.export_targets(session, targets, self.root / "exports", output_format="txt")
            second, _ = cli.export_targets(session, targets, self.root / "exports", output_format="json")
        self.assertNotEqual(first, second)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertFalse(list(first.rglob("消息.json")))
        self.assertFalse(list(second.rglob("消息.txt")))
        with self.assertRaises(core.ExportError):
            cli.create_run_directory(self.database_root, self.account)

    def test_cleanup_also_runs_when_reading_fails(self):
        with self.assertRaises(RuntimeError):
            with self.session():
                raise RuntimeError("Synthetic error")
        invalid_keyfile = self.root / "invalid.json"
        invalid_keyfile.write_text("{}", encoding="utf-8")
        with self.assertRaises(core.ExportError):
            with cli.SnapshotSession(self.account, invalid_keyfile, progress=lambda message: None):
                pass
        self.assertEqual(list((self.root / ".work").iterdir()), [])

    def test_redirected_documents_are_discovered(self):
        redirected = self.root / "relocated-documents"
        (redirected / "xwechat_files").mkdir(parents=True)
        with patch.object(core.wxbak, "documents_folder", return_value=redirected), \
             patch.object(core.wxbak, "_ini_data_roots", return_value=[]), \
             patch.object(Path, "home", return_value=self.root):
            self.assertIn(redirected / "xwechat_files", core.wxbak.find_data_roots())

    def test_explicit_data_directory_does_not_fall_back_to_another_account(self):
        with patch.object(core.wxbak, "find_accounts", return_value=[self.account]):
            with self.assertRaises(core.ExportError):
                cli.discover_accounts(str(self.root / "missing"))

    def test_committed_wal_messages_are_replayed_and_incomplete_tail_is_ignored(self):
        key_info = self.keys[str(Path("message") / "message_0.db")]
        key, salt = bytes.fromhex(key_info["enc_key"]), bytes.fromhex(key_info["salt"])
        with contextlib.closing(sqlite3.connect(str(self.plain_messages))) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(f'INSERT INTO "{self.friend_table}" VALUES (5,105,5,2,?,1,?,0,0)',
                               (timestamp("2026-10-06 01:00:00"), "仅存在 WAL 的新消息"))
            connection.commit()
            encrypted_wal = self.database_root / "message" / "message_0.db-wal"
            encrypt_wal(self.plain_messages.with_name(self.plain_messages.name + "-wal"), encrypted_wal, key, salt)
            with encrypted_wal.open("ab") as output:
                output.write(b"incomplete frame")
            with self.session() as session:
                targets = core.resolve_targets(session.contacts, ["Alice"])
                _, results = cli.export_targets(session, targets, self.root / "exports")
                self.assertGreater(session.wal_records[0]["frames"], 0)
        self.assertEqual(results[0]["count"], 5)
        self.assertEqual(results[0]["last_message"], "2026-10-06 01:00:00")


def run():
    core.wxbak.require_deps()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ExportChecks)
    with contextlib.redirect_stdout(io.StringIO()):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1
