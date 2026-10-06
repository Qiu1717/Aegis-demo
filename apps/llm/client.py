# -*- coding: utf-8 -*-
"""
apps/llm/client.py — 大模型调用客户端

职责：
    1. 按 provider_chain 顺序依次尝试各个服务商
    2. 某个服务商失败（欠费 / Key 无效 / 限流 / 断网 / 模型不存在）自动切下一个
    3. 全部失败时降级到本地模式应答引擎，保证系统永不报错
    4. 统一 OpenAI 兼容的流式（SSE）与非流式调用

对外产出的是事件字典，便于上层直接转成 SSE：
    {"type": "provider", ...}  已选定某个服务商
    {"type": "delta",    ...}  一段增量文本
    {"type": "notice",   ...}  提示信息（如已降级）
    {"type": "error",    ...}  最终失败
"""
import json
from typing import Dict, Generator, List, Optional, Tuple

import requests

from . import offline
from .providers import PRESETS, Provider, build_provider
from apps import logger
from apps.security.errors import classify


class ProviderFailed(Exception):
    """某个服务商调用失败，应当切换下一个"""

    def __init__(self, message: str, info: Optional[dict] = None):
        super().__init__(message)
        #: 结构化分级错误 {code, reason, action, need_settings, retryable, detail}
        self.info = info or {}


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数（不依赖 tiktoken，保证零第三方依赖、跨平台可用）。
    混合中英文场景下：中文约 1.5 字符/token，英文约 4 字符/token。
    这里统一用 len/2 作为偏保守的近似——宁可少装几条，也不撑爆上下文窗口。
    """
    if not text:
        return 0
    return max(1, (len(text) + 1) // 2)


#: 压缩 system 文本时优先保留的段首标记（按顺序匹配）
#: 用意：长期记忆被裁剪时，尽量保住「必须遵守」的硬约束，其次是身份/能力说明，
#: 最后才是一般性偏好 —— 顺序即优先级。
_COMPRESS_KEEP_PREFIXES = (
    "你必须", "必须", "禁止", "不得", "不要", "严格",
    "你的身份", "你是", "身份",
    "能力", "你可以", "你能",
    "规则", "约束", "要求",
    "偏好", "风格", "偏好：",
)


def _compress_for_budget(text: str, budget_tokens: int) -> str:
    """
    把 system 文本压缩到预算内（结构化裁剪，不做模型改写）。

    为什么用「规则裁剪」而不是「让模型总结」：
        · 压缩必须**零额外成本、零延迟**。若调模型做摘要，一次请求的成本和
          延迟会抵消压缩本身的意义，且摘要本身还要再占一次上下文。
        · system 提示词里的硬约束（禁止/必须/不得）被模型改写后可能**语义漂移**，
          这是安全相关的风险，不该交给概率模型。
    代价：压缩是「按结构裁剪」而非「语义摘要」，可能丢细节。
          因此调用方应把预算留得足够宽（见 build_messages 的 room 计算）。

    策略（从高到低）：
        1. 原文已在预算内 → 原样返回（常见情况，零成本）
        2. 逐段丢弃低优先级段（偏好/风格/一般说明），保留高优先级段
        3. 仍超预算 → 按行截断，并在末尾标注被截断
        4. 极端情况（单行就超预算）→ 硬截断
    """
    if not text:
        return text
    if estimate_tokens(text) <= budget_tokens:
        return text

    # 按空行分段（提示词习惯用空行分隔要点）
    blocks = [b for b in text.split("\n\n")]
    if len(blocks) > 1:
        # 给每段打分：段首命中高优先级关键词的排前面
        def priority(block: str) -> int:
            head = block.strip()[:40]
            for i, kw in enumerate(_COMPRESS_KEEP_PREFIXES):
                if head.startswith(kw):
                    return i
            return len(_COMPRESS_KEEP_PREFIXES)

        # 保持原顺序（不打乱提示词语义），只决定「丢哪些」
        keep: list = []
        used = 0
        # 先全留一遍，再从低优先级末尾开始丢
        for idx, b in enumerate(blocks):
            keep.append(True)
        for idx in sorted(range(len(blocks)),
                          key=lambda i: -priority(blocks[i])):
            if used <= budget_tokens:
                break
            if keep[idx]:
                keep[idx] = False
        for idx, b in enumerate(blocks):
            if keep[idx]:
                used += estimate_tokens(b)
        if used <= budget_tokens:
            kept = [b for idx, b in enumerate(blocks) if keep[idx]]
            if kept:
                out = "\n\n".join(kept)
                if len(kept) < len(blocks):
                    out += "\n\n（部分次要偏好因上下文预算已省略）"
                if estimate_tokens(out) <= budget_tokens:
                    return out

    # 兜底：按行截断
    limit_chars = max(64, budget_tokens * 2)
    if len(text) <= limit_chars:
        return text
    out = text[:limit_chars].rstrip()
    out += "\n…（内容因上下文长度限制被截断）"
    return out



# HTTP 状态码 -> 面向用户的中文说明
_HTTP_REASONS = {
    400: "请求被拒绝（模型名可能不存在）",
    401: "API Key 无效或已失效",
    402: "账户余额不足，服务已欠费",
    403: "无访问权限（可能未完成实名认证或未开通该模型）",
    404: "接口地址或模型不存在",
    408: "请求超时",
    429: "请求过于频繁，已触发限流",
}


def _http_reason(status: int) -> str:
    if status in _HTTP_REASONS:
        return _HTTP_REASONS[status]
    if 500 <= status < 600:
        return f"服务端错误 {status}"
    return f"HTTP {status}"


#: 运行时探测到的「服务商是否原生支持 tools」缓存：
#: {provider_key: "native" | "none" | "unknown"}
_TOOLS_CAPS: Dict[str, str] = {}
_CAPS_LOADED = False


def _body_mentions_tools(text: str) -> bool:
    """响应体是否在抱怨 tools / function 参数（用于运行时能力探测）"""
    low = (text or "").lower()
    return any(word in low for word in ("tool", "function", "tools"))


def _caps_load() -> None:
    """从 config.user.json::provider_caps 载入已缓存的探测结论（只做一次）"""
    global _CAPS_LOADED
    if _CAPS_LOADED:
        return
    _CAPS_LOADED = True
    try:
        from apps import paths
        path = paths.user_config_path()
        if not path.is_file():
            return
        with open(str(path), "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        caps = data.get("provider_caps") or {}
        for key, value in caps.items():
            if isinstance(value, dict) and value.get("tools"):
                _TOOLS_CAPS[str(key)] = str(value["tools"])
    except Exception:
        logger.exception("读取 provider_caps 失败，按未探测处理")


def _caps_persist(provider_key: str, value: str) -> None:
    """把探测结论写回 config.user.json::provider_caps（只写结论，不含任何密钥）"""
    try:
        from apps import paths
        path = paths.user_config_path()
        data = {}
        if path.is_file():
            with open(str(path), "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        caps = data.get("provider_caps") or {}
        entry = caps.get(provider_key)
        if not isinstance(entry, dict):
            entry = {}
        entry["tools"] = value
        caps[provider_key] = entry
        data["provider_caps"] = caps
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("持久化 provider_caps 失败（仅影响下次启动的探测缓存）")


def get_tools_cap(provider_key: str, default: str = "unknown") -> str:
    """取某服务商对原生 tools 的能力结论：native / none / unknown"""
    _caps_load()
    return _TOOLS_CAPS.get(provider_key, default)


def set_tools_cap(provider_key: str, value: str) -> None:
    """记录并持久化能力结论（"native" / "none" / "unknown"）"""
    _TOOLS_CAPS[provider_key] = value
    _caps_persist(provider_key, value)


def _avail_info(p: Provider, reason: str) -> dict:
    """
    本地静态可用性检查失败 -> 结构化错误。
    缺密钥归 NO_KEY，其余（未启用 / 缺 base_url）归 NO_PROVIDER。
    """
    if p.requires_key and not p.api_key:
        return classify("NO_KEY")
    info = classify("NO_PROVIDER")
    if reason:
        info["detail"] = reason
    return info


def _error_event(content: str, info: Optional[dict]) -> dict:
    """构造一个携带分级信息的 error 事件（向后兼容：content 字段不变）"""
    info = info or {}
    return {
        "type": "error",
        "content": content,
        "code": info.get("code") or "SERVER_ERROR",
        "reason": info.get("reason") or content,
        "action": info.get("action") or "",
        "need_settings": bool(info.get("need_settings")),
        "retryable": bool(info.get("retryable")),
    }


def _pick_info(infos: List[dict]) -> dict:
    """
    从多个失败原因中挑一条最值得展示给用户的：
    need_settings=True 的优先（用户能自己修好），其次取第一条。
    """
    if not infos:
        return classify("NO_PROVIDER")
    for info in infos:
        if info.get("need_settings"):
            return info
    return infos[0]


class LLMClient:
    """多服务商大模型客户端"""

    def __init__(self, llm_cfg: dict):
        self.cfg = llm_cfg or {}
        self.timeout = int(self.cfg.get("timeout", 60))
        self.temperature = self.cfg.get("temperature", 0.7)
        self.max_tokens = self.cfg.get("max_tokens", 4096)
        self.system_prompt = self.cfg.get("system_prompt", "")

        providers_cfg = self.cfg.get("providers", {}) or {}
        chain = self.cfg.get("provider_chain")
        if not chain:
            chain = list(providers_cfg.keys()) or ["offline"]

        self.providers: Dict[str, Provider] = {}
        for key in chain:
            preset = PRESETS.get(key)
            if key != "offline" and preset is None and key not in providers_cfg:
                continue
            self.providers[key] = build_provider(
                key, providers_cfg.get(key), self.timeout)

        self.chain: List[str] = [k for k in chain if k in self.providers]
        if "offline" not in self.chain:
            # 本地模式永远保留在链尾，确保系统可用
            self.providers["offline"] = build_provider("offline")
            self.chain.append("offline")

        self.last_used: Optional[str] = None
        self.last_errors: List[str] = []
        #: 用户在设置中明确选中的 active_provider（config.llm.active_provider）；
        #: 模型状态展示优先跟随它，而不是「最近用过」或「链上第一个可用」。
        self.active_provider: Optional[str] = self.cfg.get("active_provider")
        #: 最近一次失败的结构化错误（供上层「前往设置」引导使用）
        self.last_error_info: Optional[dict] = None

    # ── 上下文组装 ────────────────────────────────────────
    def _guess_context_provider(self) -> Optional[Provider]:
        """
        在组装上下文前推断「本次大概会用哪个厂商」，取其上下文窗口。

        真实调用时 _pick_provider() 还会按可用性/切换链重新选，
        这里只是**预算估算**用途：宁可按窗口较小的厂商留预算，
        也不要按大窗口留了一堆历史结果被上游拒绝。
        顺序：用户显式选的 active_provider → 链上第一个可用项。
        """
        # 1) 用户在设置里明确选中的
        if self.active_provider:
            p = self.providers.get(self.active_provider)
            if p is not None:
                return p
        # 2) 链上第一个「静态可用」的厂商
        for key in self.chain:
            p = self.providers.get(key)
            if p is None:
                continue
            try:
                ok, _ = p.availability()
            except Exception:
                ok = False
            if ok:
                return p
        # 3) 兜底：链上任意一个
        for key in self.chain:
            if self.providers.get(key) is not None:
                return self.providers[key]
        return None

    def build_messages(self, history: List[dict],                       system_extra: str = None,
                       provider: Optional[Provider] = None,
                       reserve_tokens: int = 0) -> List[dict]:
        """
        把数据库里的历史消息拼成大模型 messages。

        上下文窗口策略（token 预算感知）：
            · system 提示词（含可注入的长期记忆 system_extra）始终保留；
            · 会话历史从「最近一条」往回累加，受三条约束：
              - max_context_messages：消息条数硬上限（防止极短消息刷屏）；
              - max_context_tokens  ：历史部分的 token 预算（防止长消息撑爆窗口）；
              - provider.context_length：当前厂商的上下文窗口，
                预算会按「窗口 - 本轮输出预留 - 系统提示词」动态收窄，
                避免请求发出去被上游以「上下文超限」拒绝；
            · 最新一条用户消息永远保留（即便它本身就超预算），保证本轮有上下文。
            · system_extra 过长时会被**压缩**（见 _compress_for_budget），
              这是「上下文压缩」的兜底实现：优先保留硬约束与关键结论。

        :param provider: 本次实际要用的厂商，用于取它的 context_length。
                         不传时按 active_provider → 链上首个可用项自行推断，
                         这样既有调用方（aichat/services.py）无需改动即可受益。
        :param reserve_tokens: 为本轮模型输出预留的 token（通常等于 max_tokens）。
        """
        messages = []
        if provider is None:
            provider = self._guess_context_provider()

        sys_text = self.system_prompt or ""
        if system_extra:
            sys_text = (sys_text + "\n\n" + system_extra) if sys_text else system_extra

        limit = int(self.cfg.get("max_context_messages", 20))
        budget = int(self.cfg.get("max_context_tokens", 6000))

        # ── 依据当前厂商窗口收窄预算 ──
        window = int(getattr(provider, "context_length", 0) or 0) if provider else 0
        if window > 0:
            sys_tokens = estimate_tokens(sys_text) if sys_text else 0
            # 给输出留出余量（至少 512，避免 max_tokens 配得过大时把历史挤没）
            reserve = max(int(reserve_tokens or 0), 512)
            room = window - sys_tokens - reserve
            # 窗口再大也不超过用户显式设的 max_context_tokens，
            # 避免「填了 200K 窗口就真塞 200K」把费用和延迟拉爆。
            budget = max(256, min(budget, room))

        # ── system 提示词本身超预算时先压缩 ──
        if sys_text and estimate_tokens(sys_text) > budget:
            sys_text = _compress_for_budget(sys_text, budget)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

        recent = []
        used = 0
        for msg in reversed(history):
            role = msg.get("message_role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("message_content") or ""
            t = estimate_tokens(content)
            # 第一条（最新）无论如何都保留；其后超出预算或条数上限则停止
            if recent and (used + t > budget or len(recent) >= limit):
                break
            recent.append({"role": role, "content": content})
            used += t
        recent.reverse()
        messages.extend(recent)
        return messages

    # ── 单个服务商调用 ────────────────────────────────────
    def _headers(self, p: Provider) -> dict:
        h = {"Content-Type": "application/json"}
        if p.api_key:
            h["Authorization"] = f"Bearer {p.api_key}"
        h.update(p.extra_headers or {})
        return h

    def _payload(self, p: Provider, messages: List[dict], stream: bool,
                 tools: Optional[List[dict]] = None,
                 tool_choice: Optional[str] = None) -> dict:
        """
        组装请求体。

        tools 为空时与改造前完全一致（不出现 tools / tool_choice 字段），
        保证对话模式的行为不变。
        """
        payload = {
            "model": p.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        return payload

    def _stream_one(self, p: Provider, messages: List[dict],
                    tools: Optional[List[dict]] = None,
                    tool_choice: Optional[str] = None
                    ) -> Generator[dict, None, None]:
        """调用单个服务商并产出事件；启动阶段的失败抛出 ProviderFailed"""
        try:
            resp = requests.post(
                p.endpoint,
                headers=self._headers(p),
                json=self._payload(p, messages, True, tools, tool_choice),
                stream=True,
                timeout=(10, p.timeout),
            )
        except requests.exceptions.Timeout as e:
            raise ProviderFailed("连接超时", classify(e))
        except requests.exceptions.RequestException as e:
            raise ProviderFailed("网络不可达", classify(e))

        if resp.status_code != 200:
            detail = (resp.text or "")[:160].replace("\n", " ").strip()
            reason = _http_reason(resp.status_code)
            # 详细原文只进日志，不外传（沿用现有安全约定）
            logger.info("服务商 %s 返回 HTTP %s：%s", p.key, resp.status_code, detail)
            # 运行时能力探测：带 tools 请求被拒且响应体提到 tools → 标记不支持原生工具
            if tools and resp.status_code in (400, 404) and _body_mentions_tools(detail):
                logger.info("运行时探测：服务商 %s 不支持原生 tools，切换 JSON 回退", p.key)
                set_tools_cap(p.key, "none")
                yield {"type": "tools_unsupported", "provider": p.key}
            raise ProviderFailed(reason, classify(resp.status_code, detail))

        yield {"type": "provider", "provider": p.key,
               "label": p.label, "model": p.model}

        # 注意：不能用 iter_lines(decode_unicode=True)。
        # requests 对未声明 charset 的 text/event-stream 会按 ISO-8859-1 解码，
        # 导致中文全部变成乱码，因此这里拿原始字节自己按 UTF-8 解码。
        for raw_line in resp.iter_lines(decode_unicode=False):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
                choices = chunk.get("choices") or []
                if choices:
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield {"type": "delta", "content": content}
                    # 原生 function calling：tool_calls 是**增量**片段，逐个透传
                    for item in (delta.get("tool_calls") or []):
                        yield {"type": "tool_delta", "delta": item}
                    reason = choice.get("finish_reason")
                    if reason:
                        yield {"type": "finish", "reason": reason}
            except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
                continue

    def _complete_one(self, p: Provider, messages: List[dict],
                      tools: Optional[List[dict]] = None,
                      tool_choice: Optional[str] = None
                      ) -> Tuple[str, List[dict]]:
        """
        非流式调用单个服务商。

        :return: (文本, 原生 tool_calls 列表)
        """
        try:
            resp = requests.post(
                p.endpoint,
                headers=self._headers(p),
                json=self._payload(p, messages, False, tools, tool_choice),
                timeout=(10, p.timeout),
            )
        except requests.exceptions.Timeout as e:
            raise ProviderFailed("连接超时", classify(e))
        except requests.exceptions.RequestException as e:
            raise ProviderFailed("网络不可达", classify(e))

        if resp.status_code != 200:
            detail = (resp.text or "")[:160].replace("\n", " ").strip()
            reason = _http_reason(resp.status_code)
            logger.info("服务商 %s 返回 HTTP %s：%s", p.key, resp.status_code, detail)
            if tools and resp.status_code in (400, 404) and _body_mentions_tools(detail):
                logger.info("运行时探测：服务商 %s 不支持原生 tools，切换 JSON 回退", p.key)
                set_tools_cap(p.key, "none")
            raise ProviderFailed(reason, classify(resp.status_code, detail))

        try:
            data = resp.json()
        except ValueError:
            raise ProviderFailed("返回内容不是合法 JSON")

        choices = data.get("choices") or []
        if not choices:
            return "", []
        message = choices[0].get("message") or {}
        return message.get("content") or "", list(message.get("tool_calls") or [])

    def _offline_events(self, user_text: str, errors: List[str],
                         memory_text: str = None):
        """离线模式：模拟流式输出（可携带长期记忆用于召回）"""
        yield {"type": "provider", "provider": "offline",
               "label": "离线模式", "model": "rule-engine"}
        text = offline.generate(user_text, memory_text=memory_text)
        step = 12
        for i in range(0, len(text), step):
            yield {"type": "delta", "content": text[i:i + step]}

    # ── 对外主入口：流式 ──────────────────────────────────
    def stream(self, messages: List[dict],
               user_text: str = "", memory_text: str = None
               ) -> Generator[dict, None, None]:
        self.last_errors = []
        self.last_error_info = None
        errors: List[str] = []
        infos: List[dict] = []

        for key in self._ordered_keys():
            p = self.providers[key]
            ok, reason = p.availability()
            if not ok:
                errors.append(f"{p.label}：{reason}")
                infos.append(_avail_info(p, reason))
                continue

            if p.is_offline:
                self.last_used = "offline"
                self.last_error_info = infos[-1] if infos else None
                yield from self._offline_events(user_text, errors, memory_text)
                return

            got_content = False
            try:
                for ev in self._stream_one(p, messages):
                    if ev["type"] == "delta":
                        got_content = True
                    yield ev
            except ProviderFailed as e:
                info = e.info or classify(e)
                if got_content:
                    # 已经开始输出内容后再失败，不再切换，避免串台
                    yield _error_event(f"（{p.label} 输出中断：{e}）", info)
                    self.last_used = key
                    self.last_error_info = info
                    return
                errors.append(f"{p.label}：{e}")
                infos.append(info)
                continue
            except Exception as e:  # 兜底，防止单个服务商把整个生成器搞挂
                errors.append(f"{p.label}：未知异常 {type(e).__name__}")
                infos.append(classify(e))
                continue

            self.last_used = key
            self.last_errors = errors
            return

        # ── 全链失败：给出分级提示（优先「需要用户去设置」的那一条）──
        self.last_errors = errors
        info = _pick_info(infos)
        self.last_error_info = info
        yield _error_event("所有模型服务均不可用：" + "；".join(errors[:3]), info)

    # ── 对外主入口：非流式 ────────────────────────────────
    def complete(self, messages: List[dict],
                 user_text: str = "", memory_text: str = None
                 ) -> Tuple[str, str]:
        self.last_errors = []
        self.last_error_info = None
        errors: List[str] = []
        infos: List[dict] = []

        for key in self._ordered_keys():
            p = self.providers[key]
            ok, reason = p.availability()
            if not ok:
                errors.append(f"{p.label}：{reason}")
                infos.append(_avail_info(p, reason))
                continue

            if p.is_offline:
                self.last_used = "offline"
                self.last_errors = errors
                self.last_error_info = infos[-1] if infos else None
                return offline.generate(user_text, memory_text=memory_text), "离线模式"

            try:
                text, _tool_calls = self._complete_one(p, messages)
            except ProviderFailed as e:
                errors.append(f"{p.label}：{e}")
                infos.append(e.info or classify(e))
                continue
            except Exception as e:
                errors.append(f"{p.label}：未知异常 {type(e).__name__}")
                infos.append(classify(e))
                continue

            if text:
                self.last_used = key
                self.last_errors = errors
                return text, p.label
            errors.append(f"{p.label}：返回内容为空")

        self.last_errors = errors
        self.last_error_info = _pick_info(infos)
        return "所有模型服务均不可用：" + "；".join(errors[:3]), "不可用"

    # ── 对外主入口：Agent 工具调用 ────────────────────────
    def _ordered_keys(self) -> List[str]:
        """
        本次实际尝试服务商的顺序：**用户在设置里选中者优先**，其余按 chain。

        与 `_display_info()` 的展示口径保持一致，确保「设置里选的是谁」就是
        「实际调用的是谁」。此前只按 chain 顺序取，会出现：测试连接测的是
        DeepSeek（成功），实际对话却用了链首的 zhipu（其 key 来自 .env、
        可能已失效）→ 报「API Key 失效」，两处行为完全对不上。
        """
        ordered: List[str] = []
        ap = self.active_provider
        if ap and ap in self.providers:
            ordered.append(ap)
        for key in self.chain:
            if key not in ordered:
                ordered.append(key)
        return ordered

    def _provider_note(self, key: str) -> str:
        """
        失败时说明「实际用的是哪个厂商」；若与设置里选中的不一致，明确点出
        配置未生效，让用户能区分「key 失效 / 配置被覆盖 / 接口报错」。
        注意：只带厂商名与模型名，绝不输出密钥。
        """
        p = self.providers.get(key)
        if p is None:
            return ""
        note = "实际调用 %s（%s）" % (p.label, p.model)
        ap = self.active_provider
        if ap and ap != key and ap in self.providers:
            ap_p = self.providers[ap]
            note += ("；而设置中选择的是 %s（%s）——说明该配置未生效，"
                     "请确认密钥已保存成功并在设置里重新选一次"
                     % (ap_p.label, ap_p.model))
        return note

    def first_available(self, allow_offline: bool = True) -> Optional[Provider]:
        """
        返回本次实际要用的服务商（选中者优先，其余按 chain）。

        Agent 模式用 `allow_offline=False`：离线规则引擎不具备工具调用能力，
        没有真实模型时应直接引导用户去设置，而不是假装在干活。
        """
        for key in self._ordered_keys():
            p = self.providers.get(key)
            if p is None:
                continue
            if p.is_offline and not allow_offline:
                continue
            ok, _reason = p.availability()
            if ok:
                return p
        return None

    def _tools_order(self, provider_key: Optional[str]) -> List[str]:
        """
        工具调用的尝试顺序：指定服务商优先，其余按 chain。

        **始终排除本地模式**：规则引擎不会真的调工具，降级只会产出假结果，
        Agent 模式宁可报错引导用户去设置。
        """
        order: List[str] = []
        preferred = provider_key or self.active_provider
        if preferred and preferred in self.providers:
            order.append(preferred)
        for key in self._ordered_keys():
            if key not in order and key in self.providers:
                order.append(key)
        return [k for k in order if not self.providers[k].is_offline]

    def stream_tools(self, messages: List[dict],
                     tools: Optional[List[dict]] = None,
                     provider_key: Optional[str] = None,
                     tool_choice: Optional[str] = None
                     ) -> Generator[dict, None, None]:
        """
        Agent 模式的流式调用（在原 stream() 之上支持原生工具调用）。

        产出事件：
            provider / delta / tool_delta / finish / tools_unsupported / error

        与 `stream()` 的区别：
            · 支持 tools / tool_choice；
            · **不降级到本地模式**（Agent 需要真实模型，降级只会产出假结果）；
            · 失败时产出带分级信息的 error 事件（code / reason / action / need_settings）。
        """
        self.last_errors = []
        self.last_error_info = None
        errors: List[str] = []
        infos: List[dict] = []

        for key in self._tools_order(provider_key):
            p = self.providers[key]
            ok, reason = p.availability()
            if not ok:
                errors.append(f"{p.label}：{reason}")
                infos.append(_avail_info(p, reason))
                continue

            got_content = False
            try:
                for event in self._stream_one(p, messages, tools, tool_choice):
                    if event.get("type") in ("delta", "tool_delta"):
                        got_content = True
                    yield event
            except ProviderFailed as e:
                info = dict(e.info or classify(e))
                # 带上「实际用的是哪个厂商 / 模型」，便于区分
                # key 失效、配置未生效（选中的与实际调用的不是同一个）、接口报错
                info.setdefault("provider", key)
                info.setdefault("provider_label", p.label)
                info.setdefault("model", p.model)
                # 模型名不存在 / 不被账号支持时，把实际用的模型名直接写进提示，
                # 让用户一眼看出填错了什么（如 deepseek-v4.1-flash 多了个 .1）
                if info.get("code") == "MODEL_NOT_FOUND":
                    info["reason"] = "%s（当前使用的模型名：%s）" % (
                        info.get("reason") or "模型不可用", p.model)
                    info["action"] = ("到「设置」把模型名改成本服务商支持的名称，"
                                      "改完可用「测试连接」验证")
                if got_content:
                    # 已经开始输出后再失败，不再切换，避免串台
                    yield _error_event(
                        "（%s 输出中断：%s｜%s）" % (p.label, e, self._provider_note(key)),
                        info)
                    self.last_used = key
                    self.last_error_info = info
                    return
                errors.append("%s：%s（%s）" % (p.label, e, self._provider_note(key)))
                infos.append(info)
                continue
            except Exception as e:
                errors.append(f"{p.label}：未知异常 {type(e).__name__}")
                infos.append(classify(e))
                continue

            self.last_used = key
            self.last_errors = errors
            if self.last_used and tools:
                # 带 tools 调用成功 → 确认其原生能力，供下次直接使用
                if get_tools_cap(key, "") != "native":
                    set_tools_cap(key, "native")
            return

        self.last_errors = errors
        info = _pick_info(infos)
        self.last_error_info = info
        yield _error_event("所有模型服务均不可用：" + "；".join(errors[:3]), info)

    def complete_tools(self, messages: List[dict],
                       tools: Optional[List[dict]] = None,
                       provider_key: Optional[str] = None,
                       tool_choice: Optional[str] = None
                       ) -> dict:
        """
        Agent 模式的非流式调用（用于强制总结等短调用）。

        :return: {"ok": True, "text":..., "tool_calls":[...],
                  "provider":..., "label":..., "model":...}
                 或 {"ok": False, "error": {code, reason, action, need_settings}}
        """
        self.last_errors = []
        self.last_error_info = None
        errors: List[str] = []
        infos: List[dict] = []

        for key in self._tools_order(provider_key):
            p = self.providers[key]
            ok, reason = p.availability()
            if not ok:
                errors.append(f"{p.label}：{reason}")
                infos.append(_avail_info(p, reason))
                continue

            try:
                text, tool_calls = self._complete_one(p, messages, tools, tool_choice)
            except ProviderFailed as e:
                errors.append(f"{p.label}：{e}")
                infos.append(e.info or classify(e))
                continue
            except Exception as e:
                errors.append(f"{p.label}：未知异常 {type(e).__name__}")
                infos.append(classify(e))
                continue

            self.last_used = key
            self.last_errors = errors
            return {"ok": True, "text": text or "", "tool_calls": tool_calls or [],
                    "provider": p.key, "label": p.label, "model": p.model}

        self.last_errors = errors
        info = _pick_info(infos)
        self.last_error_info = info
        return {"ok": False, "error": info}

    # ── 状态视图 ──────────────────────────────────────────
    def _display_info(self) -> tuple:
        """(显示名, 模型名)：优先用户在设置中明确选中的 active_provider；
        其次最近实际使用的厂商；否则链上第一个可用厂商。

        未配置任何大模型服务时返回 ("未配置模型", "")，
        前端不应展示"离线兜底"一类字样。
        """
        # 1) 用户在设置里显式选中的 active_provider（与保存的配置保持一致）
        ap = self.active_provider
        if ap and ap in self.providers and self.providers[ap].key != "offline":
            p = self.providers[ap]
            ok, _ = p.availability()
            if ok:
                return (f"{p.label} · {p.model}", p.model)
        # 2) 最近实际使用的厂商
        if self.last_used and self.last_used in self.providers:
            p = self.providers[self.last_used]
            if p.key != "offline":
                return (f"{p.label} · {p.model}", p.model)
        # 3) 链上第一个可用厂商
        for key in self.chain:
            p = self.providers[key]
            if p.key == "offline":
                continue
            ok, _ = p.availability()
            if ok:
                return (f"{p.label} · {p.model}", p.model)
        return ("未配置模型", "")

    def describe(self) -> dict:
        display, active_model = self._display_info()
        return {
            "chain": self.chain,
            "active": self.last_used,
            "display": display,
            "active_model": active_model,
            "last_errors": self.last_errors,
            "last_error": self.last_error_info,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "max_context_messages": self.cfg.get("max_context_messages", 20),
            "providers": [self.providers[k].describe() for k in self.chain],
        }

    def active_label(self) -> str:
        """当前（或默认）使用的模型显示名，用于前端展示"""
        return self._display_info()[0]
