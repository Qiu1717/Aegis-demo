import io
import json
import zipfile

import pytest

from apps import paths


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path / "profile"))
    import apps
    monkeypatch.setattr(apps, "_SQLITE_INITIALIZED", False)
    monkeypatch.setattr(apps, "_DB_TYPE", "sqlite")
    import main
    from apps.settings import services
    monkeypatch.setattr(services, "get_workspace", lambda: {})
    app = main.create_app()
    return app.test_client(), {"X-Aegis-Token": main.get_token()}


def upload(client, name="example.py", content=b"answer = 42\n"):
    c, headers = client
    return c.post("/api/agent/attachments", data={"file": (io.BytesIO(content), name)},
                  headers=headers).get_json()


def test_attachment_is_stored_and_reaches_real_model(client, fake_client):
    model = fake_client()
    result = upload(client)
    assert result["code"] == 0
    item = result["data"]
    assert item["name"] == "example.py"
    assert list((paths.data_dir() / "attachments").glob("*.json"))
    c, headers = client
    response = c.post("/api/agent/run", json={"message": "解释附件", "attachment_ids": [item["id"]]}, headers=headers)
    assert '"type": "done"' in response.get_data(as_text=True)
    messages = model.message_log[0]
    assert any(m["role"] == "user" and "answer = 42" in m["content"] for m in messages)
    assert all("answer = 42" not in m.get("content", "") for m in messages if m["role"] == "system")


@pytest.mark.parametrize("name,content", [("binary.exe", b"MZ"), ("file.txt", b"abc\x00def"),
                                           ("image.png", b"PNG"), ("huge.txt", b"a" * (2 * 1024 * 1024 + 1))],
                         ids=["executable", "binary", "image", "oversized"])
def test_unsupported_attachment_does_not_claim_success(client, name, content):
    result = upload(client, name, content)
    assert result["code"] == 400
    assert not list((paths.data_dir() / "attachments").glob("*.json"))


def test_invalid_attachment_id_blocks_run(client, fake_client):
    model = fake_client()
    c, headers = client
    result = c.post("/api/agent/run", json={"message": "读文件", "attachment_ids": ["../config.user"]}, headers=headers).get_json()
    assert result["code"] == 400
    assert model.calls == 0


def test_remove_attachment_cannot_be_reused(client):
    c, headers = client
    item = upload(client)["data"]
    assert c.delete("/api/agent/attachments/" + item["id"], headers=headers).get_json()["code"] == 0
    result = c.post("/api/agent/run", json={"message": "读文件", "attachment_ids": [item["id"]]}, headers=headers).get_json()
    assert result["code"] == 400


def test_appearance_persists_and_rejects_invalid_values(client):
    c, headers = client
    values = {"theme": "dark", "font_size": 16, "sidebar_collapsed": True, "task_details": False}
    assert c.put("/api/agent/appearance", json=values, headers=headers).get_json()["code"] == 0
    assert c.get("/api/agent/appearance", headers=headers).get_json()["data"] == values
    config = json.loads(paths.user_config_path().read_text(encoding="utf-8"))
    assert config["appearance"] == values
    assert c.put("/api/agent/appearance", json={"theme": "rainbow"}, headers=headers).get_json()["code"] == 400
    assert c.get("/api/agent/appearance", headers=headers).get_json()["data"] == values


@pytest.mark.parametrize("native", [True, False], ids=["native-tools", "json-tools"])
def test_imported_skill_resource_reaches_model_in_readonly_plan(client, fake_client, ws_root, native):
    from conftest import FakeProvider, native_tool_events
    c, headers = client
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("sample/SKILL.md", "---\nname: resource-test\ndescription: 阅读参考资料\n---\n请阅读 references/example.md。")
        package.writestr("sample/references/example.md", "技能参考证据\n第二行\n第三行")
    result = c.post("/api/skills/import/file", data={"file": (io.BytesIO(archive.getvalue()), "skills.zip")}, headers=headers).get_json()
    assert result["code"] == 0
    skill_id = result["data"]["imported"][0]["id"]
    assert c.put("/api/skills/" + skill_id, json={"enabled": True}, headers=headers).get_json()["code"] == 0
    args = {"skill_id": skill_id, "relative_path": "references/example.md", "start_line": 2, "end_line": 3}
    events = native_tool_events("read_skill_resource", args) if native else [{"type": "delta", "content": '```json\n' + json.dumps({"tool": "read_skill_resource", "args": args}) + '\n```'}]
    final_text = "技能处理完成。" if native else '```json\n{"final":"技能处理完成。"}\n```'
    model = fake_client(script=[events, [{"type": "delta", "content": final_text}]],
                        provider=FakeProvider(supports_tools="native" if native else "none"))
    response = c.post("/api/agent/run", json={"message": "请用资源技能分析项目", "workspace": str(ws_root), "permission_mode": "plan", "use_web": False}, headers=headers)
    events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
    tool_result = next(e for e in events if e.get("type") == "tool_result")
    assert tool_result["ok"], tool_result
    assert "第二行" in tool_result["data"]["content"]
    assert "技能参考证据" not in tool_result["data"]["content"]
    assert any("第三行" in m.get("content", "") for msgs in model.message_log for m in msgs if m["role"] in ("tool", "user"))
