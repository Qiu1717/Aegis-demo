# -*- coding: utf-8 -*-
"""
tests/test_runner.py — Agent 编排循环（25 步上限 / 审批挂起 / 事件序）

验收点（T03/T04）：
    · 步数上限生效（默认 25，可配；到顶强制总结）
    · 单条工具结果 >6000 字符截断
    · 危险命令必弹审批，且 auto_approve_write / 本任务豁免都不放行
    · 审批超时按 deny 处理，不悬挂
    · `run_end` 后补发 `done`
    · 未配置模型 → error + need_settings=true，不进循环
"""
import json
import time

import pytest

from apps.agent import runner as runner_mod
from apps.agent.approval import (DECISION_ALLOW_SESSION, DECISION_DENY,
                                  approval_manager)
from apps.agent.runner import (AgentRunner, DEFAULT_MAX_STEPS, MAX_DENIALS,
                               MAX_TOOL_ERRORS)
from apps.agent.tools import MAX_RESULT_CHARS, ToolResult

from conftest import FakeClient, FakeProvider, native_tool_events


def _collect(gen):
    return [e for e in gen]


# ── 常量与设计一致 ────────────────────────────────────────
def test_design_constants():
    assert DEFAULT_MAX_STEPS == 25
    assert MAX_TOOL_ERRORS == 3
    assert MAX_DENIALS == 3
    assert MAX_RESULT_CHARS == 6000


# ── 未配置模型：不进循环 ───────────────────────────────────
def test_no_model_yields_error_and_done(ws, monkeypatch):
    class EmptyClient(object):
        providers = {}
        def first_available(self, allow_offline=True):
            return None

    import apps.llm as llm_pkg
    monkeypatch.setattr(llm_pkg, "get_client", lambda: EmptyClient())
    monkeypatch.setattr(llm_pkg, "reset_client", lambda: None)

    r = AgentRunner(workspace=ws, user_task="hi", config={})
    events = _collect(r.run())
    types = [e["type"] for e in events]
    assert types[0] == "run_start"
    assert "error" in types
    err = next(e for e in events if e["type"] == "error")
    assert err["need_settings"] is True
    assert err["action"]  # 建议动作非空
    # run_end 之后必须补发 done
    assert types.index("run_end") < types.index("done")
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "no_model"


# ── 25 步上限 ────────────────────────────────────────────
def test_max_steps_cap_enforced(ws, fake_client):
    # 3 步脚本：每步都返回一个只读工具调用，第 4 步之后走默认 final
    script = [native_tool_events("read_file", {"path": "app.py"})
              for _ in range(10)]
    client = fake_client(script=script)

    r = AgentRunner(workspace=ws, user_task="do it",
                    config={"max_steps": 3})
    events = _collect(r.run())

    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "max_steps"
    assert end["stats"]["steps"] == 3
    assert end["stats"]["tool_calls"] == 3
    # 强制总结被产出
    assert any("（强制总结）" in (e.get("content") or "") for e in events)
    assert client.complete_calls == 1
    # run_end 之后补发 done
    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert types[-2] == "run_end"


def test_default_max_steps_is_25(ws):
    r = AgentRunner(workspace=ws, user_task="x", config={})
    assert r.max_steps == 25
    # 上限可被配置覆盖，但夹在 1..200
    assert AgentRunner(workspace=ws, user_task="x",
                       config={"max_steps": 999}).max_steps == 200
    assert AgentRunner(workspace=ws, user_task="x",
                       config={"max_steps": 0}).max_steps == 1


def test_completed_without_tool_calls(ws, fake_client):
    fake_client(script=[[{"type": "delta", "content": "全部完成。"}]])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    events = _collect(r.run())
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "completed"
    assert end["stats"]["steps"] == 1
    types = [e["type"] for e in events]
    assert types[-1] == "done" and types[-2] == "run_end"


