# -*- coding: utf-8 -*-
"""
desktop/server.py — Aegis 本地服务的启动、就绪探测与关闭

设计要点：
    1. **threaded=True 是硬要求**：Agent 的审批机制依赖「SSE 线程挂起等待用户决策，
       同时 /api/agent/approve 能被另一个工作线程处理」。若用单线程 Werkzeug，
       SSE 线程在 Event.wait() 上阻塞会把 /approve 请求饿死，直接死锁（设计 §4.3.3）。
    2. 端口从 apps/paths.py 的候选范围（默认 5000..5049）里挑，避免固定端口冲突。
    3. 启动后必须做**就绪探测**：轮询 GET /api/status，拿到 200 才认为可用，
       否则窗口会打开一个还没起来的地址，用户看到白屏。
    4. 单例锁：进程内 + 进程间都只允许一个 Aegis 服务，避免多开导致端口与数据库争用。

对外接口：
    start_server(port=None, ready_timeout=...) -> (server, port)
    shutdown_server(server) -> None
"""
import atexit
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

# ── 让 desktop 既能在仓库内直接跑，也能在打包产物里找到 apps/main ──
_ROOT_DIR = str(Path(__file__).resolve().parents[1])
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from apps import CONFIG, logger, paths  # noqa: E402

#: 只监听回环地址：桌面端不允许局域网访问（设计 §安全模型）
HOST = "127.0.0.1"

#: 就绪探测参数
READY_TIMEOUT = 30.0
READY_INTERVAL = 0.25
PROBE_TIMEOUT = 3.0

#: 单例锁文件名（放在 <data_dir>/run 下，进程退出即失效）
LOCK_NAME = "aegis-server.lock"

_SERVER_THREAD_NAME = "aegis-http"

__all__ = [
    "HOST",
    "ServerStartError",
    "start_server",
    "shutdown_server",
    "server_lock_owner",
]


class ServerStartError(Exception):
    """本地服务启动失败；port 非空表示「已有实例在该端口上运行」"""

    def __init__(self, message: str, port: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.port = port


# ── 进程存活判定 ──────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """跨平台判断 pid 是否还在运行（不依赖 psutil）"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt" or sys.platform.startswith("win"):
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                # 光看 OpenProcess 成功**不足以**判定存活：进程退出后，
                # 只要其内核进程对象尚未被完全回收，OpenProcess 依然会返回
                # 有效句柄 —— 实测一个已退出的 pid 就返回了句柄且
                # GetExitCodeProcess = 1（非 STILL_ACTIVE）。
                # 这会让陈旧锁被当成「实例还活着」，进而让用户看到
                # 「Aegis 已在运行（进程 xxx）」却怎么也启动不了，
                # 或覆盖安装时提示「进程正在使用」。
                # 因此必须再查退出码：等于 STILL_ACTIVE 才算真活着。
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                if not ok:
                    return False
                return code.value == STILL_ACTIVE
            finally:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
        except Exception:
            # 无法判定时按「已结束」处理，避免陈旧锁永久阻塞启动
            return False

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


# ── 单例锁 ────────────────────────────────────────────────
def _lock_path() -> Path:
    return paths.run_dir() / LOCK_NAME


def _read_lock() -> dict:
    path = _lock_path()
    try:
        if not path.is_file():
            return {}
        with open(str(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def server_lock_owner() -> int:
    """当前持有单例锁的进程 pid（无持有者返回 0）"""
    info = _read_lock()
    pid = 0
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        return pid
    return 0


def _acquire_lock() -> bool:
    """抢占单例锁；陈旧锁（进程已死）会自动清理"""
    paths.ensure_dirs()
    path = _lock_path()

    if path.exists():
        info = _read_lock()
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            return False
        try:
            path.unlink()
        except OSError:
            logger.warning("删除陈旧的服务锁文件失败：%s", path)

    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "created_at": time.time()}, f,
                      ensure_ascii=False)
    except OSError:
        logger.exception("写入服务锁文件失败")
        return False
    atexit.register(_release_lock)
    return True


def _release_lock() -> None:
    """释放单例锁（只删自己写的那份）"""
    path = _lock_path()
    try:
        if not path.is_file():
            return
        info = _read_lock()
        pid = int(info.get("pid") or 0)
        if pid and pid != os.getpid():
            return
        path.unlink()
    except OSError:
        pass
    except ValueError:
        pass


# ── 运行时信息 ────────────────────────────────────────────
def _publish_runtime(port: int, token: str) -> None:
    """把 port / pid / token 写进 runtime.json（进程退出即失效，不落配置）"""
    data = paths.runtime_read()
    data["host"] = HOST
    data["port"] = int(port)
    data["pid"] = os.getpid()
    data["token"] = token
    data["started_at"] = time.time()
    paths.runtime_write(data)


def _cleanup_runtime() -> None:
    """清掉本次进程写的运行时字段"""
    data = paths.runtime_read()
    for key in ("port", "pid", "started_at"):
        data.pop(key, None)
    paths.runtime_write(data)


def _existing_port() -> Optional[int]:
    """已有实例正在监听的端口（从 runtime.json 读，失效返回 None）"""
    data = paths.runtime_read()
    pid = int(data.get("pid") or 0)
    if pid and not _pid_alive(pid):
        return None
    try:
        return int(data.get("port") or 0) or None
    except (TypeError, ValueError):
        return None


# ── 就绪探测 ──────────────────────────────────────────────
def _probe_ready(port: int, token: str,
                 timeout: float = READY_TIMEOUT,
                 interval: float = READY_INTERVAL) -> bool:
    """
    轮询 GET /api/status 直到返回 HTTP 响应或超时。

    只要有 HTTP 响应（哪怕是 4xx/5xx）就说明端口已被我们的服务接管，
    视为就绪；只有连接错误才继续重试。
    """
    url = "http://%s:%d/api/status" % (HOST, port)
    deadline = time.time() + max(1.0, float(timeout))
    last_error = ""

    while time.time() < deadline:
        try:
            request = urllib.request.Request(
                url, headers={"X-Aegis-Token": token or ""})
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as resp:
                code = int(getattr(resp, "status", 0) or resp.getcode() or 0)
                if code == 200:
                    return True
                last_error = "HTTP %d" % code
        except urllib.error.HTTPError as e:
            # 服务已起但鉴权/状态异常：记录后按就绪处理，页面会给出明确提示
            logger.warning("就绪探测收到异常状态码 %s：%s", e.code, url)
            return True
        except Exception as e:
            last_error = str(e)
        time.sleep(interval)

    logger.error("本地服务就绪探测超时：port=%s 最后错误=%s", port, last_error)
    return False


# ── 打包后的资源目录兜底 ──────────────────────────────────
def _ensure_frozen_resource_dir(main_module) -> None:
    """
    PyInstaller 打包后 main.py 的 __file__ 由解释器按版本自行决定，
    少数情况下不会落在 _MEIPASS 内，导致 templates / static 找不到、页面全白。

    这里**只在原路径确实找不到 templates 时才兜底修正**，正常情况不做任何改动，
    避免误伤开发态与已经正确的打包布局。
    """
    if not getattr(sys, "frozen", False):
        return

    base = str(getattr(main_module, "BASE_DIR", "") or "")
    if base and os.path.isdir(os.path.join(base, "templates")):
        return

    exe_dir = os.path.dirname(sys.executable or "")
    candidates = (
        str(getattr(sys, "_MEIPASS", "") or ""),
        os.path.join(exe_dir, "_internal"),
        exe_dir,
    )
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "templates")):
            main_module.BASE_DIR = candidate
            logger.info("已修正打包资源目录：%s", candidate)
            return

    logger.warning("未能定位打包资源目录（templates / static），页面可能无法正常渲染")


