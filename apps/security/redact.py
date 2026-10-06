# -*- coding: utf-8 -*-
"""
apps/security/redact.py — 密钥与敏感串脱敏

用途：
    1. 任何从密钥库读出的明文，立即 register_secret()，
       之后所有日志/消息中的该串都会被替换成 ***REDACTED***；
    2. RedactingFilter 挂在 logging.Handler 上，在日志落盘/输出前完成替换，
       保证「日志文件中搜索不到完整 Key」（AEG-K05）。

实现约定：
    · 长度 < 8 的串不登记（太短会产生大量误替换，如把 "test" 全打码）；
    · 替换时按长度**升序**执行（最短的先替换），避免长串被短串截断后残留；
    · 全局状态用 RLock 保护，日志 filter 可能被多个请求线程并发调用。
"""
import logging
import threading
from typing import List, Optional

#: 日志/文本中用于替代明文的占位符
PLACEHOLDER = "***REDACTED***"

#: 低于该长度的字符串不登记，避免误伤正常单词
MIN_SECRET_LEN = 8

_lock = threading.RLock()
_secrets = set()          # type: set
_ordered_cache = None     # type: Optional[List[str]]


def _rebuild_cache() -> None:
    """重建「按长度升序」的替换顺序缓存"""
    global _ordered_cache
    # 先按长度升序；同长度再按字典序，保证结果稳定可复现
    _ordered_cache = sorted(_secrets, key=lambda s: (len(s), s))


def register_secret(secret: Optional[str]) -> bool:
    """
    登记一个需要脱敏的明文串。

    :param secret: 明文（如 API Key）
    :return: 是否成功登记（空串/过短返回 False）
    """
    if not secret or not isinstance(secret, str):
        return False
    value = secret.strip()
    if len(value) < MIN_SECRET_LEN:
        return False
    with _lock:
        if value in _secrets:
            return True
        _secrets.add(value)
        _rebuild_cache()
    return True


def unregister_secret(secret: Optional[str]) -> None:
    """注销一个已登记的明文串（密钥被删除时调用）"""
    if not secret or not isinstance(secret, str):
        return
    value = secret.strip()
    with _lock:
        if value in _secrets:
            _secrets.discard(value)
            _rebuild_cache()


def registered_secrets() -> List[str]:
    """返回当前已登记的明文串（仅用于测试与诊断，调用方不得打印）"""
    with _lock:
        return list(_secrets)


def clear_secrets() -> None:
    """清空登记表（仅测试使用）"""
    global _ordered_cache
    with _lock:
        _secrets.clear()
        _ordered_cache = None


def scrub(text) -> str:
    """
    把文本中出现的所有已登记明文替换为 PLACEHOLDER。

    非字符串输入会被转成字符串；None 返回空串。永不抛异常。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    with _lock:
        ordered = _ordered_cache
    if not ordered:
        return text

    result = text
    for secret in ordered:
        if secret in result:
            result = result.replace(secret, PLACEHOLDER)
    return result


class RedactingFilter(logging.Filter):
    """
    日志脱敏过滤器。

    挂在任意 Handler 上即可；它会把 record 的最终消息做一次 scrub。
    注意：先把 args 合并进 msg（record.getMessage()），再清空 args，
    避免 args 里的密钥绕过替换。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if record.args:
                record.msg = record.getMessage()
                record.args = ()
            elif not isinstance(record.msg, str):
                record.msg = str(record.msg)
            record.msg = scrub(record.msg)
        except Exception:
            # 脱敏失败绝不能影响业务日志本身的输出
            return True
        return True
