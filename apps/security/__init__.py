# -*- coding: utf-8 -*-
"""
apps/security — 安全层：密钥托管、日志脱敏、分级错误

子模块：
    redact    密钥登记与日志脱敏（纯标准库，可安全地在任何阶段导入）
    errors    分级错误分类（纯标准库）
    keystore  密钥托管（keyring 优先 / DPAPI 回退 / 末位混淆级）
    dpapi     Windows CryptProtectData 的 ctypes 实现

**避免循环导入的做法**：keystore 依赖 keyring 等较重依赖且只在真正用到时才需要，
因此这里用 PEP 562 的模块级 __getattr__ 做惰性加载，
`from apps.security import keystore` 仍能正常工作。
"""
from . import errors, redact
from .errors import classify, codes, error_info, need_settings
from .redact import (
    PLACEHOLDER,
    RedactingFilter,
    register_secret,
    registered_secrets,
    scrub,
    unregister_secret,
)

__all__ = [
    "errors",
    "redact",
    "classify",
    "codes",
    "error_info",
    "need_settings",
    "PLACEHOLDER",
    "RedactingFilter",
    "register_secret",
    "registered_secrets",
    "scrub",
    "unregister_secret",
    "keystore",
]


def __getattr__(name: str):
    """惰性导入 keystore / dpapi（它们会触及 keyring、ctypes 等较重依赖）"""
    if name in ("keystore", "dpapi"):
        import importlib

        module = importlib.import_module("." + name, __name__)
        globals()[name] = module
        return module
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
