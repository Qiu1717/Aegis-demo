# -*- coding: utf-8 -*-
"""
apps/llm — 大模型接入层

对外只暴露几个函数，业务层不需要关心底层的服务商切换细节：
    get_client()        获取全局唯一的调用客户端
    reset_client()      配置变更后重置客户端
    get_status()        返回各服务商可用状态（用于 /api/status）
    get_active_label()  当前生效的模型显示名（用于前端展示）
"""
from .client import LLMClient, ProviderFailed
from .providers import (
    AUTH_ENV_MAP,
    PRESETS,
    Provider,
    build_provider,
    build_provider_from,
    register_key_resolver,
    resolve_key,
)

__all__ = [
    "LLMClient", "ProviderFailed", "PRESETS", "AUTH_ENV_MAP",
    "Provider", "build_provider", "build_provider_from",
    "register_key_resolver", "resolve_key",
    "get_client", "reset_client",
    "get_status", "get_active_label",
]

_client: LLMClient = None


def get_client() -> LLMClient:
    """惰性构建全局客户端（避免在包导入阶段就读取配置）"""
    global _client
    if _client is None:
        from apps import CONFIG
        _client = LLMClient(CONFIG.get("llm", {}))
    return _client


def reset_client() -> None:
    """配置文件或环境变量变化后调用，使其下次重新构建"""
    global _client
    _client = None


def get_status() -> dict:
    return get_client().describe()


def get_active_label() -> str:
    return get_client().active_label()
