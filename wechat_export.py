import argparse
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import export_selected as core

TOOL_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = TOOL_ROOT / "exports"


def discover_accounts(data_dir=None):
    accounts = core.wxbak.find_accounts(data_dir)
    if data_dir:
        requested = Path(data_dir).resolve()
        accounts = [account for account in accounts if account["db_storage"].resolve().is_relative_to(requested)]
    if not accounts:
        raise core.ExportError("未找到微信数据目录，请用 --data-dir 指定 xwechat_files、账号目录或 db_storage。")
    return accounts


def select_account(data_dir=None, account_name=None):
    accounts = discover_accounts(data_dir)
    if account_name:
        return core.wxbak.pick_account(accounts, account_name)
    return accounts[0]


class SnapshotSession:
    def __init__(self, account, keyfile=None, progress=print):
        self.account = account
        self.keyfile = Path(keyfile) if keyfile else None
        self.progress = progress
        self.temporary = None
        self.prepared = {}
        self.wal_records = []
        self.counts = None

    def __enter__(self):
        core.wxbak.require_deps()
        temporary_root = TOOL_ROOT / ".work"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="snapshot-", dir=str(temporary_root))
        self.working = Path(self.temporary.name)
        self.decrypted_root = self.working / "decrypted"
        try:
            if self.keyfile:
                self.keys = json.loads(self.keyfile.read_text(encoding="utf-8"))
            else:
                core.wxbak.require_windows()
                import wcdb_key
                self.progress("正在读取本机微信数据，请保持微信登录。")
                temporary_keyfile = self.working / "keys.json"
                with contextlib.redirect_stdout(io.StringIO()):
                    wcdb_key._scan_memory_raw_key(str(self.account["db_storage"]), str(temporary_keyfile))
                self.keys = json.loads(temporary_keyfile.read_text(encoding="utf-8"))
            contact_path = self.prepare(Path("contact") / "contact.db")
            self.contacts, _ = core.wxbak._load_contact(self.account, self.decrypted_root)
            self.member_names = core.load_member_names(contact_path)
            return self
        except BaseException:
            self.temporary.cleanup()
            raise

    def __exit__(self, exception_type, exception, traceback):
        if self.temporary:
            self.temporary.cleanup()

    def prepare(self, relative):
        if relative in self.prepared:
            return self.prepared[relative]
        key_info = self.keys.get(str(relative)) or self.keys.get(relative.as_posix())
        if not isinstance(key_info, dict) or not key_info.get("enc_key"):
            raise core.ExportError("缺少数据库密钥：" + relative.as_posix())
        key = bytes.fromhex(key_info["enc_key"])
        for attempt in range(3):
            try:
                destination, wal_info = core.snapshot_database(
                    self.account["db_storage"] / relative, relative, key, self.working, report=False)
                break
            except core.SnapshotChangedError:
                if attempt == 2:
                    raise
                time.sleep(0.2)
        self.prepared[relative] = destination
        if relative.parent.name == "message":
            self.wal_records.append({"database": relative.name, **wal_info})
        return destination

    def prepare_messages(self):
        databases = core.message_shards(self.account["db_storage"])
        if not databases:
            raise core.ExportError("未发现消息库；请先将需要的手机记录迁移到电脑微信。")
        for index, database in enumerate(databases, start=1):
            relative = Path("message") / database.name
            if relative not in self.prepared:
                self.progress(f"正在准备聊天记录（{index}/{len(databases)}）。")
                self.prepare(relative)

    def list_conversations(self, query="", kind="all"):
        self.prepare_messages()
        if self.counts is None:
            self.counts = core.conversation_counts(self.decrypted_root)
        conversations = core.search_conversations(self.contacts, query, kind)
        conversations = [conversation for conversation in conversations
                         if conversation["username"] != self.account["wxid"]]
        for conversation in conversations:
            conversation["count"] = self.counts.get(hashlib.md5(conversation["username"].encode("utf-8")).hexdigest(), 0)
        return conversations


def create_run_directory(output_root, account):
    root = Path(output_root).expanduser().resolve()
    source_root = account["dir"].resolve()
    if root.is_relative_to(source_root):
        raise core.ExportError("输出目录不能位于微信账号的数据目录内，请选择其他文件夹。")
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=str(root)))


