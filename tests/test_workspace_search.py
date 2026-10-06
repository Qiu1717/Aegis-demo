# -*- coding: utf-8 -*-
"""
tests/test_workspace_search.py — 工作区跨目录搜索与文件预览接口（1.3.0）

验收点：
    · GET /api/agent/workspace/search：文件名匹配 + 内容匹配（跨目录、大小写不敏感）
    · 未设置工作区 → 明确报错（业务码 400）
    · GET /api/agent/workspace/file：返回带行号的结构化行；越界路径被拒绝
"""
import json

import pytest


def _client_with_ws(ws_root, monkeypatch):
    from apps.settings import services as settings_services
    monkeypatch.setattr(settings_services, "get_workspace",
                        lambda: {"valid": True, "path": str(ws_root),
                                 "configured": True, "reason": ""})
    import main as main_mod
    app = main_mod.create_app()
    token = main_mod.get_token()
    headers = {"X-Aegis-Token": token} if token else {}
    return app.test_client(), headers


def test_search_matches_filename_and_content(ws_root, monkeypatch):
    client, headers = _client_with_ws(ws_root, monkeypatch)
    resp = client.get("/api/agent/workspace/search?q=hello", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json().get("data") or {}
    matches = data.get("matches") or []
    assert any(m["kind"] == "content" and m["path"].endswith("app.py") and m["line"] == 1
               for m in matches), "应命中 app.py 第 1 行的 hello"
    # 跨目录：sub/mod.py 的文件名也应能被搜到
    resp2 = client.get("/api/agent/workspace/search?q=mod.py", headers=headers)
    matches2 = (resp2.get_json().get("data") or {}).get("matches") or []
    assert any(m["kind"] == "name" and m["path"] == "sub/mod.py" for m in matches2)


def test_search_without_workspace_fails_clearly(monkeypatch):
    from apps.settings import services as settings_services
    monkeypatch.setattr(settings_services, "get_workspace", lambda: {})
    import main as main_mod
    app = main_mod.create_app()
    token = main_mod.get_token()
    headers = {"X-Aegis-Token": token} if token else {}
    resp = app.test_client().get("/api/agent/workspace/search?q=x", headers=headers)
    body = resp.get_json()
    assert body.get("code") == 400
    assert "工作区" in (body.get("msg") or "")


def test_file_preview_returns_structured_lines(ws_root, monkeypatch):
    client, headers = _client_with_ws(ws_root, monkeypatch)
    resp = client.get("/api/agent/workspace/file?path=app.py", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json().get("data") or {}
    lines = data.get("lines") or []
    assert lines and lines[0]["no"] == 1
    assert lines[0]["text"] == "print('hello')"


def test_file_preview_rejects_outside_path(ws_root, monkeypatch):
    client, headers = _client_with_ws(ws_root, monkeypatch)
    resp = client.get("/api/agent/workspace/file?path=../outside/secret.txt", headers=headers)
    body = resp.get_json()
    assert body.get("code") not in (0, None)
    assert body.get("code") == 400
