# -*- coding: utf-8 -*-
"""
apps/agent/runner.py — Agent 编排循环（设计 §4.2.1 / §4.2.5）

    for step in 1..25:
        yield agent_step
        调 LLM（原生 tools 或 JSON 回退）
        有文本 → yield chunk
        无 tool_calls → 结束
        有 → **串行**执行每个 call：
            yield tool_call → 执行（审批挂起 / 沙箱 / 备份）→ yield tool_result
            有变更再 yield diff + file_change
            回填 messages（assistant 带 tool_calls + role:"tool"）

终止条件：
    无 tool_calls           → completed
    step > max_steps(25)    → max_steps（强制总结一次）
    用户取消                → user（杀子进程）
    连续 3 次工具报错        → tool_errors
    拒绝审批 ≥ 3 次          → denied
    未配置可用模型           → 直接 error + need_settings=true，**不进循环**

⚠️ 本模块**不 import flask**（避免把请求上下文带进生成器）；
   取消用 threading.Event，审批挂起用 ApprovalManager 的 Event。
"""
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional

from apps import logger

from apps.skills.services import SKILL_MARK_RE
from .approval import (
    DECISION_ALLOW_SESSION,
    DECISION_DENY,
    DENY_MESSAGE,
    RISK_DANGEROUS,
    RISK_NEED_APPROVAL,
    RISK_READONLY,
    RISK_WRITE,
    approval_manager,
)
from .fileio import DEFAULT_MAX_LINES
from .parser import (
    MAX_FALLBACK_RETRIES,
    MAX_NATIVE_RETRIES,
    ToolCall,
    accumulate_tool_delta,
    finalize_tool_calls,
    new_call_id,
    parse_fallback,
)
from .prompts import (
    FORCE_SUMMARY_INSTRUCTION,
    INVALID_ARGUMENT_HINT,
    PARSE_RETRY_HINT,
    build_final_instruction,
    build_system_prompt,
)
from .sandbox import Workspace
from .schemas import WEB_TOOL_NAME, build_tool_schemas
from .tools import ToolContext, ToolResult, dispatch, preview, risk_of
from .trace import ChangeSet, persist_change, save_message

#: 默认最大步数（可被 config.agent.max_steps 覆盖）
DEFAULT_MAX_STEPS = 25
#: 连续工具报错上限
MAX_TOOL_ERRORS = 3
#: 拒绝审批上限
MAX_DENIALS = 3
#: 默认命令超时 / 输出上限
DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_OUTPUT_LIMIT = 20000
DEFAULT_SEARCH_RESULTS = 50
# 审批等待上限（秒）。
# 实测证据：审批卡片不可见时用户无法决策，每个审批各阻塞 300s，
# 一次任务里多个审批累计可挂起 25 分钟（09:38:19→10:03:33），
# 期间发送按钮完全无法使用。压到 120s：给足决策时间，最坏情况也只等 2 分钟。
DEFAULT_APPROVAL_TIMEOUT = 120

#: 历史消息参与 Agent 上下文的最大条数（防止撑爆窗口）
MAX_HISTORY_MESSAGES = 20


def new_task_id() -> str:
    """生成本次任务的短 id（用于备份目录与变更清单）"""
    return "t-%s" % uuid.uuid4().hex[:6]


# ── 任务注册表（供 T04 的 /cancel /changes /undo 路由按 task_id 定位）──
_RUNNERS: Dict[str, "AgentRunner"] = {}
_REGISTRY_LOCK = threading.RLock()

#: 变更登记表（独立于 _RUNNERS —— 任务结束后仍可被 /changes /undo 查询，
#   直到全部撤销完成才回收，避免内存泄漏；桌面端单用户，体量可控）
_CHANGESETS: Dict[str, "ChangeSet"] = {}
_CHANGESETS_LOCK = threading.RLock()


def register_runner(runner: "AgentRunner") -> None:
    """登记一个进行中的任务"""
    with _REGISTRY_LOCK:
        _RUNNERS[runner.task_id] = runner


def unregister_runner(task_id: str) -> None:
    """任务结束后注销（避免内存泄漏）"""
    with _REGISTRY_LOCK:
        _RUNNERS.pop(task_id, None)


def get_runner(task_id: str) -> Optional["AgentRunner"]:
    """按 task_id 取运行中的 Runner（不存在返回 None）"""
    with _REGISTRY_LOCK:
        return _RUNNERS.get(task_id)


