# Aegis 打包与分发说明

本文档覆盖 Aegis 桌面端从**源码到可分发安装包**的完整流程：前置条件、构建步骤、产物说明、首次运行、分发与绕过 SmartScreen、卸载与数据清理、常见问题排查，以及 macOS 移植要点。

> 技术选型：**pywebview + PyInstaller + Inno Setup**。不依赖 Node 工具链，不打包 Chromium（Windows 复用系统 WebView2）；安装包约 **12MB**，安装后占用约 **24MB**。

---

## 1. 前置条件

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统（构建机） | Windows 10 1809（build 17763）及以上，64 位 | 目标用户机器要求相同 |
| Python | **3.10.x** | 3.11/3.12 理论上可用，但本项目以 3.10 为基准验证；`build.bat` 会检查版本，低于 3.10 直接中止 |
| 磁盘空间 | 建议预留 3GB | venv 约 400MB，`build/` 与 `dist/` 中间产物约 1.5GB |
| Inno Setup | 6.x（免费版即可） | 官方下载：<https://jrsoftware.org/isdl.php>，选 `innosetup-6.x.x.exe`；一路下一步即可 |
| WebView2 运行时 | 目标机器需具备 | 见 1.1 |
| 代码签名证书 | 本次**不签名** | 见第 5 节的 SmartScreen 绕过说明 |

### 1.1 WebView2 运行时（重要）

Aegis 的界面由系统 WebView2 渲染：

- Windows 11、以及安装了近年累积更新的 Windows 10，**通常自带** WebView2，无需额外操作；
- 若目标机器缺失，Aegis 启动时会弹出中文提示，并自动打开下载页：
  <https://developer.microsoft.com/microsoft-edge/webview2/>
  - 联网环境：下载 **Evergreen Bootstrapper**（在线安装，几 MB）；
  - 内网 / 离线环境：下载 **Evergreen Standalone Installer**（离线包，约 130MB），可随 Aegis 一起分发。

---

## 2. 构建步骤

以下命令**全部在仓库根目录 `Aiagent\` 下执行**（即 `main.py` 所在目录）。

### 2.1 一键构建（推荐）

```bat
desktop\build.bat
```

脚本会自动完成：建 venv → 装依赖 → 构建前密钥扫描 → PyInstaller 打包 → 产物二次密钥扫描。**任何一步失败都会 `exit /b 1` 并打印原因。**

| 阶段 | 预计耗时 | 说明 |
|---|---|---|
| 建 venv | 30 秒 ~ 1 分钟 | 会先清理旧的 `.venv-build`，保证产物纯净 |
| 装依赖 | 2 ~ 5 分钟 | 受网络影响；失败请换镜像源后重跑 |
| 密钥扫描（源码） | < 5 秒 | 命中即中止构建 |
| PyInstaller 打包 | 2 ~ 6 分钟 | onedir + GUI 模式 |
| 密钥扫描（产物） | < 10 秒 | 命中即禁止分发 |

产物目录：`dist\Aegis\`

### 2.2 手动构建（需要排错时用）

```bat
python -m venv .venv-build
.venv-build\Scripts\python.exe -m pip install --upgrade pip
.venv-build\Scripts\pip.exe install -r requirements.txt -r desktop\requirements-desktop.txt
.venv-build\Scripts\pyinstaller.exe --noconfirm desktop\build.spec
```

> 手动构建**不会**执行密钥扫描。正式出包请务必走 `build.bat`。

### 2.3 生成安装包

用 Inno Setup 编译：

- 图形界面：右键 `desktop\installer.iss` → **Compile**；
- 命令行：`ISCC.exe desktop\installer.iss`。

耗时约 1 ~ 3 分钟，产物：`desktop\Output\Aegis-Setup-1.4.1.exe`。

### 2.4 版本号同步（改版本时必须改三处）

| 文件 | 位置 |
|---|---|
| `desktop\app.py` | `__version__ = "1.4.1"` |
| `desktop\installer.iss` | `#define MyAppVersion "1.4.1"` |
| `desktop\build.bat` | `set "VERSION=1.4.1"` |

