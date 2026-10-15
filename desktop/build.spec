# -*- mode: python ; coding: utf-8 -*-
"""
Aegis 打包配置（PyInstaller 6.x，onedir + GUI 模式）

用法（务必在仓库根目录 Aiagent/ 下执行）：
    pyinstaller --noconfirm desktop/build.spec

产物：
    dist/Aegis/Aegis.exe          主程序（console=False，无黑窗）
    dist/Aegis/_internal/         依赖、templates、static、config

设计取舍：
    · onefile=False（onedir）：启动无需解压到临时目录，冷启动更快、排错更容易；
      代价是目录而非单文件，由 Inno Setup 负责最终封成安装包。
    · console=False：桌面端不弹控制台；stdout/stderr 由 desktop/app.py 重定向到日志文件。
    · 不打包 Chromium：Windows 复用系统 WebView2，产物体积可控（约 60~90MB）。
"""
import os
import sys

# SPECPATH 由 PyInstaller 注入，指向本 spec 所在目录（.../Aiagent/desktop）
SPEC_DIR = os.path.abspath(SPECPATH)
ROOT_DIR = os.path.abspath(os.path.join(SPEC_DIR, os.pardir))  # .../Aiagent

# 让 apps / main / desktop 在 spec 解析阶段就可被导入（collect_submodules 需要）
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

APP_NAME = "Aegis"
ENTRY_SCRIPT = os.path.join(SPEC_DIR, "app.py")

# ── 1. 必须随包携带的数据目录 ──────────────────────────────
#    templates/ 页面、static/ 前端资源、config/ 默认配置，缺一不可。
datas = []
for folder in ("templates", "static", "config"):
    src = os.path.join(ROOT_DIR, folder)
    if not os.path.isdir(src):
        raise SystemExit("[build.spec] 缺少待打包目录：%s" % src)
    datas.append((src, folder))

# ── 1b. 洁净度闸门：禁止把本地数据/密钥打进安装包 ───────────
#     安装包必须「全新未配置」。开发时 .env、config.user.json、*.db
#     都躺在仓库根目录，一旦有人图省事把整个目录写进 datas 就会随包发布。
#     这里用**白名单 + 逐文件检查**兜死：只允许下面这些扩展名进包。
_ALLOWED_EXT = {".html", ".js", ".css", ".json", ".png", ".jpg", ".jpeg",
                ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf",
                ".sql", ".md", ".mermaid"}
# 明确禁止的文件名（即使扩展名合法）
_DENY_NAMES = {".env", "aegis.db", "aiagent.db", "config.user.json",
               "secrets.enc", "keys.index.json", "runtime.json",
               ".env.example"}

_packaged_bad = []
for _folder in ("templates", "static", "config"):
    _root = os.path.join(ROOT_DIR, _folder)
    for _dirpath, _dirnames, _filenames in os.walk(_root):
        for _fn in _filenames:
            _fp = os.path.join(_dirpath, _fn)
            _rel = os.path.relpath(_fp, ROOT_DIR)
            # .env.example 是配置模板，必须放行
            if _fn in _DENY_NAMES and _fn != ".env.example":
                _packaged_bad.append("%s（本地数据/密钥）" % _rel)
                continue
            _ext = os.path.splitext(_fn)[1].lower()
            if _ext not in _ALLOWED_EXT:
                _packaged_bad.append("%s（扩展名 %s 不在白名单）" % (_rel, _ext or "无"))

if _packaged_bad:
    raise SystemExit(
        "[build.spec] 洁净度检查未通过，以下文件不应进入安装包：\n  "
        + "\n  ".join(_packaged_bad[:20])
        + "\n请移除这些文件，或修正本白名单。安装包必须是全新未配置版本。")
print("[build.spec] 洁净度检查通过：待打包文件均为模板/静态资源/默认配置")