def cancel_task(task_id: str) -> bool:
    """
    取消一个任务：置 cancel_event（会杀掉在跑的子进程）+ 把挂起的审批按拒绝处理。

    :return: 是否命中一个已知任务
    """
    runner = get_runner(task_id)
    if not runner:
        return False
    runner.cancel_event.set()
    try:
        approval_manager.cancel_task(task_id)
    except Exception:
        logger.exception("取消任务时清理挂起审批失败 task_id=%s", task_id)
    return True


def get_changes(task_id: str) -> List[dict]:
    """查询某任务的变更清单（任务已结束也能查，依赖 _CHANGESETS 登记表）"""
    cs = _CHANGESETS.get(task_id)
    if not cs:
        return []
    return cs.to_list()


def undo_task(task_id: str, change_id: Optional[int] = None) -> dict:
    """
    回滚某任务的变更（逆序）。

    :return: {"restored": n, "removed": n, "failed": [...]}
             任务不存在时返回 {"ok": False, "reason": "任务不存在或已结束"}
    """
    cs = _CHANGESETS.get(task_id)
    if not cs:
        return {"ok": False, "restored": 0, "removed": 0, "failed": [],
                "reason": "任务不存在或已结束，无法撤销"}
    outcome = cs.undo(change_id)
    outcome["ok"] = not outcome.get("failed")
    # 全部撤销完成 → 回收登记表，避免内存泄漏（单用户桌面端）
    if not cs.records:
        with _CHANGESETS_LOCK:
            _CHANGESETS.pop(task_id, None)
    return outcome


@dataclass
class Reply:
    """一次 LLM 回复的统一结构"""
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    error: Optional[dict] = None
    raw_text: str = ""
    retry_step: bool = False


