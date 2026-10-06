# -*- coding: utf-8 -*-
"""
tests/conftest.py — Aegis 桌面端编码 Agent（T01~T04）测试公共夹具

要点：
  · 必须在**导入 apps 之前**把 AEGIS_DATA_DIR 指到临时目录，
    否则测试会把真实用户数据目录（%APPDATA%\\Aegis）弄脏；
  · 每个用例自带一个临时工作区（workspace）与一个工作区外的目录（outside），
    用于验证沙箱越界拦截；
  · 提供 FakeClient / FakeProvider，便于在不联网、无真实 Key 的情况下
    驱动 runner 的 25 步编排循环。
"""
import json
import os
import sys
import threading
import uuid

import pytest

# ── 保证项目根目录在 sys.path 上（pytest 只会把 tests/ 加进来）──
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── 在任何 apps 导入之前把数据目录重定向到临时目录 ────────────
_TMP_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmpdata")
os.makedirs(_TMP_DATA, exist_ok=True)
os.environ["AEGIS_DATA_DIR"] = _TMP_DATA

from apps.agent import fileio, sandbox, tools  # noqa: E402
from apps.agent.trace import ChangeSet  # noqa: E402

DEFAULT_TASK_ID = "t-test0001"


# ── 目录夹具 ──────────────────────────────────────────────
@pytest.fixture
def outside_root(tmp_path):
    """工作区**之外**的目录（含一个敏感文件）"""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.txt").write_text("TOP-SECRET-OUTSIDE\n", encoding="utf-8")
    (d / "deep").mkdir()
    (d / "deep" / "secret2.txt").write_text("SECRET2\n", encoding="utf-8")
    return d


@pytest.fixture
def ws_root(tmp_path, outside_root):
    """一个合法工作区（与 outside_root 同级但互不包含）"""
    d = tmp_path / "workspace"
    d.mkdir()
    (d / "app.py").write_text("print('hello')\nprint('hello')\n", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    return d


@pytest.fixture
def ws(ws_root):
    """打开好的 Workspace 对象"""
    return sandbox.open_workspace(str(ws_root))


@pytest.fixture
def changeset(ws):
    return ChangeSet(task_id=DEFAULT_TASK_ID, workspace=str(ws.root))


@pytest.fixture
def ctx(ws, changeset):
    """一个默认（非自动放行）的 ToolContext"""
    return tools.ToolContext(
        workspace=ws,
        task_id=DEFAULT_TASK_ID,
        changeset=changeset,
        cancel_event=threading.Event(),
        approvals=None,
        auto_approve_write=False,
    )


# ── 假 LLM 客户端（驱动 runner，不需要真实 Key / 网络）────────
class FakeProvider(object):
    """最小可用的 Provider 替身"""

    def __init__(self, key="fake", model="fake-model", supports_tools="native"):
        self.key = key
        self.label = "FakeProvider"
        self.model = model
        self.supports_tools = supports_tools
        self.requires_key = True
        self.api_key = "sk-fake-key-0000000000"
        self.is_offline = False

    def availability(self):
        return True, ""


class FakeClient(object):
    """
    可编排的假客户端。

    :param script: 每次 stream_tools 时产出的事件列表的**列表**；
                   用尽后默认产出一条纯文本 final 回复（结束循环）。
    """

    def __init__(self, script=None, provider=None):
        self.script = list(script or [])
        self.provider = provider or FakeProvider()
        self.providers = {self.provider.key: self.provider}
        self.calls = 0
        self.complete_calls = 0
        #: 每次调用看到的 messages 快照（用于断言回填内容）
        self.message_log = []

    # ── 与 apps.llm 同名的接口 ───────────────────────────
    def first_available(self, allow_offline=True):
        return self.provider

    def stream_tools(self, messages, tools=None, provider_key=None):
        self.calls += 1
        self.message_log.append([dict(m) for m in messages])
        if self.script:
            events = self.script.pop(0)
        else:
            events = [{"type": "delta", "content": "任务已完成。"}]
        for e in events:
            yield e

    def complete_tools(self, messages, tools=None):
        self.complete_calls += 1
        return {"ok": True, "text": "（强制总结）已达到最大步数。"}


def native_tool_events(name, args):
    """构造一次「原生 function calling」的流式事件"""
    call_id = "call_%s" % uuid.uuid4().hex[:8]
    payload = json.dumps(args, ensure_ascii=False)
    return [
        {"type": "tool_delta",
         "delta": {"index": 0, "id": call_id, "type": "function",
                   "function": {"name": name, "arguments": payload[:1]}}},
        {"type": "tool_delta",
         "delta": {"index": 0,
                   "function": {"arguments": payload[1:]}}},
        {"type": "finish"},
    ]


@pytest.fixture
def fake_client(monkeypatch):
    """
    把 apps.llm.get_client 换成可控的假客户端。

    用法：fake_client(script=[native_tool_events(...), ...])
    """
    import apps.llm as llm_pkg

    def _make(script=None, provider=None):
        client = FakeClient(script=script, provider=provider)
        monkeypatch.setattr(llm_pkg, "get_client", lambda: client, raising=True)
        return client

    return _make


# ── 自动清理 redact 登记表，避免用例间互相污染 ────────────────
@pytest.fixture(autouse=True)
def _clean_state():
    from apps.agent import approval as approval_mod
    from apps.security import redact

    redact.clear_secrets()
    approval_mod.approval_manager.clear()
    from apps.agent import runner as runner_mod
    for tid in list(runner_mod._RUNNERS.keys()):
        runner_mod.unregister_runner(tid)
    yield
    redact.clear_secrets()
    approval_mod.approval_manager.clear()
    for tid in list(runner_mod._RUNNERS.keys()):
        runner_mod.unregister_runner(tid)
