# Aegis 文档索引

> 完整说明、安装步骤、技术栈见仓库根目录的 **[README.md](../README.md)**。

本目录存放详细文档。

---

## 文档清单

| 文档 | 内容 |
|---|---|
| [../README.md](../README.md) | 项目简介、安装与运行、技术栈、项目结构（**从这里开始**） |
| [../CHANGELOG.md](../CHANGELOG.md) | 更新日志，按版本号记录全部变更 |
| [大模型配置指南.md](大模型配置指南.md) | 各服务商配置方式、免费额度、故障排查 |
| [开发与使用手册.md](开发与使用手册.md) | 开发环境、调试、常见问题 |
| [DESIGN-桌面端编码Agent-架构.md](DESIGN-桌面端编码Agent-架构.md) | 架构设计、安全模型、数据流 |
| [PRD-桌面端编码Agent-增量.md](PRD-桌面端编码Agent-增量.md) | 编码 Agent 增量需求文档 |
| [class-diagram.mermaid](class-diagram.mermaid) | 类图 |
| [sequence-diagram.mermaid](sequence-diagram.mermaid) | 时序图 |

---

## 快速上手

```bash
git clone https://github.com/Qiu1717/Aegis-demo.git
cd Aegis-demo
pip install -r requirements.txt
python main.py          # 浏览器端 → http://127.0.0.1:5000
```

也可以直接下载 [Releases](https://github.com/Qiu1717/Aegis-demo/releases) 里的
`Aegis-Setup-<版本>.exe` 安装，无需配置环境。

---

## 常见问题

**Q：数据存在哪里？**
A：不在安装目录。统一在 `%APPDATA%\Aegis\`：`aegis.db`（数据库）、
`config.user.json`（配置）、`logs/`（日志）。密钥存系统凭据管理器。
卸载程序不会删除这些数据。

**Q：为什么不用 Docker？**
A：早期版本用 Docker 部署，现已改为 PyInstaller 打包的 Windows 桌面应用
（见 `desktop/build.spec`），用户无需安装 Python 或 Docker。

**Q：数据库必须用 MySQL 吗？**
A：不是。默认 SQLite（落用户数据目录），配置 `.env` 里的 `DB_*` 后才启用 MySQL。
