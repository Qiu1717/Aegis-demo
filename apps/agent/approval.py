# -*- coding: utf-8 -*-
"""
apps/agent/approval.py — 风险分级与人在环路审批（安全模型第③道闸）

分级顺序（设计 §4.3.1）：

    黑名单命中？──是──► DANGEROUS  必须二次确认，**不可被「本任务内允许」豁免**
         │否
    只读白名单命中？──是──► READONLY  自动放行
         │否
    写入类（write_file / edit_file）？──是──► WRITE  需确认（可被豁免）
         │否
       UNKNOWN                        需确认（可被豁免）

外加规则：**命令中出现工作区之外的绝对路径 → 直接拒绝，不进审批**。

线程模型：
    ApprovalManager.request() 建一个 threading.Event 并登记；
    runner 先 yield `approval_request` 事件，再 `req.event.wait(timeout)`；
    /api/agent/approve（T04）在**另一个 Flask 工作线程**里调 resolve() 唤醒它。
    超时按 deny 处理，避免线程永久悬挂（Flask 必须 threaded=True，否则死锁）。
"""
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .sandbox import Workspace

# ── 风险等级 ──────────────────────────────────────────────
RISK_READONLY = "readonly"
RISK_WRITE = "write"
RISK_DANGEROUS = "dangerous"
RISK_UNKNOWN = "unknown"

#: 需要用户确认的等级
RISK_NEED_APPROVAL = (RISK_WRITE, RISK_UNKNOWN, RISK_DANGEROUS)
#: 可被「本任务内始终允许」豁免的等级（危险命令**不在其中**）
RISK_EXEMPTABLE = (RISK_WRITE, RISK_UNKNOWN)

# ── 决策 ──────────────────────────────────────────────────
DECISION_ALLOW_ONCE = "allow_once"
DECISION_ALLOW_SESSION = "allow_session"
DECISION_DENY = "deny"

#: 审批等待超时（秒，可被 config.agent.approval_wait_timeout 覆盖）
DEFAULT_WAIT_TIMEOUT = 300

#: 拒绝时回填给模型的文案
DENY_MESSAGE = "用户拒绝了该操作，请换一种方式或先向用户说明。"


# ── 危险命令黑名单（正则，大小写不敏感，照抄设计 §4.3.1）──
DANGEROUS_PATTERNS: List[str] = [
    r"rm\s+(-rf|-r|-f|--recursive|--force)",
    r"rmdir\s+/s",
    r"del\s+(/f|/s|/q)",
    r"format\s",
    r"diskpart",
    r"mkfs",
    r"dd\s+if=",
    r"shutdown",
    r"\brestart\b",
    r"/r\s+/t",
    r"taskkill",
    r"wmic\s+process\s+call\s+create",
    r"net\s+(user|localgroup|stop|start|share)",
    r"sc\s+(create|delete|stop|start|config)",
    r"reg\s+(add|delete)",
    r"regedit",
    r"takeown",
    r"icacls",
    r"attrib\s+[+-]",
    r"setx\s",
    r"\$env:\s*\w+\s*=",
    r"schtasks",
    r"certutil",
    r"bitsadmin",
    r"curl\s+[^\n]*\|\s*(sh|bash|zsh|powershell)",
    r"iwr\s+[^\n]*\|\s*iex",
    r"Invoke-Expression",
    r"iex\s",
    r"powershell\s+-e(nc)?\s",
    r"git\s+push\s+(-f|--force)",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-fd",
    r"git\s+checkout\s+(\.|--)",
    r"git\s+stash\s+drop",
    r"pip\s+uninstall",
    r"npm\s+(i|install)\s+-g",
    r"npm\s+uninstall\s+-g",
    r"conda\s+remove",
    r"chmod\s+777",
    r"chown\s+-R",
]

# ── 只读命令白名单（整条命令前缀匹配；含重定向/管道一律不放行）──
READONLY_PATTERNS: List[str] = [
    r"^git\s+(status|diff|log|show|branch|ls-files|rev-parse|describe|remote\s+-v)\b",
    r"^(ls|dir|cat|type|head|tail|find|where|which|echo|tree)\b",
    r"^(rg|grep|findstr)\b",
    r"^pytest\b",
    r"^python\s+-m\s+pytest\b",
    r"^python\s+(--version|-V)\b",
    r"^pip\s+(list|show|freeze)\b",
    r"^node\s+(-v|--version)\b",
    r"^npm\s+(-v|--version)\b",
    r"^npm\s+run\s+test\b",
    r"^go\s+version\b",
    r"^cargo\s+--version\b",
]

