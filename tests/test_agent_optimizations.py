# -*- coding: utf-8 -*-
"""
tests/test_agent_optimizations.py — Agent 优化的针对性回归

覆盖 4 项新行为（优化前均缺失）：
    1. payload_text 截断后仍是合法 JSON（旧实现硬切字符串产生非法 JSON）
    2. _trim_messages 任务内上下文预算裁剪（组删除 / 任务指令保护 / 最近消息保护）
    3. cancel_task 唤醒「暂停闸」—— 修复暂停中取消导致线程永久挂死
    4. _summary_safe_messages 强制总结的消息简化（tool 结构转纯文本）
"""
import json
import threading

from apps.agent import runner as runner_mod
from apps.agent.runner import (AgentRunner, DEFAULT_CONTEXT_TOKEN_BUDGET,
                               HISTORY_KEEP_CHARS, PROTECT_RECENT)
from apps.agent.tools import MAX_RESULT_CHARS, ToolResult
from apps.agent.prompts import build_system_prompt, load_workspace_instructions

from conftest import FakeProvider, native_tool_events


# ── 1. payload_text：截断后必须是合法 JSON ─────────────────
def test_payload_long_string_stays_valid_json():
    r = ToolResult(call_id="c1", tool="read_file", ok=True,
                   data={"path": "a.py", "content": "x" * 20000, "lines": 2000})
    text = r.payload_text()
    parsed = json.loads(text)          # 旧实现在这里抛 JSONDecodeError
    assert len(text) <= MAX_RESULT_CHARS + 64
    assert "已截断" in parsed["data"]["content"]


def test_payload_long_list_stays_valid_json():
    data = [{"path": "f%d.py" % i, "line": i, "text": "t" * 200}
            for i in range(80)]
    r = ToolResult(call_id="c2", tool="search_code", ok=True, data=data)
    parsed = json.loads(r.payload_text())
    assert parsed["data"][-1] == "…（已截断）"


def test_payload_small_result_untouched():
    r = ToolResult(call_id="c3", tool="list_dir", ok=True,
                   data={"entries": ["a", "b"]})
    assert json.loads(r.payload_text())["data"] == {"entries": ["a", "b"]}


def test_payload_unserializable_data_fallback():
    class Obj(object):
        pass
    r = ToolResult(call_id="c4", tool="t", ok=True, data={"x": Obj()})
    assert isinstance(json.loads(r.payload_text()), dict)


def test_payload_huge_error_stays_valid():
    r = ToolResult(call_id="c5", tool="t", ok=False,
                   error={"code": "X", "message": "e" * 9000})
    parsed = json.loads(r.payload_text())
    assert parsed["error"]["code"] == "X"
    assert len(r.payload_text()) <= MAX_RESULT_CHARS + 64


# ── 2. _trim_messages：任务内上下文预算 ────────────────────
def _mk_runner_with_messages(ws, messages, budget):
    r = AgentRunner(workspace=ws, user_task="do it", config={})
    r.context_token_budget = budget
    r.messages = messages
    # 记录任务指令（与 _init_messages 的行为一致）
    r._task_instruction = "工作区：%s\n\n任务：do it" % (ws.root if ws else "")
    return r


def _tool_group(call_id, size=6000):
    """一组原生通道消息：assistant(tool_calls) + tool 结果"""
    return [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": call_id, "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": "{\"path\":\"a.py\"}"}}]},
        {"role": "tool", "tool_call_id": call_id, "name": "read_file",
         "content": json.dumps({"ok": True, "tool": "read_file",
                                "data": {"content": "x" * size}})},
    ]


def test_trim_keeps_task_instruction_and_recent(ws):
    budget = 6000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(6):
        msgs.extend(_tool_group("call_%d" % i))
    msgs.append({"role": "user", "content": "工作区：%s\n\n任务：do it" % ws.root})
    r = _mk_runner_with_messages(ws, msgs, budget)
    r._trim_messages()
    contents = [m.get("content") for m in r.messages]
    # 任务指令必须还在
    assert r._task_instruction in contents
    # 预算收敛
    total = sum(r._msg_tokens(m) for m in r.messages)
    assert total <= budget
    # 保留下来的消息内容必须原样（保护区语义 = 不删不截，而非凑满 8 条）
    tools = [m for m in r.messages if m.get("role") == "tool"]
    assert tools and all(len(m["content"]) > 3000 for m in tools)


def test_trim_drops_whole_tool_groups(ws):
    """删除必须是成组的：assistant(tool_calls) 与其 tool 结果同生共死"""
    budget = 4000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(10):
        msgs.extend(_tool_group("call_%d" % i, size=3000))
    msgs.append({"role": "user", "content": "工作区：%s\n\n任务：do it" % ws.root})
    r = _mk_runner_with_messages(ws, msgs, budget)
    r._trim_messages()
    # 剩余消息里不允许出现「assistant 带 tool_calls 却没有对应 tool 消息」
    pending = set()
    for m in r.messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                pending.add(c["id"])
        elif m.get("role") == "tool":
            pending.discard(m.get("tool_call_id"))
    assert not pending, "发现未配对的 tool_call：%s" % pending


