# -*- coding: utf-8 -*-
"""
apps/agent/tools.py — 工具的实现与统一分发

    read_file     读取文件（带行号，超 2000 行截断）
    list_dir      列出目录树（忽略 .git/node_modules/__pycache__/.venv/.aegis_backup）
    search_code   内容检索（关键词 / 正则，返回 文件:行号:内容）
    web_search    联网搜索（复用 apps/websearch/services.py，受 use_web 开关控制）
    write_file    新建 / 整文件覆盖（先备份，需审批）
    edit_file     精确字符串替换（多处匹配必须报错，需审批）
    run_command   在工作区根目录执行命令（需分级审批）

对外三个入口（供 runner 编排）：
    risk_of(ctx, tool, args)    -> 风险分级（决定是否弹审批）
    preview(ctx, tool, args)    -> 预演（不落盘），产出审批卡片的 diff
    dispatch(ctx, tool, args)   -> 真正执行，返回 ToolResult

所有失败都收敛成 `{"ok": False, "error": {code, message, detail}}`，
**绝不把异常抛给上层**；详细异常只写日志。
"""
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from apps import logger

from .approval import RISK_DANGEROUS, RISK_READONLY, classify_tool
from .fileio import (edit_file, preview_edit, preview_write, read_text_file,
                     write_file)
from .sandbox import SandboxError, Workspace
from .schemas import (
    ToolError,
    normalize_tool_name,
    summarize_args,
    tool_label,
    validate_args,
)
from .terminal import run_command
from .trace import ChangeSet

#: 回填给模型时的单条结果字符上限（超过截断，防止撑爆上下文）
MAX_RESULT_CHARS = 6000


@dataclass
class ToolContext:
    """一次任务执行期间共享的上下文"""
    workspace: Workspace
    task_id: str
    changeset: ChangeSet
    cancel_event: threading.Event = field(default_factory=threading.Event)
    approvals: Any = None
    auto_approve_write: bool = False
    command_timeout: int = 120
    output_limit: int = 20000
    read_max_lines: int = 2000
    search_max_results: int = 50
    session_id: Optional[int] = None
    #: 已「本任务内始终允许」豁免的键集合（形如 "write_file" / "run_command"）
    session_allowed: set = field(default_factory=set)
    #: 事件外发回调（run_command 用它推送实时 terminal 输出）
    emit: Optional[Callable[[dict], None]] = None

    def emit_event(self, event: dict) -> None:
        if self.emit:
            try:
                self.emit(event)
            except Exception:
                logger.exception("Agent 事件外发失败")


@dataclass
class ToolResult:
    """一次工具调用的结果（可转成 SSE 事件，也可回填 messages）"""
    call_id: str
    tool: str
    ok: bool
    data: Any = None
    error: Optional[dict] = None
    elapsed_ms: int = 0
    diff: Optional[str] = None
    file_change: Optional[dict] = None
    risk: str = RISK_READONLY
    summary: str = ""

    def to_event(self) -> dict:
        """转成 SSE `tool_result` 事件载荷"""
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "risk": self.risk,
            "summary": self.summary,
        }

    def payload_text(self, limit: int = MAX_RESULT_CHARS) -> str:
        """回填给模型的文本（JSON，超长截断）"""
        payload = {
            "ok": self.ok,
            "tool": self.tool,
            "data": self.data,
            "error": self.error,
        }
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            text = json.dumps({"ok": False, "tool": self.tool,
                               "error": {"code": "SERIALIZE_FAILED"}},
                              ensure_ascii=False)
        if len(text) > limit:
            text = text[:limit] + "...（已截断 %d 字符）" % (len(text) - limit)
        return text

    def as_tool_message(self, limit: int = MAX_RESULT_CHARS) -> dict:
        """原生 tools 通道的回填消息"""
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "name": self.tool,
            "content": self.payload_text(limit),
        }

    def as_user_message(self, limit: int = MAX_RESULT_CHARS) -> dict:
        """JSON 回退通道的回填消息（无 tool_call_id，明确标注来源）"""
        return {
            "role": "user",
            "content": "[工具结果] %s: %s" % (self.tool, self.payload_text(limit)),
        }


# ── 风险分级与预演（供 runner 决定是否弹审批）───────────────
def risk_of(ctx: ToolContext, tool: str, args: Dict[str, Any]) -> dict:
    """风险分级（委托 approval.classify_tool）"""
    return classify_tool(tool, args or {}, ctx.workspace)


