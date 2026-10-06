# -*- coding: utf-8 -*-
"""
apps/security/errors.py — 分级错误分类（AG-K04 / 设计 §6.5）

统一入口::

    classify(status_or_exc) -> {
        "code":          str,   # 稳定错误码，前端据此分支
        "reason":        str,   # 面向用户的中文原因
        "action":        str,   # 建议的下一步动作
        "need_settings": bool,  # 是否需要引导用户去「设置 → 模型服务商」
        "retryable":     bool,  # 重试是否有意义
        "detail":        str,   # 详细原文（只写日志，不回传前端）
    }

约定：
    · 详细异常（服务商返回的原始 body 片段）只写日志，前端只拿 code/reason/action；
    · 新增错误码必须同时补进 _ERRORS，禁止在各处自造字符串（设计 §12.2）。
"""
import socket
from typing import Any, Dict, Optional

# ── 10 类错误码（文案照抄设计 §6.5 表格）────────────────────
_ERRORS: Dict[str, Dict[str, Any]] = {
    "NO_PROVIDER": {
        "reason": "尚未配置可用的模型服务",
        "action": "打开设置 → 选择服务商并填写 API Key",
        "need_settings": True,
        "retryable": False,
    },
    "NO_KEY": {
        "reason": "未配置 API Key，无法调用模型",
        "action": "前往设置填写（可在设置页一键打开申请链接）",
        "need_settings": True,
        "retryable": False,
    },
    "INVALID_KEY": {
        "reason": "API Key 无效或已失效",
        "action": "重新生成 Key 并粘贴；注意前后空格",
        "need_settings": True,
        "retryable": False,
    },
    "NO_BALANCE": {
        "reason": "账户余额不足，服务已欠费",
        "action": "充值，或切换到标注免费的服务商",
        "need_settings": True,
        "retryable": False,
    },
    "NO_PERMISSION": {
        "reason": "无访问权限（可能未完成实名认证或未开通该模型）",
        "action": "完成实名认证，或换一个该账号已开通的模型",
        "need_settings": True,
        "retryable": False,
    },
    "MODEL_NOT_FOUND": {
        "reason": "模型名不存在或不被该账号支持",
        "action": "在设置里重新选择模型（可点「刷新模型」）",
        "need_settings": True,
        "retryable": False,
    },
    "RATE_LIMIT": {
        "reason": "请求过于频繁，已触发限流",
        "action": "稍后重试；已自动尝试切换链上的下一个服务商",
        "need_settings": False,
        "retryable": True,
    },
    "TIMEOUT": {
        "reason": "连接超时（网络较慢或服务端无响应）",
        "action": "重试；或调大设置里的「超时」秒数",
        "need_settings": False,
        "retryable": True,
    },
    "NETWORK": {
        "reason": "网络不可达，请检查本机网络",
        "action": "检查网络/代理后重试；也可在设置中检查服务商配置",
        "need_settings": False,
        "retryable": True,
    },
    "SERVER_ERROR": {
        "reason": "模型服务端错误",
        "action": "稍后重试；已自动切换下一个服务商",
        "need_settings": False,
        "retryable": True,
    },
}

#: HTTP 状态码 -> 错误码
_STATUS_MAP: Dict[int, str] = {
    400: "MODEL_NOT_FOUND",
    401: "INVALID_KEY",
    402: "NO_BALANCE",
    403: "NO_PERMISSION",
    404: "MODEL_NOT_FOUND",
    408: "TIMEOUT",
    409: "SERVER_ERROR",
    422: "MODEL_NOT_FOUND",
    429: "RATE_LIMIT",
    500: "SERVER_ERROR",
    502: "SERVER_ERROR",
    503: "SERVER_ERROR",
    504: "TIMEOUT",
}

#: 兜底错误码（未知情况一律按服务端错误处理，保证调用方永远拿到完整结构）
_FALLBACK_CODE = "SERVER_ERROR"


def codes() -> list:
    """返回全部错误码（便于测试与前端枚举）"""
    return list(_ERRORS.keys())


