"""Settings/runtime regressions; run this file directly for fully isolated pytest."""
import builtins
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile

import pytest


def _run_isolated():
    """Block local secrets/config and all network before importing the application."""
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    real_open = builtins.open
    real_isfile = os.path.isfile
    config_path = os.path.normcase(str(root / "config" / "config.json"))
    dotenv_path = os.path.normcase(str(root / ".env"))
    safe_config = json.dumps({"database": {"type": "sqlite"},
                              "llm": {"provider_chain": ["offline"]}, "agent": {}})

    def safe_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            name = os.path.normcase(os.path.abspath(file))
            if name == dotenv_path:
                raise AssertionError("Tests must not read project .env")
            if name == config_path:
                return io.StringIO(safe_config)
        return real_open(file, *args, **kwargs)

    builtins.open = safe_open
    os.path.isfile = lambda name: False if os.path.normcase(os.path.abspath(name)) == dotenv_path else real_isfile(name)
    for name in list(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN")) or name.startswith(("LLM_", "DB_")):
            os.environ.pop(name)
    with tempfile.TemporaryDirectory(prefix="aegis-settings-tests-") as data:
        os.environ["AEGIS_DATA_DIR"] = data
        import apps
        from apps import paths
        from apps.security import keystore
        import requests

        legacy_test_dir = str(root / "tests" / ".tmpdata")
        paths.data_dir = lambda: Path(data if os.environ.get("AEGIS_DATA_DIR") == legacy_test_dir
                                     else os.environ.get("AEGIS_DATA_DIR", data))
        keys = {}
        keystore.load_key = lambda key: keys.get(key, "")
        keystore.save_key = lambda key, value: keys.__setitem__(key, value) is None
        keystore.delete_key = lambda key: keys.pop(key, None)
        keystore.describe = lambda: {"backend": "test-memory"}

        def no_network(*args, **kwargs):
            raise AssertionError("Real network is forbidden during tests")

        requests.sessions.Session.request = no_network
        result = pytest.main(sys.argv[1:] or [str(Path(__file__)), "-q", "--tb=short"])
        logging.shutdown()
    sys.exit(result)


if __name__ == "__main__":
    _run_isolated()


@pytest.fixture
def settings_env(monkeypatch, tmp_path):
    import apps
    import apps.llm as llm
    from apps import paths
    from apps.llm import providers
    from apps.settings import services

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(apps, "_USER_CONFIG_PATH", str(tmp_path / "config.user.json"))
    monkeypatch.setitem(apps.CONFIG, "llm", {})
    monkeypatch.setitem(apps.CONFIG, "agent", {})
    keys = {}
    monkeypatch.setattr(services.keystore, "load_key", lambda key: keys.get(key, ""))
    monkeypatch.setattr(services.keystore, "save_key", lambda key, value: keys.__setitem__(key, value) is None)
    monkeypatch.setattr(services.keystore, "delete_key", lambda key: keys.pop(key, None))
    monkeypatch.setattr(services.keystore, "describe", lambda: {"backend": "test-memory"})
    monkeypatch.setattr(providers, "_KEY_RESOLVER", lambda key: keys.get(key, ""))
    monkeypatch.setattr(services, "get_workspace", lambda: {"path": str(tmp_path), "valid": True})
    for env_names in providers.AUTH_ENV_MAP.values():
        for name in env_names:
            monkeypatch.delenv(name, raising=False)
    llm.reset_client()
    yield services, keys
    llm.reset_client()


def model_config(context=0, active="primary"):
    return {"active_provider": active, "provider_chain": ["primary", "backup", "offline"],
            "max_context_messages": 20, "max_context_tokens": 6000, "max_tokens": 8192,
            "providers": {
                "primary": {"base_url": "https://primary.invalid/v1", "model": "test-model",
                            "api_key": "test-primary", "context_length": context},
                "backup": {"base_url": "https://backup.invalid/v1", "model": "backup-model",
                           "api_key": "test-backup"}}}


@pytest.mark.parametrize("mode", ["stream", "complete", "stream_tools", "complete_tools"])
def test_selected_provider_failure_never_calls_backup(settings_env, monkeypatch, mode):
    from apps.llm.client import LLMClient, ProviderFailed

    client = LLMClient(model_config())
    attempts = []

    def fail(provider, *args, **kwargs):
        attempts.append(provider.key)
        raise ProviderFailed("unauthorized", {"code": "INVALID_KEY", "reason": "invalid", "action": "fix key"})

    monkeypatch.setattr(client, "_stream_one", fail)
    monkeypatch.setattr(client, "_complete_one", fail)
    outcome = getattr(client, mode)([{"role": "user", "content": "hi"}])
    if mode.startswith("stream"):
        assert list(outcome)[-1]["code"] == "INVALID_KEY"
    elif mode == "complete_tools":
        assert outcome["ok"] is False
    else:
        assert outcome[1] == "不可用"
    assert attempts == ["primary"]
    assert client.last_used is None


def test_selected_provider_outside_legacy_chain_is_used(settings_env):
    from apps.llm.client import LLMClient

    cfg = model_config(active="backup")
    cfg["provider_chain"] = ["primary", "offline"]
    assert LLMClient(cfg).first_available(False).key == "backup"


def test_unavailable_selection_and_explicit_tool_hint_cannot_select_another(settings_env):
    from apps.llm.client import LLMClient

    cfg = model_config()
    cfg["providers"]["primary"]["enabled"] = False
    client = LLMClient(cfg)
    assert client.first_available(False) is None
    assert client.complete_tools([], provider_key="backup")["ok"] is False
    assert "backup-model" not in client.active_label()


def test_missing_active_migrates_chain_head_without_changing_legacy_chain(settings_env):
    import apps
    from apps.llm.client import LLMClient

    services, _ = settings_env
    cfg = model_config()
    cfg.pop("active_provider")
    cfg["provider_chain"] = ["backup", "primary", "offline"]
    apps.CONFIG["llm"] = cfg
    services.write_user_config({"llm": cfg})
    assert services.get_providers()["active"] == "backup"
    services.save_providers({})
    saved = services.read_user_config()["llm"]
    assert saved["active_provider"] == "backup"
    assert saved["provider_chain"] == ["backup", "primary", "offline"]
    assert LLMClient(saved).first_available(False).key == "backup"


@pytest.mark.parametrize("input_key", ["", "   ", "test...saved", "********", "test…saved"])
def test_blank_or_masked_key_preserves_saved_key(settings_env, input_key):
    services, keys = settings_env
    keys["deepseek"] = "test-saved-key"
    services.write_user_config({"llm": {"providers": {"deepseek": {"has_key": True, "key_ref": "aegis:deepseek"}}}})
    services.save_providers({"active": "deepseek", "providers": [{"key": "deepseek", "api_key": input_key}]})
    assert keys["deepseek"] == "test-saved-key"
    assert services.read_user_config()["llm"]["providers"]["deepseek"]["has_key"] is True


def test_saved_key_beats_stale_environment(settings_env, monkeypatch):
    from apps.llm.providers import build_provider

    _, keys = settings_env
    keys["deepseek"] = "test-saved-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-stale-env")
    assert build_provider("deepseek").api_key == "test-saved-key"