def test_trim_truncates_old_history_text(ws):
    budget = 3000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(6):
        msgs.append({"role": "user", "content": "历史提问%d %s" % (i, "y" * 5000)})
        msgs.append({"role": "assistant", "content": "回答%d %s" % (i, "z" * 5000)})
    msgs.append({"role": "user", "content": "工作区：%s\n\n任务：do it" % ws.root})
    r = _mk_runner_with_messages(ws, msgs, budget)
    r._trim_messages()
    truncated = [m for m in r.messages
                 if isinstance(m.get("content"), str)
                 and m["content"].endswith("…（早期内容已省略）")]
    assert truncated, "旧历史长文本应被截断"
    for m in truncated:
        assert len(m["content"]) <= HISTORY_KEEP_CHARS + 20


def test_trim_noop_within_budget(ws):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"}]
    r = _mk_runner_with_messages(ws, [dict(m) for m in msgs],
                                 DEFAULT_CONTEXT_TOKEN_BUDGET)
    before = [dict(m) for m in r.messages]
    r._trim_messages()
    assert r.messages == before


def test_trim_protects_recent_messages(ws):
    """保护区内的消息（最后 8 条）即使超预算也不删不截"""
    budget = 1000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(20):
        msgs.extend(_tool_group("old_%d" % i))
    for i in range(PROTECT_RECENT):
        msgs.append({"role": "user", "content": "recent_%d %s" % (i, "r" * 200)})
    msgs.append({"role": "user", "content": "工作区：%s\n\n任务：do it" % ws.root})
    r = _mk_runner_with_messages(ws, msgs, budget)
    r._trim_messages()
    tail_contents = [m.get("content") for m in r.messages[-(PROTECT_RECENT + 1):-1]]
    for i, c in enumerate(tail_contents):
        assert c.startswith("recent_%d" % i), "保护区消息被破坏"


def test_trim_compacts_oversized_protected_tool_result(ws):
    """即使最近工具结果很大，也要在预算内保持 tool 配对。"""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(4):
        msgs.extend(_tool_group("old_%d" % i, size=800))
    msgs.extend(_tool_group("recent", size=30000))
    msgs.append({"role": "user", "content": "工作区：%s\n\n任务：do it" % ws.root})
    r = _mk_runner_with_messages(ws, msgs, budget=1200)

    r._trim_messages()

    assert sum(r._msg_tokens(m) for m in r.messages) <= 1200
    assert r._task_instruction in [m.get("content") for m in r.messages]
    assistant = next(m for m in r.messages
                     if m.get("role") == "assistant" and m.get("tool_calls"))
    tool = next(m for m in r.messages if m.get("role") == "tool")
    assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"]
    assert "上下文已压缩" in tool["content"]
    assert json.loads(tool["content"])["truncated"] is True


# ── 3. cancel_task 唤醒暂停闸 ──────────────────────────────
def test_cancel_wakes_paused_runner(ws, monkeypatch):
    """暂停中取消：pause_event 必须被置位，否则 SSE 线程永久挂死（旧 bug）"""
    import apps.llm as llm_pkg

    class EmptyClient(object):
        providers = {}

        def first_available(self, allow_offline=True):
            return None

    monkeypatch.setattr(llm_pkg, "get_client", lambda: EmptyClient())
    monkeypatch.setattr(llm_pkg, "reset_client", lambda: None)

    r = AgentRunner(workspace=ws, user_task="hi", config={})
    r.pause_event.clear()                      # 模拟用户点了暂停
    assert runner_mod.cancel_task(r.task_id) is True
    assert r.cancel_event.is_set()
    assert r.pause_event.is_set(), "取消必须唤醒暂停闸（旧实现挂死此处）"


def test_cancel_unknown_task_returns_false():
    assert runner_mod.cancel_task("no-such-task") is False


# ── 4. _summary_safe_messages：tool 结构转纯文本 ────────────
def test_summary_safe_messages_flattens_tool_structure(ws):
    r = AgentRunner(workspace=ws, user_task="do it", config={})
    r.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file",
                                      "arguments": "{\"path\":\"a.py\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file",
         "content": json.dumps({"ok": True, "data": {"content": "xxx"}})},
        {"role": "assistant", "content": "完成了"},
    ]
    safe = r._summary_safe_messages(r.messages)
    roles = [m["role"] for m in safe]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    # 不允许残留任何 tool 结构
    assert all(not m.get("tool_calls") for m in safe)
    assert all(m["role"] != "tool" for m in safe)
    # 工具调用痕迹转成了文本
    assert "[调用工具: read_file]" in safe[2]["content"]
    assert "[工具结果]" in safe[3]["content"]


