# -*- coding: utf-8 -*-
"""
tests/test_unified_chat.py — 统一对话窗口（无工作区纯对话）与桌面目录选择桥（1.2.1）

验收点：
    · AgentRunner(workspace=None) 构造与事件流正常，run_start 的 workspace 为 ""
    · chat 模式下模型强行产出工具调用 → TOOLS_DISABLED，不执行、不崩溃
    · POST /api/agent/run 未设置工作区时不再 400，降级为「仅保留对话」跑通 SSE
    · 显式携带无效工作区仍按 400 拒绝（校验行为不回归）
    · AegisApi.open_folder_dialog 走 window 实例方法；异常不被吞（原样抛出）
"""
import json
import sys
import types

import pytest

from apps.agent.runner import AgentRunner


def _sse_events(body):
    events = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except Exception:
                pass
    return events


def _test_client():
    import main as main_mod
    app = main_mod.create_app()
    token = main_mod.get_token()
    headers = {"X-Aegis-Token": token} if token else {}
    return app.test_client(), headers


# ── runner 层：workspace=None 不崩溃 ──────────────────────
def test_runner_without_workspace_constructs_and_runs(fake_client):
    fake_client(script=[[{"type": "delta", "content": "好的，纯聊天回复。"}]])
    r = AgentRunner(workspace=None, user_task="你好", config={},
                    permission_mode="chat")
    events = [e for e in r.run()]
    types = [e["type"] for e in events]
    assert types[0] == "run_start"
    assert events[0]["workspace"] == ""
    assert "run_end" in types and "done" in types
    assert r.ws_root == ""
    assert r.tools_enabled is False


def test_runner_chat_mode_blocks_tool_call_without_workspace(fake_client):
    # chat 模式下走 JSON 指令通道；模型强行产出工具调用 → TOOLS_DISABLED，不执行
    payload = {"tool": "run_command", "args": {"command": "echo hi"},
               "thought": "试图执行命令"}
    block = "```json\n%s\n```" % json.dumps(payload, ensure_ascii=False)
    final_block = "```json\n%s\n```" % json.dumps(
        {"final": "当前为纯对话模式，不会执行任何命令。"}, ensure_ascii=False)
    fake_client(script=[
        [{"type": "delta", "content": block}],
        [{"type": "delta", "content": final_block}],
    ])
    r = AgentRunner(workspace=None, user_task="跑个命令", config={},
                    permission_mode="chat")
    events = [e for e in r.run()]
    results = [e for e in events if e["type"] == "tool_result"]
    assert results, "工具调用应产生 tool_result（被拒绝）"
    err = results[0].get("error") or {}
    assert err.get("code") == "TOOLS_DISABLED"


# ── 路由层：无工作区 → SSE 降级纯对话，不再 400 ─────────────
def test_run_route_without_workspace_degrades_to_chat(fake_client, monkeypatch):
    fake_client(script=[[{"type": "delta", "content": "pong"}]])
    # 确保设置里也没有已保存的工作区（不受其他用例写入临时数据目录影响）
    from apps.settings import services as settings_services
    monkeypatch.setattr(settings_services, "get_workspace", lambda: {})

    client, headers = _test_client()
    resp = client.post("/api/agent/run", json={"message": "你好"}, headers=headers)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "未指定工作区" not in body
    events = _sse_events(body)
    types = [e.get("type") for e in events]
    assert "run_start" in types and "run_end" in types
    start = next(e for e in events if e.get("type") == "run_start")
    assert start.get("workspace") == ""


def test_run_route_with_invalid_workspace_still_400(fake_client):
    fake_client(script=[[{"type": "delta", "content": "pong"}]])
    client, headers = _test_client()
    resp = client.post(
        "/api/agent/run",
        json={"message": "x", "workspace": "Z:\\definitely-not-exist-12345"},
        headers=headers)
    # 既有约定：api_fail 走 HTTP 200 + 业务码（前端按 body.code 判定）
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("code") == 400
    assert data.get("msg")


def test_env_endpoint_reports_ua():
    """/api/env 环境检测信息（排障用）：返回 UA 且不报错"""
    client, headers = _test_client()
    resp = client.get("/api/env", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json().get("data") or {}
    assert "ua" in data and "env_debug" in data


# ── 桌面桥：目录选择对话框走 window 实例方法，异常不吞 ───────
class _FakeWindow(object):
    def __init__(self, result=None, exc=None):
        self.calls = []
        self._result = result
        self._exc = exc

    def create_file_dialog(self, dialog_type):
        self.calls.append(dialog_type)
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def fake_webview_module(monkeypatch):
    """sys python 没装 pywebview，往 sys.modules 塞个替身即可（仅需常量）"""
    stub = types.SimpleNamespace(FOLDER_DIALOG=20)
    monkeypatch.setitem(sys.modules, "webview", stub)
    return stub


def test_open_folder_dialog_uses_window_instance(fake_webview_module):
    from desktop.app import AegisApi

    api = AegisApi()
    # 窗口未注入 → 明确报错而不是静默返回空
    with pytest.raises(RuntimeError):
        api.open_folder_dialog()

    win = _FakeWindow(result=("E:\\proj",))
    api._win = win
    assert api.open_folder_dialog() == "E:\\proj"
    assert win.calls == [20], "必须走 window.create_file_dialog（pywebview 5.x 实例方法）"


def test_open_folder_dialog_cancel_returns_empty(fake_webview_module):
    from desktop.app import AegisApi
    api = AegisApi()
    api._win = _FakeWindow(result=None)
    assert api.open_folder_dialog() == ""


def test_open_folder_dialog_error_not_swallowed(fake_webview_module):
    from desktop.app import AegisApi
    api = AegisApi()
    api._win = _FakeWindow(exc=OSError("boom"))
    with pytest.raises(OSError):
        api.open_folder_dialog()


def test_js_api_must_not_expose_public_window_attribute():
    """回归：js_api 对象的公开非方法属性会被 pywebview 递归序列化给前端。
    AegisApi 绝不能持有名为 window 的公开属性（1.2.1 曾因此导致桥注入整体失败，
    前端把桌面壳误判为浏览器）。"""
    from desktop.app import AegisApi
    api = AegisApi()
    public_non_method = [n for n in dir(api)
                         if not n.startswith("_") and not callable(getattr(api, n))]
    assert public_non_method == [], "js_api 不得有公开非方法属性：%s" % public_non_method