def error_info(code: str, detail: str = "") -> Dict[str, Any]:
    """
    按错误码取结构化信息（带 detail 槽位，detail 只用于日志）。

    :param code: 错误码；未知码一律回落到 SERVER_ERROR
    :param detail: 详细原文（服务商返回的 body 片段等），只写日志
    """
    spec = _ERRORS.get(code) or _ERRORS[_FALLBACK_CODE]
    return {
        "code": code if code in _ERRORS else _FALLBACK_CODE,
        "reason": spec["reason"],
        "action": spec["action"],
        "need_settings": bool(spec["need_settings"]),
        "retryable": bool(spec["retryable"]),
        "detail": detail or "",
    }


def _classify_status(status: int, detail: str = "") -> Dict[str, Any]:
    """HTTP 状态码 -> 结构化错误"""
    try:
        status = int(status)
    except (TypeError, ValueError):
        return error_info(_FALLBACK_CODE, detail)

    if status in _STATUS_MAP:
        code = _STATUS_MAP[status]
        info = error_info(code, detail)
        if code == "SERVER_ERROR":
            # 服务端错误把状态码带进原因，方便用户反馈（如「模型服务端错误 503」）
            info["reason"] = "模型服务端错误 %d" % status
        return info

    if 500 <= status < 600:
        info = error_info("SERVER_ERROR", detail)
        info["reason"] = "模型服务端错误 %d" % status
        return info
    if 400 <= status < 500:
        return error_info("MODEL_NOT_FOUND", detail)
    return error_info(_FALLBACK_CODE, detail)


def _classify_exception(exc: BaseException, detail: str = "") -> Dict[str, Any]:
    """异常对象 -> 结构化错误（不 import requests，避免额外耦合）"""
    name = type(exc).__name__.lower()

    # 超时优先判定：requests 的 Timeout 同时是 RequestException / OSError 的子类
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return error_info("TIMEOUT", detail)
    if "timeout" in name or name in ("connecttimeout", "readtimeout"):
        return error_info("TIMEOUT", detail)

    # 网络类：requests 的 ConnectionError 继承 OSError/IOError
    if isinstance(exc, OSError):
        return error_info("NETWORK", detail)
    if "connection" in name or "network" in name or "proxy" in name or "ssl" in name:
        return error_info("NETWORK", detail)

    return error_info(_FALLBACK_CODE, detail or type(exc).__name__)


def classify(status_or_exc: Any = None, detail: str = "") -> Dict[str, Any]:
    """
    把 HTTP 状态码 / 异常对象 / 已知错误码字符串统一成结构化错误。

    :param status_or_exc: int(HTTP 状态码) | Exception | str(错误码或数字) | None
    :param detail: 详细原文，只写日志，不回传前端
    :return: {code, reason, action, need_settings, retryable, detail}
    """
    if status_or_exc is None:
        return error_info(_FALLBACK_CODE, detail)

    # 已是结构化错误时原样透传（避免重复包装）
    if isinstance(status_or_exc, dict) and "code" in status_or_exc:
        return status_or_exc

    if isinstance(status_or_exc, BaseException):
        return _classify_exception(status_or_exc, detail)

    if isinstance(status_or_exc, bool):
        return error_info(_FALLBACK_CODE, detail)

    if isinstance(status_or_exc, int):
        return _classify_status(status_or_exc, detail)

    if isinstance(status_or_exc, str):
        text = status_or_exc.strip()
        if text in _ERRORS:
            return error_info(text, detail)
        if text.isdigit():
            return _classify_status(int(text), detail)
        # 形如 "HTTP 401 xxx" 的文本，尽量提取状态码
        for token in text.replace("：", " ").replace(":", " ").split():
            if token.isdigit() and len(token) == 3:
                return _classify_status(int(token), detail)
        return error_info(_FALLBACK_CODE, detail or text)

    return error_info(_FALLBACK_CODE, detail)


def need_settings(info: Optional[Dict[str, Any]]) -> bool:
    """便捷判定：该错误是否需要引导用户去设置页"""
    return bool(info and info.get("need_settings"))
