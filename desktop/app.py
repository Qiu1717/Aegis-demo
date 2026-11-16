# -*- coding: utf-8 -*-
"""
desktop/app.py — Aegis 桌面壳入口（pywebview + 本地服务）

职责：
    1. 启动内嵌的本地服务（desktop/server.py），拿到真实端口；
    2. 用 pywebview 打开 http://127.0.0.1:<port>/（由服务端重定向到首页，
       首页地址统一在 main.py 的 "/" 路由定义，桌面端不再写死具体页面）；
    3. 窗口关闭 → 关服务 → 进程退出，**不留后台残留**；
    4. 单实例检测：已有实例时直接把浏览器带到那个实例，然后退出；
    5. WebView2 运行时缺失时给中文友好提示 + 下载指引。

用法：
    python desktop/app.py          # 开发态直接跑
    dist\\Aegis\\Aegis.exe          # 打包产物
"""
import os
import sys
import traceback
from pathlib import Path

#: 应用标识（与安装目录、快捷方式、进程名保持一致）
APP_NAME = "Aegis"
__version__ = "1.8.2"

#: WebView2 运行时下载地址（常青版引导程序）
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"

#: 日志与数据目录（与 apps/paths.py 的定义一致，这里先算出来只为兜底输出）
def _default_log_dir() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home()
    return base / APP_NAME / "logs"