def test_read_edit_run_minimal_flow(ws, ws_root, fake_client):
    """
    T03 核心场景：read → edit → run_command 最小任务可自动跑完
    （read 只读放行；edit 需审批 → 无人审批按超时 deny；
      run_command(readonly) 放行）
    """
    (ws_root / "target.py").write_text("V = 1\n", encoding="utf-8")
    script = [
        native_tool_events("read_file", {"path": "target.py"}),
        native_tool_events("edit_file",
                          {"path": "target.py", "old_text": "V = 1",
                           "new_text": "V = 2"}),
        native_tool_events("run_command",
                           {"command": "echo ok"}),
        [{"type": "delta", "content": "完成。"}],
    ]
    fake_client(script=script)
    r = AgentRunner(workspace=ws, user_task="x", config={})
    r.approval_timeout = 1  # 测试加速（生产默认 300）
    events = _collect(r.run())
    kinds = [e["type"] for e in events]
    assert "tool_result" in kinds
    assert "approval_request" in kinds  # edit_file 弹了审批
    assert "approval_resolved" in kinds
    assert "terminal" in kinds           # run_command 实时输出
    # edit 被拒 → 文件未被修改
    assert (ws_root / "target.py").read_text(encoding="utf-8") == "V = 1\n"
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "completed"


# ── 审批：危险命令不可豁免 ────────────────────────────────
def test_dangerous_command_needs_approval_even_with_all_exemptions(ws, fake_client):
    fake_client(script=[native_tool_events(
        "run_command", {"command": "rm -rf .", "timeout": 5})])
    r = AgentRunner(workspace=ws, user_task="x",
                    auto_approve_write=True, config={})
    # 模拟「本任务内允许 run_command」已被用户豁免过
    r.ctx.session_allowed.add("run_command")
    r.approval_timeout = 1

    events = _collect(r.run())
    reqs = [e for e in events if e["type"] == "approval_request"]
    assert len(reqs) == 1, "危险命令没有弹审批（被 auto_approve/豁免放行了）"
    assert reqs[0]["dangerous"] is True
    assert DECISION_ALLOW_SESSION not in reqs[0]["options"]
    res = [e for e in events if e["type"] == "tool_result"]
    assert res and res[0]["ok"] is False
    assert res[0]["error"]["code"] == "APPROVAL_DENIED"


def test_dangerous_allow_session_still_asks_next_time(ws, fake_client):
    """allow_session 只豁免非危险命令；危险命令每次都要确认"""
    fake_client(script=[
        native_tool_events("run_command", {"command": "git reset --hard"}),
    ])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    r.ctx.session_allowed.add("run_command")  # 用户之前允许过
    r.approval_timeout = 1
    events = _collect(r.run())
    assert any(e["type"] == "approval_request" and e["dangerous"] for e in events)


def test_write_tool_auto_approve_when_enabled(ws, fake_client, ws_root):
    fake_client(script=[native_tool_events(
        "write_file", {"path": "new.py", "content": "print(1)\n"})])
    r = AgentRunner(workspace=ws, user_task="x",
                    auto_approve_write=True, config={})
    events = _collect(r.run())
    assert not any(e["type"] == "approval_request" for e in events)
    assert (ws_root / "new.py").read_text(encoding="utf-8") == "print(1)\n"
    assert any(e["type"] == "file_change" for e in events)


def test_allow_session_exempts_same_tool(ws, fake_client, ws_root):
    """allow_session(write_file) 后，同类写操作不再弹审批（设计允许）"""
    fake_client(script=[
        native_tool_events("write_file", {"path": "a.py", "content": "1\n"}),
        native_tool_events("write_file", {"path": "b.py", "content": "2\n"}),
        [{"type": "delta", "content": "done"}],
    ])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    r.ctx.session_allowed.add("write_file")
    events = _collect(r.run())
    assert not any(e["type"] == "approval_request" for e in events)
    assert (ws_root / "a.py").exists() and (ws_root / "b.py").exists()


