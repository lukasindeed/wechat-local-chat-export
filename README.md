# 微信聊天导出工具

将 Windows 微信中指定联系人或群聊的本机聊天记录导出为 TXT 和 JSON。支持搜索会话、一次导出多个会话、按日期筛选、多账号和迁移后的文档目录。

本项目**基于他人的开源项目扩展**，借用了 [Z-V-I/wechat4-db-export](https://github.com/Z-V-I/wechat4-db-export) 的基础代码，以及其引用的 [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool) 的密钥读取代码。新增部分包括交互选择、群聊和批量导出、日期筛选、WAL 校验与临时数据清理。上游作者的版权和 MIT 许可均保留，详见 [UPSTREAM.md](UPSTREAM.md) 和 [LICENSE](LICENSE)。

本机已验证微信 **4.1.15.13**。密钥读取使用微信 4.1 的内存结构，其他版本需要通过实际运行确认兼容性。

## 直接使用

1. 打开电脑微信并登录。
2. 双击本目录的 **启动微信聊天导出.cmd**。
3. 选择联系人或群聊，输入名称搜索，再选择一个或多个编号。
4. 日期留空即可导出全部本机记录，也可以填写开始、结束日期。
5. 导出结束后会显示文件位置。

首次使用前请安装 Python 3.10 或更新版本，并在工具目录运行 `python -m pip install -r requirements.txt`。如果提示无法读取微信进程，可右键启动文件，选择“以管理员身份运行”。

默认输出到工具目录内的 `exports`。每次导出都会新建一个带时间的文件夹，每个会话使用独立子文件夹；名称相同的会话通过 ID 摘要区分。

```text
exports/
  20261007-153000-随机后缀/
    示例联系人_ID摘要/
      消息.txt
      消息.json
      导出说明.json
    项目讨论群_ID摘要/
      消息.txt
      消息.json
      导出说明.json
    导出汇总.json
```

## 命令行使用

在 PowerShell 中切换到工具目录：

```powershell
cd .\wechat-local-chat-export
```

导出一个联系人，备注、昵称、微信号或 wxid 都可以作为名称：

```powershell
python wechat_export.py export --who "示例联系人"
```

搜索群聊，再用名称或唯一群 ID 导出：

```powershell
python wechat_export.py list --kind group --search "项目"
python wechat_export.py export --kind group --who "项目讨论群"
python wechat_export.py export --who "123456789@chatroom"
```

上面的群名、群 ID 是示例，请换成 `list` 返回的实际值。名称匹配优先使用完全相同的值，再尝试唯一的部分匹配；遇到多个同名会话会列出候选 ID，要求改用唯一 ID。

一次导出多个联系人或群聊，重复使用 `--who`：

```powershell
python wechat_export.py export --who "示例联系人" --who "项目讨论群"
```

按日期筛选，自定义输出位置：

```powershell
python wechat_export.py export --who "示例联系人" --start 2026-01-01 --end 2026-01-31 --out 'D:\微信聊天导出'
```

结束日期包含当天的全部记录。也可以传入精确时间，例如 `--start "2026-10-01 09:00:00" --end "2026-10-01 18:00:00"`；没有时区的时间按本机时区解释，精确结束时间包含该时刻。未指定的一侧不限制。

只导出 JSON，或修改自己的显示名：

```powershell
python wechat_export.py export --who "示例联系人" --format json --self-name "我"
```

查看账号和环境：

```powershell
python wechat_export.py accounts
python wechat_export.py doctor
```

工具会读取 Windows 实际的“文档”目录，包括迁移到 D 盘的目录，并结合微信配置查找数据。默认选择消息数据库最近更新的账号目录。多账号时可明确指定账号；如果同一账号有新旧两份目录，使用完整路径可以精确选择。

```powershell
python wechat_export.py list --account "wxid_example" --search "示例"
python wechat_export.py export --data-dir 'D:\WeChatData\xwechat_files' --who "示例联系人"
```

`--data-dir` 支持 `xwechat_files`、账号目录或 `db_storage`。明确指定后只在该目录内选账号，目录不正确时会报错。

如果已有合法的数据库密钥文件，也可以使用 `--keyfile '路径\all_keys.json'`，此时无需从微信进程重新读取密钥。密钥文件格式沿用底层工具，按数据库相对路径保存 `enc_key`。用户提供的文件会保留。

兼容旧脚本入口：

```powershell
python export_selected.py --who "示例联系人"
```

完整参数：

```powershell
python wechat_export.py --help
python wechat_export.py export --help
python wechat_export.py list --help
```

## 导出的内容

- TXT：按时间、排序序号排列，标注每条消息的发送者。
- JSON：包含精确时间、发送者 ID、是否由自己发送、消息类型、可读内容、原始消息内容、来源消息库，以及可用的消息标识。
- 群聊：根据数据库中的发送者字段区分群成员；有结构化群昵称时使用群昵称，否则使用联系人备注、昵称或原始 ID。
- 图片、语音、视频、表情、文件等：保留消息条目和原始消息内容，实际附件二进制不导出，也不进行语音转写。
- 导出说明：包含来源表条数、筛选后的条数、时间范围、消息类型统计和文件 SHA-256。

JSON 的 `schema_version` 当前为 `1`。空日期范围或本机没有消息的联系人会产生消息数为 `0` 的导出文件；不会把空结果当成丢失消息。公众号独立消息库不在当前导出范围内。

范围是**本机已经保存的记录**。只有手机上保存的历史记录，需要先用微信的迁移功能迁移到电脑。

## 本地处理和清理

导出时只读复制联系人库和所需消息库到临时目录，对副本解密、合并已提交的 WAL 增量，并执行 SQLite 完整性和条数检查。群聊和单聊共用这条处理流程。

工具运行时不联网。自动读取的密钥、加密副本和解密数据库临时保存在 `.work` 内，正常结束、取消或发生 Python 异常时都会清理。强制结束进程或断电可能留下临时目录；确认工具已关闭后，可以删除 `.work` 中的残留子目录。

## 换电脑安装与自检

要求 Windows、Python 3.10 或更新版本，以及已登录的微信 4.1。下载项目源码到另一台电脑后，在该目录安装依赖：

```powershell
python -m pip install -r requirements.txt
python wechat_export.py doctor
python wechat_export.py selftest
```

`selftest` 使用合成的加密 SQLite 数据和群聊消息，不读取真实微信聊天。它验证群聊发言人、重名选择、跨消息库导出、压缩内容、日期边界、WAL 新消息重放、输出隔离和失败时的临时数据清理。

## 源码

- `wechat_export.py`：命令行、交互选择、账号发现、临时数据生命周期和批量导出。
- `export_selected.py`：数据库快照、WAL 解密及校验、会话匹配、消息解析、导出和校验。
- `scripts/wcdb_key.py`、`scripts/wxbak.py`：来自上游项目的密钥读取和基础解密代码。
- `scripts/export_selftest.py`：离线自检。

上游来源和本地改动见 [UPSTREAM.md](UPSTREAM.md)。
