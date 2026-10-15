import json

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path / "data"))
    import apps
    monkeypatch.setattr(apps, "_DB_TYPE", "sqlite")
    monkeypatch.setattr(apps, "_SQLITE_INITIALIZED", False)
    from apps.settings import services
    monkeypatch.setattr(services, "get_workspace", lambda: {})
    import main
    return main.create_app().test_client(), {"X-Aegis-Token": main.get_token()}


def test_title_and_session_id_are_available_before_model_response(client, fake_client):
    model = fake_client()
    c, headers = client
    response = c.post("/api/agent/run", json={"message": "检查实时标题"}, headers=headers, buffered=False)
    first = json.loads(next(iter(response.response)).decode().split("data: ", 1)[1])
    assert first["type"] == "run_start"
    assert first["session_title"] == "检查实时标题"
    assert first["session_id"]
    assert model.calls == 0
    from apps.aichat import services
    assert services.get_session(first["session_id"])["session_title"] == "检查实时标题"
    response.get_data()


def test_live_title_preserves_manual_rename(client, fake_client):
    fake_client()
    from apps.aichat import services
    sid = services.create_session("自定义项目标题")
    c, headers = client
    response = c.post("/api/agent/run", json={"message": "另一条问题", "session_id": sid}, headers=headers)
    events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
    first = next(e for e in events if e["type"] == "run_start")
    assert first["session_title"] == "自定义项目标题"
    assert services.get_session(sid)["session_title"] == "自定义项目标题"