def preview(ctx: ToolContext, tool: str, args: Dict[str, Any]) -> dict:
    """
    预演一次写操作（**不落盘**），产出审批卡片需要的 diff。

    :return: {"diff":..., "stats":..., "title":..., "detail":...}
    """
    args = args or {}
    if tool == "edit_file":
        result = preview_edit(ctx.workspace, str(args.get("path") or ""),
                              args.get("old_text") or "",
                              args.get("new_text") or "",
                              args.get("occurrence"))
    elif tool == "write_file":
        result = preview_write(ctx.workspace, str(args.get("path") or ""),
                               args.get("content") or "")
    else:
        return {"diff": None, "stats": None, "title": "", "detail": ""}

    title = "%s %s" % (tool_label(tool), str(args.get("path") or ""))
    if not result.get("ok"):
        error = result.get("error") or {}
        return {"diff": None, "stats": None, "title": title,
                "detail": error.get("message") or "无法预览该改动"}
    stats = result.get("stats") or {"added": 0, "removed": 0}
    detail = "新增 %d 行，删除 %d 行" % (stats.get("added", 0), stats.get("removed", 0))
    return {"diff": result.get("diff"), "stats": stats, "title": title, "detail": detail}


# ── 六个工具的具体实现 ────────────────────────────────────
def _tool_read_file(ctx: ToolContext, args: Dict[str, Any]) -> dict:
    return read_text_file(
        ctx.workspace,
        str(args.get("path") or ""),
        start_line=args.get("start_line"),
        end_line=args.get("end_line"),
        max_lines=ctx.read_max_lines,
    )


def _tool_list_dir(ctx: ToolContext, args: Dict[str, Any]) -> dict:
    rel = str(args.get("path") or ".")
    try:
        max_depth = int(args.get("max_depth") or 2)
    except (TypeError, ValueError):
        max_depth = 2
    try:
        max_entries = int(args.get("max_entries") or 300)
    except (TypeError, ValueError):
        max_entries = 300
    entries, tree, truncated = ctx.workspace.walk(
        rel, max_depth=max(1, min(5, max_depth)),
        max_entries=max(10, min(2000, max_entries)))
    return {"path": rel, "entries": entries, "tree": tree, "truncated": truncated}


