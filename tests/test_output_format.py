# -*- coding: utf-8 -*-
"""
tests/test_output_format.py — 模型输出格式解析（1.2.2）

验收点：
    · JSON 回退通道：模型输出纯文本（未尝试 JSON）→ 直接视为最终答复，不再报「无法解析」
    · 模型输出疑似 JSON 但损坏（截断/转义错误）→ 明确 PARSE_FAILED，不静默
    · 空输出仍报错；未知工具 / 非法参数仍报错（模型试图调工具就该暴露问题）
    · DELETE /api/aichat/sessions/<id> 删除成功；重复删除返回「会话不存在」
"""
import json

import pytest

from apps.agent.parser import ERR_PARSE_FAILED, ERR_UNKNOWN_TOOL, parse_fallback


# ── parse_fallback：纯文本直通 ────────────────────────────
def test_plain_text_reply_is_final():
    out = parse_fallback("用户只是打招呼，直接友好回应即可，无需调用工具。")
    assert out["kind"] == "final"
    assert out["final"] == "用户只是打招呼，直接友好回应即可，无需调用工具。"


def test_plain_text_multiline_is_final():
    text = "第一行结论。\n第二行补充说明。"
    out = parse_fallback(text)
    assert out["kind"] == "final" and out["final"] == text


def test_empty_output_still_errors():
    out = parse_fallback("   ")
    assert out["kind"] == "none"
    assert out["error"]["code"] == ERR_PARSE_FAILED


def test_broken_json_fence_errors_clearly():
    # 模型尝试输出 JSON 但被截断/转义错误 → 必须明确报错，不能静默当纯文本
    out = parse_fallback('```json\n{"thought":"看一下", "tool":"read_file", "args":{')
    assert out["kind"] == "none"
    assert out["error"]["code"] == ERR_PARSE_FAILED
    assert "无法解析" in out["error"]["message"]


def test_bare_broken_object_errors():
    out = parse_fallback('{"tool": "read_file"')  # 未闭合
    assert out["kind"] == "none"
    assert out["error"]["code"] == ERR_PARSE_FAILED


def test_unknown_tool_still_errors():
    out = parse_fallback('```json\n{"tool":"rm_rf","args":{"path":"x"}}\n```')
    assert out["kind"] == "none"
    assert out["error"]["code"] == ERR_UNKNOWN_TOOL


def test_valid_tool_call_unaffected():
    out = parse_fallback('```json\n{"thought":"定位入口","tool":"read_file","args":{"path":"main.py"}}\n```')
    assert out["kind"] == "tool"
    assert out["tool"] == "read_file" and out["args"] == {"path": "main.py"}


# ── runner 端到端：纯文本回复直接作为最终答复 ────────────────
def test_runner_fallback_plain_text_final(ws, fake_client, monkeypatch):
    """JSON 通道下模型直接回纯文本 → 不再出现「输出格式无法解析」，直接完成任务"""
    import apps.llm.client as llm_client_mod
    monkeypatch.setattr(llm_client_mod, "get_tools_cap",
                        lambda key, default="unknown": "none")
    fake_client(script=[[{"type": "delta", "content": "你好！我是 Aegis，有什么可以帮你？"}]])
    from apps.agent.runner import AgentRunner
    r = AgentRunner(workspace=ws, user_task="你好", config={})
    events = [e for e in r.run()]
    notices = [e for e in events if e["type"] == "notice" and "无法解析" in (e.get("content") or "")]
    assert not notices, "纯文本回复不应触发解析重试提示"
    end = next(e for e in events if e["type"] == "run_end")
    assert end["stats"]["stopped_by"] == "completed"
    assert r.final_text == "你好！我是 Aegis，有什么可以帮你？"


# ── 对话删除路由 ──────────────────────────────────────────
def test_delete_session_route_roundtrip():
    import main as main_mod

    app = main_mod.create_app()
    client = app.test_client()
    token = main_mod.get_token()
    headers = {"X-Aegis-Token": token} if token else {}

    # 通过路由创建会话（服务层 create_session 直接返回 int，路由层包装成 dict）
    resp = client.post("/api/aichat/sessions", json={"title": "待删除对话"},
                       headers=headers)
    assert resp.status_code == 200
    created = resp.get_json().get("data") or {}
    sid = created.get("session_id")
    assert sid, "创建会话应返回 session_id"

    resp = client.delete("/api/aichat/sessions/%d" % sid, headers=headers)
    assert resp.status_code == 200
    assert resp.get_json().get("code") == 0

    # 已删除的会话再次删除 → 明确的「会话不存在」
    resp2 = client.delete("/api/aichat/sessions/%d" % sid, headers=headers)
    body = resp2.get_json()
    assert body.get("code") not in (0, None)
    assert "不存在" in (body.get("msg") or "")