三处保持一致，否则安装包名与程序内版本会对不上。

> ⚠️ **改完 `build.spec` 或新增 `apps/` 子模块后必须重新出包**。业务模块以字节码形式内嵌在
> `Aegis.exe` 里（见 3.1），只要源码改动没有重新执行 PyInstaller，安装包里就仍是旧代码——
> 源码测试全绿也代表不了安装包是新的。新增整个子包（如 1.4.0 的 `apps/skills/`）时，
> 除依赖 `build.spec` 里的 `collect_submodules("apps")` 兜底外，**务必再往 `hiddenimports`
> 显式补一行**，双保险，避免被静态分析漏掉。

---

## 3. 产物说明

### 3.1 `dist\Aegis\` 目录结构

```
dist\Aegis\                     （整体约 24MB）
├── Aegis.exe            主程序（约 5.3MB；GUI 模式，无控制台黑窗）
│                        └─ 业务代码（main / apps / desktop 的所有模块，含 apps/skills）
│                           以字节码形式内嵌在 exe 内的 PYZ 归档里（zlib 压缩），
│                           ★ 不是散落的 .py/.pyc 文件——源码改了必须重新打包才生效
└── _internal\           （约 18.7MB）第三方依赖与随包资源
    ├── python310.dll
    ├── base_library.zip            Python 标准库
    ├── templates\                  Jinja2 页面模板
    ├── static\                     css / js 前端资源
    ├── config\                     config.json 默认配置（不含任何密钥）
    ├── webview\                    pywebview（含 WebView2 桥接 dll）
    ├── flask-* / werkzeug-* / keyring-* / pythonnet\ / clr_loader\ …
    └── …                           各类 .pyd / .dll 原生扩展
```