# ── 工作区外的绝对路径：直接拒绝，不进审批 ────────────────────
def test_outside_path_rejected_without_approval(ws, outside_root, fake_client):
    fake_client(script=[native_tool_events(
        "run_command", {"command": "python %s" % (outside_root / "evil.py")})])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    events = _collect(r.run())
    assert not any(e["type"] == "approval_request" for e in events), (
        "越界路径不应弹审批，应直接拒绝")
    res = [e for e in events if e["type"] == "tool_result"]
    assert res[0]["ok"] is False
    assert res[0]["error"]["code"] == "PATH_OUT_OF_WORKSPACE"


# ── 审批唤醒（T04：另一线程 resolve）──────────────────────
def test_approval_wake_up_from_another_thread(ws, ws_root, fake_client):
    import threading
    fake_client(script=[native_tool_events(
        "write_file", {"path": "wake.py", "content": "ok\n"})])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    started = {}

    def gen():
        for ev in r.run():
            if ev["type"] == "approval_request":
                rid = ev["request_id"]
                t = threading.Timer(0.2, lambda: approval_manager.resolve(
                    rid, DECISION_ALLOW_SESSION))
                t.daemon = True
                t.start()
            yield ev

    events = _collect(gen())
    assert (ws_root / "wake.py").read_text(encoding="utf-8") == "ok\n"
    resolved = [e for e in events if e["type"] == "approval_resolved"]
    assert resolved and resolved[0]["decision"] == "allow_session"


# ── 连续 3 次拒绝 / 连续 3 次工具报错 → 提前终止 ─────────────
def test_three_denials_stop_task(ws, fake_client):
    script = [native_tool_events("write_file",
                                 {"path": "n%d.py" % i, "content": "x"})
              for i in range(6)] + [[{"type": "delta", "content": "end"}]]
    fake_client(script=script)
    r = AgentRunner(workspace=ws, user_task="x", config={})

    def gen():
        for ev in r.run():
            if ev["type"] == "approval_request":
                rid = ev["request_id"]
                t = __import__("threading").Timer(
                    0.05, lambda: approval_manager.resolve(rid, DECISION_DENY))
                t.daemon = True
                t.start()
            yield ev

    events = _collect(gen())
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "denied"
    assert end["stats"]["tool_calls"] == 3


def test_three_tool_errors_stop_task(ws, fake_client):
    script = [native_tool_events("read_file", {"path": "missing%d.py" % i})
              for i in range(6)] + [[{"type": "delta", "content": "end"}]]
    fake_client(script=script)
    r = AgentRunner(workspace=ws, user_task="x", config={})
    events = _collect(r.run())
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "tool_errors"


# ── 用户取消 ──────────────────────────────────────────────
def test_user_cancel_stops_task(ws, fake_client):
    fake_client(script=[native_tool_events("read_file", {"path": "app.py"})
                        for _ in range(20)])
    r = AgentRunner(workspace=ws, user_task="x", config={})

    def gen():
        for ev in r.run():
            r.cancel_event.set()
            yield ev

    events = _collect(gen())
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "user"


# ── 工具结果截断 6000 ────────────────────────────────────
def test_tool_result_payload_truncated_to_6000():
    big = {"stdout": "x" * 100000}
    res = ToolResult(call_id="c1", tool="run_command", ok=True, data=big)
    text = res.payload_text()
    assert len(text) <= MAX_RESULT_CHARS + 60
    assert "已截断" in text


def test_tool_result_small_payload_intact():
    res = ToolResult(call_id="c1", tool="read_file", ok=True,
                     data={"content": "1\thello"})
    assert "hello" in res.payload_text()
    assert len(res.payload_text()) <= MAX_RESULT_CHARS


# ── run() 收尾清理 ────────────────────────────────────────
def test_run_cleans_registry_and_pending_approvals(ws, fake_client):
    fake_client(script=[[{"type": "delta", "content": "ok"}]])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    tid = r.task_id
    _collect(r.run())
    assert runner_mod.get_runner(tid) is None
    assert approval_manager.pending_for(tid) == []


def test_cancel_task_api(ws, fake_client):
    fake_client(script=[[{"type": "delta", "content": "ok"}]])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    assert runner_mod.get_runner(r.task_id) is r
    assert runner_mod.cancel_task(r.task_id) is True
    assert runner_mod.cancel_task("t-nope") is False