def export_targets(session, targets, output_root, start=None, end=None, output_format="both", self_name="我"):
    start_timestamp, end_timestamp = core.date_bounds(start, end)
    session.prepare_messages()
    run_directory = create_run_directory(output_root, session.account)
    results = []
    for target in targets:
        if target["username"] == session.account["wxid"]:
            raise core.ExportError("所选 ID 是当前账号，请选择联系人或群聊。")
        messages, database_counts = core.read_conversation(
            session.account, session.decrypted_root, target, session.contacts, session.member_names,
            start_timestamp, end_timestamp, self_name)
        directory, manifest = core.write_conversation(
            run_directory, target, messages, database_counts, session.wal_records, start, end, output_format)
        result = {"conversation": target["display"], "kind": target["kind"], "username": target["username"],
                  "count": len(messages), "directory": directory.name,
                  "first_message": manifest["first_message"], "last_message": manifest["last_message"]}
        results.append(result)
        session.progress(f"已导出 {target['display']} [{target['kind']}]：{len(messages):,} 条。")
    (run_directory / "导出汇总.json").write_text(json.dumps({"schema_version": 1,
                                                          "account": session.account["wxid"], "conversations": results,
                                                          "count": sum(result["count"] for result in results)},
                                                         ensure_ascii=False, indent=2), encoding="utf-8")
    session.progress("文件位置：" + str(run_directory))
    return run_directory, results


def show_conversations(conversations, limit=None):
    shown = conversations[:limit] if limit else conversations
    for index, conversation in enumerate(shown, start=1):
        print(f"{index:>3}. [{conversation['kind']}] {conversation['display']}  "
              f"{conversation['count']:,} 条  ID={conversation['username']}")
    if len(shown) < len(conversations):
        print(f"共 {len(conversations)} 个匹配，显示前 {len(shown)} 个；可缩小关键词。")
    else:
        print(f"共 {len(conversations)} 个匹配。")
    return shown


def interactive(arguments):
    print("微信聊天导出：联系人 / 群聊，TXT / JSON")
    accounts = discover_accounts(arguments.data_dir)
    if arguments.account:
        account = select_account(arguments.data_dir, arguments.account)
    else:
        latest_accounts = {}
        for candidate in accounts:
            latest_accounts.setdefault(candidate["wxid"], candidate)
        choices = list(latest_accounts.values())
        account = choices[0]
        if len(choices) > 1:
            for index, candidate in enumerate(choices, start=1):
                print(f"{index}. {candidate['wxid']}  {candidate['dir']}")
            choice = input("选择账号编号（回车使用 1）：").strip() or "1"
            if not choice.isdigit() or not 1 <= int(choice) <= len(choices):
                raise core.ExportError("账号编号无效。")
            account = choices[int(choice) - 1]
    print("当前账号数据：" + str(account["dir"]))
    with SnapshotSession(account, arguments.keyfile) as session:
        while True:
            selection = input("会话类型：1 联系人 / 2 群聊 / 3 全部（回车使用 3）：").strip() or "3"
            kind = {"1": "person", "2": "group", "3": "all"}.get(selection)
            if not kind:
                print("请输入 1、2 或 3。")
                continue
            query = input("输入备注、昵称、群名或 ID 搜索（回车显示全部）：").strip()
            conversations = session.list_conversations(query, kind)
            shown = show_conversations(conversations, limit=50)
            if not shown:
                if input("继续搜索？[Y/n]：").strip().casefold() == "n":
                    return 0
                continue
            choice = input("选择要导出的编号，多个用逗号分隔（回车重新搜索）：").strip()
            if not choice:
                continue
            numbers = choice.replace("，", ",").split(",")
            if any(not number.strip().isdigit() or not 1 <= int(number.strip()) <= len(shown) for number in numbers):
                print("编号无效，请重新搜索和选择。")
                continue
            selected = list(dict.fromkeys(int(number.strip()) - 1 for number in numbers))
            targets = [shown[index] for index in selected]
            start = input("开始日期 YYYY-MM-DD（回车不限）：").strip() or None
            end = input("结束日期 YYYY-MM-DD（回车不限，包含当天）：").strip() or None
            output = input(f"输出文件夹（回车使用 {DEFAULT_OUTPUT}）：").strip().strip('"') or DEFAULT_OUTPUT
            try:
                export_targets(session, targets, output, start, end)
            except core.ExportError as error:
                print("导出失败：" + str(error))
            if input("继续导出其他会话？[y/N]：").strip().casefold() != "y":
                return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Windows 微信本地聊天导出：联系人、群聊和多个会话。")
    commands = parser.add_subparsers(dest="command")
    for command, description in (("accounts", "查看本机账号和数据目录"), ("doctor", "检查运行环境"),
                                 ("list", "搜索联系人和群聊，查看本机消息条数"),
                                 ("export", "导出指定联系人或群聊"), ("interactive", "交互式选择并导出")):
        subparser = commands.add_parser(command, help=description)
        subparser.add_argument("--data-dir", help="微信数据目录；默认自动识别迁移后的文档目录")
        subparser.add_argument("--account", help="账号目录名、wxid 或账号目录完整路径")
        if command in ("list", "export", "interactive"):
            subparser.add_argument("--keyfile", help="使用已有密钥文件；不指定则自动读取已登录微信")
        if command in ("list", "export"):
            subparser.add_argument("--kind", choices=("all", "person", "group"), default="all",
                                   help="限制会话类型：全部、联系人、群聊")
        if command == "list":
            subparser.add_argument("--search", default="", help="搜索备注、昵称、群名或 ID")
        if command == "export":
            subparser.add_argument("--who", action="append", required=True, help="备注、昵称、群名或唯一 ID；可重复指定")
            subparser.add_argument("--start", help="开始日期或时间；默认不限")
            subparser.add_argument("--end", help="结束日期或时间；日期包含整天，默认不限")
            subparser.add_argument("--out", default=str(DEFAULT_OUTPUT), help="输出根目录，每次生成新文件夹")
            subparser.add_argument("--format", choices=("both", "txt", "json"), default="both")
            subparser.add_argument("--self-name", default="我", help="自己的显示名称")
    commands.add_parser("selftest", help="离线自检，不读取微信或真实聊天记录")
    return parser