class Response:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self.data = data if data is not None else {"object": "list", "data": [{"id": "test-model", "object": "model", "owned_by": "deepseek"}]}
        self.text = text

    def json(self):
        return self.data


@pytest.mark.parametrize("url, expected", [
    (None, "https://api.deepseek.com/models"),
    (" https://api.deepseek.com/v1/ ", "https://api.deepseek.com/v1/models"),
    ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com/models"),
    ("https://api.deepseek.com/v1/models", "https://api.deepseek.com/v1/models")])
def test_deepseek_get_models_uses_preset_or_normalized_url_and_saved_key(settings_env, monkeypatch, url, expected):
    services, keys = settings_env
    keys["deepseek"] = "test-saved-key"
    calls = []

    def get(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return Response()

    monkeypatch.setattr(services.requests, "get", get)
    result = services.list_models("deepseek", api_key="", base_url=url)
    assert result["ok"] is True
    assert result["models"] == ["test-model"]
    assert calls[0][0] == expected
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-saved-key"
    assert calls[0][1]["timeout"] == (6, 12)
    assert calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("status, code", [(401, "INVALID_KEY"), (403, "NO_PERMISSION"),
    (429, "RATE_LIMIT"), (500, "SERVER_ERROR"), (404, "MODELS_UNAVAILABLE"), (405, "MODELS_UNAVAILABLE")])
def test_models_http_errors_keep_real_category_and_hide_remote_detail(settings_env, monkeypatch, status, code):
    services, keys = settings_env
    keys["deepseek"] = "test-saved-key"
    monkeypatch.setattr(services.requests, "get", lambda *a, **kw: Response(status, text="secret echo test-saved-key"))
    result = services.list_models("deepseek", base_url="https://api.deepseek.com")
    assert result["ok"] is False
    assert result["error"]["code"] == code
    assert "test-saved-key" not in json.dumps(result)
    assert "自动" not in result["error"].get("action", "")


def test_models_timeout_and_bad_schema_are_not_fake_success(settings_env, monkeypatch):
    services, keys = settings_env
    keys["deepseek"] = "test-saved-key"

    def timeout(*args, **kwargs):
        raise services.requests.exceptions.Timeout("test timeout")

    monkeypatch.setattr(services.requests, "get", timeout)
    assert services.list_models("deepseek", base_url="https://api.deepseek.com")["error"]["code"] == "TIMEOUT"
    monkeypatch.setattr(services.requests, "get", lambda *a, **kw: Response(data={"error": "bad"}))
    result = services.list_models("deepseek", base_url="https://api.deepseek.com")
    assert result["ok"] is False
    assert result["error"]["code"] == "BAD_RESPONSE"


def history(count=60, chars=2000):
    return [{"message_role": "user" if i % 2 == 0 else "assistant",
             "message_content": str(i) + "测" * chars} for i in range(count)]


def test_explicit_300k_chat_keeps_history_beyond_6k_and_20_messages(settings_env):
    from apps.llm.client import LLMClient

    client = LLMClient(model_config(300000))
    messages = client.build_messages(history(), system_extra="system context")
    assert len(messages) == 61
    assert messages[1]["content"].startswith("0测")


def test_default_chat_still_has_safe_limits(settings_env):
    from apps.llm.client import LLMClient

    messages = LLMClient(model_config()).build_messages(history())
    assert 1 <= len(messages) <= 20
    assert sum(len(m["content"]) // 2 for m in messages) <= 6000


def test_chat_budget_reserves_configured_output_and_system(settings_env):
    from apps.llm.client import LLMClient

    cfg = model_config(12000)
    cfg["system_prompt"] = "测" * 2000
    messages = LLMClient(cfg).build_messages(history(20, 1000))
    assert sum(len(m["content"]) // 2 + 4 for m in messages) + 8192 + 256 <= 12000


def test_agent_300k_history_and_tool_schema_share_real_window(settings_env, monkeypatch, ws):
    import apps.llm as llm
    from apps.agent.runner import AgentRunner
    from apps.llm.client import LLMClient, estimate_tokens

    client = LLMClient(model_config(300000))
    monkeypatch.setattr(llm, "get_client", lambda: client)
    captured = []

    def stream(provider, messages, tools=None, tool_choice=None):
        captured.append([dict(m) for m in messages])
        yield {"type": "delta", "content": "done"}

    monkeypatch.setattr(client, "_stream_one", stream)
    runner = AgentRunner(workspace=ws, user_task="test task", history=history(), config={"max_steps": 1})
    list(runner.run())
    assert len(captured[0]) == 62
    assert captured[0][1]["content"].startswith("0测")
    assert runner.context_token_budget > 280000
    tool_tokens = estimate_tokens(json.dumps(runner.tool_schemas(), ensure_ascii=False))
    assert runner.context_token_budget + 8192 + tool_tokens + 256 <= 300000


def test_agent_small_window_reserves_tools_before_request(settings_env, monkeypatch, ws):
    import apps.llm as llm
    from apps.agent.runner import AgentRunner
    from apps.llm.client import LLMClient

    cfg = model_config(16000)
    client = LLMClient(cfg)
    monkeypatch.setattr(llm, "get_client", lambda: client)
    captured = []

    def stream(provider, messages, tools=None, tool_choice=None):
        captured.append(sum(AgentRunner._msg_tokens(m) for m in messages))
        yield {"type": "delta", "content": "done"}

    monkeypatch.setattr(client, "_stream_one", stream)
    runner = AgentRunner(workspace=ws, user_task="test task", history=history(), config={"max_steps": 1})
    list(runner.run())
    assert captured and captured[0] <= runner.context_token_budget < 7800


def test_settings_values_reach_runtime_and_zero_resets_context(settings_env, monkeypatch):
    import apps.llm as llm

    services, keys = settings_env
    keys["deepseek"] = "test-saved-key"
    payload = {"active": "deepseek", "temperature": 0, "max_tokens": 8192,
               "providers": [{"key": "deepseek", "model": "test-model", "timeout": 137, "context_length": 300000}]}
    services.save_providers(payload)
    client = llm.get_client()
    provider = client.first_available(False)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(data={"choices": [{"message": {"content": "done"}}]})

    monkeypatch.setattr(services.requests, "post", post)
    assert client.complete([{ "role": "user", "content": "hi"}])[0] == "done"
    assert calls[0][1]["json"]["temperature"] == 0
    assert calls[0][1]["json"]["max_tokens"] == 8192
    assert calls[0][1]["timeout"] == (10, 137)
    assert provider.context_length == 300000
    services.save_providers({"providers": [{"key": "deepseek", "context_length": 0}]})
    assert services.get_providers()["providers"][0]["context_length"] == 0


def test_chat_thinking_setting_reaches_system_prompt(settings_env, monkeypatch):
    import apps.llm as llm
    from apps.aichat import services as chat
    from apps.llm.client import LLMClient

    services, _ = settings_env
    services.set_thinking_level("max")
    monkeypatch.setattr(llm, "get_client", lambda: LLMClient(model_config(300000)))
    monkeypatch.setattr(chat, "get_client", llm.get_client)
    messages = chat._build_llm_messages(history(1, 10))
    assert messages[0]["role"] == "system"
    assert "极致性能" in messages[0]["content"]


@pytest.mark.parametrize("payload", [{"temperature": 3}, {"temperature": float("nan")},
    {"max_tokens": 0}, {"max_tokens": 1.5}, {"providers": [{"key": "deepseek", "timeout": -1}]}])
def test_invalid_runtime_settings_are_rejected_without_writing(settings_env, payload):
    services, _ = settings_env
    result = services.save_providers(payload)
    assert result["ok"] is False
    assert services.read_user_config() == {}


def test_bootstrap_checks_selected_model_not_any_legacy_provider(settings_env):
    import apps

    services, _ = settings_env
    cfg = model_config()
    cfg["providers"]["primary"]["enabled"] = False
    apps.CONFIG["llm"] = cfg
    assert services.bootstrap()["need_setup"] is True


def test_enabled_skill_resource_schema_survives_plan_filter_but_chat_has_no_tools(settings_env, monkeypatch, ws):
    from apps.agent import runner as module

    def schemas(web, local, skill_enabled=False):
        return [{"type": "function", "function": {"name": name}} for name in
                (["read_skill_resource", "write_file"] if skill_enabled else ["write_file"])]

    monkeypatch.setattr(module, "build_tool_schemas", schemas)
    runner = module.AgentRunner(workspace=ws, user_task="test task", permission_mode="plan",
                                skills=[{"name": "test skill"}])
    assert [s["function"]["name"] for s in runner.tool_schemas()] == ["read_skill_resource"]
    runner.tools_enabled = False
    assert runner.tool_schemas() is None


def test_deleting_selected_provider_disables_legacy_default_and_selects_no_paid_model(settings_env):
    import apps
    from apps.llm.client import LLMClient

    services, _ = settings_env
    apps.CONFIG["llm"] = model_config()
    services.write_user_config({"llm": model_config()})
    services.save_providers({"remove": ["primary"]})
    saved = services.read_user_config()["llm"]
    assert saved["providers"]["primary"]["enabled"] is False
    assert saved["active_provider"] == "offline"
    assert LLMClient(saved).first_available(False) is None


def test_large_system_prompt_is_compressed_inside_input_budget(settings_env):
    from apps.llm.client import LLMClient

    cfg = model_config(12000)
    cfg["system_prompt"] = "测" * 30000
    messages = LLMClient(cfg).build_messages(history(1, 100))
    assert sum(len(m["content"]) // 2 + 4 for m in messages) + 8192 + 256 <= 12000


def test_malformed_legacy_context_uses_safe_default(settings_env):
    from apps.llm.client import LLMClient

    cfg = model_config()
    cfg["providers"]["primary"]["context_length"] = "bad old setting"
    messages = LLMClient(cfg).build_messages(history())
    assert 1 <= len(messages) <= 20


def test_model_response_with_no_valid_ids_is_not_success(settings_env, monkeypatch):
    services, keys = settings_env
    keys["deepseek"] = "test-saved-key"
    monkeypatch.setattr(services.requests, "get", lambda *a, **kw: Response(data={"data": [{}]}))
    result = services.list_models("deepseek")
    assert result["ok"] is False
    assert result["error"]["code"] == "BAD_RESPONSE"