def test_get_changes_and_undo(ws, fake_client, ws_root):
    fake_client(script=[native_tool_events(
        "write_file", {"path": "chg.py", "content": "V=2\n"})])
    r = AgentRunner(workspace=ws, user_task="x", auto_approve_write=True,
                    config={})
    _collect(r.run())
    changes = runner_mod.get_changes(r.task_id)
    assert len(changes) == 1 and changes[0]["path"] == "chg.py"
    out = runner_mod.undo_task(r.task_id)
    assert out["ok"] is True and out["removed"] == 1
    assert not (ws_root / "chg.py").exists()
    assert runner_mod.undo_task("t-nope")["ok"] is False


# ── JSON 回退通道 ─────────────────────────────────────────
def test_fallback_json_channel(ws, fake_client, monkeypatch, ws_root):
    """模型不支持原生 tools → 切 JSON 通道并执行工具"""
    import apps.llm.client as llm_client_mod
    monkeypatch.setattr(llm_client_mod, "get_tools_cap",
                        lambda key, default="unknown": "none")
    payload = {"tool": "write_file", "args": {"path": "fb.py", "content": "x\n"},
               "thought": "thinking..."}
    block = "```json\n%s\n```" % json.dumps(payload, ensure_ascii=False)
    fake_client(script=[[{"type": "delta", "content": block}]])

    r = AgentRunner(workspace=ws, user_task="x", auto_approve_write=True,
                    config={})
    events = _collect(r.run())
    assert (ws_root / "fb.py").read_text(encoding="utf-8") == "x\n"
    assert any("thinking" in (e.get("content") or "") for e in events
               if e["type"] == "chunk")


def test_fallback_final_text(ws, fake_client, monkeypatch):
    import apps.llm.client as llm_client_mod
    monkeypatch.setattr(llm_client_mod, "get_tools_cap",
                        lambda key, default="unknown": "none")
    payload = {"final": "任务结束，共修改 2 个文件。"}
    block = "```json\n%s\n```" % json.dumps(payload, ensure_ascii=False)
    fake_client(script=[[{"type": "delta", "content": block}]])
    r = AgentRunner(workspace=ws, user_task="x", config={})
    events = _collect(r.run())
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "completed"
    assert r.final_text.startswith("任务结束")


# ── parser：原生工具参数流式累积 ───────────────────────────
def test_native_tools_accumulate_and_finalize():
    from apps.agent.parser import accumulate_tool_delta, finalize_tool_calls
    buf = {}
    args = {"path": "app.py", "start_line": 1}
    s = json.dumps(args)
    accumulate_tool_delta(buf, {"index": 0, "id": "call_1",
                                "function": {"name": "read_file",
                                             "arguments": s[:5]}})
    accumulate_tool_delta(buf, {"index": 0,
                                "function": {"arguments": s[5:]}})
    calls, failures = finalize_tool_calls(buf)
    assert failures == [] and len(calls) == 1
    assert calls[0].name == "read_file" and calls[0].args == args
    assert calls[0].id == "call_1"


def test_native_tools_bad_json_fails():
    from apps.agent.parser import accumulate_tool_delta, finalize_tool_calls
    buf = {}
    accumulate_tool_delta(buf, {"index": 0,
                                "function": {"name": "read_file",
                                             "arguments": "{not json"}})
    calls, failures = finalize_tool_calls(buf)
    assert calls == [] and failures[0]["code"] == "INVALID_ARGUMENT"


def test_native_tools_unknown_tool_fails():
    from apps.agent.parser import accumulate_tool_delta, finalize_tool_calls
    buf = {}
    accumulate_tool_delta(buf, {"index": 0,
                                "function": {"name": "format_c_disk",
                                             "arguments": "{}"}})
    calls, failures = finalize_tool_calls(buf)
    assert calls == [] and failures[0]["code"] == "UNKNOWN_TOOL"
