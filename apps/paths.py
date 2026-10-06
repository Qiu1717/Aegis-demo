# -*- coding: utf-8 -*-
"""
apps/paths.py — 用户数据目录、日志目录与运行时文件

设计要点：
    桌面端安装目录应当是只读的（普通用户权限即可运行），所有可写数据
    （数据库、用户配置、密钥库文件、滚动日志、运行时信息）一律落到
    用户数据目录：

        Windows : %APPDATA%\\Aegis\\
        macOS   : ~/Library/Application Support/Aegis/
        其他     : ~/.aegis

    本模块**只依赖标准库**（不引入 platformdirs / psutil 等第三方包），
    保证在任何环境（含打包后的 PyInstaller 产物）都能最先被导入。

    环境变量 AEGIS_DATA_DIR 可覆盖数据目录，便于测试与便携版场景。
"""
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 常量 ──────────────────────────────────────────────────
APP_NAME = "Aegis"

#: 端口候选范围（设计 §2.3 / AEG-D05：5000 起，最多探测到 5049）
PORT_START = 5000
PORT_END = 5050  # 不含

#: 代码包根目录（Aiagent/），用于定位仓库内的默认配置
BASE_DIR = Path(__file__).resolve().parent.parent

#: 数据目录覆盖开关（测试 / 便携版）
_ENV_DATA_DIR = "AEGIS_DATA_DIR"

_LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")


# ── 目录 ──────────────────────────────────────────────────
def _platform_data_dir() -> Path:
    """按平台推算用户数据目录（纯标准库实现）"""
    override = os.environ.get(_ENV_DATA_DIR, "").strip()
    if override:
        return Path(override).expanduser()

    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / APP_NAME
        # APPDATA 缺失（极罕见）时退回用户目录下的标准位置
        return Path.home() / "AppData" / "Roaming" / APP_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    # Linux / 其他 POSIX
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / (".%s" % APP_NAME.lower())


def data_dir() -> Path:
    """用户数据目录（不存在时不自动创建，由 ensure_dirs() 负责）"""
    return _platform_data_dir()


def logs_dir() -> Path:
    """日志目录：<data_dir>/logs"""
    return data_dir() / "logs"


def run_dir() -> Path:
    """运行时目录：<data_dir>/run（port / token / pid，进程退出即失效）"""
    return data_dir() / "run"


def db_path() -> Path:
    """SQLite 数据库文件：<data_dir>/aegis.db"""
    return data_dir() / "aegis.db"


def user_config_path() -> Path:
    """用户级配置文件：<data_dir>/config.user.json（不含任何密钥）"""
    return data_dir() / "config.user.json"


def secrets_path() -> Path:
    """DPAPI / 回退后端的密文文件：<data_dir>/secrets.enc"""
    return data_dir() / "secrets.enc"


def key_index_path() -> Path:
    """密钥索引文件：<data_dir>/keys.index.json（只记 provider_key，不记密钥）"""
    return data_dir() / "keys.index.json"


def default_config_path() -> Path:
    """仓库内置默认配置：Aiagent/config/config.json"""
    return BASE_DIR / "config" / "config.json"


def ensure_dirs() -> None:
    """创建数据 / 日志 / 运行时目录（幂等，失败不抛异常）"""
    for d in (data_dir(), logs_dir(), run_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 目录不可写时不阻断启动：日志会退化为仅控制台输出
            pass


# ── 本机判定与端口 ────────────────────────────────────────
def is_local_address(addr: Optional[str]) -> bool:
    """判定远程地址是否来自本机（用于 /token.js 等敏感接口的访问控制）"""
    if not addr:
        return False
    addr = addr.strip()
    if addr in _LOCAL_ADDRS:
        return True
    return addr.startswith("127.")


def port_candidates(start: int = PORT_START, end: int = PORT_END) -> List[int]:
    """返回端口候选列表（默认 5000..5049）"""
    return list(range(start, end))


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否可绑定（仅探测，不占用）"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def pick_port(preferred: Optional[int] = None,
              host: str = "127.0.0.1") -> Optional[int]:
    """
    在候选范围内挑一个空闲端口；preferred 优先尝试。
    全部被占用时返回 None（调用方应给出明确提示，而不是随便挑一个）。
    """
    ordered: List[int] = []
    if preferred:
        ordered.append(int(preferred))
    ordered.extend(p for p in port_candidates() if p != preferred)
    for port in ordered:
        if is_port_free(port, host):
            return port
    return None


# ── 运行时文件（runtime.json）──────────────────────────────
def runtime_path() -> Path:
    """运行时信息文件：<data_dir>/run/runtime.json"""
    return run_dir() / "runtime.json"


def new_token() -> str:
    """生成一个随机本地鉴权 token（不落任何配置文件）"""
    return secrets.token_hex(32)


def _tighten_permission(path: Path) -> None:
    """尽量把运行时文件权限收紧为仅当前用户可读写（Windows 上为 no-op）"""
    if sys.platform.startswith("win"):
        return
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def runtime_read() -> Dict[str, Any]:
    """读取 runtime.json；不存在或损坏时返回空 dict（绝不抛异常）"""
    path = runtime_path()
    try:
        if not path.is_file():
            return {}
        with open(str(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def runtime_write(data: Dict[str, Any]) -> bool:
    """整体写入 runtime.json；失败返回 False（绝不抛异常）"""
    try:
        ensure_dirs()
        path = runtime_path()
        with open(str(path), "w", encoding="utf-8") as f:
            json.dump(data or {}, f, ensure_ascii=False, indent=2)
        _tighten_permission(path)
        return True
    except OSError:
        return False


def runtime_get(field: str, default: Any = None) -> Any:
    """读取 runtime.json 中的单个字段"""
    return runtime_read().get(field, default)


def runtime_set(field: str, value: Any) -> bool:
    """写入 runtime.json 中的单个字段（保留其他字段）"""
    data = runtime_read()
    data[field] = value
    return runtime_write(data)


def runtime_clear() -> bool:
    """清空 runtime.json（进程退出时调用）"""
    return runtime_write({})