def test_summary_safe_messages_caps_recent(ws):
    r = AgentRunner(workspace=ws, user_task="do it", config={})
    msgs = [{"role": "system", "content": "sys"}]
    msgs += [{"role": "user", "content": "m%d" % i} for i in range(60)]
    safe = r._summary_safe_messages(msgs, keep_recent=10)
    assert len(safe) == 11                      # system + 10
    assert safe[0]["role"] == "system"
    assert safe[-1]["content"] == "m59"


def test_system_prompt_requires_plan_verify_and_relevant_tools(ws):
    prompt = build_system_prompt(ws=ws, native_tools=True)
    assert "理解/定位" in prompt
    assert "验证" in prompt
    assert "不需要：天气、新闻、汇率" in prompt
    assert "先判断" in prompt


def test_workspace_instructions_are_loaded_with_limits(ws, ws_root):
    (ws_root / "AGENTS.md").write_text("仓库约定：先运行测试。", encoding="utf-8")
    (ws_root / "CLAUDE.md").write_text("补充约定：保留现有 API。", encoding="utf-8")
    (ws_root / ".aegis").mkdir()
    (ws_root / ".aegis" / "instructions.md").write_text("x" * 10000, encoding="utf-8")

    section = load_workspace_instructions(ws, max_file_chars=200, max_total_chars=500)

    assert "AGENTS.md" in section
    assert "CLAUDE.md" in section
    assert "仓库约定" in section
    assert len(section) <= 700
    prompt = build_system_prompt(ws=ws)
    assert "仓库级指令" in prompt
    assert "不能覆盖 Aegis 的安全边界" in prompt


def test_plan_mode_exposes_only_read_tools(ws):
    r = AgentRunner(workspace=ws, user_task="分析项目", permission_mode="plan", config={})
    r.native_tools = True
    names = [item["function"]["name"] for item in r.tool_schemas()]
    assert r.permission_mode == "plan"
    assert set(names) == {"read_file", "list_dir", "search_code", "web_search"}
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "run_command" not in names


def test_plan_mode_rejects_forced_write_without_side_effect(ws, ws_root, fake_client):
    fake_client(script=[native_tool_events(
        "write_file", {"path": "should_not_exist.py", "content": "x"})])
    r = AgentRunner(workspace=ws, user_task="先给我方案", permission_mode="plan", config={})
    events = list(r.run())
    result = next(e for e in events if e.get("type") == "tool_result")
    assert result["ok"] is False
    assert result["error"]["code"] == "PLAN_MODE_READONLY"
    assert not (ws_root / "should_not_exist.py").exists()


def test_workspace_instructions_skip_outside_link(ws, ws_root, outside_root):
    import pytest
    target = outside_root / "secret.txt"
    try:
        (ws_root / "AGENTS.md").symlink_to(target)
    except OSError:
        pytest.skip("当前账号不允许创建文件符号链接")
    assert "TOP-SECRET" not in load_workspace_instructions(ws)


def test_failed_command_is_a_failed_tool_with_output(ctx, monkeypatch):
    import apps.agent.tools as tool_mod
    monkeypatch.setattr(tool_mod, "run_command", lambda *a, **kw: {
        "exit_code": 1, "stdout": "1 failed", "stderr": "AssertionError",
        "timed_out": False, "interrupted": False})
    result = tool_mod.dispatch(ctx, "run_command", {"command": "python -m pytest"})
    assert not result.ok
    assert result.error["code"] == "COMMAND_FAILED"
    assert result.data["stderr"] == "AssertionError"


def test_plan_fallback_blocks_commands(ws, fake_client, monkeypatch):
    import apps.llm.client as client_mod
    monkeypatch.setattr(client_mod, "get_tools_cap", lambda *a: "none")
    client = fake_client(script=[[{"type": "delta", "content": json.dumps({
        "tool": "run_command", "args": {"command": "echo forbidden"}})}]])
    r = AgentRunner(workspace=ws, user_task="分析项目", permission_mode="plan", config={})
    events = list(r.run())
    assert next(e for e in events if e["type"] == "tool_result")["error"]["code"] == "PLAN_MODE_READONLY"
    assert "【计划模式】" in client.message_log[0][0]["content"]


def test_settings_accepts_plan_mode(monkeypatch):
    from apps.settings import services
    saved = {}
    monkeypatch.setattr(services, "read_user_config", lambda: saved)
    monkeypatch.setattr(services, "write_user_config", lambda value: saved.update(value) is None)
    monkeypatch.setattr(services.apps, "reload_config", lambda: None)
    assert services.set_permission_mode("plan")["ok"]
    assert services.get_permission_mode() == "plan"


def test_repo_override_replaces_agents(ws, ws_root):
    (ws_root / "AGENTS.md").write_text("OLD_RULE", encoding="utf-8")
    (ws_root / "AGENTS.override.md").write_text("NEW_RULE", encoding="utf-8")
    section = load_workspace_instructions(ws)
    assert "NEW_RULE" in section
    assert "OLD_RULE" not in section