# ── 2. hiddenimports：动态导入的模块必须在这里显式列出 ──────
hiddenimports = [
    # 项目自身
    "main",
    "desktop",
    "desktop.server",
    "apps",
    "apps.paths",
    "apps.logsetup",
    # Agent 内核（T03/T04 全量子模块）
    "apps.agent",
    "apps.agent.schemas",
    "apps.agent.sandbox",
    "apps.agent.approval",
    "apps.agent.trace",
    "apps.agent.fileio",
    "apps.agent.terminal",
    "apps.agent.tools",
    "apps.agent.prompts",
    "apps.agent.parser",
    "apps.agent.runner",
    "apps.agent.routes",
    # 设置与密钥体系
    "apps.settings",
    "apps.settings.services",
    "apps.settings.routes",
    "apps.security",
    "apps.security.redact",
    "apps.security.errors",
    "apps.security.dpapi",
    "apps.security.keystore",
    # 模型接入
    "apps.llm",
    "apps.llm.providers",
    "apps.llm.client",
    "apps.llm.offline",
    # 既有业务模块
    "apps.aichat",
    "apps.aichat.services",
    "apps.aichat.routes",
    "apps.aichat.memory",
    "apps.aicontrol",
    "apps.aicontrol.services",
    "apps.aicontrol.routes",
    "apps.websearch",
    "apps.websearch.services",
    "apps.websearch.routes",
    "apps.websearch.weather",
    # 外部 Skill 接入机制（1.4.0）：显式列出，避免新增 skill 子模块被漏打包
    "apps.skills",
    "apps.skills.services",
    "apps.skills.routes",
    # Web 框架
    "flask",
    "flask_cors",
    "werkzeug",
    "werkzeug.serving",
    "jinja2",
    "markupsafe",
    "itsdangerous",
    "click",
    "blinker",
    # 网络与数据库
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "pymysql",
    # 桌面窗口（pywebview 会按平台动态加载子模块）
    "webview",
    "webview.util",
    "webview.js_bridge",
    "webview.screen",
    "webview.menu",
    "webview.platforms",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.cef",
    "webview.platforms.cocoa",
    "webview.platforms.qt",
    "webview.platforms.gtk",
    # 密钥库（keyring 后端是运行时按平台挑的，必须全部带上）
    "keyring",
    "keyring.backend",
    "keyring.backends",
    "keyring.backends.fail",
    "keyring.backends.chainer",
    "keyring.backends.null",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.macOS",
    "keyring.credentials",
    "jaraco.classes",
    "jaraco.context",
    "jaraco.functools",
]

# 用 PyInstaller 的钩子再补一层，防止后续新增子模块被漏掉
try:
    from PyInstaller.utils.hooks import collect_submodules

    for pkg in ("apps", "desktop", "keyring", "webview"):
        for module_name in collect_submodules(pkg):
            if module_name not in hiddenimports:
                hiddenimports.append(module_name)
except Exception:
    print("[build.spec] 提示：collect_submodules 不可用，仅使用显式 hiddenimports 列表")

# ── 3. excludes：剔除用不到且体积大的包 ────────────────────
#    注意：只剔「确定没被引用」的；误剔会让打包产物起不来。
excludes = [
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "sklearn",
    "skimage",
    "cv2",
    "PIL",
    "torch",
    "tensorflow",
    "IPython",
    "notebook",
    "pytest",
    "unittest",
    "pydoc",
    "doctest",
    "lib2to3",
]

# ── 4. 组装 ───────────────────────────────────────────────
a = Analysis(
    [ENTRY_SCRIPT],
    pathex=[ROOT_DIR, SPEC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

# 需要自定义图标时：把 desktop/aegis.ico 放好后，把 icon=None 改成 icon=r"desktop\aegis.ico"
ICON_PATH = os.path.join(SPEC_DIR, "aegis.ico")
icon_arg = ICON_PATH if os.path.isfile(ICON_PATH) else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