_DANGEROUS_RE: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]
_READONLY_RE: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in READONLY_PATTERNS]

#: 管道 / 重定向逃逸字符（出现即不认为「只读」）
_SHELL_ESCAPE_CHARS = (">", ">>", "|", "&", "&&", "`", "$(")

#: Windows 绝对路径 C:\foo
_WIN_ABS_RE = re.compile(r"(?:^|[\s\"'=])([A-Za-z]:[\\/][^\s\"'|>&]*)")
#: POSIX 绝对路径（至少两级，避免把 `dir /s` 的 /s 误判成路径）
_POSIX_ABS_RE = re.compile(r"(?:^|[\s\"'=])(/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)")
#: 相对路径逃逸：`../a`、`..\a`、`..\..\a`（只含 `..` 段后才算逃逸，
#   避免把 `a..b`、`git ..master` 这类合法用法误伤）
_REL_ESCAPE_RE = re.compile(r"(?:\.\.[\\/][^\s\"'|>&]*|\.\.(?=\s|$))")


def has_shell_escape(command: str) -> bool:
    """命令里是否有重定向 / 管道 / 命令替换（有则不享受只读豁免）"""
    return any(ch in command for ch in _SHELL_ESCAPE_CHARS)


def find_outside_paths(command: str, workspace: Optional[Workspace]) -> List[str]:
    """
    找出命令中所有位于工作区之外的路径（绝对路径 **或** 相对 ``..`` 逃逸）。

    ``type ..\\outside\\secret.txt`` 这类相对路径逃逸此前只被当作 readonly 自动放行，
    会读到工作区之外的文件内容 —— 这是沙箱逃逸，必须 100% 拦截。
    """
    if not workspace:
        return []
    found: List[str] = []
    # 绝对路径：用捕获组（去掉前置的空白 / 引号分隔符），保持原行为
    for regex in (_WIN_ABS_RE, _POSIX_ABS_RE):
        for match in regex.finditer(command or ""):
            raw = match.group(1)
            try:
                workspace.resolve(raw)
            except Exception:
                if raw not in found:
                    found.append(raw)
    # 相对路径逃逸：整段匹配（本身就是越界片段）
    for match in _REL_ESCAPE_RE.finditer(command or ""):
        raw = match.group(0)
        try:
            workspace.resolve(raw)
        except Exception:
            if raw not in found:
                found.append(raw)
    return found


#: 向后兼容别名（旧调用方仍可 import）
find_outside_abs_paths = find_outside_paths


def classify_command(command: str, workspace: Optional[Workspace] = None) -> dict:
    """
    给一条命令定风险等级。

    :return: {"risk":..., "reason":..., "outside_paths":[...], "pattern":...}
             risk 可能是 dangerous / readonly / unknown
    """
    cmd = (command or "").strip()
    if not cmd:
        return {"risk": RISK_UNKNOWN, "reason": "空命令", "outside_paths": [], "pattern": ""}

    # ① 工作区之外的绝对路径 / 相对 ``..`` 逃逸 → 直接拒绝（不进审批）
    outside = find_outside_paths(cmd, workspace)
    if outside:
        return {
            "risk": RISK_DANGEROUS,
            "reason": "命令中包含工作区之外的绝对路径：%s" % "、".join(outside[:3]),
            "outside_paths": outside,
            "pattern": "PATH_OUT_OF_WORKSPACE",
        }

    # ② 危险黑名单
    for regex in _DANGEROUS_RE:
        match = regex.search(cmd)
        if match:
            return {
                "risk": RISK_DANGEROUS,
                "reason": "命中危险命令黑名单（%s）" % match.group(0)[:40],
                "outside_paths": [],
                "pattern": regex.pattern,
            }

    # ③ 只读白名单（必须是整条命令前缀，且不含管道/重定向逃逸）
    if not has_shell_escape(cmd):
        for regex in _READONLY_RE:
            if regex.match(cmd):
                return {
                    "risk": RISK_READONLY,
                    "reason": "只读命令白名单命中，自动放行",
                    "outside_paths": [],
                    "pattern": regex.pattern,
                }

    # ④ 其余一律需确认
    return {"risk": RISK_UNKNOWN, "reason": "未知风险命令，需要确认",
            "outside_paths": [], "pattern": ""}


