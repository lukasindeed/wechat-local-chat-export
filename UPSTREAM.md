# 上游来源

本项目是在已有开源项目基础上的扩展，基础解密和密钥读取功能借用了其他作者的代码，并非从零独立实现。

| 上游项目 | 本项目借用的部分 | 许可 |
| --- | --- | --- |
| [Z-V-I/wechat4-db-export](https://github.com/Z-V-I/wechat4-db-export) | `scripts/wxbak.py` 的页面解密、联系人读取和消息渲染，以及 `scripts/wcdb_key.py` 的微信 4.1 扫描实现 | MIT，版权声明保留在 [LICENSE](LICENSE) |
| [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool) | 上述 `wcdb_key.py` 中引用的 Windows 密钥读取工具基础代码；模块原有来源链接保留 | MIT，原始许可保留在 [LICENSES/wcdb-key-tool-MIT.txt](LICENSES/wcdb-key-tool-MIT.txt) |

保留的上游模块：

- `scripts/wcdb_key.py`：Windows 微信 4.1 Config.Cipher 的只读密钥扫描。
- `scripts/wxbak.py`：SQLCipher 页面解密、联系人读取和富媒体消息渲染。

对 `wxbak.py` 的调整：使用 `Cryptodome` 加密库；通过 Windows 文档目录 API 识别迁移后的目录；在账号活跃时间中纳入 WAL 更新时间。

新增入口 `wechat_export.py` 和导出核心 `export_selected.py` 提供群聊、多会话、日期筛选、独立输出目录、增量日志校验和临时数据清理。对 WAL 重新计算解密后的校验和，并使用合成 SQLite 数据验证已提交的 WAL 内容能够被 SQLite 正确读取。

新增实现也按 MIT 许可提供；上游版权声明与许可条款保留，不将上游实现表述为本项目的原创成果。

本目录不是上游仓库的完整克隆。上游原有命令行入口仍保留在模块中；日常使用请以本目录 README 和 `wechat_export.py --help` 为准。
