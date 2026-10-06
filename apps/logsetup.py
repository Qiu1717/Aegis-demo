# -*- coding: utf-8 -*-
"""
apps/logsetup.py — 滚动日志 + 控制台输出 + 密钥脱敏

设计要点（AEG-D06 / AEG-K05）：
    · RotatingFileHandler：单文件 5MB，最多 3 个历史文件，日志不会无限增长；
    · 控制台 Handler 复用 apps 包已有的 StreamHandler（不重复输出）；
    · 所有 Handler 挂 apps.security.redact.RedactingFilter，
      保证「日志文件中搜索不到完整 Key」；
    · setup() 幂等：重复调用不会重复挂 Handler。

注意：本模块只依赖标准库与 apps.paths / apps.security.redact，
可以在 apps/__init__.py 的启动阶段安全导入。
"""
import logging
import logging.handlers
import sys
from typing import Optional

from . import paths
from .security.redact import RedactingFilter

#: 单日志文件上限与保留份数
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

#: 幂等标记：挂在 Handler 上，重复 setup 时据此跳过
_HANDLER_FLAG = "_aegis_handler"

_setup_done = False


def _is_aegis_handler(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _HANDLER_FLAG, False))


def _mark(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _HANDLER_FLAG, True)
    return handler


def log_file_path() -> str:
    """当前日志文件路径（<data_dir>/logs/aegis.log）"""
    return str(paths.logs_dir() / "aegis.log")


def setup(logger_name: str = "aiagent",
          level: int = logging.INFO,
          console: bool = True) -> bool:
    """
    初始化日志系统（幂等）。

    :param logger_name: 目标 logger 名称（默认复用 apps.logger）
    :param level: 日志级别
    :param console: 是否确保存在控制台输出
    :return: 是否成功挂载了文件日志
    """
    global _setup_done
    if _setup_done:
        return True

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    redactor = RedactingFilter()
    ok = False

    # ── 文件日志（滚动 + 脱敏）──────────────────────────────
    try:
        paths.ensure_dirs()
        file_handler = logging.handlers.RotatingFileHandler(
            log_file_path(),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        file_handler.addFilter(redactor)
        logger.addHandler(_mark(file_handler))
        ok = True
    except OSError as e:
        # 目录不可写时退化为纯控制台日志，绝不阻断启动
        sys.stderr.write("[WARN] 初始化文件日志失败，仅输出到控制台：%s\n" % e)

    # ── 控制台日志（复用已有 StreamHandler，避免重复输出）────
    if console:
        has_console = any(
            isinstance(h, logging.StreamHandler) and not isinstance(
                h, logging.handlers.RotatingFileHandler)
            for h in logger.handlers
        )
        if not has_console:
            stream = logging.StreamHandler()
            stream.setLevel(level)
            stream.setFormatter(logging.Formatter(LOG_FORMAT))
            stream.addFilter(redactor)
            logger.addHandler(_mark(stream))
        else:
            # 已有控制台 Handler 也要补上脱敏过滤器
            for h in logger.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(
                        h, logging.handlers.RotatingFileHandler):
                    if redactor not in h.filters:
                        h.addFilter(redactor)

    # 避免向 root logger 二次传播导致重复输出
    logger.propagate = False

    _setup_done = True
    return ok


def reset_for_test() -> None:
    """测试用：清除幂等标记（生产代码不要调用）"""
    global _setup_done
    _setup_done = False