# ── 对外接口 ──────────────────────────────────────────────
def start_server(port: Optional[int] = None,
                 ready_timeout: float = READY_TIMEOUT) -> Tuple[object, int]:
    """
    启动本地服务。

    :param port: 指定端口；None 时按 config.json 的 app.port 优先、再退到候选范围
    :param ready_timeout: 就绪探测超时（秒）
    :return: (werkzeug server 对象, 实际端口)
    :raises ServerStartError: 已有实例运行 / 端口耗尽 / 服务初始化失败
    """
    if not _acquire_lock():
        owner = server_lock_owner()
        running_port = _existing_port()
        message = "Aegis 已经在运行（进程 %d），请勿重复启动。" % owner if owner \
            else "Aegis 已经在运行，请勿重复启动。"
        logger.warning("单例锁被占用：owner=%s port=%s", owner, running_port)
        raise ServerStartError(message, port=running_port)

    # 延迟导入：main 会初始化 Flask 与数据库，失败时要能给出中文提示
    try:
        import main as main_module
        from main import create_app, get_token
    except Exception:
        _release_lock()
        logger.exception("导入应用入口失败")
        raise ServerStartError("应用组件加载失败，请查看服务日志")

    # 打包态下修正模板 / 静态资源的根目录（开发态为空操作）
    try:
        _ensure_frozen_resource_dir(main_module)
    except Exception:
        logger.exception("修正打包资源目录失败，继续按原路径启动")

    try:
        app = create_app()
    except Exception:
        _release_lock()
        logger.exception("创建 Flask 应用失败")
        raise ServerStartError("本地服务初始化失败，请查看服务日志")

    token = ""
    try:
        token = str(get_token() or "")
    except Exception:
        _release_lock()
        logger.exception("生成本地鉴权 token 失败")
        raise ServerStartError("本地鉴权初始化失败，请查看服务日志")

    app_cfg = CONFIG.get("app") if isinstance(CONFIG.get("app"), dict) else {}
    preferred = int(port) if port else int(app_cfg.get("port") or paths.PORT_START)
    chosen = paths.pick_port(preferred, host=HOST)
    if not chosen:
        _release_lock()
        raise ServerStartError(
            "端口 %d~%d 均被占用，无法启动本地服务，请先关闭占用端口的程序"
            % (paths.PORT_START, paths.PORT_END - 1))

    try:
        from werkzeug.serving import make_server
        # threaded=True：审批机制的生命线，绝对不能改成单线程
        server = make_server(host=HOST, port=chosen, app=app, threaded=True)
    except Exception:
        _release_lock()
        logger.exception("监听本地端口失败 port=%s", chosen)
        raise ServerStartError("无法监听本地端口 %d，请查看服务日志" % chosen)

    thread = threading.Thread(target=server.serve_forever,
                              name=_SERVER_THREAD_NAME, daemon=True)
    thread.start()

    if not _probe_ready(chosen, token, timeout=ready_timeout):
        try:
            server.shutdown()
        except Exception:
            logger.exception("关闭未就绪的服务失败")
        _release_lock()
        raise ServerStartError("本地服务启动超时，请查看服务日志")

    _publish_runtime(chosen, token)
    logger.info("Aegis 本地服务已就绪：http://%s:%d", HOST, chosen)
    return server, chosen


def shutdown_server(server: object) -> None:
    """关闭本地服务并清理锁与运行时信息（幂等，绝不抛异常）"""
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        logger.exception("关闭本地服务失败")
    _cleanup_runtime()
    _release_lock()
    logger.info("Aegis 本地服务已关闭")
