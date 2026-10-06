# -*- coding: utf-8 -*-
"""
apps/agent/parser.py — 双通道工具调用解析

通道 A（原生 function calling）：
    累积 `delta.tool_calls[]` 的 id / name / arguments（arguments 是**增量拼接**），
    `finish_reason=stop` 后 `json.loads`；
    解析失败 → 视为 `INVALID_ARGUMENT` 工具结果回填，让模型重试（最多 2 次）。

通道 B（JSON 回退文本）：
    ① 优先取**最后一个** ```json 代码块；
    ② 否则用**括号平衡扫描**提取首个完整 `{...}`；
    ③ `json.loads` 后校验 tool 在白名单、args 满足必填项；
    找不到 / 解析失败 → 回填 `PARSE_FAILED` 并提示重试 1 次，仍失败则结束并汇报。
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schemas import normalize_tool_name, summarize_args, tool_names, validate_args

#: 解析失败的错误码
ERR_PARSE_FAILED = "PARSE_FAILED"
ERR_INVALID_ARGUMENT = "INVALID_ARGUMENT"
ERR_UNKNOWN_TOOL = "UNKNOWN_TOOL"

#: 原生通道允许的参数纠错重试次数
MAX_NATIVE_RETRIES = 2
#: JSON 回退通道允许的重试次数
MAX_FALLBACK_RETRIES = 1

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class ToolCall:
    """一次工具调用（两个通道统一的结构）"""
    id: str
    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""

    def to_event(self) -> dict:
        """转成 SSE `tool_call` 事件载荷"""
        return {
            "call_id": self.id,
            "tool": self.name,
            "args": self.args,
            "summary": summarize_args(self.name, self.args),
        }

    def as_assistant_message(self) -> dict:
        """原生通道：assistant 消息携带 tool_calls"""
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": self.id,
                "type": "function",
                "function": {
                    "name": self.name,
                    "arguments": self.raw_arguments or json.dumps(self.args, ensure_ascii=False),
                },
            }],
        }


# ── 通道 A：原生 tool_calls 累积 ──────────────────────────
def accumulate_tool_delta(buffer: Dict[int, dict], delta: dict) -> None:
    """
    把一个 `delta.tool_calls[]` 元素累积进 buffer。

    :param buffer: {index: {"id":..., "name":..., "arguments":...}}
    :param delta: 形如 {"index":0,"id":"call_1","type":"function",
                        "function":{"name":"read_file","arguments":"{\\"pa"}}
    """
    if not isinstance(delta, dict):
        return
    index = delta.get("index")
    try:
        index = int(index) if index is not None else 0
    except (TypeError, ValueError):
        index = 0

    slot = buffer.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if delta.get("id"):
        slot["id"] = str(delta["id"])
    func = delta.get("function") or {}
    if func.get("name"):
        slot["name"] = str(func["name"])
    if func.get("arguments"):
        slot["arguments"] = (slot["arguments"] or "") + str(func["arguments"])


def finalize_tool_calls(buffer: Dict[int, dict]) -> Tuple[List[ToolCall], List[dict]]:
    """
    把累积结果解析成 ToolCall 列表。

    :return: (成功的 ToolCall 列表, 失败项列表 [{index, code, message}])
    """
    calls: List[ToolCall] = []
    failures: List[dict] = []

    for index in sorted(buffer.keys()):
        slot = buffer[index]
        name = normalize_tool_name(slot.get("name")) or ""
        raw = slot.get("arguments") or ""

        if not name:
            failures.append({"index": index, "code": ERR_INVALID_ARGUMENT,
                             "message": "工具名为空"})
            continue
        if name not in tool_names():
            failures.append({"index": index, "code": ERR_UNKNOWN_TOOL,
                             "message": "未知工具：%s" % name})
            continue

        args: Any = {}
        if raw.strip():
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                failures.append({"index": index, "code": ERR_INVALID_ARGUMENT,
                                 "message": "工具参数不是合法 JSON"})
                continue
        if not isinstance(args, dict):
            failures.append({"index": index, "code": ERR_INVALID_ARGUMENT,
                             "message": "工具参数必须是对象"})
            continue

        problem = validate_args(name, args)
        if problem:
            failures.append({"index": index, "code": ERR_INVALID_ARGUMENT,
                             "message": problem})
            continue

        calls.append(ToolCall(
            id=slot.get("id") or ("call_%s" % index),
            name=name,
            args=args,
            raw_arguments=raw,
        ))
    return calls, failures


# ── 通道 B：JSON 回退文本 ─────────────────────────────────
def extract_json_objects(text: str) -> List[str]:
    """
    从模型输出里提取 JSON 候选串。

    顺序：① 所有 ```json 代码块（按出现顺序）；② 括号平衡扫描得到的首个完整 {...}
    """
    candidates: List[str] = []
    for match in _JSON_BLOCK_RE.finditer(text or ""):
        block = (match.group(1) or "").strip()
        if block:
            candidates.append(block)
        else:
            continue
    balanced = balanced_json_object(text or "")
    if balanced:
        candidates.append(balanced)
    return candidates


def balanced_json_object(text: str) -> Optional[str]:
    """
    括号平衡扫描：跳过字符串与转义，返回**最后一个**完整 `{...}` 片段。

    取最后一个是因为模型常在解释文字之后才给出最终 JSON。
    """
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    found: Optional[str] = None
    begin = -1

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin != -1:
                found = text[begin:i + 1]
                begin = -1
    return found


def parse_fallback(text: str) -> dict:
    """
    解析 JSON 回退通道的模型输出。

    :return: {"kind": "tool"|"final"|"none", "thought":..., "tool":...,
              "args":..., "final":..., "error": {...}}
    """
    raw = (text or "").strip()
    if not raw:
        return {"kind": "none", "error": {"code": ERR_PARSE_FAILED,
                                          "message": "模型返回内容为空"}}

    for candidate in extract_json_objects(raw):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue

        thought = str(obj.get("thought") or "").strip()
        if "final" in obj and obj.get("final") is not None:
            return {"kind": "final", "thought": thought,
                    "final": str(obj.get("final") or "")}

        tool = normalize_tool_name(obj.get("tool"))
        if not tool:
            continue
        if tool not in tool_names():
            return {"kind": "none",
                    "error": {"code": ERR_UNKNOWN_TOOL,
                              "message": "未知工具：%s" % tool,
                              "thought": thought}}
        args = obj.get("args") or {}
        if not isinstance(args, dict):
            return {"kind": "none",
                    "error": {"code": ERR_INVALID_ARGUMENT,
                              "message": "args 必须是对象",
                              "thought": thought}}
        problem = validate_args(tool, args)
        if problem:
            return {"kind": "none",
                    "error": {"code": ERR_INVALID_ARGUMENT,
                              "message": problem, "thought": thought}}
        return {"kind": "tool", "thought": thought, "tool": tool, "args": args}

    # ── 没有任何可解析的 JSON 对象 ─────────────────────────
    # 情形一：模型输出了疑似 JSON 指令但损坏（截断/转义错误）→ 明确报错，
    #         由上层回填提示要求重试，绝不静默吞掉。
    stripped = raw.lstrip()
    if "```" in raw or stripped.startswith("{") or stripped.startswith("["):
        return {"kind": "none",
                "error": {"code": ERR_PARSE_FAILED,
                          "message": "输出中包含疑似 JSON 指令但无法解析"
                                     "（可能被截断、转义错误或缺少必要字段）",
                          "thought": ""}}

    # 情形二：模型输出的是纯文本（没有尝试 JSON 语法）→ 视为最终答复。
    # 纯聊天/直接回答不该被当成解析失败打断重试——直接把文字转交给用户。
    return {"kind": "final", "thought": "", "final": raw}


def new_call_id(prefix: str = "call") -> str:
    """JSON 回退通道没有 tool_call_id，这里生成一个本地 id"""
    import uuid
    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])