`_internal` 是 PyInstaller 6 的 onedir 布局标准目录，**不要单独拷贝 `Aegis.exe`**，必须与整个 `Aegis\` 目录一起分发/安装。

> 校验方法（排查“打包里到底有没有某个新模块”时用）：业务模块位于 `Aegis.exe` 内嵌的 `PYZ.pyz` 中，
> 可用 PyInstaller 自带的 `CArchiveReader(exe).open_embedded_archive("PYZ.pyz").toc` 列出全部模块名，
> 确认目标模块（如 `apps.skills.services`）在列即代表已正确打包。

### 3.2 最终安装包

| 项目 | 值 |
|---|---|
| 路径 | `desktop\Output\Aegis-Setup-1.4.1.exe` |
| 体积 | 安装包约 **12MB**；安装后占用约 **24MB**（`dist\Aegis\` 整体） |
| 默认安装目录 | `C:\Program Files\Aegis` |
| 快捷方式 | 开始菜单「Aegis」文件夹；桌面快捷方式可选（默认不勾选） |

体积主要来自 Python 解释器 + Flask 全家桶 + WebView2 桥接层。若需要进一步瘦身，可考虑裁剪 `pymysql`（未使用 MySQL 时）——但需先确认 `apps/__init__.py` 的数据库探测逻辑不受影响。

---

## 4. 安装与首次运行

1. **双击** `Aegis-Setup-1.4.1.exe`，按向导一步步安装（需要管理员权限写入 Program Files；UAC 弹窗可降权安装到用户目录）。
2. 安装结束勾选「立即启动 Aegis」，或之后从开始菜单 / 桌面快捷方式打开。
3. **首次启动向导**会依次引导你：
   1. 选择模型服务商（智谱 / ModelScope / SiliconFlow / 通义千问 / Ollama 等）；
   2. 填写**你自己的** API Key（填 `<你的 API Key>`，Aegis 不会内置任何密钥）；
      - 密钥优先写入系统凭据管理器（Windows 凭据管理器 / DPAPI），不写进配置文件；
   3. 选择**工作区目录**——Agent 只能读写该目录内的文件，越界路径会被直接拒绝。
4. 完成后即可在左侧输入任务，让 Aegis 读代码、改文件、跑命令（写文件与执行命令都会先给你看差异并等待确认）。

> 数据位置：`%APPDATA%\Aegis\`（数据库、用户配置、日志、运行时文件）。安装目录保持只读，便于普通权限运行。

### 4.1 覆盖升级（从旧版升到新版）

新版安装包可**直接双击就地升级，无需先卸载旧版**。机制来自 `installer.iss`：

| 配置 | 作用 |
|---|---|
| `AppId` 为固定常量（不随版本变化） | Inno Setup 据此识别为“同一应用的新版本”，走升级而非并存 |
| `DefaultDirName={autopf}\Aegis` 固定 | 新版覆盖到同一安装目录 |
| `[Files]` 带 `ignoreversion recursesubdirs createallsubdirs` | 逐文件覆盖旧程序（含 `_internal`），同名文件以新版为准 |
| 无 `[UninstallDelete]` 段 | 升级/卸载都不会碰用户数据目录 |

**升级时被保留**（都在 `%APPDATA%\Aegis\`，不在安装目录内）：会话数据库、`config.user.json`（含已注册/启用的技能、自定义模型服务商）、系统凭据库中的密钥、日志。旧配置若还没有 `skills` 段，首次启动读取技能清单时会自动补齐内置技能，无需手工迁移。

> 建议：跨大版本升级前先备份 `%APPDATA%\Aegis\`（复制整个文件夹即可），出问题可整目录还原。

---

## 5. 分发

### 5.1 分发方式

- 内网共享 / 网盘：直接放 `Aegis-Setup-1.4.1.exe`；
- 邮件/IM：注意附件大小（约 35~55MB，多数邮箱可承受）；
- 不要单独分发 `dist\Aegis\Aegis.exe`，会缺少 `_internal` 而启动失败。

### 5.2 未签名时的 SmartScreen 绕过（本次不签名，必读）

未签名的 exe 在 Windows 上会触发 **Microsoft Defender SmartScreen** 拦截：

> “Windows 已保护你的电脑” / “未知发布者”

绕过步骤：

1. 在蓝色警告窗口点击 **“更多信息”**（More info）；
2. 出现 **“仍要运行”**（Run anyway）按钮后点击；
3. 之后按正常流程安装。

管理员若想在企业内网批量消警，可以：
- 对企业内部分发使用 `右键 → 属性 → 解除锁定`（Unblock）；
- 或在域策略/SmartScreen 白名单中加入该文件哈希。

> 若后续要做正式对外分发，强烈建议购买代码签名证书（OV/EV）并对 exe 签名——EV 证书还能立刻建立 SmartScreen 信誉。签名不属于本次范围。

### 5.3 完整性校验（建议）

分发时附一段校验信息，接收方核对：

```bat
certutil -hashfile Aegis-Setup-1.4.1.exe SHA256
```

把输出的哈希值与文件大小、日期一起随包公布即可（无需强制）。

---

## 6. 卸载与数据清理

### 6.1 卸载程序

- 开始菜单 → Aegis → **卸载 Aegis**；
- 或：Windows 设置 → 应用 → 已安装的应用 → Aegis → 卸载。

### 6.2 用户数据目录（卸载后保留）

卸载**不会**删除 `%APPDATA%\Aegis\`，其中包含：

| 内容 | 路径 |
|---|---|
| 会话与消息数据库 | `%APPDATA%\Aegis\aegis.db` |
| 用户配置（服务商、模型、工作区） | `%APPDATA%\Aegis\config.user.json` |
| 密钥库密文 / 索引 | `%APPDATA%\Aegis\secrets.enc`、`keys.index.json` |
| 日志 | `%APPDATA%\Aegis\logs\aegis.log` |
| 运行时文件（端口、token、pid） | `%APPDATA%\Aegis\run\runtime.json` |

**保留的理由**：卸载重装后配置不丢；误删等于永久丢失用户数据（含密钥）。

### 6.3 手动彻底清理

确实要清空时：

1. 先卸载 Aegis；
2. 在资源管理器地址栏输入 `%APPDATA%\Aegis` 回车，删除整个 `Aegis` 文件夹；
3. 若要连系统凭据一起清：控制面板 → 凭据管理器 → Windows 凭据，删除 Aegis 相关条目。

> 清理前请确认已备份需要的会话数据——这一步**不可逆**。

---

## 7. 常见问题排查

| 现象 | 原因与处理 |
|---|---|
| 启动弹窗提示“缺少 WebView2 运行时” | 按第 1.1 节安装 WebView2（联网用 Bootstrapper，离线用 Standalone 包），装完重启 Aegis |
| 提示“Aegis 已经在运行” | 单例保护生效：已自动打开正在运行的那个实例。若确认没有窗口，删除 `%APPDATA%\Aegis\run\aegis-server.lock` 后重试 |
| 提示“端口 5000~5049 均被占用” | 关闭占用端口的程序，或重启机器后再启动 |
| 窗口打开后一直白屏 | 看 `%APPDATA%\Aegis\logs\aegis.log`；多为服务未就绪或模板/静态资源缺失（检查 `_internal\templates` 与 `_internal\static` 是否存在） |
| 服务起不来、立刻退出 | 看日志；开发态可直接 `python main.py` 复现。GUI 模式下的崩溃输出会落到 `%APPDATA%\Aegis\logs\aegis-stdio.log` |
| 模型调用报“未配置 API Key / 服务商不可用” | 打开设置面板重新选择服务商并填写自己的 API Key；Agent 模式不会回退到离线引擎 |
| 审批卡片点了没反应 | 服务必须是多线程模式（`desktop/server.py` 的 `threaded=True`）；若被改成单线程，SSE 会与审批请求互相饿死 |
| 改了代码但界面没变 | 前端资源被打包进 `_internal`，需重新执行 `build.bat` 并重新安装 |

**日志位置（最重要）**：`%APPDATA%\Aegis\logs\aegis.log`
（滚动日志，单文件 5MB，最多保留 3 个历史文件；日志内容会做密钥脱敏。）

---

## 8. macOS 说明

**本次不提供 macOS 构建物**，但代码已按可移植方式编写：全路径用 `pathlib`/`os.path` 处理，平台差异集中在 `sys.platform` 分支（`desktop/app.py` 的 GUI 后端选择、`desktop/server.py` 的进程存活判定、WebView2 检测在非 Windows 平台直接跳过）。

若后续要补 macOS，主要工作点：

1. **窗口后端**：`desktop/app.py` 已在 darwin 分支选择 `cocoa`，pywebview 会用系统 WKWebView，无需 WebView2；
2. **密钥存储**：`apps/security/keystore.py` 已有分层实现，macOS 分支需接 Keychain（`keyring.backends.macOS`），并保留 DPAPI/对称回退之外的新回退路径；
3. **数据目录**：`apps/paths.py` 已定义 `~/Library/Application Support/Aegis/`，无需改动；
4. **打包**：仍可用 PyInstaller（`build.spec` 的 `console=False`、onedir 布局通用），但需产出 `.app` 束与 `.dmg`，建议改用 `create-dmg` 或 `hdiutil` 自行封装；
5. **签名与公证**：macOS 强制要求 Developer ID 签名 + Apple 公证（notarization），否则 Gatekeeper 会拦截，**这一步无法绕过**，需要 Apple 开发者账号；
6. **安装器**：Inno Setup 仅适用于 Windows，macOS 需改用 dmg 拖拽安装或 pkg 安装包。

---

## 附：密钥安全红线

- 任何文件（含注释、示例、spec/iss/bat/md）都**不得**出现真实或占位密钥；占位一律写作 `<你的 API Key>`；
- 密钥只在用户机器上、由用户本人通过设置面板填写，落到系统凭据管理器；
- `build.bat` 在构建前与构建后各做一次密钥扫描（常见密钥前缀、`api_key` 被赋字符串字面量、JSON 中非空字面量赋值），命中即 `exit /b 1`，**禁止出包**；
- 日志统一走 `apps/security/redact.py` 脱敏，绝不把密钥写进 `aegis.log`。
