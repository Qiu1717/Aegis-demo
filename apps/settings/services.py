# -*- coding: utf-8 -*-
"""
apps/settings/services.py — 服务商配置、密钥保存、测试连接、工作区

关键链路（设计 §6.3，保证「保存即生效」）：
    写 keystore → 写 config.user.json（只写 key_ref / has_key，绝不写密钥）
    → apps.reload_config()（原地 clear+update）→ apps.llm.reset_client()

安全约定：
    · 本模块**绝不**把密钥写进任何 JSON 配置文件；
    · 路由层拿到的响应里只有掩码（masked_key）；
    · 详细异常只写日志，前端只拿 code / reason / action。
"""
import json
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

import apps
from apps import CONFIG, logger  # noqa: F401  CONFIG 需要保持引用以便读取最新值
from apps import paths
from apps.llm import providers as llm_providers
from apps.llm.providers import (
    PRESETS,
    build_provider,
    build_provider_from,
    register_key_resolver,
)
from apps.security import errors as sec_errors
from apps.security import keystore
from apps.security import redact

# ── 常量 ──────────────────────────────────────────────────
#: 推荐服务商（DeepSeek：速度快、性价比高，注册即送额度）
RECOMMENDED_KEY = "deepseek"

#: 默认故障切换链（offline 恒保留在末尾，不可移除）
DEFAULT_CHAIN = ["deepseek", "zhipu", "modelscope", "siliconflow", "offline"]

#: 前端回传的「未修改」占位（掩码形态，含省略号）
MASK_MARK = "..."

#: config.user.json 结构版本
USER_CFG_VERSION = 1

#: 测试连接的最小请求
_TEST_MESSAGE = "hi"
_TEST_MAX_TOKENS = 16
_TEST_TIMEOUT = 20

#: 拉取模型列表的超时（比测试连接更短：只是列个清单，不必等太久）
_MODELS_TIMEOUT = 12
#: 模型清单最多返回多少条（防止某些中转站返回上万条把前端撑爆）
_MODELS_LIMIT = 300


# ── 包初始化：把 GUI 密钥库挂到 providers 的密钥解析链上 ────
def _install_key_resolver() -> None:
    """
    providers.build_provider() 的密钥解析顺序：cfg → env → resolver()。
    注册后，GUI 保存的密钥立即生效（设计 §6.2）。
    """
    try:
        register_key_resolver(keystore.load_key)
    except Exception:
        logger.exception("注册密钥解析器失败（GUI 配置的密钥将不可用）")


_install_key_resolver()


# ── 用户级配置读写 ────────────────────────────────────────
def _user_cfg_path() -> str:
    return str(paths.user_config_path())