def _bootstrap_stdio() -> None:
    """
    console=False 打包时 sys.stdout / sys.stderr 为 None，
    任何未捕获异常都会无声消失。这里在导入业务模块之前就把它们重定向到日志文件，
    保证「起不来」也能留下线索。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = _default_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(str(log_dir / "aegis-stdio.log"), "a", encoding="utf-8")
        sys.stdout = handle
        sys.stderr = handle
    except Exception:
        # 连日志文件都开不了就彻底放弃，后续所有输出都会被静默丢弃
        pass


_bootstrap_stdio()

# ── 让 desktop 包与仓库根都能被找到（开发态与打包态一致）──
_ROOT_DIR = str(Path(__file__).resolve().parents[1])
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

import logging  # noqa: E402

logger = logging.getLogger("aegis-desktop")

_IMPORT_ERROR = ""
CONFIG = {}
ServerStartError = Exception
start_server = None
shutdown_server = None

try:
    from apps import CONFIG as _CONFIG, logger as _apps_logger  # noqa: E402
    from desktop.server import (  # noqa: E402
        ServerStartError as _ServerStartError,
        shutdown_server as _shutdown_server,
        start_server as _start_server,
    )

    CONFIG = _CONFIG
    logger = _apps_logger
    ServerStartError = _ServerStartError
    start_server = _start_server
    shutdown_server = _shutdown_server
except Exception:
    _IMPORT_ERROR = traceback.format_exc()
    try:
        logger.exception("导入 Aegis 运行组件失败")
    except Exception:
        pass


# ── 原生消息框（不引入 tkinter，构建时已被排除）────────────
def show_message(title, text, icon=0x10):
    """
    弹出一个原生消息框；非 Windows 或弹框失败时退回日志 + 控制台。

    :param icon: 0x10 = MB_ICONERROR，0x40 = MB_ICONINFORMATION
    """
    try:
        logger.error("%s | %s", title, str(text).replace("\n", " "))
    except Exception:
        pass

    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, str(text), str(title), icon)
            return
        except Exception:
            try:
                logger.exception("弹出原生消息框失败")
            except Exception:
                pass

    try:
        print("[%s] %s" % (title, text))
    except Exception:
        pass


def _webview2_installed() -> bool:
    """
    检测 WebView2 运行时是否已安装。

    非 Windows 平台直接返回 True（macOS 用系统 WKWebView，不需要 WebView2）。
    读不到注册表时不阻断启动，交给 pywebview 自己报错，避免误判。
    """
    if not sys.platform.startswith("win"):
        return True
    try:
        import winreg
    except Exception:
        return True

    sub_keys = (
        (winreg.HKEY_LOCAL_MACHINE,
         "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\"
         "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE,
         "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\"
         "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER,
         "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\"
         "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    )
    for root, sub in sub_keys:
        try:
            with winreg.OpenKey(root, sub) as handle:
                value, _ = winreg.QueryValueEx(handle, "pv")
                if str(value or "").strip():
                    return True
        except OSError:
            continue
        except Exception:
            try:
                logger.exception("读取 WebView2 注册表项失败")
            except Exception:
                pass
    return False


def _open_url(url: str) -> bool:
    """用系统默认浏览器打开地址；失败返回 False"""
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:
        try:
            logger.exception("打开浏览器失败")
        except Exception:
            pass
        return False


def _desktop_config() -> dict:
    """读取 config.json 的 desktop 段（缺失时用内置默认值兜底）"""
    section = CONFIG.get("desktop") if isinstance(CONFIG, dict) else None
    cfg = dict(section) if isinstance(section, dict) else {}
    return {
        "app_name": str(cfg.get("app_name") or APP_NAME),
        "window_title": str(cfg.get("window_title") or APP_NAME),
        "window_width": int(cfg.get("window_width") or 1280),
        "window_height": int(cfg.get("window_height") or 820),
        "window_min_width": int(cfg.get("window_min_width") or 1024),
        "window_min_height": int(cfg.get("window_min_height") or 680),
    }


def _gui_backend():
    """按平台挑 pywebview 后端；None 表示让 pywebview 自动选择"""
    if sys.platform.startswith("win"):
        return "edgechromium"
    if sys.platform == "darwin":
        return "cocoa"
    return None


def _start_gui(webview, gui):
    """
    启动 pywebview 主循环。

    指定后端（如 Windows 的 edgechromium）失败时退回自动选择，
    避免因为版本差异直接把整个桌面端打死。
    """
    try:
        webview.start(gui=gui, debug=False, private_mode=False)
        return True
    except Exception:
        logger.exception("以 %s 后端启动窗口失败，尝试自动选择后端", gui)
        if gui is None:
            return False
    try:
        webview.start(debug=False, private_mode=False)
        return True
    except Exception:
        logger.exception("自动选择后端启动窗口同样失败")
        return False


class AegisApi:
    """暴露给前端 JS 的原生能力桥（window.pywebview.api.*）"""

    def __init__(self):
        #: 主窗口引用，create_window 之后由 main() 注入。
        #: ⚠️ 必须用下划线私有属性：pywebview 会把 js_api 对象的**公开非方法属性**
        #: 递归序列化暴露给前端（util.get_functions），把 WinForms 窗体对象挂成
        #: 公开属性会让 js_api 枚举抛异常 → api 永远注入失败（表现为前端判定为浏览器）。
        #: pywebview 5.x 已移除模块级 create_file_dialog，必须走 window 实例方法。
        self._win = None

    def open_folder_dialog(self):
        """
        弹出系统目录选择框。

        用户取消返回 ""；出错时记录日志并**原样抛出**——
        pywebview 的 js_bridge_call 会把异常转成 JS 侧 rejected promise，
        前端据此提示（绝不静默吞掉）。
        """
        import webview
        if self._win is None:
            raise RuntimeError("桌面窗口尚未就绪，无法打开目录选择器")
        try:
            result = self._win.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            logger.exception("打开系统目录选择器失败")
            raise
        if result and isinstance(result, (list, tuple)) and len(result) > 0:
            return str(result[0])
        return ""


def main() -> int:
    """桌面壳主流程；返回进程退出码"""
    if _IMPORT_ERROR:
        show_message(
            APP_NAME,
            "Aegis 启动失败：运行组件加载异常。\n\n"
            "请确认已按 README 完成依赖安装，或查看日志：\n%s"
            % (_default_log_dir() / "aegis.log"),
        )
        return 1

    if not _webview2_installed():
        show_message(
            APP_NAME,
            "缺少 WebView2 运行时，Aegis 无法打开界面。\n\n"
            "请点击「确定」前往微软官网下载并安装 Evergreen Bootstrapper，"
            "安装完成后重新启动 Aegis。\n\n下载地址：%s" % WEBVIEW2_URL,
        )
        _open_url(WEBVIEW2_URL)
        return 2

    try:
        import webview
    except Exception:
        logger.exception("导入 pywebview 失败")
        show_message(
            APP_NAME,
            "缺少界面组件 pywebview。\n\n"
            "请在构建环境中执行：\n"
            "pip install -r desktop\\requirements-desktop.txt",
        )
        return 3

    # ── 启动本地服务 ──
    try:
        server, port = start_server()
    except ServerStartError as e:
        message = getattr(e, "message", None) or str(e)
        existing_port = getattr(e, "port", None)
        if existing_port:
            url = "http://127.0.0.1:%d/" % int(existing_port)
            _open_url(url)
            show_message(
                APP_NAME,
                "%s\n\n已为你打开正在运行的那个 Aegis：\n%s" % (message, url),
                icon=0x40,
            )
        else:
            show_message(
                APP_NAME,
                "本地服务启动失败：%s\n\n日志位置：\n%s"
                % (message, _default_log_dir() / "aegis.log"),
            )
        return 4
    except Exception:
        logger.exception("启动本地服务时发生未预期错误")
        show_message(APP_NAME, "本地服务启动失败，请查看服务日志。")
        return 4

    url = "http://127.0.0.1:%d/" % int(port)
    cfg = _desktop_config()
    logger.info("Aegis 窗口启动：%s", url)

    try:
        api = AegisApi()
        window = webview.create_window(
            title=cfg["window_title"],
            url=url,
            width=cfg["window_width"],
            height=cfg["window_height"],
            min_size=(cfg["window_min_width"], cfg["window_min_height"]),
            background_color="#FFFFFF",
            confirm_close=False,
            js_api=api,
        )
        # 注入窗口引用：open_folder_dialog 需要走 window 实例方法。
        # 私有属性（下划线开头），避免被 pywebview 当作要暴露给前端的 API 枚举。
        api._win = window
    except Exception:
        logger.exception("创建窗口失败")
        shutdown_server(server)
        show_message(APP_NAME, "窗口创建失败，请查看服务日志。")
        return 5

    def on_closed():
        """窗口关闭：立刻关服务，避免留下后台残留进程"""
        try:
            shutdown_server(server)
        except Exception:
            logger.exception("窗口关闭时停止服务失败")

    try:
        window.events.closed += on_closed
    except Exception:
        logger.exception("绑定窗口关闭事件失败")

    # webview.start() 会阻塞，直到所有窗口关闭
    started = _start_gui(webview, _gui_backend())

    # 兜底：无论正常关闭、异常还是被强制结束，都要把服务收掉
    try:
        shutdown_server(server)
    except Exception:
        pass

    if not started:
        show_message(APP_NAME, "界面组件启动失败，请重新安装 Aegis 或查看日志。")
        return 6
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            logger.exception("Aegis 桌面壳崩溃")
        except Exception:
            pass
        sys.exit(1)