def doctor(arguments):
    checks = [("Windows", os.name == "nt"), ("Python 3.10+", sys.version_info >= (3, 10)),
              ("AES 解密依赖", core.wxbak.AES is not None), ("消息解压依赖", core.wxbak.zstandard is not None)]
    for name, available in checks:
        print(f"{'通过' if available else '缺失'}：{name}")
    versions = core.wxbak.wechat_versions()
    print("已安装微信：" + (", ".join(versions) or "未识别"))
    print("本机已实测：微信 4.1.15.13；其他版本以实际导出结果为准。")
    for account in discover_accounts(arguments.data_dir):
        print("账号：" + account["wxid"] + "  " + str(account["dir"]))
    print("首次导出需要微信处于登录状态；遇到读取权限错误时，可用管理员身份运行。")
    return 0 if all(available for _, available in checks) else 1


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        arguments = parser.parse_args(["interactive"])
    try:
        if arguments.command == "selftest":
            import export_selftest
            return export_selftest.run()
        if arguments.command == "doctor":
            return doctor(arguments)
        if arguments.command == "accounts":
            for account in discover_accounts(arguments.data_dir):
                updated = datetime.fromtimestamp(account["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{account['wxid']}  最近更新 {updated}\n  {account['dir']}")
            return 0
        if arguments.command == "interactive":
            return interactive(arguments)
        if arguments.command == "export":
            core.date_bounds(arguments.start, arguments.end)
        account = select_account(arguments.data_dir, arguments.account)
        with SnapshotSession(account, arguments.keyfile) as session:
            if arguments.command == "list":
                show_conversations(session.list_conversations(arguments.search, arguments.kind))
            else:
                targets = core.resolve_targets(session.contacts, arguments.who, arguments.kind)
                export_targets(session, targets, arguments.out, arguments.start, arguments.end,
                               arguments.format, arguments.self_name)
        return 0
    except (core.ExportError, RuntimeError, OSError, ValueError, sqlite3.DatabaseError) as error:
        print("操作失败：" + str(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消，临时密钥和解密数据库已清理。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