def _tool_search_code(ctx: ToolContext, args: Dict[str, Any]) -> dict:
    pattern = args.get("pattern")
    if not pattern or not isinstance(pattern, str):
        raise ToolError("INVALID_ARGUMENT", "pattern 不能为空")

    use_regex = bool(args.get("regex", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    file_glob = str(args.get("file_glob") or "*")
    base_rel = str(args.get("path") or ".")
    try:
        max_results = int(args.get("max_results") or ctx.search_max_results)
    except (TypeError, ValueError):
        max_results = ctx.search_max_results
    max_results = max(1, min(500, max_results))

    flags = 0 if case_sensitive else re.IGNORECASE
    rx = None
    if use_regex:
        try:
            rx = re.compile(pattern, flags)
        except re.error:
            raise ToolError("INVALID_ARGUMENT", "正则表达式不合法：%s" % pattern[:40])

    needle = pattern if case_sensitive else pattern.lower()
    matches = []
    truncated = False

    for file_path in ctx.workspace.iter_files(base_rel, file_glob):
        if ctx.cancel_event.is_set():
            break
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError, ValueError):
            continue
        rel = ctx.workspace.rel_path(file_path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx is not None:
                hit = rx.search(line) is not None
            else:
                hit = (needle in line) if case_sensitive else (needle in line.lower())
            if not hit:
                continue
            matches.append({"path": rel, "line": lineno,
                            "text": line[:300] if len(line) > 300 else line})
            if len(matches) >= max_results:
                truncated = True
                break
        if truncated:
            break

    return {
        "pattern": pattern,
        "matches": matches,
        "total": len(matches),
        "truncated": truncated,
    }


def _tool_web_search(ctx: ToolContext, args: Dict[str, Any]) -> dict:
    """
    联网搜索：复用 apps/websearch 的现有后端能力（不新增依赖、不新增服务商代码）。

    web_search 内部已做最多 20 条的收口与多引擎兜底：
    全部引擎网络异常时抛 RuntimeError，这里转成 ToolError 交给 dispatch 统一处理
    （**绝不吞异常**，否则模型会拿到「成功但无意义」的结果）。
    """
    from apps.websearch.services import web_search as _web_search

    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("INVALID_ARGUMENT", "query 不能为空")

    try:
        max_results = int(args.get("max_results") or 8)
    except (TypeError, ValueError):
        max_results = 8
    max_results = max(1, min(20, max_results))

    try:
        return _web_search(query, max_results=max_results, provider="auto")
    except ToolError:
        raise
    except ValueError as e:
        # 参数不合法（如空 query）
        raise ToolError("INVALID_ARGUMENT", str(e) or "检索词不合法")
    except RuntimeError as e:
        # 所有引擎均网络异常
        raise ToolError("NETWORK_ERROR", str(e) or "网络请求失败，请检查网络连接后重试")
    except Exception as e:
        # 其余异常（解析失败等）统一收敛并写日志
        logger.exception("联网搜索执行异常 query=%s", query[:60])
        raise ToolError("SEARCH_FAILED", "联网搜索失败：%s" % (str(e)[:80] or "未知原因"))


def _tool_write_file(ctx: ToolContext, args: Dict[str, Any]) -> dict:
    create_dirs = args.get("create_dirs")
    return write_file(
        ctx.workspace, ctx.changeset,
        str(args.get("path") or ""),
        args.get("content") or "",
        create_dirs=True if create_dirs is None else bool(create_dirs),
    )


def _tool_edit_file(ctx: ToolContext, args: Dict[str, Any]) -> dict:
    return edit_file(
        ctx.workspace, ctx.changeset,
        str(args.get("path") or ""),
        args.get("old_text") or "",
        args.get("new_text") or "",
        args.get("occurrence"),
    )


def _tool_run_command(ctx: ToolContext, args: Dict[str, Any]) -> dict:
    try:
        timeout = int(args.get("timeout") or ctx.command_timeout)
    except (TypeError, ValueError):
        timeout = ctx.command_timeout

    def on_output(stream: str, text: str) -> None:
        ctx.emit_event({"type": "terminal", "stream": stream, "text": text})

    return run_command(
        ctx.workspace,
        str(args.get("command") or ""),
        timeout=timeout,
        cancel_event=ctx.cancel_event,
        on_output=on_output,
        output_limit=ctx.output_limit,
    )


_TOOL_IMPL = {
    "read_file": _tool_read_file,
    "list_dir": _tool_list_dir,
    "search_code": _tool_search_code,
    "web_search": _tool_web_search,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "run_command": _tool_run_command,
}


# ── 统一分发 ──────────────────────────────────────────────
def dispatch(ctx: ToolContext, tool: str, args: Dict[str, Any],
             call_id: str = "") -> ToolResult:
    """
    执行一次工具调用（**不含审批**，审批由 runner 在调用前完成）。

    :return: ToolResult；任何异常都被吞掉并转成 error 结果
    """
    started = time.time()
    normalized = normalize_tool_name(tool) or ""
    args = args if isinstance(args, dict) else {}
    summary = summarize_args(normalized, args)
    risk_info = risk_of(ctx, normalized, args)
    risk = risk_info.get("risk") or "unknown"

    def done(data: Any) -> ToolResult:
        diff = None
        file_change = None
        if isinstance(data, dict):
            # 只要有变更记录就产出 file_change（新建文件可能没有 diff，但仍有 change_id）
            if data.get("change_id") is not None:
                file_change = {
                    "change_id": data.get("change_id"),
                    "path": data.get("path"),
                    "action": data.get("action"),
                    "backup_path": data.get("backup_path"),
                    "bytes": data.get("bytes", 0),
                }
            if data.get("diff"):
                diff = data.get("diff")
        return ToolResult(
            call_id=call_id, tool=normalized, ok=True, data=data,
            elapsed_ms=int((time.time() - started) * 1000),
            diff=diff, file_change=file_change, risk=risk, summary=summary,
        )

    def failed(error: dict) -> ToolResult:
        return ToolResult(
            call_id=call_id, tool=normalized, ok=False, data=None, error=error,
            elapsed_ms=int((time.time() - started) * 1000),
            risk=risk, summary=summary,
        )

    if normalized not in _TOOL_IMPL:
        return failed({"code": "UNKNOWN_TOOL",
                       "message": "未知工具：%s" % (tool or "(空)")})

    problem = validate_args(normalized, args)
    if problem:
        return failed({"code": "INVALID_ARGUMENT", "message": problem})

    impl = _TOOL_IMPL[normalized]
    try:
        return done(impl(ctx, args))
    except ToolError as e:
        logger.info("工具 %s 执行失败：%s / %s", normalized, e.code, e.message)
        return failed(e.to_dict())
    except SandboxError as e:
        logger.info("工具 %s 命中沙箱拦截：%s", normalized, e.code)
        return failed(e.to_dict())
    except Exception:
        logger.exception("工具 %s 执行异常", normalized)
        return failed({"code": "INTERNAL_ERROR",
                       "message": "工具执行异常，请查看服务日志"})


def is_dangerous(risk: str) -> bool:
    """该风险等级是否为「危险」（不可豁免）"""
    return risk == RISK_DANGEROUS