class AgentRunner:
    """单次 Agent 任务的编排器（产出 SSE 事件字典）"""

    def __init__(self, workspace: Workspace, user_task: str,
                 session_id: Optional[int] = None,
                 task_id: Optional[str] = None,
                 history: Optional[List[dict]] = None,
                 cancel_event: Optional[threading.Event] = None,
                 auto_approve_write: bool = False,
                 system_prompt: str = "",
                 config: Optional[dict] = None,
                 permission_mode: str = "approve",
                 thinking_level: str = "",
                 skills: Optional[List[dict]] = None,
                 use_web: bool = True):
        self.ws = workspace
        #: 工作区根路径字符串；无工作区（纯对话降级）时为空串，避免各处重复判空
        self.ws_root = str(workspace.root) if workspace else ""
        self.user_task = (user_task or "").strip()
        self.session_id = session_id
        self.task_id = task_id or new_task_id()
        self.history = list(history or [])
        self.cancel_event = cancel_event or threading.Event()

        # ── 权限模式（三级；见 apps/settings 的 PERMISSION_MODES）──
        #   full    完全访问：文件读写类操作不再逐次审批（危险命令仍需确认）
        #   approve 需用户批准：读自动放行，写/命令逐次确认（默认）
        #   chat    仅保留对话：完全禁用一切工具，只回答与讨论
        mode = str(permission_mode or "approve").strip().lower()
        if mode not in ("full", "approve", "chat"):
            mode = "approve"
        self.permission_mode = mode
        #: 是否允许使用任何工具（chat 模式硬禁用）
        self.tools_enabled = mode != "chat"
        # 完全访问等价于「自动批准写入」，危险命令始终需要确认（安全底线不变）
        self.auto_approve_write = bool(auto_approve_write) or (mode == "full")
        self.system_prompt = system_prompt or ""
        #: 思考量级（light / balanced / max）—— 注入 system 提示词，仅作倾向引导
        self.thinking_level = thinking_level or ""
        #: 已启用技能的注入清单（[{name, description, instructions}]）
        self.skills = list(skills or [])
        #: 暂停/继续闸（Pause 功能的核心；置位=运行，清除=暂停）
        self.pause_event = threading.Event()
        self.pause_event.set()
        #: 本次任务已广播过的技能（去重，避免重复发 skill_used）
        self._emitted_skills = set()
        self.skills_used: List[str] = []
        #: 联网开关（前端「联网搜索」开关透传；缺省 True 以保持向后兼容）。
        # 关闭时工具表里不会出现 web_search，模型无从调用，整个任务期间零网络请求。
        self.web_enabled = bool(use_web)

        cfg = config or {}
        self.max_steps = self._positive_int(cfg.get("max_steps"), DEFAULT_MAX_STEPS, 1, 200)
        self.command_timeout = self._positive_int(
            cfg.get("command_timeout"), DEFAULT_COMMAND_TIMEOUT, 1, 600)
        self.output_limit = self._positive_int(
            cfg.get("output_limit"), DEFAULT_OUTPUT_LIMIT, 1000, 200000)
        self.read_max_lines = self._positive_int(
            cfg.get("read_max_lines"), DEFAULT_MAX_LINES, 100, 20000)
        self.search_max_results = self._positive_int(
            cfg.get("search_max_results"), DEFAULT_SEARCH_RESULTS, 1, 500)
        self.approval_timeout = self._positive_int(
            cfg.get("approval_wait_timeout"), DEFAULT_APPROVAL_TIMEOUT, 10, 3600)

        self.changeset = ChangeSet(task_id=self.task_id, workspace=self.ws_root)
        # 登记变更集，供任务结束后 /changes /undo 仍可按 task_id 查到
        with _CHANGESETS_LOCK:
            _CHANGESETS[self.task_id] = self.changeset
        self._outbox: List[dict] = []
        self.ctx = ToolContext(
            workspace=self.ws,
            task_id=self.task_id,
            changeset=self.changeset,
            cancel_event=self.cancel_event,
            approvals=approval_manager,
            auto_approve_write=self.auto_approve_write,
            command_timeout=self.command_timeout,
            output_limit=self.output_limit,
            read_max_lines=self.read_max_lines,
            search_max_results=self.search_max_results,
            session_id=session_id,
            emit=self._emit,
        )

        self.native_tools = True
        self.messages: List[dict] = []
        self.final_text = ""
        self._arg_retries = 0
        self._parse_retries = 0

        # 登记到全局注册表，便于 /cancel、/changes、/undo 按 task_id 定位
        register_runner(self)

    # ── 小工具 ────────────────────────────────────────────
    @staticmethod
    def _positive_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            num = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, num))

    def _emit(self, event: dict) -> None:
        """工具侧（如 run_command 的实时输出）投递事件，主循环统一外发"""
        self._outbox.append(event)

    def _drain(self) -> List[dict]:
        events = list(self._outbox)
        self._outbox = []
        return events

    def tool_schemas(self) -> Optional[List[dict]]:
        """
        本次调用实际发给模型的工具表。

        关闭工具（chat 模式 / 不支持原生 tools）时为 None；
        联网开关关闭时，工具表里**不包含** web_search；
        问题与工作区无关时（如问天气、闲聊），**不包含本地文件/目录工具**，
        避免为了一句无关提问就去扫工作区的目录和文件。
        """
        if not (self.native_tools and self.tools_enabled):
            return None
        return build_tool_schemas(self.web_enabled, self.local_tools_ok())

    # ── 意图判断：这个问题真的需要工作区上下文吗 ─────────────────
    #: 明确指向本地代码 / 工作区的信号
    _WS_SIGNALS = (
        "文件", "目录", "文件夹", "代码", "项目", "工程", "仓库", "工作区",
        "函数", "类", "变量", "模块", "接口", "报错", "异常", "日志", "堆栈",
        "配置", "依赖", "构建", "编译", "运行", "测试", "重构", "实现", "源码",
        "修改", "新增", "添加", "删除", "改一下", "帮我看", "读一下", "看一下",
        "git", "分支", "提交", "diff", "patch", "这里", "这个", "本地",
    )
    #: 明确与代码库无关的通用问题（命中且不含代码信号 → 不需要工作区）
    _NON_WS_SIGNALS = (
        "天气", "气温", "下雨", "几度", "湿度", "空气质量", "汇率", "股价",
        "新闻", "今天几号", "星期几", "现在几点", "什么时候", "翻译",
        "什么意思", "是谁", "多大", "多少岁", "你好", "你是谁", "谢谢",
        "讲个笑话", "写首诗", "祝福语", "脑筋急转弯",
    )
    #: 代码文件后缀（出现即视为需要读文件）
    _CODE_EXT_RE = re.compile(
        r"\.(py|js|ts|jsx|tsx|json|ya?ml|md|html|css|scss|go|java|rs|c|cpp|h|hpp|"
        r"sh|bat|ps1|toml|ini|env|sql|vue)\b", re.I)
    #: 路径分隔特征（如 apps/agent/tools.py）
    _PATH_RE = re.compile(r"[\w.-]+[/\\][\w.-]+")

    def local_tools_ok(self) -> bool:
        """
        判断本次提问是否需要工作区上下文，决定要不要把本地文件/目录工具
        交给模型。目的是让「今天武汉天气怎么样」这类问题不再去扫工作区的
        目录和文件。

        判定顺序：
            ① 未配置工作区 → 不需要（也没得扫）
            ② 命中明确的通用/闲聊信号且不含代码信号 → 不需要
            ③ 命中代码/工作区信号，或出现路径/代码后缀 → 需要
            ④ 兜底：已配工作区时保守放行（宁可多一次检索，也不漏掉真正
               需要读文件的请求）
        """
        if not self.ws or not self.ws_root:
            return False

        text = (self.user_task or "").lower()
        if not text:
            return False

        hit_ws = any(s in text for s in self._WS_SIGNALS)
        hit_non = any(s in text for s in self._NON_WS_SIGNALS)
        if hit_non and not hit_ws:
            return False
        if hit_ws or self._CODE_EXT_RE.search(text) or self._PATH_RE.search(text):
            return True
        return True

    def _emit_skill_marks(self, text: str) -> Generator[dict, None, None]:
        """
        扫描模型回复里「[技能:名称]」标注，去重后发 skill_used 事件供前端展示调用状态。
        标注约定见 apps.skills.services.SKILL_MARK_RE / apps/agent/prompts.build_skills_section。
        """
        if not text:
            return
        for name in SKILL_MARK_RE.findall(text):
            key = name.strip().lower()
            if not key or key in self._emitted_skills:
                continue
            self._emitted_skills.add(key)
            self.skills_used.append(name.strip())
            yield {"type": "skill_used", "task_id": self.task_id, "skill": name.strip()}

    def _emit_result(self, step: int, result: "ToolResult") -> Generator[dict, None, None]:
        """
        统一外发一次工具调用的结果事件（tool_result + 可选 diff + file_change）。

        注意：**所有出口（含审批拒绝、越界拒绝）都必须经过这里**，
        否则前端收不到 tool_result，会导致任务状态卡死 / 测试断言落空。
        """
        event = {"type": "tool_result", "step": step}
        event.update(result.to_event())
        yield event
        if result.diff:
            yield {"type": "diff", "step": step,
                   "path": (result.file_change or {}).get("path") or "",
                   "diff": result.diff,
                   "stats": self._stats_of(result)}
        if result.file_change:
            change_event = {"type": "file_change", "step": step,
                            "task_id": self.task_id}
            change_event.update(result.file_change)
            yield change_event
            self._persist_change(result.file_change.get("change_id"))

    def _tools_cap(self, provider) -> str:
        """该服务商对原生 tools 的能力：native / none / unknown"""
        try:
            from apps.llm.client import get_tools_cap
        except Exception:
            return getattr(provider, "supports_tools", "unknown") or "unknown"
        cap = get_tools_cap(provider.key) or ""
        if cap:
            return cap
        return getattr(provider, "supports_tools", "unknown") or "unknown"

    def _no_model_info(self, client) -> dict:
        """没有可用模型时的分级错误（need_settings=True）"""
        try:
            from apps.security.errors import classify
        except Exception:
            return {"code": "NO_KEY", "reason": "未配置 API Key，无法调用模型",
                    "action": "打开设置 → 选择服务商并填写 API Key", "need_settings": True}

        providers = [p for k, p in getattr(client, "providers", {}).items()
                     if not getattr(p, "is_offline", False)]
        if not providers:
            code = "NO_PROVIDER"
        elif all(p.requires_key and not p.api_key for p in providers):
            code = "NO_KEY"
        else:
            code = "NO_PROVIDER"
        info = classify(code)
        info["need_settings"] = True
        return info

    # ── messages 组装 ─────────────────────────────────────
    def _build_system(self) -> str:
        return build_system_prompt(
            self.ws, native_tools=self.native_tools, base_prompt=self.system_prompt,
            thinking_level=self.thinking_level, skills=self.skills,
            web_enabled=self.web_enabled)

    def _init_messages(self) -> List[dict]:
        messages: List[dict] = [{"role": "system", "content": self._build_system()}]
        recent = [m for m in self.history
                  if isinstance(m, dict)
                  and m.get("message_role") in ("user", "assistant")
                  and (m.get("message_content") or "").strip()][-MAX_HISTORY_MESSAGES:]
        for item in recent:
            messages.append({"role": item["message_role"],
                             "content": item["message_content"]})
        messages.append({
            "role": "user",
            "content": build_final_instruction(self.user_task, self.ws_root),
        })
        return messages

    def _rebuild_system(self) -> None:
        """切换通道后重写 system 提示词"""
        if not self.messages:
            return
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system()
        else:
            self.messages.insert(0, {"role": "system", "content": self._build_system()})

    # ── LLM 调用 ──────────────────────────────────────────
    def _llm_call(self) -> Generator[dict, None, Reply]:
        """调用一次大模型；流式产出 chunk 事件，最后 return Reply"""
        from apps.llm import get_client

        client = get_client()
        provider = client.first_available(allow_offline=False)
        if provider is None:
            return Reply(error=self._no_model_info(client))

        tools = self.tool_schemas()
        text_parts: List[str] = []
        buffer: Dict[int, dict] = {}
        error_info: Optional[dict] = None
        unsupported = False
        stream_to_ui = bool(self.native_tools and self.tools_enabled)

        try:
            events = client.stream_tools(
                self.messages, tools=tools, provider_key=provider.key)
            for event in events:
                etype = event.get("type")
                if etype in ("delta", "chunk"):
                    text = event.get("content") or ""
                    if not text:
                        continue
                    text_parts.append(text)
                    if stream_to_ui:
                        yield {"type": "chunk", "content": text}
                elif etype == "tool_delta":
                    accumulate_tool_delta(buffer, event.get("delta") or {})
                elif etype == "finish":
                    continue
                elif etype == "tools_unsupported":
                    unsupported = True
                elif etype == "error":
                    error_info = event
        except Exception:
            logger.exception("Agent 调用大模型失败")
            try:
                from apps.security.errors import classify
                error_info = classify("SERVER_ERROR")
            except Exception:
                error_info = {"code": "SERVER_ERROR", "reason": "模型调用异常",
                              "action": "稍后重试", "need_settings": False}

        raw_text = "".join(text_parts)

        if error_info:
            return Reply(raw_text=raw_text, error=error_info)

        # 运行时探测到「不支持原生 tools」→ 切 JSON 回退通道，本步重来
        if unsupported and self.native_tools:
            self.native_tools = False
            self._rebuild_system()
            yield {"type": "notice",
                   "content": "⚠️ 该模型不支持原生工具调用，已切换为 JSON 指令通道。"}
            return Reply(raw_text=raw_text, retry_step=True)

        # ── 通道 A：原生 tool_calls ──
        if self.native_tools:
            if not buffer:
                return Reply(text=raw_text, raw_text=raw_text)
            calls, failures = finalize_tool_calls(buffer)
            if failures:
                return self._argument_retry(failures, raw_text)
            return Reply(text=raw_text, tool_calls=calls, raw_text=raw_text)

        # ── 通道 B：JSON 回退 ──
        parsed = parse_fallback(raw_text)
        kind = parsed.get("kind")
        if kind == "final":
            thought = parsed.get("thought") or ""
            if thought:
                yield {"type": "chunk", "content": thought}
            return Reply(text=parsed.get("final") or "", raw_text=raw_text)
        if kind == "tool":
            thought = parsed.get("thought") or ""
            if thought:
                yield {"type": "chunk", "content": thought}
            call = ToolCall(id=new_call_id(), name=parsed.get("tool") or "",
                            args=parsed.get("args") or {},
                            raw_arguments=json.dumps(parsed.get("args") or {},
                                                     ensure_ascii=False))
            return Reply(text=thought, tool_calls=[call], raw_text=raw_text)

        # 解析失败 → 提示重试 1 次，仍失败则结束
        self._parse_retries += 1
        error = parsed.get("error") or {}
        if self._parse_retries <= MAX_FALLBACK_RETRIES:
            self.messages.append({"role": "assistant", "content": raw_text})
            self.messages.append({"role": "user", "content": PARSE_RETRY_HINT})
            yield {"type": "notice",
                   "content": "⚠️ 输出格式无法解析，已要求模型重试一次。"}
            return Reply(raw_text=raw_text, retry_step=True)
        return Reply(text="", raw_text=raw_text,
                     error={"code": error.get("code") or "PARSE_FAILED",
                            "reason": error.get("message") or "无法解析模型输出",
                            "action": "换个模型或重试", "need_settings": False})

    def _argument_retry(self, failures: List[dict], raw_text: str) -> Reply:
        """原生通道参数非法：回填提示，最多重试 2 次"""
        self._arg_retries += 1
        detail = failures[0].get("message") if failures else "参数不合法"
        self.messages.append({"role": "assistant", "content": raw_text})
        self.messages.append({
            "role": "user",
            "content": "%s（%s）" % (INVALID_ARGUMENT_HINT, detail),
        })
        if self._arg_retries <= MAX_NATIVE_RETRIES:
            return Reply(raw_text=raw_text, retry_step=True)
        return Reply(text="", raw_text=raw_text,
                     error={"code": "INVALID_ARGUMENT",
                            "reason": "模型连续输出了非法的工具参数",
                            "action": "换个支持工具调用的模型再试",
                            "need_settings": False})

    # ── 工具执行（含审批）─────────────────────────────────
    def _is_exempt(self, tool: str, risk: str, dangerous: bool) -> bool:
        """是否免审批"""
        if dangerous:
            return False
        if risk == RISK_READONLY:
            return True
        if self.auto_approve_write and risk == RISK_WRITE:
            return True
        return tool in self.ctx.session_allowed

    def _kind_of(self, tool: str) -> str:
        return {
            "write_file": "file_write",
            "edit_file": "file_edit",
            "run_command": "command",
        }.get(tool, tool or "unknown")

    def _execute(self, call: ToolCall, step: int) -> Generator[dict, None, ToolResult]:
        """执行一个工具调用（生成器：会产出审批/终端/结果等事件并返回 ToolResult）"""
        # 「仅保留对话」权限：硬禁用一切工具（即使模型强行产出调用也不执行）
        if not self.tools_enabled:
            event = {"type": "tool_call", "step": step}
            event.update(call.to_event())
            event["risk"] = RISK_DANGEROUS
            yield event
            result = ToolResult(
                call_id=call.id, tool=call.name, ok=False,
                error={"code": "TOOLS_DISABLED",
                       "message": "当前权限为「仅保留对话」，不会执行任何文件或命令操作"},
                risk="dangerous", summary=call.to_event().get("summary", ""),
            )
            yield from self._emit_result(step, result)
            return result

        # 联网开关关闭时的纵深防御：即便模型仍产出了 web_search（例如从历史
        # 上下文里学到过），也不执行、不发起任何网络请求，直接回填明确报错。
        if call.name == WEB_TOOL_NAME and not self.web_enabled:
            event = {"type": "tool_call", "step": step}
            event.update(call.to_event())
            event["risk"] = RISK_READONLY
            yield event
            disabled = ToolResult(
                call_id=call.id, tool=call.name, ok=False,
                error={"code": "WEB_SEARCH_DISABLED",
                       "message": "联网搜索已关闭，本次任务不会发起任何网络请求；"
                                  "请改用本地工具（list_dir / search_code / read_file）完成任务。"},
                risk=RISK_READONLY, summary=call.to_event().get("summary", ""),
            )
            yield from self._emit_result(step, disabled)
            return disabled

        risk_info = risk_of(self.ctx, call.name, call.args)
        risk = risk_info.get("risk") or "unknown"
        dangerous = (risk == RISK_DANGEROUS)

        event = {"type": "tool_call", "step": step}
        event.update(call.to_event())
        event["risk"] = risk
        yield event

        # 工作区之外的绝对路径：**直接拒绝，不进审批**（设计 §4.3.1 外加规则）
        if risk_info.get("pattern") == "PATH_OUT_OF_WORKSPACE":
            result = ToolResult(
                call_id=call.id, tool=call.name, ok=False,
                error={"code": "PATH_OUT_OF_WORKSPACE",
                       "message": risk_info.get("reason") or "路径超出工作区范围，已拒绝"},
                risk=risk, summary=call.to_event().get("summary", ""),
            )
            yield from self._emit_result(step, result)
            return result

        need_approval = risk in RISK_NEED_APPROVAL
        if need_approval and not self._is_exempt(call.name, risk, dangerous):
            preview_info = preview(self.ctx, call.name, call.args)
            detail = preview_info.get("detail") or risk_info.get("reason") or ""
            if call.name == "run_command":
                detail = "命令：%s" % (call.args.get("command") or "")

            request = self.ctx.approvals.request(
                task_id=self.task_id,
                kind=self._kind_of(call.name),
                title=preview_info.get("title") or "%s" % call.name,
                detail=detail,
                diff=preview_info.get("diff"),
                dangerous=dangerous,
            )
            approval_event = {"type": "approval_request", "step": step}
            approval_event.update(request.to_event())
            yield approval_event

            decided = self.ctx.approvals.wait(request, timeout=self.approval_timeout)
            if not decided:
                self.ctx.approvals.resolve(request.id, DECISION_DENY)
                yield {"type": "notice", "content": "审批超时，已按拒绝处理"}
            decision = request.decision or DECISION_DENY
            yield {"type": "approval_resolved",
                   "request_id": request.id, "decision": decision}

            if decision == DECISION_DENY:
                result = ToolResult(
                    call_id=call.id, tool=call.name, ok=False,
                    error={"code": "APPROVAL_DENIED", "message": DENY_MESSAGE},
                    risk=risk, summary=call.to_event().get("summary", ""),
                )
                yield from self._emit_result(step, result)
                return result
            if decision == DECISION_ALLOW_SESSION and not dangerous:
                self.ctx.session_allowed.add(call.name)

        result = dispatch(self.ctx, call.name, call.args, call_id=call.id)

        # 终端实时输出等由工具侧投递的事件
        for out_event in self._drain():
            out_event.setdefault("call_id", call.id)
            yield out_event

        yield from self._emit_result(step, result)
        return result

    def _stats_of(self, result: ToolResult) -> dict:
        data = result.data if isinstance(result.data, dict) else {}
        stats = data.get("stats")
        return stats if isinstance(stats, dict) else {"added": 0, "removed": 0}

    def _persist_change(self, change_id: Any) -> None:
        """变更记录落库（session_id 为空时只保留内存清单）"""
        if not self.session_id or change_id is None:
            return
        record = self.changeset.get(change_id)
        if not record:
            return
        try:
            persist_change(self.session_id, record)
        except Exception:
            logger.exception("落库变更记录失败 change_id=%s", change_id)

    # ── 强制总结 ──────────────────────────────────────────
    def _force_summary(self) -> str:
        """达到最大步数后不带 tools 再要一次总结"""
        from apps.llm import get_client

        messages = list(self.messages)
        messages.append({"role": "user", "content": FORCE_SUMMARY_INSTRUCTION})
        try:
            client = get_client()
            outcome = client.complete_tools(messages, tools=None)
        except Exception:
            logger.exception("Agent 强制总结失败")
            return "已达到最大步数上限，本次任务未能自动收尾，请查看上面的步骤与工具结果。"

        if outcome.get("ok"):
            return outcome.get("text") or "已达到最大步数上限。"
        error = outcome.get("error") or {}
        return "已达到最大步数上限，且无法生成总结：%s" % (
            error.get("reason") or "模型不可用")

    # ── 主循环 ────────────────────────────────────────────
    def run(self) -> Generator[dict, None, None]:
        """
        对外主入口：产出 SSE 事件流。

        这里做一层薄包装，保证无论正常结束、异常还是被消费者提前关闭，
        都能把任务从注册表里摘掉并清理挂起审批（避免线程悬挂与内存泄漏）。
        """
        try:
            for event in self._run():
                yield event
        finally:
            unregister_runner(self.task_id)
            try:
                approval_manager.cancel_task(self.task_id)
            except Exception:
                logger.exception("任务收尾清理挂起审批失败 task_id=%s", self.task_id)

    def _run(self) -> Generator[dict, None, None]:
        started = time.time()
        steps = 0
        tool_calls_count = 0
        consecutive_errors = 0
        denials = 0
        stopped_by = "completed"

        from apps.llm import get_client

        yield {"type": "run_start", "task_id": self.task_id, "mode": "agent",
               "workspace": self.ws_root, "max_steps": self.max_steps,
               "thinking_level": self.thinking_level or "balanced",
               "skills": [s.get("name") for s in self.skills if s.get("name")],
               "model": self._model_label()}

        client = get_client()
        provider = client.first_available(allow_offline=False)
        if provider is None:
            # 未配置模型：直接给分级错误 + 引导去设置，**不进循环**
            info = self._no_model_info(client)
            yield {"type": "error", "code": info.get("code"),
                   "content": info.get("reason"), "reason": info.get("reason"),
                   "action": info.get("action"), "need_settings": True}
            yield {"type": "run_end", "task_id": self.task_id,
                   "stats": {"steps": 0, "tool_calls": 0, "changes": 0,
                             "stopped_by": "no_model",
                             "elapsed_ms": int((time.time() - started) * 1000)}}
            yield {"type": "done", "session_id": self.session_id}
            return

        self.native_tools = self.tools_enabled and (self._tools_cap(provider) != "none")
        self.messages = self._init_messages()

        for step in range(1, self.max_steps + 1):
            if self.cancel_event.is_set():
                stopped_by = "user"
                break

            steps = step
            # 暂停闸：用户点了「暂停」后，循环在此阻塞直到「继续」置位
            if not self.pause_event.is_set():
                yield {"type": "paused", "task_id": self.task_id}
                self.pause_event.wait()
                yield {"type": "resumed", "task_id": self.task_id}

            yield {"type": "agent_step", "step": step, "status": "thinking",
                   "title": "思考中…"}

            reply = yield from self._llm_call()

            if reply.error:
                yield {"type": "error", "code": reply.error.get("code") or "SERVER_ERROR",
                       "content": reply.error.get("reason") or "模型调用失败",
                       "reason": reply.error.get("reason") or "模型调用失败",
                       "action": reply.error.get("action") or "",
                       "need_settings": bool(reply.error.get("need_settings"))}
                stopped_by = "error"
                break

            if reply.retry_step:
                continue

            if not reply.tool_calls:
                self.final_text = reply.text
                for ev in self._emit_skill_marks(reply.text):
                    yield ev
                stopped_by = "completed"
                break

            for call in reply.tool_calls:
                if self.cancel_event.is_set():
                    stopped_by = "user"
                    break

                tool_calls_count += 1
                result = yield from self._execute(call, step)

                if not result.ok:
                    code = (result.error or {}).get("code") or ""
                    if code == "APPROVAL_DENIED":
                        denials += 1
                        consecutive_errors = 0
                    else:
                        consecutive_errors += 1
                else:
                    consecutive_errors = 0

                # 回填 messages
                if self.native_tools:
                    self.messages.append(call.as_assistant_message())
                    self.messages.append(result.as_tool_message())
                else:
                    self.messages.append({"role": "assistant",
                                          "content": reply.raw_text or reply.text})
                    self.messages.append(result.as_user_message())

                if denials >= MAX_DENIALS:
                    stopped_by = "denied"
                    break
                if consecutive_errors >= MAX_TOOL_ERRORS:
                    stopped_by = "tool_errors"
                    break

            if stopped_by in ("user", "denied", "tool_errors"):
                break
        else:
            # for 正常走完 = 触达 max_steps
            stopped_by = "max_steps"
            summary = self._force_summary()
            if summary:
                yield {"type": "chunk", "content": summary}
            self.final_text = summary

        if stopped_by == "user":
            yield {"type": "notice", "content": "已停止本次任务。"}
        elif stopped_by == "tool_errors":
            yield {"type": "notice",
                   "content": "⚠️ 工具连续报错已达上限，已停止本次任务，请查看上面的错误信息。"}
        elif stopped_by == "denied":
            yield {"type": "notice",
                   "content": "⚠️ 你连续拒绝了多次操作，已停止本次任务。"}

        elapsed_ms = int((time.time() - started) * 1000)
        yield {"type": "run_end", "task_id": self.task_id,
               "stats": {"steps": steps, "tool_calls": tool_calls_count,
                         "changes": len(self.changeset.records),
                         "stopped_by": stopped_by, "elapsed_ms": elapsed_ms}}

        self._finalize()
        # 旧前端的收尾事件（向后兼容，AG-09②）
        yield {"type": "done", "session_id": self.session_id}

    def _model_label(self) -> str:
        try:
            from apps.llm import get_client
            provider = get_client().first_available(allow_offline=True)
            if provider is not None:
                return "%s · %s" % (provider.label, provider.model)
        except Exception:
            pass
        return "Aegis"

    def _finalize(self) -> None:
        """落库最终答复（无 session 时跳过）"""
        if not self.session_id:
            return
        text = (self.final_text or "").strip()
        if not text:
            return
        try:
            save_message(self.session_id, "assistant", text,
                         metadata={"task_id": self.task_id,
                                   "changes": len(self.changeset.records),
                                   "workspace": self.ws_root})
        except Exception:
            logger.exception("保存 Agent 最终答复失败")