def read_user_config() -> Dict[str, Any]:
    """读取 config.user.json；不存在或损坏时返回空 dict"""
    try:
        p = _user_cfg_path()
        if not os.path.isfile(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        logger.exception("读取用户配置失败，按空配置处理")
        return {}


def write_user_config(cfg: Dict[str, Any]) -> bool:
    """原子写 config.user.json（tmp + os.replace）"""
    try:
        paths.ensure_dirs()
        target = _user_cfg_path()
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except OSError:
        logger.exception("写入用户配置失败")
        return False


def _llm_section() -> Dict[str, Any]:
    """当前生效配置里的 llm 段"""
    section = CONFIG.get("llm")
    return section if isinstance(section, dict) else {}


def _providers_section() -> Dict[str, Any]:
    section = _llm_section().get("providers")
    return section if isinstance(section, dict) else {}


def _chain() -> List[str]:
    chain = _llm_section().get("provider_chain")
    valid = [k for k in chain if isinstance(k, str)] if isinstance(chain, list) else []
    return valid or list(DEFAULT_CHAIN)


def _hidden_providers() -> List[str]:
    """用户主动删除掉的预设厂商 key 列表。

    内置预设来自 PRESETS 常量，删掉用户侧配置后 get_providers 仍会把它们
    全部塞回列表。靠这份「隐藏名单」才能让删除真正生效，
    同时用户可在「添加厂商」里随时加回来（加回即从名单移除）。
    """
    raw = _llm_section().get("hidden_providers")
    return [k for k in raw if isinstance(k, str)] if isinstance(raw, list) else []


def _is_masked(value: Optional[str]) -> bool:
    """前端回传的是否为掩码（表示「未修改」）"""
    if not value or not isinstance(value, str):
        return True
    return MASK_MARK in value or "…" in value or set(value.strip()) <= {"*", "•"}


# ── 服务商视图 ────────────────────────────────────────────
def _preset_view(key: str) -> Dict[str, Any]:
    """PRESETS 中一项的对外视图（不含任何密钥）"""
    preset = PRESETS.get(key) or PRESETS.get("custom") or {}
    return {
        "key": key,
        "label": preset.get("label", key),
        "base_url": preset.get("base_url", ""),
        "default_model": preset.get("default_model", ""),
        "candidate_models": list(preset.get("candidate_models") or []),
        "free_note": preset.get("free_note", ""),
        "key_url": preset.get("key_url", ""),
        "requires_key": bool(preset.get("requires_key", True)),
        "supports_tools": preset.get("supports_tools", "unknown"),
        "recommended": bool(preset.get("recommended", False)),
    }


def all_presets() -> List[Dict[str, Any]]:
    """全部服务商预设（供下拉与向导使用）"""
    keys = [k for k in PRESETS.keys() if k != "offline"]
    keys.sort(key=lambda k: (not PRESETS.get(k, {}).get("recommended", False), k))
    return [_preset_view(k) for k in keys]


def _provider_view(key: str) -> Dict[str, Any]:
    """单个服务商的当前状态（掩码显示）"""
    cfg = _providers_section().get(key) or {}
    preset = PRESETS.get(key) or PRESETS.get("custom") or {}
    provider = build_provider(key, cfg, int(_llm_section().get("timeout", 60)))
    if provider.api_key:
        # 明文一旦进入内存立即登记，保证日志脱敏
        redact.register_secret(provider.api_key)

    ok, reason = provider.availability()
    key_ref = cfg.get("key_ref")
    if not isinstance(key_ref, str) or not key_ref:
        key_ref = "aegis:%s" % key
    has_key = bool(cfg.get("has_key")) or bool(provider.api_key)
    env_names = cfg.get("auth_env") or preset.get("auth_env") or []
    from_env = bool(llm_providers._first_env(env_names))

    return {
        "key": key,
        "label": provider.label,
        "custom": key not in PRESETS,
        "enabled": bool(cfg.get("enabled", True)),
        "base_url": cfg.get("base_url") or preset.get("base_url", ""),
        "model": cfg.get("model") or preset.get("default_model", ""),
        "timeout": int(cfg.get("timeout") or _llm_section().get("timeout", 60)),
        "candidate_models": list(preset.get("candidate_models") or []),
        "free_note": preset.get("free_note", ""),
        "key_url": preset.get("key_url", ""),
        "requires_key": bool(provider.requires_key),
        "supports_tools": preset.get("supports_tools", "unknown"),
        "recommended": bool(preset.get("recommended", False)),
        "has_key": has_key,
        "from_env": from_env,
        "key_ref": key_ref,
        "masked_key": provider.masked_key(),
        "available": ok,
        "reason": reason,
        "auth_env": list(env_names),
        # 上下文窗口：回传「已配置值」（0 = 未配置，按模型自动推断）
        "context_length": int(cfg.get("context_length") or 0),
        # 供前端提示用：该厂商当前实际生效的窗口
        "context_length_effective": int(provider.context_length or 0),
    }


def get_providers() -> Dict[str, Any]:
    """设置页 / 向导需要的完整视图（密钥一律掩码）"""
    chain = _chain()
    hidden = set(_hidden_providers())
    known = set(PRESETS.keys()) - hidden
    ordered: List[str] = []
    for key in chain:
        if key not in ordered and key not in hidden:
            ordered.append(key)
    for key in _providers_section().keys():
        if key not in ordered and key not in hidden:
            ordered.append(key)
    for key in known:
        if key not in ordered and key != "offline":
            ordered.append(key)

    active = llm_providers.selected_provider_key(_llm_section())

    return {
        "active": active,
        "chain": [k for k in ordered if k != "offline"] + ["offline"],
        "providers": [_provider_view(k) for k in ordered if k != "offline"],
        # 预设下拉同步过滤已删除项，否则用户会看到「删了却又出现在添加列表里」
        "presets": [p for p in all_presets() if p.get("key") not in hidden],
        "recommended": RECOMMENDED_KEY,
        "workspace": get_workspace().get("path", ""),
        "caps": dict(read_user_config().get("provider_caps") or {}),
        "agent": dict(read_user_config().get("agent") or {}),
        "keystore": keystore.describe(),
        "active_label": _active_label(),
        "permission_mode": get_permission_mode(),
        "temperature": _llm_section().get("temperature", 0.7),
        "max_tokens": _llm_section().get("max_tokens", 4096),
    }


def _active_label() -> str:
    try:
        import apps.llm as llm_pkg
        return llm_pkg.get_active_label()
    except Exception:
        return "未配置模型"


# ── 保存 ──────────────────────────────────────────────────
#: 自定义厂商 key 命名规范（小写字母开头，字母数字下划线）
_CUSTOM_KEY_OK = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def _normalize_chain(chain: Any, providers_known: Optional[Dict] = None) -> List[str]:
    """清洗故障切换链：去重、过滤未知项、offline 恒在末尾且不可移除。

    保留链中「属于 PRESETS 成员」或「存在于 providers 配置段」的 key，
    使前端新增的自定义厂商也能留在链中并被使用。

    providers_known 为本「次请求正在保存」的 providers（如 providers_user），
    必须一并纳入保留判定；否则首次把自定义厂商（如 deepseek）加入链并保存时，
    全局 CONFIG 尚未包含该厂商，_keep 会把它误删，导致该厂商既不在链中、
    也不会被使用（模型显示回退到链首默认）。
    """
    providers_cfg = _providers_section()
    known = set(providers_cfg.keys())
    if isinstance(providers_known, dict):
        known.update(providers_known.keys())
    def _keep(key) -> bool:
        return (isinstance(key, str)
                and (key in PRESETS or key in known))
    valid = [k for k in (chain or []) if _keep(k)]
    result: List[str] = []
    for key in valid:
        if key == "offline":
            continue
        if key not in result:
            result.append(key)
    if not result:
        result = [k for k in DEFAULT_CHAIN if k != "offline"]
    result.append("offline")
    return result


def save_providers(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    保存服务商配置。

    payload 约定::

        {
          "active": "deepseek",                     # 可选
          "chain": ["deepseek", "zhipu"],           # 可选，offline 自动补末尾
          "providers": [                           # 可选
            {"key":"deepseek", "enabled":true, "base_url":"...",
             "model":"deepseek-chat", "api_key":"<明文或留空>", "timeout":60}
          ],
          "workspace": "C:\\\\Users\\\\me\\\\project"      # 可选
        }

    api_key 留空或为掩码表示「不修改已保存的密钥」。
    """
    payload = payload if isinstance(payload, dict) else {}
    # 先验证，避免参数无效时已写入密钥或删除厂商。
    ranges = {"temperature": (0, 2), "max_tokens": (1, 4_000_000)}
    incoming_items = payload.get("providers") or []
    if isinstance(incoming_items, dict):
        incoming_items = list(incoming_items.values())
    checks = [(field, payload[field], low, high) for field, (low, high) in ranges.items() if field in payload]
    if isinstance(incoming_items, list):
        for item in incoming_items:
            if isinstance(item, dict):
                checks.extend((field, item[field], low, high) for field, low, high in
                              (("timeout", 5, 600), ("context_length", 0, 4_000_000)) if field in item)
    for field, value, low, high in checks:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high
                or (field != "temperature" and int(value) != value)):
            return {"ok": False, "error": {"code": "BAD_REQUEST", "reason": "%s 数值无效" % field,
                                          "action": "请检查设置范围后再保存"}}
    user_cfg = read_user_config()

    llm_user = user_cfg.get("llm")
    if not isinstance(llm_user, dict):
        llm_user = {}
    providers_user = llm_user.get("providers")
    if not isinstance(providers_user, dict):
        providers_user = {}

    incoming = payload.get("providers")
    if isinstance(incoming, dict):
        incoming = [dict(v, key=k) for k, v in incoming.items()]
    if not isinstance(incoming, list):
        incoming = []

    saved_keys: List[str] = []
    key_failed: List[str] = []
    invalid_providers: List[Dict[str, str]] = []

    # ── 删除厂商 ────────────────────────────────────────
    # 删除厂商时保留禁用标记，防止默认配置深合并后又重新启用。
    # 实际使用 Aegis 的人都是自带 API Key 接自己的中转/私有模型，
    # 内置那几档用不上却删不掉，属于强加约束。
    #
    # 预设厂商来自 PRESETS 常量，单删 providers_user 里的配置无效 ——
    # get_providers 会把所有 PRESETS 重新塞回列表。所以要删预设必须
    # 额外记一笔「用户已隐藏」，见 _hidden_providers()。
    remove_list = payload.get("remove")
    if isinstance(remove_list, list):
        for rkey in remove_list:
            rkey = str(rkey or "").strip()
            if not rkey or rkey == "offline":
                continue
            providers_user[rkey] = {"enabled": False}
            hidden = list(llm_user.get("hidden_providers", _hidden_providers()))
            if rkey not in hidden:
                hidden.append(rkey)
            llm_user["hidden_providers"] = hidden
            if rkey == llm_providers.selected_provider_key(dict(_llm_section(), **llm_user)):
                llm_user["active_provider"] = "offline"
            try:
                keystore.delete_key(rkey)
            except Exception:
                logger.warning("删除厂商 %s 的密钥失败（忽略）", rkey)

    for item in incoming:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key or key == "offline":
            continue

        is_custom = key not in PRESETS
        if is_custom and not _CUSTOM_KEY_OK.match(key):
            invalid_providers.append(
                {"key": key, "reason": "标识只能用小写字母开头的字母数字下划线（2-32 位）"})
            continue

        # 重新添加曾被删除的预设厂商 → 从隐藏名单里移除，恢复显示。
        # 没有这一步的话，用户删掉 zhipu 后再也无法加回来。
        if key in set(llm_user.get("hidden_providers", _hidden_providers())):
            llm_user["hidden_providers"] = [k for k in llm_user.get("hidden_providers", _hidden_providers()) if k != key]

        entry = providers_user.get(key)
        if not isinstance(entry, dict):
            entry = {}

        # 自定义厂商：首次必须给出可访问的 base_url
        base_url_in = item.get("base_url")
        if is_custom and not entry.get("base_url"):
            bu = (base_url_in or "").strip() if isinstance(base_url_in, str) else ""
            if not bu.lower().startswith(("http://", "https://")):
                invalid_providers.append(
                    {"key": key, "reason": "API 地址必须以 http(s):// 开头"})
                continue

        if "enabled" in item:
            entry["enabled"] = bool(item.get("enabled"))
        if isinstance(base_url_in, str) and base_url_in.strip():
            entry["base_url"] = llm_providers.normalize_base_url(base_url_in)
        label = item.get("label")
        if isinstance(label, str) and label.strip():
            entry["label"] = label.strip()
        model = item.get("model")
        if isinstance(model, str) and model.strip():
            entry["model"] = model.strip()
        timeout = item.get("timeout")
        if isinstance(timeout, (int, float)) and timeout > 0:
            entry["timeout"] = int(timeout)

        # 0 必须落库，才能覆盖之前显式设置的窗口并恢复默认预算。
        ctx_len = item.get("context_length")
        if isinstance(ctx_len, (int, float)):
            entry["context_length"] = int(ctx_len)

        # ── 密钥：写系统凭据区，配置文件只留 key_ref / has_key ──
        api_key = item.get("api_key")
        if isinstance(api_key, str) and api_key.strip() and not _is_masked(api_key):
            secret = api_key.strip()
            # 写后立即回读校验：确保密钥确实可被读出（防「看似保存成功但重启后丢失」）
            if keystore.save_key(key, secret) and keystore.load_key(key) == secret:
                entry["key_ref"] = "aegis:%s" % key
                entry["has_key"] = True
                saved_keys.append(key)
            else:
                key_failed.append(key)
                logger.error("保存 %s 的密钥失败或回读校验未通过（本机密钥库不可用）", key)

        providers_user[key] = entry

    llm_user["providers"] = providers_user
    for field in ranges:
        if field in payload:
            llm_user[field] = payload[field]

    if "chain" in payload:
        llm_user["provider_chain"] = _normalize_chain(payload.get("chain"), providers_user)

    # ── 删除自定义厂商后同步清出故障切换链 ─────────────────
    removed_keys = ({str(r or "").strip() for r in remove_list}
                    if isinstance(remove_list, list) else set())
    if removed_keys:
        chain_after = [k for k in (llm_user.get("provider_chain") or _chain())
                       if k not in removed_keys]
        llm_user["provider_chain"] = _normalize_chain(chain_after)

    active = payload.get("active")
    if isinstance(active, str) and active.strip():
        akey = active.strip()
        # 允许自定义厂商（存在于 providers 配置段）被设为 active。
        # 注意：必须同时检查「本次请求正在保存的 providers_user」，
        # 否则首次把自定义厂商（如 deepseek）设为 active 时，全局 CONFIG
        # 尚未包含该厂商，active_provider 不会被写入，导致模型显示回退到链首默认。
        if akey in PRESETS or akey in providers_user or akey in _providers_section():
            llm_user["active_provider"] = akey

    if not llm_user.get("active_provider"):
        effective = dict(_llm_section(), **llm_user)
        llm_user["active_provider"] = llm_providers.selected_provider_key(effective)

    user_cfg["llm"] = llm_user
    user_cfg["version"] = USER_CFG_VERSION

    # ── 工作区（可选，同一请求里一起保存）──────────────────
    workspace = payload.get("workspace")
    if isinstance(workspace, str) and workspace.strip():
        ws = _validate_workspace(workspace.strip())
        if ws["valid"]:
            user_cfg["workspace"] = ws["path"]
        else:
            logger.warning("保存时忽略无效工作区：%s", ws.get("reason"))

    if not write_user_config(user_cfg):
        return {"ok": False, "error": sec_errors.error_info("SERVER_ERROR")}

    # ── 保存即生效 ────────────────────────────────────────
    try:
        apps.reload_config()
    except Exception:
        logger.exception("保存后重新加载配置失败")
    try:
        import apps.llm as llm_pkg
        llm_pkg.reset_client()
    except Exception:
        logger.exception("保存后重置大模型客户端失败")

    view = get_providers()
    view["saved_keys"] = saved_keys
    view["key_failed"] = key_failed
    view["invalid_providers"] = invalid_providers
    view["active_label"] = _active_label()
    return view


# ── 拉取模型列表 ──────────────────────────────────────────
def list_models(provider_key: str,
                api_key: Optional[str] = None,
                base_url: Optional[str] = None) -> Dict[str, Any]:
    """
    拉取某个服务商可用的模型清单。

    走 OpenAI 兼容端点通用的 ``GET {base_url}/models``，因此对所有
    「OpenAI 兼容」的自定义厂商都能用（用户自己的中转站、私有推理服务等）。
    部分厂商不实现该接口，此时回退到其预设的 ``candidate_models``。

    :return: {"ok": True,  "models": ["gpt-4o", ...], "source": "remote"|"preset"}
             或 {"ok": False, "error": {code, reason, action}, "models": [...]}
    """
    key = str(provider_key or "").strip()
    if not key:
        return {"ok": False, "error": sec_errors.error_info("NO_PROVIDER"),
                "models": []}

    # 未指定厂商时视为「自定义端点」，用前端传来的 base_url 探测
    if key in ("custom", "-", "manual"):
        key = "__custom_probe__"
        cfg = {"base_url": (base_url or "").strip()}
    else:
        cfg = dict(_providers_section().get(key) or {})
        if base_url:
            cfg["base_url"] = base_url

    secret = ""
    if api_key and not _is_masked(api_key):
        secret = api_key.strip()
    elif key != "__custom_probe__":
        secret = build_provider(key, cfg).api_key or ""
    cfg["api_key"] = secret

    # 探测自定义端点时借 "custom" 预设的字段（base_url 是 OpenAI 兼容的 /v1 形态）
    pkey = key
    provider = build_provider_from(cfg, pkey)
    if key == "__custom_probe__":
        # 未保存的新端点不能借用其他厂商的已保存密钥。
        provider.api_key = secret
        provider.requires_key = False
    ok, reason = provider.availability()
    if not ok:
        code = "NO_KEY" if provider.requires_key and not provider.api_key else "NO_PROVIDER"
        info = _public_error(code)
        info["reason"] = reason
        return {"ok": False, "error": info, "models": []}

    # 组装 {base_url}/models —— provider.endpoint 指向的是 chat/completions，
    # 这里要的是同级的 models 端点，所以从 base_url 拼而不是复用 endpoint。
    base = provider.base_url
    url = base + "/models"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(provider.extra_headers or {})
    if secret:
        headers["Authorization"] = "Bearer %s" % secret
        redact.register_secret(secret)

    started = time.time()
    try:
        resp = requests.get(url, headers=headers,
                            timeout=(6, _MODELS_TIMEOUT), allow_redirects=False)
    except Exception as e:
        logger.warning("拉取模型列表 %s 异常：%s", pkey, type(e).__name__)
        return {"ok": False, "error": _public_error(e),
                "models": _preset_models(key), "source": "preset",
                "latency_ms": int((time.time() - started) * 1000)}

    latency_ms = int((time.time() - started) * 1000)

    if resp.status_code != 200:
        logger.info("拉取模型列表 %s 未成功：HTTP %s", pkey, resp.status_code)
        info = _public_error(resp.status_code)
        if resp.status_code in (404, 405, 501):
            info = {"code": "MODELS_UNAVAILABLE", "reason": "模型列表端点不可用（HTTP %s）" % resp.status_code,
                    "action": "检查 Base URL；该端点可能不支持列模型，可手动填写模型名"}
        elif resp.status_code in (400, 422):
            info = {"code": "BAD_REQUEST", "reason": "模型列表请求被拒绝（HTTP %s）" % resp.status_code,
                    "action": "检查 Base URL 与服务商的接口格式"}
        return {"ok": False,
                "error": info,
                "models": _preset_models(key), "source": "preset",
                "latency_ms": latency_ms}

    try:
        data = resp.json()
    except ValueError:
        return {"ok": False,
                "error": {"code": "BAD_RESPONSE",
                          "reason": "模型列表返回的不是合法 JSON",
                          "action": "确认该地址确实是 OpenAI 兼容端点"},
                "models": _preset_models(key), "source": "preset",
                "latency_ms": latency_ms}

    # OpenAI 兼容格式：{"data": [{"id": "gpt-4o", ...}, ...]}
    # 少数实现返回 {"models": [...]} 或直接是字符串数组
    items = None
    if isinstance(data, dict):
        items = data.get("data")
        if items is None:
            items = data.get("models")
    elif isinstance(data, list):
        items = data

    if not isinstance(items, list):
        return {"ok": False, "error": {"code": "BAD_RESPONSE", "reason": "模型列表响应缺少 data 数组",
                                      "action": "检查 Base URL 是否为 OpenAI 兼容端点"},
                "models": _preset_models(key), "source": "preset", "latency_ms": latency_ms}

    models: List[str] = []
    if isinstance(items, list):
        for it in items:
            name = ""
            if isinstance(it, str):
                name = it.strip()
            elif isinstance(it, dict):
                for field in ("id", "name", "model"):
                    v = it.get(field)
                    if isinstance(v, str) and v.strip():
                        name = v.strip()
                        break
            if name and name not in models:
                models.append(name)

    if items and not models:
        return {"ok": False, "error": {"code": "BAD_RESPONSE", "reason": "模型列表中没有有效模型标识",
                                      "action": "检查服务商模型列表的响应格式"},
                "models": _preset_models(key), "source": "preset", "latency_ms": latency_ms}
    source = "remote"
    if len(models) > _MODELS_LIMIT:
        models = models[:_MODELS_LIMIT]

    models.sort(key=str.lower)
    return {"ok": True, "models": models, "source": source,
            "latency_ms": latency_ms, "count": len(models)}


def _public_error(status_or_exc) -> Dict[str, Any]:
    """不向 UI 返回远端原文，也不承诺自动换服务商。"""
    info = sec_errors.classify(status_or_exc)
    info.pop("detail", None)
    if info["code"] in ("RATE_LIMIT", "SERVER_ERROR"):
        info["action"] = "稍后重试；如需更换服务商，请在设置中手动选择"
    return info


def _preset_models(key: str) -> List[str]:
    """厂商预设里的候选模型（远端不可用时的兜底）"""
    if key == "__custom_probe__":
        return []
    preset = PRESETS.get(key) or {}
    out = []
    for m in (preset.get("candidate_models") or []):
        if isinstance(m, str) and m.strip() and m.strip() not in out:
            out.append(m.strip())
    if not out and PRESETS.get(key, {}).get("default_model"):
        out.append(str(PRESETS[key]["default_model"]))
    return out


# ── 测试连接 ──────────────────────────────────────────────
def test_connection(provider_key: str,
                    api_key: Optional[str] = None,
                    base_url: Optional[str] = None,
                    model: Optional[str] = None,
                    timeout: Optional[int] = None) -> Dict[str, Any]:
    """
    对单个服务商发一次最小请求，验证连通性与密钥有效性。

    :return: {"ok": True, "latency_ms": 320, "provider": ..., "model": ...}
             或 {"ok": False, "error": {code, reason, action, need_settings}}
    """
    key = str(provider_key or "").strip()
    if not key or key == "offline":
        return {"ok": False,
                "error": sec_errors.error_info("NO_PROVIDER"),
                "latency_ms": 0}

    cfg = dict(_providers_section().get(key) or {})
    if base_url:
        cfg["base_url"] = base_url
    if model:
        cfg["model"] = model

    secret = ""
    if api_key and not _is_masked(api_key):
        secret = api_key.strip()
    else:
        # 未临时传 key 时，走与正式调用一致的解析顺序（env / keystore）
        secret = build_provider(key, cfg).api_key or ""
    cfg["api_key"] = secret

    provider = build_provider_from(cfg, key)

    ok, reason = provider.availability()
    if not ok:
        code = "NO_KEY" if (provider.requires_key and not provider.api_key) else "NO_PROVIDER"
        info = sec_errors.error_info(code)
        info["reason"] = reason or info["reason"]
        return {"ok": False, "error": info, "latency_ms": 0}

    payload = {
        "model": provider.model,
        "messages": [{"role": "user", "content": _TEST_MESSAGE}],
        "max_tokens": _TEST_MAX_TOKENS,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = "Bearer %s" % provider.api_key
        redact.register_secret(provider.api_key)

    started = time.time()
    try:
        resp = requests.post(
            provider.endpoint,
            headers=headers,
            json=payload,
            timeout=(10, int(timeout or _TEST_TIMEOUT)),
        )
    except Exception as e:
        logger.warning("测试连接 %s 异常：%s", key, type(e).__name__)
        return {"ok": False, "error": _public_error(e), "latency_ms": 0}

    latency_ms = int((time.time() - started) * 1000)

    if resp.status_code != 200:
        detail = (resp.text or "")[:160].replace("\n", " ").strip()
        info = _public_error(resp.status_code)
        logger.warning("测试连接 %s 失败：HTTP %s", key, resp.status_code)
        # 顺带把「不支持 tools」的探测结果缓存下来（设计 §1.3）
        if resp.status_code in (400, 404) and _mentions_tools(detail):
            _set_cap(key, {"tools": "none"})
        return {"ok": False, "error": info, "latency_ms": latency_ms}

    _set_cap(key, {"tools": PRESETS.get(key, {}).get("supports_tools", "unknown")})
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "provider": provider.key,
        "label": provider.label,
        "model": provider.model,
    }


def _mentions_tools(text: str) -> bool:
    """错误体是否提示不支持 tools（用于运行时探测，设计 §1.3）"""
    low = (text or "").lower()
    return any(w in low for w in ("tool", "function", "tools"))


def _set_cap(provider_key: str, cap: Dict[str, Any]) -> None:
    """把运行时探测结果写进 config.user.json::provider_caps"""
    user_cfg = read_user_config()
    caps = user_cfg.get("provider_caps")
    if not isinstance(caps, dict):
        caps = {}
    entry = caps.get(provider_key)
    if not isinstance(entry, dict):
        entry = {}
    entry.update(cap)
    caps[provider_key] = entry
    user_cfg["provider_caps"] = caps
    write_user_config(user_cfg)


# ── 工作区 ────────────────────────────────────────────────
def _validate_workspace(raw: str) -> Dict[str, Any]:
    """校验工作区路径：必须存在且是目录"""
    path = (raw or "").strip().strip('"')
    if not path:
        return {"valid": False, "path": "", "reason": "工作区路径为空"}
    try:
        resolved = os.path.abspath(os.path.expanduser(path))
    except (OSError, ValueError):
        return {"valid": False, "path": "", "reason": "工作区路径不合法"}
    if not os.path.exists(resolved):
        return {"valid": False, "path": resolved, "reason": "该目录不存在"}
    if not os.path.isdir(resolved):
        return {"valid": False, "path": resolved, "reason": "该路径不是目录"}
    # 可读性校验：打开目录句柄试探（不遍历内容），无权限时明确提示
    try:
        handle = os.scandir(resolved)
        handle.close()
    except PermissionError:
        return {"valid": False, "path": resolved,
                "reason": "该目录不可读（当前账号无访问权限）"}
    except OSError:
        return {"valid": False, "path": resolved, "reason": "该目录无法访问"}
    return {"valid": True, "path": resolved, "reason": ""}


def get_workspace() -> Dict[str, Any]:
    """当前工作区；未配置时给出一个建议路径（用户主目录下的 AegisWorkspace）"""
    user_cfg = read_user_config()
    current = str(user_cfg.get("workspace") or "").strip()
    info = _validate_workspace(current) if current else {
        "valid": False, "path": "", "reason": "尚未选择工作区"}
    info["configured"] = bool(current)
    info["suggest"] = os.path.join(os.path.expanduser("~"), "AegisWorkspace")
    return info


def set_workspace(path: str) -> Dict[str, Any]:
    """设置工作区（后端二次校验，UI 不能绕过）"""
    result = _validate_workspace(path)
    if not result["valid"]:
        return {"ok": False, **result}

    user_cfg = read_user_config()
    user_cfg["workspace"] = result["path"]
    user_cfg["version"] = USER_CFG_VERSION
    if not write_user_config(user_cfg):
        return {"ok": False, "valid": False, "path": "",
                "reason": "写入用户配置失败，请查看日志"}

    try:
        apps.reload_config()
    except Exception:
        logger.exception("设置工作区后重新加载配置失败")

    return {"ok": True, **result}


# ── 权限模式（三级）────────────────────────────────────────
#: 四档权限：完全访问 / 需用户批准 / 只读计划 / 仅保留对话
PERMISSION_MODES = ("full", "approve", "plan", "chat")
#: 默认权限（与历史行为一致：写操作逐次确认）
DEFAULT_PERMISSION_MODE = "approve"


def get_permission_mode() -> str:
    """当前权限模式（未设置时返回默认值）"""
    try:
        agent_cfg = read_user_config().get("agent")
        mode = str((agent_cfg or {}).get("permission_mode") or "").strip().lower()
    except Exception:
        mode = ""
    return mode if mode in PERMISSION_MODES else DEFAULT_PERMISSION_MODE


def set_permission_mode(mode: str) -> Dict[str, Any]:
    """保存权限模式（持久化到 config.user.json，重启后保留）"""
    m = str(mode or "").strip().lower()
    if m not in PERMISSION_MODES:
        return {"ok": False, "reason": "权限模式无效", "mode": get_permission_mode()}
    user_cfg = read_user_config()
    agent_cfg = user_cfg.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    agent_cfg["permission_mode"] = m
    user_cfg["agent"] = agent_cfg
    user_cfg["version"] = USER_CFG_VERSION
    if not write_user_config(user_cfg):
        return {"ok": False, "reason": "写入用户配置失败，请查看日志", "mode": get_permission_mode()}
    try:
        apps.reload_config()
    except Exception:
        logger.exception("保存权限模式后重新加载配置失败")
    return {"ok": True, "mode": m, "reason": ""}


# ── 思考量级（三档）───────────────────────────────────────
#: 轻量级=速度优先省 token；均衡=默认；极致性能=更强推理、允许更高耗时
THINKING_LEVELS = ("light", "balanced", "max")
DEFAULT_THINKING_LEVEL = "balanced"
THINKING_LABELS = {"light": "轻量级", "balanced": "均衡", "max": "极致性能"}


def get_thinking_level() -> str:
    """当前思考量级（未设置时返回默认值）"""
    try:
        value = str(read_user_config().get("thinking_level") or "").strip().lower()
    except Exception:
        value = ""
    return value if value in THINKING_LEVELS else DEFAULT_THINKING_LEVEL


def set_thinking_level(level: str) -> Dict[str, Any]:
    """保存思考量级（持久化到 config.user.json，重启后保留）"""
    value = str(level or "").strip().lower()
    if value not in THINKING_LEVELS:
        return {"ok": False, "reason": "思考量级无效（可选 light / balanced / max）",
                "level": get_thinking_level()}
    user_cfg = read_user_config()
    user_cfg["thinking_level"] = value
    user_cfg["version"] = USER_CFG_VERSION
    if not write_user_config(user_cfg):
        return {"ok": False, "reason": "写入用户配置失败，请查看日志",
                "level": get_thinking_level()}
    return {"ok": True, "level": value, "label": THINKING_LABELS[value], "reason": ""}


# ── 首次启动引导 ──────────────────────────────────────────
def _has_usable_provider() -> bool:
    """仅检查选中者，其他已配置的服务商不能掩盖选择不可用。"""
    key = llm_providers.selected_provider_key(_llm_section())
    if key == "offline" or (key not in PRESETS and key not in _providers_section()):
        return False
    provider = build_provider(key, _providers_section().get(key), int(_llm_section().get("timeout", 60)))
    return provider.availability()[0]


def bootstrap() -> Dict[str, Any]:
    """首次启动向导需要的一切信息（不含任何密钥）"""
    workspace = get_workspace()
    has_provider = _has_usable_provider()
    has_workspace = bool(workspace.get("valid"))

    presets = all_presets()
    for item in presets:
        item["has_key"] = bool(keystore.list_keys().get(item["key"]))

    return {
        "need_setup": (not has_provider) or (not has_workspace),
        "has_provider": has_provider,
        "has_workspace": has_workspace,
        "recommended": RECOMMENDED_KEY,
        "presets": presets,
        "workspace": workspace.get("path", ""),
        "workspace_suggest": workspace.get("suggest", ""),
        "active": _llm_section().get("active_provider") or RECOMMENDED_KEY,
        "active_label": _active_label(),
        "keystore": keystore.describe(),
        "app_name": "Aegis",
    }


# ── 导出（不含密钥）───────────────────────────────────────
def export_config() -> Dict[str, Any]:
    """导出配置模板（AEG-K06）：**不含任何密钥**，可安全分享/换机"""
    user_cfg = read_user_config()
    providers_out: List[Dict[str, Any]] = []
    for key in _chain():
        if key == "offline":
            continue
        cfg = _providers_section().get(key) or {}
        preset = PRESETS.get(key) or {}
        providers_out.append({
            "key": key,
            "label": preset.get("label", key),
            "enabled": bool(cfg.get("enabled", True)),
            "base_url": cfg.get("base_url") or preset.get("base_url", ""),
            "model": cfg.get("model") or preset.get("default_model", ""),
            "timeout": int(cfg.get("timeout") or _llm_section().get("timeout", 60)),
            "key_ref": cfg.get("key_ref") or ("aegis:%s" % key),
            "has_key": bool(cfg.get("has_key")),
            # 导入时需用户重新填写，这里刻意留空
            "api_key": "",
        })

    return {
        "app": "Aegis",
        "version": USER_CFG_VERSION,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "notice": "本文件不含任何 API Key，导入后请在设置中重新填写密钥。",
        "active_provider": _llm_section().get("active_provider") or RECOMMENDED_KEY,
        "provider_chain": [k for k in _chain() if k != "offline"],
        "providers": providers_out,
        "provider_caps": dict(user_cfg.get("provider_caps") or {}),
        "workspace": workspace_path() or "",
        "agent": dict(user_cfg.get("agent") or {}),
        "llm": {
            "timeout": int(_llm_section().get("timeout", 60)),
            "temperature": _llm_section().get("temperature", 0.7),
            "max_tokens": _llm_section().get("max_tokens", 4096),
        },
    }


def workspace_path() -> str:
    """当前工作区绝对路径（未配置返回空串）"""
    return str(get_workspace().get("path") or "")
