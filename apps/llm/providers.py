# -*- coding: utf-8 -*-
"""
apps/llm/providers.py — 大模型服务商预设

设计要点：
    所有云端服务商都对外提供 OpenAI 兼容的 /chat/completions 协议，
    因此底层只需要一套请求与解析代码，切换服务商只改配置不动代码。

免费额度说明（2026-09 核实，平台政策可能调整，请以官方定价页为准）：
    deepseek    官网注册送额度，V3 系列速度快、性价比高（**默认首选**）
    zhipu       GLM-4-Flash 官方标注永久免费、不限 Token，并发 30
    modelscope  实名用户每天总计 2000 次，单模型动态限流最高 500 次
    siliconflow 提供若干永久免费模型，使用免费模型需完成实名认证
    ollama      本机运行，完全免费，不消耗任何外部额度

新增一个服务商：
    1. 在 PRESETS 里加一条；
    2. 在 config.json 的 provider_chain / providers 里引用它。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import os

# ── 服务商预设 ────────────────────────────────────────────
PRESETS: Dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "candidate_models": ["deepseek-chat", "deepseek-reasoner"],
        "auth_env": ["DEEPSEEK_API_KEY"],
        "requires_key": True,
        "free_note": "官网注册送额度，V3 系列速度快、性价比高",
        "key_url": "https://platform.deepseek.com/usage",
        # 是否原生支持 function calling：native / none / unknown
        "supports_tools": "unknown",
        "recommended": True,
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "candidate_models": ["glm-4-flash", "glm-z1-flash", "glm-4-plus"],
        "auth_env": ["ZHIPU_API_KEY", "BIGMODEL_API_KEY"],
        "requires_key": True,
        "free_note": "GLM-4-Flash 永久免费、不限 Token，并发 30",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        # 是否原生支持 function calling：native / none / unknown
        # 未知时由 client 先走原生 tools，返回 400/404 且提示不支持则自动回退 JSON
        "supports_tools": "unknown",
        # 推荐位已归 DeepSeek（配置默认链首），此处仅作为降级备用
        "recommended": False,
    },
    "modelscope": {
        "label": "魔搭 ModelScope",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "default_model": "Qwen/Qwen3.5-27B",
        "candidate_models": [
            "Qwen/Qwen3.5-27B",
            "Qwen/Qwen3-8B",
            "Qwen/Qwen3-32B",
            "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        ],
        "auth_env": ["MODELSCOPE_API_KEY", "MODELSCOPE_TOKEN"],
        "requires_key": True,
        "free_note": "实名用户每天 2000 次总额度，单模型上限 500 次",
        "key_url": "https://modelscope.cn/my/myaccesstoken",
        "supports_tools": "unknown",
        "recommended": False,
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "THUDM/GLM-4-9B-0414",
        "candidate_models": [
            "THUDM/GLM-4-9B-0414",
            "THUDM/GLM-Z1-9B-0414",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
        "auth_env": ["SILICONFLOW_API_KEY"],
        "requires_key": True,
        "free_note": "多款永久免费模型，速率限制较宽松",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "supports_tools": "unknown",
        "recommended": False,
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5:7b",
        "candidate_models": ["qwen2.5:7b", "qwen2.5:3b", "llama3.1:8b"],
        "auth_env": [],
        "requires_key": False,
        "free_note": "本机推理，零成本、数据不出本机",
        "key_url": "https://ollama.com",
        "supports_tools": "unknown",
        "recommended": False,
    },
    "dashscope": {
        "label": "阿里云百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "candidate_models": ["qwen-plus", "qwen-turbo", "qwen-max"],
        "auth_env": ["DASHSCOPE_API_KEY"],
        "requires_key": True,
        "free_note": "付费服务，新用户有一次性体验额度",
        "key_url": "https://bailian.console.aliyun.com",
        "supports_tools": "native",
        "recommended": False,
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "deepseek/deepseek-chat:free",
        "candidate_models": [
            "deepseek/deepseek-chat:free",
            "qwen/qwen3-8b:free",
        ],
        "auth_env": ["OPENROUTER_API_KEY"],
        "requires_key": True,
        "free_note": "带 :free 后缀的模型免费",
        "key_url": "https://openrouter.ai/keys",
        "supports_tools": "native",
        "recommended": False,
    },
    "custom": {
        "label": "自定义端点",
        "base_url": "",
        "default_model": "gpt-3.5-turbo",
        "candidate_models": [],
        "auth_env": ["LLM_API_KEY"],
        "requires_key": False,
        "free_note": "任意 OpenAI 兼容端点",
        "key_url": "",
        "supports_tools": "unknown",
        "recommended": False,
    },
    "offline": {
        "label": "本地模式",
        "base_url": "",
        "default_model": "rule-engine",
        "candidate_models": [],
        "auth_env": [],
        "requires_key": False,
        "free_note": "无需密钥的本地应答模式",
        "key_url": "",
        "supports_tools": "none",
        "recommended": False,
    },
}

# 服务商 ID -> 允许读取的环境变量名
AUTH_ENV_MAP: Dict[str, List[str]] = {
    key: preset["auth_env"] for key, preset in PRESETS.items()
}


@dataclass
class Provider:
    """一个可直接调用的大模型服务端点"""
    key: str
    label: str
    base_url: str
    model: str
    api_key: str = ""
    enabled: bool = True
    requires_key: bool = True
    timeout: int = 60
    extra_headers: dict = field(default_factory=dict)
    free_note: str = ""
    key_url: str = ""
    #: 是否原生支持 function calling：native / none / unknown（设计 §1.3）
    supports_tools: str = "unknown"
    #: 上下文窗口大小（token 数）。0 / None 表示「未知，按默认策略处理」。
    #: 由厂商配置项 `context_length` 覆盖；缺省时按 model 猜测。
    context_length: int = 0
    context_length_explicit: bool = False

    @property
    def endpoint(self) -> str:
        """OpenAI 兼容的 chat/completions 完整地址"""
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def is_offline(self) -> bool:
        return self.key == "offline"

    def availability(self) -> Tuple[bool, str]:
        """
        本地静态可用性检查（不发起网络请求）
        :return: (是否可用, 不可用原因)
        """
        if not self.enabled:
            return False, "已在配置中禁用"
        if self.is_offline:
            return True, ""
        if not self.base_url:
            return False, "未配置 base_url"
        try:
            url = urlsplit(self.base_url)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                return False, "API 地址必须是无凭据、查询参数的 http(s) Base URL"
        except ValueError:
            return False, "API 地址格式无效"
        if self.requires_key and not self.api_key:
            names = " / ".join(AUTH_ENV_MAP.get(self.key, [])) or "LLM_API_KEY"
            return False, (f"缺少 API Key，请到「设置 → 模型服务商」填写，"
                           f"或设置环境变量 {names}")
        return True, ""

    def masked_key(self) -> str:
        if not self.api_key:
            return ""
        k = self.api_key
        if len(k) <= 8:
            return "*" * len(k)
        return f"{k[:4]}...{k[-4:]}"

    def describe(self) -> dict:
        ok, reason = self.availability()
        return {
            "key": self.key,
            "label": self.label,
            "model": self.model,
            "enabled": self.enabled,
            "available": ok,
            "reason": reason,
            "api_key": self.masked_key(),
            "free_note": self.free_note,
            "key_url": self.key_url,
            "supports_tools": self.supports_tools,
        }


def _first_env(names) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


# ── 密钥解析器钩子（由 apps/settings 注入，避免 LLM 层反向依赖 settings）──
_KEY_RESOLVER = None  # type: ignore[assignment]


def register_key_resolver(fn) -> None:
    """
    注册一个「按服务商取密钥」的回调：fn(provider_key) -> str。

    apps/settings/services.py 在包初始化时注册 keystore.load_key，
    从而让 GUI 保存的密钥参与解析（设计 §6.2）。
    未注册时行为与改造前完全一致（解耦、无循环导入）。
    """
    global _KEY_RESOLVER
    _KEY_RESOLVER = fn if callable(fn) else None


def resolve_key(provider_key: str) -> str:
    """通过已注册的解析器取密钥；未注册或失败时返回空串（绝不抛异常）"""
    if not _KEY_RESOLVER:
        return ""
    try:
        return _KEY_RESOLVER(provider_key) or ""
    except Exception:
        return ""


def _resolve_api_key(provider_key: str, cfg: dict, auth_env) -> str:
    """
    密钥解析优先级（设计 §6.2 / 任务书要求）：
        ① config.json / config.user.json 中的 api_key
        ② 已注册的密钥解析器（GUI 密钥库 keystore）
        ③ 环境变量 AUTH_ENV_MAP[key]
    """
    return ((cfg.get("api_key") or "").strip()
            or resolve_key(provider_key)
            or _first_env(auth_env)
            or "")


def normalize_base_url(value: str) -> str:
    """允许粘贴 Base URL 或同级模型/对话端点。"""
    base = str(value or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if base.endswith(suffix):
            return base[:-len(suffix)].rstrip("/")
    return base


def selected_provider_key(cfg: dict) -> str:
    """旧链只用于迁移一个选择，不在失败后选择其他服务商。"""
    if cfg.get("active_provider"):
        return str(cfg["active_provider"])
    providers = cfg.get("providers") or {}
    chain = cfg.get("provider_chain") or list(providers)
    for key in chain:
        if key != "offline" and (providers.get(key) or {}).get("enabled", True):
            return key
    return "offline"


# ── 上下文窗口推断 ────────────────────────────────────────
#: 常见模型的上下文窗口（token）。仅用于**未显式配置** context_length 时给出
#: 一个合理缺省值；用户可在设置里按厂商覆盖。
#: 数值取自各模型公开的规格，个别服务商同名模型窗口可能更大，
#: 因此这里偏保守 —— 宁可少留历史，也不要请求被上游拒绝。
_MODEL_CONTEXT_HINTS = (
    # (小写模型名子串, 窗口)
    ("gpt-4o-mini", 128000),
    ("gpt-4o", 128000),
    ("gpt-4-turbo", 128000),
    ("gpt-4", 8192),
    ("gpt-3.5", 16385),
    ("o1-mini", 128000),
    ("o1-", 200000),
    ("o3-mini", 200000),
    ("claude-3-5-sonnet", 200000),
    ("claude-3-opus", 200000),
    ("claude-3-sonnet", 200000),
    ("claude-3-haiku", 200000),
    ("claude-3", 200000),
    ("claude-", 200000),
    ("deepseek-chat", 64000),
    ("deepseek-reasoner", 64000),
    ("deepseek-coder", 64000),
    ("qwen3", 32768),
    ("qwen-max", 32768),
    ("qwen-plus", 131072),
    ("qwen-turbo", 1000000),
    ("qwen", 32768),
    ("glm-4-flash", 128000),
    ("glm-4-air", 128000),
    ("glm-4-plus", 128000),
    ("glm-4", 128000),
    ("glm", 128000),
    ("moonshot", 128000),
    ("kimi", 200000),
    ("abab", 200000),
    ("ernie", 128000),
    ("doubao", 128000),
    ("llama-3.1", 128000),
    ("llama-3", 8192),
    ("llama", 4096),
    ("mistral", 32768),
    ("mixtral", 32768),
    ("gemma", 8192),
    ("phi-3", 128000),
    ("phi-", 4096),
    ("yi-", 32768),
)

#: 完全无法判断时的兜底窗口（保守值）
DEFAULT_CONTEXT_LENGTH = 32768


def guess_context_length(model: str, provider_key: str = "") -> int:
    """按模型名猜测上下文窗口（token）；猜不到返回默认值。"""
    name = (model or "").strip().lower()
    if not name:
        return DEFAULT_CONTEXT_LENGTH
    for token, size in _MODEL_CONTEXT_HINTS:
        if token in name:
            return size
    return DEFAULT_CONTEXT_LENGTH


def build_provider(key: str,
                   cfg: Optional[dict] = None,
                   default_timeout: int = 60) -> Provider:
    """
    把配置片段 + 环境变量 + 密钥库合成一个 Provider 实例
    优先级：cfg.api_key > 密钥库（keystore）> 环境变量
    """
    cfg = cfg or {}
    preset = PRESETS.get(key, PRESETS["custom"])
    # custom 允许完全由环境变量驱动
    auth_env = cfg.get("auth_env") or preset["auth_env"]

    base_url = (cfg.get("base_url")
                or os.environ.get("LLM_BASE_URL", "").strip()
                or preset["base_url"])
    model = (cfg.get("model")
             or preset["default_model"])
    try:
        explicit_context = int(cfg.get("context_length") or 0) > 0
    except (TypeError, ValueError):
        explicit_context = False

    return Provider(
        key=key,
        label=cfg.get("label") or preset["label"],
        base_url=normalize_base_url(base_url),
        model=model,
        api_key=_resolve_api_key(key, cfg, auth_env),
        enabled=bool(cfg.get("enabled", True)),
        requires_key=bool(cfg.get("requires_key", preset["requires_key"])),
        timeout=int(cfg.get("timeout") or default_timeout),
        extra_headers=cfg.get("extra_headers") or {},
        free_note=preset["free_note"],
        key_url=preset["key_url"],
        supports_tools=cfg.get("supports_tools") or preset.get("supports_tools", "unknown"),
        # 上下文窗口：显式配置优先，否则按模型名猜测
        context_length=_resolve_context_length(cfg.get("context_length"), model, key),
        context_length_explicit=explicit_context,
    )


def _resolve_context_length(raw, model: str, provider_key: str) -> int:
    """厂商配置里没填 context_length 时，按模型名推断一个保守值。"""
    try:
        v = int(raw or 0)
    except (TypeError, ValueError):
        v = 0
    if v > 0:
        # 上限护栏：填到几百万的多半是误填，按 4M 封顶
        return min(v, 4_000_000)
    return guess_context_length(model, provider_key)


def build_provider_from(cfg: dict,
                        key: str = "custom",
                        default_timeout: int = 60) -> Provider:
    """
    用一个「临时配置字典」构造 Provider，供「测试连接」使用。

    与 build_provider 的区别：
        · 不读取 CONFIG 里该服务商的配置，完全由传入的 cfg 决定；
        · 不写入 / 影响 provider_chain；
        · 密钥仍按 cfg → 环境变量 → 密钥库 的顺序解析。

    :param cfg: {"base_url":..., "model":..., "api_key":..., "timeout":...}
    :param key: 服务商标识（决定预设默认值与环境变量名）
    """
    cfg = dict(cfg or {})
    return build_provider(key, cfg, default_timeout)