def classify_tool(tool: str, args: dict, workspace: Optional[Workspace] = None) -> dict:
    """
    给一次工具调用定风险等级。

    web_search 是只读的信息检索（只发 GET 请求取回标题/链接/摘要，不落盘、
    不改文件、不执行命令），显式归为 READONLY 自动放行 —— 不依赖
    「未知名默认 readonly」的隐式行为，避免将来加工具时语义漂移。

    :return: 同 classify_command
    """
    if tool == "run_command":
        return classify_command(str((args or {}).get("command") or ""), workspace)
    if tool == "web_search":
        return {"risk": RISK_READONLY, "reason": "联网检索为只读操作，自动放行",
                "outside_paths": [], "pattern": ""}
    if tool in ("write_file", "edit_file"):
        path = str((args or {}).get("path") or "")
        outside = find_outside_paths(path, workspace)
        if outside:
            return {"risk": RISK_DANGEROUS, "reason": "路径超出工作区范围",
                    "outside_paths": outside, "pattern": "PATH_OUT_OF_WORKSPACE"}
        return {"risk": RISK_WRITE, "reason": "文件写入类操作，需要确认",
                "outside_paths": [], "pattern": ""}
    return {"risk": RISK_READONLY, "reason": "只读操作，自动放行",
            "outside_paths": [], "pattern": ""}


# ── 审批请求 ──────────────────────────────────────────────
@dataclass
class ApprovalRequest:
    """一次挂起中的审批请求"""
    id: str
    task_id: str
    kind: str                      # file_write / file_edit / command / ...
    title: str
    detail: str = ""
    diff: Optional[str] = None
    dangerous: bool = False
    options: List[str] = field(default_factory=list)
    event: threading.Event = field(default_factory=threading.Event)
    decision: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None

    @property
    def is_dangerous(self) -> bool:
        return bool(self.dangerous)

    def allowed(self) -> bool:
        return self.decision in (DECISION_ALLOW_ONCE, DECISION_ALLOW_SESSION)

    def to_event(self) -> dict:
        """转成 SSE `approval_request` 事件载荷"""
        return {
            "request_id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "diff": self.diff,
            "dangerous": self.dangerous,
            # 危险命令不提供 allow_session（不可豁免）
            "options": list(self.options),
        }


class ApprovalManager:
    """
    审批请求的登记与唤醒中心（进程内单例即可，桌面端单用户）。
    """

    def __init__(self):
        self._pending: Dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()

    def request(self, task_id: str, kind: str, title: str, detail: str = "",
                diff: Optional[str] = None, dangerous: bool = False) -> ApprovalRequest:
        """登记一个审批请求（不阻塞；阻塞由 runner 的 event.wait 完成）"""
        options = [DECISION_ALLOW_ONCE, DECISION_DENY]
        if not dangerous:
            options.insert(1, DECISION_ALLOW_SESSION)
        req = ApprovalRequest(
            id="ar-%s" % uuid.uuid4().hex[:10],
            task_id=task_id,
            kind=kind,
            title=title,
            detail=detail or "",
            diff=diff,
            dangerous=bool(dangerous),
            options=options,
        )
        with self._lock:
            self._pending[req.id] = req
        return req

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            return self._pending.get(request_id)

    def resolve(self, request_id: str, decision: str) -> bool:
        """
        由 /api/agent/approve 在另一个线程里调用，唤醒挂起的 Runner。

        :param decision: allow_once / allow_session / deny
        :return: 是否命中了一个挂起中的请求
        """
        if decision not in (DECISION_ALLOW_ONCE, DECISION_ALLOW_SESSION, DECISION_DENY):
            return False
        with self._lock:
            req = self._pending.pop(request_id, None)
        if not req:
            return False
        # 危险命令强制降级为「仅此一次」，不允许豁免
        if req.dangerous and decision == DECISION_ALLOW_SESSION:
            decision = DECISION_ALLOW_ONCE
        req.decision = decision
        req.resolved_at = time.time()
        req.event.set()
        return True

    def wait(self, req: ApprovalRequest, timeout: int = DEFAULT_WAIT_TIMEOUT) -> bool:
        """等待用户决策；返回 False 表示超时"""
        return bool(req.event.wait(timeout=timeout))

    def cancel_task(self, task_id: str) -> int:
        """取消某任务的全部挂起审批（按 deny 处理），返回处理条数"""
        with self._lock:
            ids = [k for k, v in self._pending.items() if v.task_id == task_id]
        count = 0
        for request_id in ids:
            if self.resolve(request_id, DECISION_DENY):
                count += 1
        return count

    def pending_for(self, task_id: str) -> List[dict]:
        """某任务当前挂起的审批（供前端重连后恢复卡片）"""
        with self._lock:
            return [r.to_event() for r in self._pending.values()
                    if r.task_id == task_id]

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()


#: 进程内单例（T04 的 /api/agent/approve 路由直接 import 它）
approval_manager = ApprovalManager()
