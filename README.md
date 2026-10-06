# Aegis

> 桌面端编码智能体 —— 装上就能用，用自己的 API Key 接任意 OpenAI 兼容大模型。

Aegis 是一个运行在**本机**的编码智能体：能读代码、改代码、跑命令，多步自主迭代把任务做完。
不依赖 Docker、不依赖 Node、不需要手动装 Python 环境 —— 下载安装包，双击即用。

---

## 特性

| 能力 | 说明 |
|---|---|
| **7 个工具** | 读取文件 / 列出目录 / 检索代码 / 写入文件 / 编辑文件 / 执行命令 / 联网搜索 |
| **多步自主迭代** | Plan → Observe → Iterate 循环，能自己决定下一步做什么 |
| **三档权限** | 需用户批准（默认）/ 完全访问 / 仅保留对话（不碰文件） |
| **变更可回滚** | 完整变更清单，支持单条撤销与全部撤销 |
| **不会断线** | 多服务商自动降级链 + 本地离线兜底，厂商挂了自动切下一个 |
| **自带密钥库** | API Key 存系统凭据管理器，配置文件里只留引用 |
| **工作区沙箱** | 符号链接逃逸、`..` 穿越、大小写绕过均被拦截 |
| **会话管理** | 侧边栏历史、置顶、重命名、归档、删除 |

---

## 安装与运行

### 方式一：安装包（推荐）

1. 从 [Releases](https://github.com/Qiu1717/Aegis-demo/releases) 下载 `Aegis-Setup-<版本>.exe`
2. 双击安装
3. 启动 Aegis，按首启向导填写服务商与 API Key

> 覆盖升级：直接运行新版安装包即可，`AppId` 固定会就地覆盖，用户数据（会话、配置、密钥）不受影响。
> 若提示「进程正在使用」，先完全退出 Aegis（含托盘）再重试。

### 方式二：源码运行

```bash
# 1. 克隆
git clone https://github.com/Qiu1717/Aegis-demo.git
cd Aegis-demo

# 2. 建议用虚拟环境（Python 3.10）
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置（可选，也能用首启向导在界面里配）
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux

# 5. 运行
python main.py               # 浏览器端，访问 http://127.0.0.1:5000
python desktop/app.py        # 桌面端（pywebview 窗口）
```

### 打包成安装包

```bash
pip install -r requirements.txt -r desktop/requirements-desktop.txt
desktop\build.bat            # Windows：建 venv → 密钥扫描 → PyInstaller 打包
```

产物：`dist\Aegis\Aegis.exe`（免安装目录）与 `desktop\Output\Aegis-Setup-<版本>.exe`（安装包，需 Inno Setup 6）。

---

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | 原生 HTML + JavaScript + CSS（无构建链、无框架） |
| 后端 | Flask 3.0（Python 3.10） |
| 界面壳 | pywebview 5.x（Windows WebView2） |
| 数据库 | SQLite（默认，落用户数据目录）/ MySQL 8.0（配置 `.env` 后启用） |
| 大模型 | 任意 OpenAI 兼容端点：智谱 GLM / 魔搭 ModelScope / 硅基流动 / 通义 / Ollama / 自定义中转 |
| 密钥 | keyring（系统凭据管理器） |
| 打包 | PyInstaller 6.x + Inno Setup 6 |
| 依赖总数 | **6 个运行时依赖**（刻意保持精简，无 LangChain / torch / numpy 等重物） |

---

## 项目结构

```
Aegis-demo/
├── main.py                  # Web 端入口（Flask 应用工厂）
├── requirements.txt         # 运行时依赖（6 个）
├── pyproject.toml           # 项目元数据与工具配置
├── CHANGELOG.md             # 更新日志
│
├── apps/                    # 后端业务模块
│   ├── agent/               # 编码 Agent 内核：工具、沙箱、审批、Runner
│   ├── aichat/              # 会话与消息
│   ├── settings/            # 服务商配置、密钥、连接测试
│   ├── llm/                 # 多服务商客户端与降级链
│   ├── skills/              # 技能注册中心
│   ├── guard/               # 回答限制规则
│   ├── websearch/           # 联网检索
│   ├── security/            # 密钥库、脱敏
│   └── paths.py             # 路径与用户数据目录
│
├── desktop/                 # 桌面壳与打包
│   ├── app.py               # pywebview 窗口入口
│   ├── server.py            # 本地服务启动、单实例锁
│   ├── build.bat            # 一键构建
│   ├── build.spec           # PyInstaller 配置
│   ├── installer.iss        # Inno Setup 安装包脚本
│   └── README-打包与分发.md
│
├── static/                  # 前端资源（css / js）
├── templates/               # Jinja2 模板
├── config/config.json       # 默认配置
├── resource/sql/            # MySQL 建表脚本
├── tests/                   # 测试（pytest + Node 状态机测试）
└── docs/                    # 架构设计、PRD、使用手册
```

---

## 数据存放位置

程序**不写安装目录**，用户数据统一在：

| 内容 | 路径 |
|---|---|
| 数据库 | `%APPDATA%\Aegis\aegis.db` |
| 用户配置 | `%APPDATA%\Aegis\config.user.json` |
| 密钥 | 系统凭据管理器（keyring） |
| 日志 | `%APPDATA%\Aegis\logs\` |

卸载程序不会删除这些数据。

---

## 测试

```bash
pip install pytest

# 后端测试（205 个用例）
pytest tests/ -q

# 前端状态机测试（190 个断言，需 Node）
node tests/test_ui_defects.js
node tests/test_session_create_sync.js
node tests/test_session_delete_sync.js
node tests/test_settings_close.js
node tests/test_welcome_view.js
node tests/test_realtime_feedback.js
```

---

## 安全说明

- `.env` 已被 `.gitignore` 排除，**切勿提交含真实密钥的配置**
- API Key 存于系统凭据管理器，配置文件只留 `key_ref` 引用
- 日志输出经脱敏过滤，密钥不会落盘
- 本地服务仅监听 `127.0.0.1`，并有随机 Token 校验（防止局域网内其他程序调用）
- Agent 的文件与命令操作受工作区沙箱约束，越界路径一律拒绝

---

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

---

## 开源协议

MIT License —— 见 [LICENSE](LICENSE)。
