# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import importlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.web import app_web_handlers
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _build_external_cli_publish_url,
    _detect_external_cli_agent,
    _flatten_external_cli_agents_for_config_panel,
    _flatten_modes_team_for_config_panel,
    _flatten_symphony_for_config_panel,
    _inject_external_cli_publish_url,
    _normalize_feishu_conf,
    _normalize_xiaoyi_conf,
    _register_web_handlers,
    _validate_wechat_numeric_params,
)


class FakeWebChannel:
    def __init__(self):
        self.channel_id = "web"
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []
        self.connect_handler = None
        self.disconnect_handler = None

    def register_method(self, name, handler):
        self.methods[name] = handler

    def on_connect(self, handler):
        self.connect_handler = handler

    def on_disconnect(self, handler):
        self.disconnect_handler = handler

    async def send_response(self, ws, req_id, *, ok, payload=None, error=None, code=None):
        self.responses.append(
            {
                "id": req_id,
                "ok": ok,
                "payload": payload,
                "error": error,
                "code": code,
            }
        )


class FakeAgentClient:
    def __init__(self):
        self.reload_started = asyncio.Event()
        self.release_reload = asyncio.Event()
        self.reload_finished = asyncio.Event()

    async def send_request(self, envelope):
        self.reload_started.set()
        try:
            await self.release_reload.wait()
            return type("Resp", (), {"ok": True, "payload": {}})()
        finally:
            self.reload_finished.set()


class FakeMessageHandler:
    def __init__(self):
        self.disconnected_websockets: list[tuple[str, str]] = []

    async def unregister_ws_subscriptions(self, channel_id: str, ws_id: str) -> int:
        self.disconnected_websockets.append((channel_id, ws_id))
        return 1


@pytest.mark.asyncio
async def test_web_disconnect_unregisters_physical_subscriptions() -> None:
    channel = FakeWebChannel()
    message_handler = FakeMessageHandler()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, message_handler=message_handler)
    )
    ws = SimpleNamespace(_jiuwen_ws_id="web-ws-dead")

    await channel.disconnect_handler(ws, {"sess-web"})

    assert message_handler.disconnected_websockets == [("web", "web-ws-dead")]


class FakeChannelManager:
    def __init__(self):
        self.configs: dict[str, dict] = {}

    async def set_conf(self, channel_id, new_conf):
        self.configs[channel_id] = dict(new_conf)

    def get_conf(self, channel_id):
        return dict(self.configs.get(channel_id, {}))


class FakeHeartbeatService:
    def __init__(self):
        self.config = {"every": 60.0, "target": "web"}

    async def set_heartbeat_conf(self, *, every=None, target=None, active_hours=None):
        if every is not None:
            self.config["every"] = every
        if target is not None:
            self.config["target"] = target
        if active_hours is not None:
            self.config["active_hours"] = active_hours

    def get_heartbeat_conf(self):
        return dict(self.config)


@pytest.mark.asyncio
async def test_path_select_file_returns_selected_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    channel = FakeWebChannel()
    captured: dict[str, str | None] = {}
    selected_path = str(tmp_path / "claude")

    def fake_select_file_native(*, initial_dir: str | None = None, title: str | None = None) -> str:
        captured["initial_dir"] = initial_dir
        captured["title"] = title
        return selected_path

    monkeypatch.setattr(
        "jiuwenswarm.channels.web.directory_picker.select_file_native",
        fake_select_file_native,
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["path.select_file"](
        object(),
        "req-file",
        {
            "initial_path": str(tmp_path / "old-claude"),
            "title": "Choose Claude",
        },
        "sess-1",
    )

    assert captured == {"initial_dir": str(tmp_path), "title": "Choose Claude"}
    assert channel.responses[-1] == {
        "id": "req-file",
        "ok": True,
        "payload": {"path": selected_path, "cancelled": False},
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_path_select_file_returns_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = FakeWebChannel()
    monkeypatch.setattr(
        "jiuwenswarm.channels.web.directory_picker.select_file_native",
        lambda **_: None,
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["path.select_file"](
        object(),
        "req-file-cancel",
        {},
        "sess-1",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {"path": None, "cancelled": True}


@pytest.mark.asyncio
async def test_session_list_preserves_team_name_in_projected_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        lambda **_kwargs: (
            [
                {
                    "session_id": "sess-team-1",
                    "mode": "team",
                    "team_name": "dev-team-swarm_sess-team-1",
                    "title": "team task",
                    "delivery_context": {"channel_id": "internal"},
                }
            ],
            1,
        ),
    )
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["session.list"](
        object(),
        "req-session-list",
        {},
        "current-session",
    )

    payload = channel.responses[-1]["payload"]
    assert payload["sessions"][0]["mode"] == "team"
    assert payload["sessions"][0]["team_name"] == "dev-team-swarm_sess-team-1"
    assert "delivery_context" not in payload["sessions"][0]


@pytest.mark.asyncio
async def test_path_set_reloads_config_and_resets_agent_browser_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeWebChannel()
    agent_client = object()
    saved_configs: list[dict] = []
    lifecycle_calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        app_web_handlers,
        "update_browser_in_config",
        lambda config: saved_configs.append(config),
    )
    monkeypatch.setattr(
        app_web_handlers,
        "get_config",
        lambda: {"browser": {"chrome_path": "", "headless": True}},
    )

    async def fake_clear(client):
        lifecycle_calls.append(("reload", client))

    async def fake_restart(client, **kwargs):
        lifecycle_calls.append(("restart", client, kwargs))

    monkeypatch.setattr(app_web_handlers, "_clear_agent_config_cache", fake_clear)
    monkeypatch.setattr(
        app_web_handlers,
        "_restart_agent_browser_runtime",
        fake_restart,
    )
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    await channel.methods["path.set"](
        object(),
        "req-path",
        {"chrome_path": " C:\\Chrome\\chrome.exe ", "headless": False},
        "sess-1",
    )

    assert saved_configs == [
        {"chrome_path": "C:\\Chrome\\chrome.exe", "browser_type": "auto", "headless": False}
    ]
    assert lifecycle_calls == [
        ("reload", agent_client),
        (
            "restart",
            agent_client,
            {
                "previous_chrome_path": "",
                "previous_headless": True,
            },
        ),
    ]
    assert channel.responses[-1] == {
        "id": "req-path",
        "ok": True,
        "payload": {
            "chrome_path": "C:\\Chrome\\chrome.exe",
            "browser_type": "auto",
            "headless": False,
        },
        "error": None,
        "code": None,
    }


class FakeUpdaterService:
    def get_runtime_config(self):
        return {"release_api_type": "gitcode", "release_api_url": ""}


@pytest.fixture
def cleared_openai_account_login_jobs():
    with app_web_handlers._OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        app_web_handlers._OPENAI_ACCOUNT_LOGIN_JOBS.clear()
    yield
    with app_web_handlers._OPENAI_ACCOUNT_LOGIN_JOBS_LOCK:
        app_web_handlers._OPENAI_ACCOUNT_LOGIN_JOBS.clear()


class FakeOpenAIAccountAuthManager:
    authenticated = False
    needs_refresh = False
    poll_started = threading.Event()
    release_poll = threading.Event()

    def __init__(self):
        self.base_url = "https://chatgpt.com/backend-api/codex"

    @classmethod
    def reset(cls):
        cls.authenticated = False
        cls.needs_refresh = False
        cls.poll_started = threading.Event()
        cls.release_poll = threading.Event()

    def status(self):
        return SimpleNamespace(
            authenticated=self.authenticated,
            auth_path=Path("test-auth.json"),
            has_refresh_token=self.authenticated,
            expires_at=None,
            needs_refresh=self.needs_refresh,
            error=None,
        )

    def poll_device_login(self, device_code):
        del device_code
        self.poll_started.set()
        if not self.release_poll.wait(timeout=2):
            raise TimeoutError("test poll was not released")
        type(self).authenticated = True
        type(self).needs_refresh = False
        return object()

    def logout(self):
        type(self).authenticated = False
        type(self).needs_refresh = False
        return True


class FakeOpenAIAccountModelCatalog:
    def __init__(self, *, base_url):
        self.base_url = base_url

    def list_model_ids(self, *, auth_manager):
        type(auth_manager).authenticated = True
        type(auth_manager).needs_refresh = False
        return ["gpt-test"]


@pytest.mark.asyncio
async def test_openai_account_models_list_returns_refreshed_auth_status(
        monkeypatch,
        cleared_openai_account_login_jobs,
):
    del cleared_openai_account_login_jobs
    FakeOpenAIAccountAuthManager.reset()
    FakeOpenAIAccountAuthManager.needs_refresh = True
    monkeypatch.setattr(app_web_handlers, "OpenAIAccountAuthManager", FakeOpenAIAccountAuthManager)
    monkeypatch.setattr(app_web_handlers, "OpenAIAccountModelCatalog", FakeOpenAIAccountModelCatalog)
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["openai_account.models.list"](object(), "req-models", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {
        "models": ["gpt-test"],
        "base_url": "https://chatgpt.com/backend-api/codex",
        "auth": {
            "authenticated": True,
            "auth_path": "test-auth.json",
            "has_refresh_token": True,
            "expires_at": None,
            "needs_refresh": False,
            "error": None,
            "base_url": "https://chatgpt.com/backend-api/codex",
        },
    }


@pytest.mark.asyncio
async def test_models_list_active_model_falls_back_to_zen_free_when_default_is_placeholder(
    monkeypatch,
) -> None:
    """默认模型为 .env 占位符（首次启动）时，active_model 回退到 Zen 免费模型。"""
    placeholder_entry = {
        "model_client_config": {
            "api_base": "https://example.com/compatible-mode/v1",
            "api_key": "sk-xxxxxxxxx",
            "model_name": "your-model-name",
            "client_provider": "OpenAI",
        },
        "model_config_obj": {"temperature": 0.95},
        "is_default": True,
    }
    zen_entry = {
        "model_client_config": {
            "api_base": "https://opencode.ai/zen/v1",
            "api_key": "public",
            "model_name": "deepseek-v4-flash-free",
            "client_provider": "OpenAI",
        },
        "model_config_obj": {"temperature": 0.95},
        "alias": "DeepSeek V4 Flash",
        "is_free": True,
    }

    from jiuwenswarm.server.runtime import opencode_zen

    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(app_web_handlers, "get_default_models", lambda config: [placeholder_entry])
    monkeypatch.setattr(opencode_zen, "get_zen_free_model_entries", lambda: [zen_entry])
    monkeypatch.setattr(opencode_zen, "get_zen_default_free_model_entry", lambda: zen_entry)
    monkeypatch.setattr(opencode_zen, "get_zen_free_context_window", lambda: 200000)

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["models.list"](object(), "req-models", {}, "sess-1")

    payload = channel.responses[-1]["payload"]
    assert payload["active_model"] == "deepseek-v4-flash-free"
    assert payload["models"][0]["model_name"] == "your-model-name"
    assert payload["models"][1]["model_name"] == "deepseek-v4-flash-free"
    assert payload["models"][1]["is_free"] is True


@pytest.mark.asyncio
async def test_updater_reset_source_registered_and_restores_defaults(monkeypatch):
    captured: dict[str, object] = {}

    def fake_update_updater_in_config(updates):
        captured.update(updates)

    monkeypatch.setattr(app_web_handlers, "update_updater_in_config", fake_update_updater_in_config)

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, updater_service=FakeUpdaterService()))

    await channel.methods["updater.reset_source"](object(), "req-reset", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {"release_api_type": "gitcode", "release_api_url": ""}
    assert captured == app_web_handlers.DEFAULT_SOURCE_CONFIG


@pytest.mark.asyncio
async def test_openai_account_logout_clears_pending_login_jobs(
        monkeypatch,
        cleared_openai_account_login_jobs,
):
    del cleared_openai_account_login_jobs
    FakeOpenAIAccountAuthManager.reset()
    FakeOpenAIAccountAuthManager.authenticated = True
    monkeypatch.setattr(app_web_handlers, "OpenAIAccountAuthManager", FakeOpenAIAccountAuthManager)
    app_web_handlers._store_openai_account_login_job(
        "login-1",
        app_web_handlers._OpenAIAccountLoginJob(
            device_code=SimpleNamespace(),
            created_at=time.time(),
            expires_at=time.time() + 60,
        ),
    )
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["openai_account.auth.logout"](object(), "req-logout", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    assert app_web_handlers._OPENAI_ACCOUNT_LOGIN_JOBS == {}


@pytest.mark.asyncio
async def test_openai_account_logout_wins_against_inflight_poll(
        monkeypatch,
        cleared_openai_account_login_jobs,
):
    del cleared_openai_account_login_jobs
    FakeOpenAIAccountAuthManager.reset()
    monkeypatch.setattr(app_web_handlers, "OpenAIAccountAuthManager", FakeOpenAIAccountAuthManager)
    app_web_handlers._store_openai_account_login_job(
        "login-1",
        app_web_handlers._OpenAIAccountLoginJob(
            device_code=SimpleNamespace(),
            created_at=time.time(),
            expires_at=time.time() + 60,
        ),
    )
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    poll_task = asyncio.create_task(channel.methods["openai_account.auth.poll_login"](
        object(), "req-poll", {"login_id": "login-1"}, "sess-1",
    ))
    await asyncio.wait_for(asyncio.to_thread(FakeOpenAIAccountAuthManager.poll_started.wait), timeout=1)
    logout_task = asyncio.create_task(channel.methods["openai_account.auth.logout"](
        object(), "req-logout", {}, "sess-1",
    ))
    await asyncio.sleep(0.05)
    FakeOpenAIAccountAuthManager.release_poll.set()
    await asyncio.gather(poll_task, logout_task)

    assert FakeOpenAIAccountAuthManager.authenticated is False
    assert app_web_handlers._OPENAI_ACCOUNT_LOGIN_JOBS == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("channel.feishu.set_conf", {"apps": [{"app_id": "app-1"}]}),
        ("channel.dingtalk.set_conf", {"enabled": False, "client_id": "client-1"}),
        ("heartbeat.set_conf", {"every": 30, "target": "web"}),
    ],
)
async def test_config_save_handlers_respond_before_agent_reload_finishes(monkeypatch, method, params):
    channel = FakeWebChannel()
    agent_client = FakeAgentClient()
    channel_manager = FakeChannelManager()
    heartbeat_service = FakeHeartbeatService()
    persisted: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_channel_in_config",
        lambda channel_id, conf: persisted.append((channel_id, dict(conf))),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        lambda channel_id, subsection, conf, keep_keys: persisted.append((channel_id, conf)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_heartbeat_in_config",
        lambda payload: persisted.append(("heartbeat", dict(payload))),
    )

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=agent_client,
            channel_manager=channel_manager,
            heartbeat_service=heartbeat_service,
        )
    )

    task = asyncio.create_task(channel.methods[method](object(), "req-save", params, "sess-1"))
    try:
        await asyncio.wait_for(agent_client.reload_started.wait(), timeout=0.5)

        assert persisted
        assert channel.responses[-1]["id"] == "req-save"
        assert channel.responses[-1]["ok"] is True
    finally:
        agent_client.release_reload.set()
        await task
        await asyncio.wait_for(agent_client.reload_finished.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_config_set_applies_scoped_reload_before_responding(monkeypatch, tmp_path):
    channel = FakeWebChannel()
    reload_started = asyncio.Event()
    release_first_reload = asyncio.Event()
    reload_calls: list[tuple[set[str], dict, dict]] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ENV_FILE",
        tmp_path / ".env",
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"models": {"defaults": []}},
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        reload_calls.append((set(updated_keys), dict(env_updates), dict(reload_options)))
        reload_started.set()
        await release_first_reload.wait()
        return True

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            on_config_saved=on_config_saved,
        )
    )

    task = asyncio.create_task(channel.methods["config.set"](
        object(),
        "req-1",
        {"api_base": "https://example.com/one"},
        "sess-1",
    ))

    await asyncio.wait_for(reload_started.wait(), timeout=1)
    assert channel.responses == []

    release_first_reload.set()
    await task

    assert reload_calls[0][0] == {"API_BASE"}
    assert reload_calls[0][2]["target_channel_id"] == "web"
    assert reload_calls[0][2]["reload_scopes"] == ["model"]
    assert channel.responses[-1]["id"] == "req-1"
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["applied_without_restart"] is True


@pytest.mark.asyncio
async def test_config_set_reports_saved_when_hot_reload_callback_fails(monkeypatch, tmp_path):
    channel = FakeWebChannel()

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ENV_FILE",
        tmp_path / ".env",
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"models": {"defaults": []}},
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        raise RuntimeError("agent unreachable at 10.0.0.1")

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            on_config_saved=on_config_saved,
        )
    )

    await channel.methods["config.set"](
        object(),
        "req-hot-reload-failed",
        {"api_base": "https://example.com/one"},
        "sess-1",
    )

    assert channel.responses == [
        {
            "id": "req-hot-reload-failed",
            "ok": True,
            "payload": {
                "updated": ["api_base"],
                "applied_without_restart": False,
            },
            "error": None,
            "code": None,
        }
    ]


@pytest.mark.asyncio
async def test_config_set_persists_setup_guide_without_runtime_reload(monkeypatch):
    channel = FakeWebChannel()
    persisted: list[bool] = []
    reload_options_seen: list[dict] = []

    monkeypatch.setattr(
        app_web_handlers,
        "get_config_raw",
        lambda: {"setup_guide": {"enabled": True}},
    )
    monkeypatch.setattr(
        app_web_handlers,
        "get_config",
        lambda: {"setup_guide": {"enabled": False}},
    )
    monkeypatch.setattr(
        app_web_handlers,
        "update_setup_guide_enabled_in_config",
        lambda enabled: persisted.append(enabled),
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        del updated_keys, env_updates, config_payload
        reload_options_seen.append(dict(reload_options))
        return True

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            on_config_saved=on_config_saved,
        )
    )

    await channel.methods["config.set"](
        object(),
        "req-setup-guide",
        {"setup_guide_enabled": "false"},
        "sess-setup-guide",
    )

    assert persisted == [False]
    assert reload_options_seen == [{
        "target_channel_id": "web",
        "reload_scopes": ["web_ui"],
    }]
    assert channel.responses[-1]["payload"] == {
        "updated": ["setup_guide_enabled"],
        "applied_without_restart": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_config", "expected"),
    [
        ({}, "true"),
        ({"setup_guide": {"enabled": False}}, "false"),
    ],
)
async def test_config_get_returns_setup_guide_switch(monkeypatch, raw_config, expected):
    channel = FakeWebChannel()
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: raw_config)
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["config.get"](
        object(),
        "req-get-setup-guide",
        {},
        "sess-get-setup-guide",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["setup_guide_enabled"] == expected


@pytest.mark.asyncio
async def test_models_replace_all_applies_scoped_reload_before_responding(monkeypatch):
    channel = FakeWebChannel()
    reload_started = asyncio.Event()
    release_reload = asyncio.Event()
    persisted: list[list[dict]] = []
    reload_options_seen: list[dict] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"models": {"defaults": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_default_models",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_default_models_in_config",
        lambda models: persisted.append(list(models)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.registry.ExtensionRegistry.get_instance",
        lambda: type(
            "Registry",
            (),
            {"get_crypto_provider": lambda self: type("Crypto", (), {"encrypt": lambda self, value: value})()},
        )(),
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        reload_options_seen.append(dict(reload_options))
        reload_started.set()
        await release_reload.wait()
        return True

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            on_config_saved=on_config_saved,
        )
    )

    task = asyncio.create_task(channel.methods["models.replace_all"](
        object(),
        "req-models",
        {
            "models": [
                {
                    "model_name": "model-one",
                    "api_base": "https://example.com/v1",
                    "api_key": "secret",
                    "model_provider": "OpenAI",
                    "is_default": True,
                }
            ]
        },
        "sess-1",
    ))

    await asyncio.wait_for(reload_started.wait(), timeout=1)
    assert channel.responses == []

    release_reload.set()
    await task

    assert persisted
    assert reload_options_seen[-1]["target_channel_id"] == "web"
    assert reload_options_seen[-1]["reload_scopes"] == ["model"]
    assert channel.responses[-1]["id"] == "req-models"
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["applied_without_restart"] is True


@pytest.mark.asyncio
async def test_config_set_routes_team_payload_to_modes_team_helper(monkeypatch):
    channel = FakeWebChannel()
    recorded: list[dict] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_teams_in_config",
        lambda payload: recorded.append(payload),
    )

    await channel.methods["config.set"](
        object(),
        "req-1",
        {
            "agents": {"agent_1": {"model": {"provider": "OpenAI"}}},
            "team": [{"team_name": "alpha_team", "leader": {"agent_key": "agent_1"}}],
        },
        "sess-1",
    )

    assert recorded and recorded[0]["team"][0]["team_name"] == "alpha_team"
    assert channel.responses[-1] == {
        "id": "req-1",
        "ok": True,
        "payload": {"updated": ["modes.team"], "applied_without_restart": True},
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_config_set_installs_codex_dependency_before_team_save(monkeypatch):
    channel = FakeWebChannel()
    dependency_checks: list[set[str]] = []
    recorded: list[dict] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers."
        "_ensure_external_cli_dependencies_available_or_start_install",
        lambda cli_agents: dependency_checks.append(cli_agents) or {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_teams_in_config",
        lambda payload: recorded.append(payload),
    )

    await channel.methods["config.set"](
        object(),
        "req-codex",
        {
            "agents": {"agent_1": {"model": {"provider": "OpenAI"}}},
            "team": [{
                "team_name": "alpha_team",
                "external_cli_agents": [{"cli_agent": "codex"}],
                "leader": {"agent_key": "agent_1"},
            }],
        },
        "sess-codex",
    )

    assert dependency_checks == [{"codex"}]
    assert recorded and recorded[0]["team"][0]["external_cli_agents"] == [{"cli_agent": "codex"}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_installs_claude_dependency_for_claude_only(monkeypatch):
    channel = FakeWebChannel()
    dependency_checks: list[set[str]] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers."
        "_ensure_external_cli_dependencies_available_or_start_install",
        lambda cli_agents: dependency_checks.append(cli_agents) or {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_teams_in_config",
        lambda payload: None,
    )

    await channel.methods["config.set"](
        object(),
        "req-claude",
        {
            "agents": {"agent_1": {"model": {"provider": "OpenAI"}}},
            "team": [{
                "team_name": "alpha_team",
                "external_cli_agents": [{"cli_agent": "claude"}],
                "leader": {"agent_key": "agent_1"},
            }],
        },
        "sess-claude",
    )

    assert dependency_checks == [{"claude"}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_updates_external_cli_switches_without_team_save(monkeypatch):
    channel = FakeWebChannel()
    dependency_checks: list[set[str]] = []
    updates: list[tuple[list[str], str | None]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers."
        "_ensure_external_cli_dependencies_available_or_start_install",
        lambda cli_agents: dependency_checks.append(cli_agents) or {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_teams_in_config",
        lambda payload: pytest.fail("external CLI switches must not save full team payload"),
    )

    await channel.methods["config.set"](
        object(),
        "req-external-cli",
        {
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "true",
        },
        "sess-external-cli",
    )

    assert dependency_checks == [{"codex"}]
    assert updates == [([{"cli_agent": "codex"}], "ws://127.0.0.1:19000/ws")]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_saves_external_cli_path_after_detection(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._detect_external_cli_agent",
        lambda cli_agent, cli_path="": {
            "cli_agent": cli_agent,
            "status": "ok",
            "path": f"/resolved/{cli_agent}",
            "version": "1.2.3",
            "reference_version": "1.2.3",
            "message": "",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers."
        "_ensure_external_cli_dependencies_available_or_start_install",
        lambda cli_agents: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )

    await channel.methods["config.set"](
        object(),
        "req-external-cli-path",
        {
            "external_cli_agent_claude_enabled": "true",
            "external_cli_agent_claude_cli_path": "/custom/claude",
        },
        "sess-external-cli-path",
    )

    assert updates == [([{"cli_agent": "claude", "cli_path": "/resolved/claude"}], "ws://127.0.0.1:19000/ws")]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_uses_builtin_codex_without_validating_stale_windows_script(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {
            "preferred_language": "zh",
            "modes": {
                "team": {
                    "jiuwen_team": {
                        "external_cli_agents": [
                            {"cli_agent": "codex", "cli_path": "C:/Users/admin/AppData/Roaming/npm/codex.cmd"},
                        ],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._detect_external_cli_agent",
        lambda cli_agent, cli_path="": pytest.fail("built-in mode must not validate cli_path"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers."
        "_ensure_external_cli_dependencies_available_or_start_install",
        lambda cli_agents: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )

    await channel.methods["config.set"](
        object(),
        "req-codex-builtin-stale-script",
        {
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "true",
        },
        "sess-codex-builtin-stale-script",
    )

    assert updates == [([{"cli_agent": "codex"}], "ws://127.0.0.1:19000/ws")]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_rejects_codex_windows_script_when_manual_path(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._detect_external_cli_agent",
        lambda cli_agent, cli_path="": {
            "cli_agent": cli_agent,
            "status": "unsupported",
            "path": cli_path,
            "reason": "windows_script",
            "message": "windows_script",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )

    await channel.methods["config.set"](
        object(),
        "req-codex-manual-script",
        {
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "false",
            "external_cli_agent_codex_cli_path": "C:/Users/admin/AppData/Roaming/npm/codex.cmd",
        },
        "sess-codex-manual-script",
    )

    assert updates == []
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"
    assert "codex cli_path is not available" in channel.responses[-1]["error"]


@pytest.mark.asyncio
async def test_config_set_rejects_unavailable_external_cli_path(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._detect_external_cli_agent",
        lambda cli_agent, cli_path="": {
            "cli_agent": cli_agent,
            "status": "missing",
            "path": "",
            "message": "not found",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )

    await channel.methods["config.set"](
        object(),
        "req-external-cli-path-missing",
        {
            "external_cli_agent_claude_enabled": "true",
            "external_cli_agent_claude_cli_path": "/missing/claude",
        },
        "sess-external-cli-path-missing",
    )

    assert updates == []
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"
    assert "claude cli_path is not available" in channel.responses[-1]["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cli_agent", ["claude", "codex"])
async def test_config_set_starts_external_cli_dependency_install_without_saving_agent(monkeypatch, cli_agent: str):
    channel = FakeWebChannel()
    updates: list[tuple[list[str], str | None]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers."
        "_ensure_external_cli_dependencies_available_or_start_install",
        lambda cli_agents: {
            cli_agent: {"status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0}
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )

    await channel.methods["config.set"](
        object(),
        f"req-{cli_agent}-installing",
        {
            f"external_cli_agent_{cli_agent}_enabled": "true",
            f"external_cli_agent_{cli_agent}_use_builtin": "true",
        },
        f"sess-{cli_agent}-installing",
    )

    assert updates == [([], "ws://127.0.0.1:19000/ws")]
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["external_cli_dependency_installs"][cli_agent]["status"] == "running"


@pytest.mark.asyncio
async def test_config_set_saves_claude_while_codex_dependency_is_installing(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[str], str | None]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers."
        "_ensure_external_cli_dependencies_available_or_start_install",
        lambda cli_agents: {
            "codex": {"status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0}
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )

    await channel.methods["config.set"](
        object(),
        "req-claude-codex-installing",
        {
            "external_cli_agent_claude_enabled": "true",
            "external_cli_agent_claude_use_builtin": "true",
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "true",
        },
        "sess-claude-codex-installing",
    )

    assert updates == [([{"cli_agent": "claude"}], "ws://127.0.0.1:19000/ws")]
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["external_cli_dependency_installs"]["codex"]["status"] == "running"


def test_build_external_cli_publish_url_uses_web_channel_env(monkeypatch):
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("WEB_PORT", "29100")

    assert _build_external_cli_publish_url() == "ws://127.0.0.1:29100/ws"


def test_inject_external_cli_publish_url_only_for_codex_team(monkeypatch):
    monkeypatch.setenv("WEB_PORT", "29100")
    payload = {
        "team": [
            {"team_name": "alpha", "external_cli_agents": [{"cli_agent": "claude"}]},
            {"team_name": "beta", "external_cli_agents": [{"cli_agent": "codex"}]},
        ],
    }

    injected = _inject_external_cli_publish_url(payload)

    assert "external_cli_publish_url" not in injected["team"][0]
    assert injected["team"][1]["external_cli_publish_url"] == "ws://127.0.0.1:29100/ws"
    assert "external_cli_publish_url" not in payload["team"][1]


def test_codex_dependency_install_is_not_started_twice(monkeypatch):
    install_started = threading.Event()
    release_install = threading.Event()
    install_calls: list[None] = []

    with app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS["codex"]:
        app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS["codex"].update({
            "status": "idle",
            "error": "",
            "started_at": 0.0,
            "finished_at": 0.0,
        })

    original_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.importlib.util.find_spec",
        lambda name: None if name == "openai_codex" else original_find_spec(name),
    )

    def install_once() -> None:
        install_calls.append(None)
        install_started.set()
        release_install.wait(timeout=5)

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._install_external_cli_dependency",
        lambda cli_agent: install_once(),
    )

    first = app_web_handlers._ensure_external_cli_dependency_available_or_start_install("codex")
    assert install_started.wait(timeout=5)
    second = app_web_handlers._ensure_external_cli_dependency_available_or_start_install("codex")
    release_install.set()

    assert first and first["status"] == "running"
    assert second and second["status"] == "running"
    assert len(install_calls) == 1


def test_codex_dependency_install_is_not_started_in_frozen_desktop(monkeypatch):
    with app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS["codex"]:
        app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS["codex"].update({
            "status": "idle",
            "phase": "idle",
            "error": "",
            "last_log": "",
            "log_tail": [],
            "started_at": 0.0,
            "finished_at": 0.0,
        })

    original_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.importlib.util.find_spec",
        lambda name: None if name == "openai_codex" else original_find_spec(name),
    )
    monkeypatch.setattr(app_web_handlers.sys, "frozen", True, raising=False)

    def fail_thread_start(*args: object, **kwargs: object) -> None:
        raise AssertionError("frozen desktop must not start a pip install thread")

    monkeypatch.setattr(app_web_handlers.threading, "Thread", fail_thread_start)

    snapshot = app_web_handlers._ensure_external_cli_dependency_available_or_start_install("codex")

    assert snapshot and snapshot["status"] == "failed"
    assert snapshot["phase"] == "failed"
    assert "desktop package does not include Codex support" in snapshot["error"]
    assert snapshot["log_tail"] == []


@pytest.mark.asyncio
async def test_external_cli_install_status_returns_snapshot():
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))
    with app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS["codex"]:
        app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS["codex"].update({
            "status": "running",
            "phase": "installing",
            "error": "",
            "last_log": "Collecting openjiuwen",
            "log_tail": ["Collecting openjiuwen"],
            "started_at": 1.0,
            "finished_at": 0.0,
            "updated_at": 2.0,
        })

    await channel.methods["external_cli.install_status"](
        object(),
        "req-codex-install-status",
        {"cli_agent": "codex"},
        "sess-codex-install-status",
    )

    payload = channel.responses[-1]["payload"]
    assert channel.responses[-1]["ok"] is True
    assert payload["cli_agent"] == "codex"
    assert payload["status"] == "running"
    assert payload["phase"] == "installing"
    assert payload["log_tail"] == ["Collecting openjiuwen"]
    assert payload["log_tail"] is not app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUS["codex"]["log_tail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "false"])
async def test_config_set_updates_canonical_skill_evolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    value: str,
) -> None:
    channel = FakeWebChannel()
    saved_updates: list[dict] = []
    evolution_updates: list[bool] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ENV_FILE",
        tmp_path / ".env",
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"react": {"evolution": {"skill_evolution": value == "true"}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_skill_evolution_enabled_in_config",
        lambda enabled: evolution_updates.append(enabled),
    )

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            on_config_saved=lambda _, **kwargs: (
                saved_updates.append(kwargs) or True
            ),
        )
    )

    await channel.methods["config.set"](
        object(),
        "req-evolution",
        {"skill_evolution": value},
        "sess-evolution",
    )

    assert evolution_updates == [value == "true"]
    assert saved_updates and saved_updates[0]["env_updates"] == {}
    assert saved_updates[0]["config_payload"]["react"]["evolution"]["skill_evolution"] is (value == "true")
    assert channel.responses[-1]["payload"] == {
        "updated": ["skill_evolution"],
        "applied_without_restart": True,
    }


@pytest.mark.asyncio
async def test_config_set_preserves_deleted_template_for_bound_team(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    channel = FakeWebChannel()
    recorded: list[dict] = []
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    binding_store.create(team_name="research_team", template_id="beta")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    current_config = {
        "preferred_language": "zh",
        "modes": {
            "team": {
                "alpha": {"team_name": "alpha", "leader": {"member_name": "alpha_leader"}},
                "beta": {"team_name": "beta", "leader": {"member_name": "beta_leader"}},
            }
        },
    }

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: current_config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_teams_in_config",
        lambda payload: recorded.append(payload),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    await channel.methods["config.set"](
        object(),
        "req-preserve",
        {
            "agents": {"agent_1": {"model": {"provider": "OpenAI"}}},
            "team": [{"team_name": "alpha", "leader": {"agent_key": "agent_1"}}],
        },
        "sess-1",
    )

    entity = entity_store.get("research_team")
    assert recorded and recorded[0]["team"][0]["team_name"] == "alpha"
    assert entity is not None
    assert entity.template_id == "beta"
    assert entity.template_snapshot["leader"]["member_name"] == "beta_leader"
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_returns_bad_request_when_team_payload_is_invalid(monkeypatch):
    channel = FakeWebChannel()

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_teams_in_config",
        lambda payload: (_ for _ in ()).throw(ValueError("duplicate team_name: alpha_team")),
    )

    await channel.methods["config.set"](
        object(),
        "req-2",
        {
            "agents": {"agent_1": {"model": {"provider": "OpenAI"}}},
            "team": [{"team_name": "alpha_team", "leader": {"agent_key": "agent_1"}}],
        },
        "sess-2",
    )

    assert channel.responses[-1] == {
        "id": "req-2",
        "ok": False,
        "payload": None,
        "error": "duplicate team_name: alpha_team",
        "code": "BAD_REQUEST",
    }


def test_config_panel_flatten_reads_standalone_agent_registry():
    raw = {
        "web_config_panel": {
            "agent_team_agents": {
                "agent_1": {
                    "model": {
                        "model_request_config": {
                            "model": "gpt-4.1",
                            "api_base": "https://api.openai.com/v1",
                            "api_key": "${OPENAI_API_KEY}",
                        },
                        "model_client_config": {"client_provider": "OpenAI"},
                    },
                    "skills": ["coding"],
                    "max_iterations": 12,
                    "completion_timeout": 34,
                }
            }
        }
    }

    flat = _flatten_modes_team_for_config_panel(raw)

    assert flat["agent_name_0"] == "agent_1"
    assert flat["agent_model_0"] == "gpt-4.1"
    assert flat["agent_skills_0"] == "coding"
    assert flat["agent_max_iterations_0"] == "12"
    assert flat["agent_completion_timeout_0"] == "34"


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        (True, "true"),
        (False, "false"),
    ],
)
def test_config_panel_flatten_reads_team_enable_permissions(enabled: bool, expected: str) -> None:
    raw = {
        "modes": {
            "team": {
                "alpha_team": {
                    "team_name": "alpha_team",
                    "enable_permissions": enabled,
                },
            },
        },
    }

    flat = _flatten_modes_team_for_config_panel(raw)

    assert flat["team_0_enable_permissions"] == expected


def test_config_panel_flatten_reads_external_cli_agents() -> None:
    raw = {
        "modes": {
            "team": {
                "alpha_team": {
                    "team_name": "alpha_team",
                    "external_cli_agents": [
                        {"cli_agent": "claude"},
                        {"cli_agent": "codex"},
                    ],
                },
            },
        },
    }

    flat = _flatten_modes_team_for_config_panel(raw)

    assert json.loads(flat["team_0_external_cli_agents"]) == [
        {"cli_agent": "claude"},
        {"cli_agent": "codex"},
    ]


def test_config_panel_flatten_reads_default_team_external_cli_switches() -> None:
    raw = {
        "modes": {
            "team": {
                "jiuwen_team": {
                    "external_cli_agents": [
                        {"cli_agent": "claude"},
                        {"cli_agent": "codex"},
                    ],
                },
            },
        },
    }

    flat = _flatten_external_cli_agents_for_config_panel(raw)

    assert flat["external_cli_agent_claude_enabled"] == "true"
    assert flat["external_cli_agent_claude_use_builtin"] == "true"
    assert flat["external_cli_agent_claude_cli_path"] == ""
    assert flat["external_cli_agent_codex_enabled"] == "true"
    assert flat["external_cli_agent_codex_use_builtin"] == "true"
    assert flat["external_cli_agent_codex_cli_path"] == ""


def test_config_panel_flatten_reads_external_cli_paths() -> None:
    raw = {
        "modes": {
            "team": {
                "jiuwen_team": {
                    "external_cli_agents": [
                        {"cli_agent": "claude", "cli_path": "/opt/claude"},
                        {"cli_agent": "codex", "cli_path": "/opt/codex"},
                    ],
                },
            },
        },
    }

    flat = _flatten_external_cli_agents_for_config_panel(raw)

    assert flat["external_cli_agent_claude_enabled"] == "true"
    assert flat["external_cli_agent_claude_use_builtin"] == "false"
    assert flat["external_cli_agent_claude_cli_path"] == "/opt/claude"
    assert flat["external_cli_agent_codex_enabled"] == "true"
    assert flat["external_cli_agent_codex_use_builtin"] == "false"
    assert flat["external_cli_agent_codex_cli_path"] == "/opt/codex"


def test_detect_external_cli_agent_rejects_windows_script_path(monkeypatch, tmp_path) -> None:
    script_path = tmp_path / "claude.cmd"
    script_path.write_text("@echo off\n", encoding="utf-8")

    monkeypatch.setattr(app_web_handlers, "_is_windows_platform", lambda: True)

    result = _detect_external_cli_agent("claude", str(script_path))

    assert result["status"] == "unsupported"
    assert result["path"] == str(script_path)


def test_config_panel_flatten_reads_symphony_enabled_and_skill_retrieval():
    raw = {
        "symphony": {
            "enabled": True,
            "evolution": {"enabled": False},
            "orchestration": {"mode": "fast"},
            "skill_retrieval": {
                "enabled": True,
                "build": {"branching_factor": 64},
                "retrieve": {"top_k": 5, "flatten_tree": True},
            },
        }
    }

    flat = _flatten_symphony_for_config_panel(raw)

    assert flat["symphony_enabled"] == "true"
    assert "symphony_dynamic_graph_enabled" not in flat
    assert "symphony_orchestration_mode" not in flat
    assert flat["skill_retrieval_enabled"] == "true"
    assert flat["skill_retrieval_build_branching_factor"] == "64"
    assert "skill_retrieval_retrieve_top_k" not in flat
    assert flat["skill_retrieval_retrieve_flatten_tree"] == "true"


@pytest.mark.asyncio
async def test_config_set_routes_symphony_payload_to_config_helper(monkeypatch):
    channel = FakeWebChannel()
    recorded_symphony: list[dict] = []
    recorded_skill_retrieval: list[dict] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
        lambda: {"symphony": {}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_symphony_in_config",
        lambda updates: recorded_symphony.append(updates),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_skill_retrieval_in_config",
        lambda updates: recorded_skill_retrieval.append(updates),
    )

    await channel.methods["config.set"](
        object(),
        "req-3",
        {
            "symphony_enabled": "true",
            "symphony_dynamic_graph_enabled": "false",
            "skill_retrieval_enabled": "false",
            "skill_retrieval_retrieve_flatten_tree": "true",
        },
        "sess-3",
    )

    assert recorded_symphony == [{"enabled": True}]
    assert recorded_skill_retrieval == [{"enabled": False, "retrieve": {"flatten_tree": True}}]
    assert channel.responses[-1] == {
        "id": "req-3",
        "ok": True,
        "payload": {
            "updated": [
                "symphony_enabled",
                "skill_retrieval_enabled",
                "skill_retrieval_retrieve_flatten_tree",
            ],
            "applied_without_restart": True,
        },
        "error": None,
        "code": None,
    }


def test_web_exposes_graph_methods_and_rejects_legacy_symphony_methods():
    skill_graph_methods = {
        "skills.graph.build",
        "skills.graph.status",
        "skills.graph.get",
        "skills.graph.cancel",
    }
    assert skill_graph_methods.issubset(app_web_handlers._FORWARD_REQ_METHODS)

    legacy_symphony_methods = {
        "symphony.build_score",
        "symphony.pause_build",
        "symphony.score_status",
        "symphony.graph",
        "symphony.plan",
        "symphony.evolution_status",
        "symphony.evolution_record_outcome",
        "symphony.evolution_rebuild",
    }
    assert legacy_symphony_methods.isdisjoint(app_web_handlers._FORWARD_REQ_METHODS)


# =====================================================================
# _normalize_feishu_conf 纯函数测试
# =====================================================================


def test_normalize_feishu_conf_empty():
    """空配置 → 返回含单默认应用的 apps 列表。"""
    result = _normalize_feishu_conf({})
    assert "apps" in result
    assert len(result["apps"]) == 1
    app = result["apps"][0]
    assert app["name"] == "默认应用"
    assert app["is_default"] is True
    assert app["enabled"] is True
    assert app["allow_from"] == ["0.0.0.0/0"]


def test_normalize_feishu_conf_non_dict():
    """非 dict 输入 → 返回 {"apps": []}。"""
    assert _normalize_feishu_conf(None) == {"apps": []}
    assert _normalize_feishu_conf("") == {"apps": []}
    assert _normalize_feishu_conf([]) == {"apps": []}


def test_normalize_feishu_conf_flat_to_apps():
    """旧平铺格式 → 转为 apps 格式，补充缺省字段。"""
    raw = {
        "enabled": True,
        "app_id": "cli_xxx",
        "app_secret": "my_secret",
        "encrypt_key": "enc_key",
        "verification_token": "verify_token",
    }
    result = _normalize_feishu_conf(raw)
    assert "apps" in result
    assert len(result["apps"]) == 1
    app = result["apps"][0]
    assert app["is_default"] is True
    assert app["app_id"] == "cli_xxx"
    assert app["app_secret"] == "my_secret"
    assert app["allow_from"] == ["0.0.0.0/0"]
    assert app["enable_streaming"] is True
    assert app["group_digital_avatar"] is False
    # 原始平铺字段仍保留在顶层
    assert result["app_id"] == "cli_xxx"


def test_normalize_feishu_conf_apps_fills_defaults():
    """已有 apps 列表 → 为每个 app 补充缺省字段。"""
    raw = {
        "apps": [
            {
                "name": "默认应用",
                "is_default": True,
                "app_id": "cli_xxx",
                "app_secret": "xxx",
                "encrypt_key": "key",
                "verification_token": "token",
            },
            {
                "name": "业务应用",
                "is_default": False,
                "app_id": "cli_yyy",
                "app_secret": "yyy",
                "encrypt_key": "key2",
                "verification_token": "token2",
            },
        ]
    }
    result = _normalize_feishu_conf(raw)
    assert len(result["apps"]) == 2

    # 第一个 app：缺省字段被填充
    app0 = result["apps"][0]
    assert app0["name"] == "默认应用"
    assert app0["enable_streaming"] is True
    assert app0["group_digital_avatar"] is False
    assert app0["allow_from"] == ["0.0.0.0/0"]

    # 第二个 app：同样补全
    app1 = result["apps"][1]
    assert app1["name"] == "业务应用"
    assert app1["enable_streaming"] is True


def test_normalize_feishu_conf_apps_empty_list():
    """空 apps 列表 → 返回 {"apps": []}。"""
    result = _normalize_feishu_conf({"apps": []})
    assert result == {"apps": []}


def test_normalize_feishu_conf_apps_preserves_extra_fields():
    """apps 中额外非标准字段应被保留（未来扩展）。"""
    raw = {"apps": [{"name": "test", "is_default": True, "app_id": "x", "app_secret": "x",
                     "encrypt_key": "x", "verification_token": "x", "custom_tag": "hello"}]}
    result = _normalize_feishu_conf(raw)
    assert result["apps"][0]["custom_tag"] == "hello"
    assert result["apps"][0]["enable_streaming"] is True  # 默认值仍在


# =====================================================================
# _normalize_xiaoyi_conf 纯函数测试
# =====================================================================


def test_normalize_xiaoyi_conf_empty():
    """空配置 → 返回含单默认应用的 apps 列表。"""
    result = _normalize_xiaoyi_conf({})
    assert "apps" in result
    assert len(result["apps"]) == 1
    app = result["apps"][0]
    assert app["name"] == "默认应用"
    assert app["is_default"] is True
    assert app["mode"] == "xiaoyi_channel"
    assert app["ws_url1"] == "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"


def test_normalize_xiaoyi_conf_non_dict():
    """非 dict 输入 → 返回 {"apps": []}。"""
    assert _normalize_xiaoyi_conf(None) == {"apps": []}
    assert _normalize_xiaoyi_conf(42) == {"apps": []}


def test_normalize_xiaoyi_conf_flat_to_apps():
    """旧平铺格式 → 转为 apps 格式，补充缺省字段。"""
    raw = {
        "enabled": True,
        "ak": "access_key",
        "sk": "secret_key",
        "agent_id": "agent_default",
        "app_id": "app_xxx",
    }
    result = _normalize_xiaoyi_conf(raw)
    assert "apps" in result
    assert len(result["apps"]) == 1
    app = result["apps"][0]
    assert app["is_default"] is True
    assert app["ak"] == "access_key"
    assert app["sk"] == "secret_key"
    assert app["agent_id"] == "agent_default"
    assert app["mode"] == "xiaoyi_channel"
    assert app["phone_tools_enabled"] is False


def test_normalize_xiaoyi_conf_apps_fills_defaults():
    """已有 apps 列表 → 为每个 app 补充缺省字段。"""
    raw = {
        "apps": [
            {
                "name": "默认应用",
                "is_default": True,
                "ak": "ak_1",
                "sk": "sk_1",
                "app_id": "app_1",
                "agent_id": "agent_1",
            }
        ]
    }
    result = _normalize_xiaoyi_conf(raw)
    assert len(result["apps"]) == 1
    app = result["apps"][0]
    assert app["mode"] == "xiaoyi_channel"
    assert app["enable_streaming"] is True
    assert app["phone_tools_enabled"] is False
    assert app["push_id"] == ""
    assert app["ws_url1"] == "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"


def test_normalize_xiaoyi_conf_apps_empty_list():
    """空 apps 列表 → 返回 {"apps": []}。"""
    assert _normalize_xiaoyi_conf({"apps": []}) == {"apps": []}


# =====================================================================
# get_conf 处理程序 — 验证归一化在读取时生效
# =====================================================================


@pytest.mark.asyncio
async def test_channel_feishu_get_conf_normalizes(monkeypatch):
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    # 预置旧平铺配置
    cm.configs["feishu"] = {"app_id": "old_id", "app_secret": "old_secret"}
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.feishu.get_conf"](object(), "req-1", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    payload = channel.responses[-1]["payload"]
    assert "config" in payload
    assert "apps" in payload["config"]
    assert len(payload["config"]["apps"]) == 1
    app = payload["config"]["apps"][0]
    assert app["app_id"] == "old_id"
    assert app["app_secret"] == "old_secret"
    # 验证归一化补充了缺省字段
    assert app["allow_from"] == ["0.0.0.0/0"]
    assert app["enable_streaming"] is True
    assert app["is_default"] is True


@pytest.mark.asyncio
async def test_channel_feishu_get_conf_empty_returns_default_apps(monkeypatch):
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.feishu.get_conf"](object(), "req-1", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    payload = channel.responses[-1]["payload"]
    assert "apps" in payload["config"]
    # 空配置 → 返回一个默认应用
    assert len(payload["config"]["apps"]) == 1
    assert payload["config"]["apps"][0]["is_default"] is True


@pytest.mark.asyncio
async def test_channel_xiaoyi_get_conf_normalizes(monkeypatch):
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    cm.configs["xiaoyi"] = {"ak": "ak_1", "sk": "sk_1", "agent_id": "agent_1"}
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.xiaoyi.get_conf"](object(), "req-1", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    payload = channel.responses[-1]["payload"]
    assert "config" in payload
    assert "apps" in payload["config"]
    app = payload["config"]["apps"][0]
    assert app["ak"] == "ak_1"
    assert app["mode"] == "xiaoyi_channel"


@pytest.mark.asyncio
async def test_channel_xiaoyi_get_conf_empty_returns_default_apps(monkeypatch):
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.xiaoyi.get_conf"](object(), "req-1", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    assert len(channel.responses[-1]["payload"]["config"]["apps"]) == 1


# =====================================================================
# set_conf 处理程序 — 多应用模式（apps 键）
# =====================================================================


@pytest.mark.asyncio
async def test_channel_feishu_set_conf_apps_mode(monkeypatch):
    """feishu.set_conf 带 apps → 写 channels.feishu.apps，返回归一化配置。"""
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    recorded_subsection: list[tuple] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        lambda cid, sub, conf, keep_keys: recorded_subsection.append((cid, sub, conf, keep_keys)),
    )

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    apps_payload = [
        {"name": "应用A", "is_default": True, "app_id": "cli_a",
         "app_secret": "sec_a", "encrypt_key": "key_a", "verification_token": "token_a"},
        {"name": "应用B", "is_default": False, "app_id": "cli_b",
         "app_secret": "sec_b", "encrypt_key": "key_b", "verification_token": "token_b"},
    ]
    await channel.methods["channel.feishu.set_conf"](
        object(), "req-apps", {"apps": apps_payload}, "sess-1"
    )

    # 验证写入了归一化后的 subsection（默认字段已填充）
    assert len(recorded_subsection) == 1
    assert recorded_subsection[0][0] == "feishu"
    assert recorded_subsection[0][1] == "apps"
    written_apps = recorded_subsection[0][2]
    assert len(written_apps) == 2
    assert written_apps[0]["name"] == "应用A"
    assert written_apps[0]["app_id"] == "cli_a"
    assert written_apps[0]["enabled"] is True  # 默认值已补充
    assert written_apps[0]["allow_from"] == ["0.0.0.0/0"]  # 默认值已补充
    assert written_apps[1]["name"] == "应用B"

    # 验证 cm 中存储了归一化后的 apps
    assert "apps" in cm.configs.get("feishu", {})
    cm_apps = cm.configs["feishu"]["apps"]
    assert len(cm_apps) == 2

    # 验证响应包含归一化后的完整配置
    assert channel.responses[-1]["ok"] is True
    config = channel.responses[-1]["payload"]["config"]
    assert len(config["apps"]) == 2
    # 缺省字段已被填充
    assert config["apps"][0]["allow_from"] == ["0.0.0.0/0"]
    assert config["apps"][0]["enable_streaming"] is True


@pytest.mark.asyncio
async def test_channel_xiaoyi_set_conf_apps_mode(monkeypatch):
    """xiaoyi.set_conf 带 apps → 写 channels.xiaoyi.apps，返回归一化配置。"""
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    recorded_subsection: list[tuple] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        lambda cid, sub, conf, keep_keys: recorded_subsection.append((cid, sub, conf, keep_keys)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._clear_agent_config_cache",
        lambda *a, **kw: None,
    )

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    apps_payload = [
        {"name": "默认应用", "is_default": True, "ak": "ak_1", "sk": "sk_1",
         "app_id": "app_1", "agent_id": "agent_1"},
    ]
    await channel.methods["channel.xiaoyi.set_conf"](
        object(), "req-apps", {"apps": apps_payload}, "sess-1"
    )

    assert len(recorded_subsection) == 1
    assert recorded_subsection[0][0] == "xiaoyi"
    assert recorded_subsection[0][1] == "apps"
    # 验证持久化的数据已归一化（默认字段被填充）
    written_apps = recorded_subsection[0][2]
    assert len(written_apps) == 1
    assert written_apps[0]["name"] == "默认应用"
    assert written_apps[0]["ak"] == "ak_1"
    assert written_apps[0]["mode"] == "xiaoyi_channel"  # 默认值已补充
    assert written_apps[0]["phone_tools_enabled"] is False  # 默认值已补充
    assert written_apps[0]["ws_url1"] == "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"  # 默认值已补充
    assert written_apps[0]["enable_streaming"] is True  # 默认值已补充

    assert channel.responses[-1]["ok"] is True
    config = channel.responses[-1]["payload"]["config"]
    assert len(config["apps"]) == 1
    # 缺省字段被填充
    assert config["apps"][0]["mode"] == "xiaoyi_channel"
    assert config["apps"][0]["phone_tools_enabled"] is False

# =====================================================================
# set_conf 处理程序 — 边界场景
# =====================================================================


@pytest.mark.asyncio
async def test_channel_feishu_set_conf_apps_empty_list(monkeypatch):
    """空 apps 列表 → 保存并返回空列表。"""
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    recorded_subsection: list[tuple] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        lambda cid, sub, conf, keep_keys: recorded_subsection.append((cid, sub, conf, keep_keys)),
    )

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.feishu.set_conf"](
        object(), "req-empty", {"apps": []}, "sess-1"
    )

    assert recorded_subsection[0][2] == []
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["config"]["apps"] == []


@pytest.mark.asyncio
async def test_channel_set_conf_channel_manager_unavailable(monkeypatch):
    """cm 为 None → 返回 SERVICE_UNAVAILABLE。"""
    channel = FakeWebChannel()
    # 不传 channel_manager，_resolve 返回 None
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["channel.feishu.set_conf"](object(), "req-1", {"apps": []}, "sess-1")
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_channel_set_conf_invalid_params():
    """params 非 dict → 返回 BAD_REQUEST。"""
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=FakeChannelManager()))

    for invalid in [None, "string", 123, []]:
        channel.responses.clear()
        await channel.methods["channel.feishu.set_conf"](object(), "req-1", invalid, "sess-1")
        assert channel.responses[-1]["ok"] is False
        assert channel.responses[-1]["code"] == "BAD_REQUEST"


# =====================================================================
# 落盘测试 — 验证 update_channel_subsection_in_config 真实写回文件
# =====================================================================


# =====================================================================
# 微信通道数值参数校验 — _validate_wechat_numeric_params + set_conf 拦截
# =====================================================================


@pytest.mark.parametrize(
    "params",
    [
        {"qrcode_poll_interval_sec": -1},           # 负数
        {"qrcode_poll_interval_sec": 0},            # 0
        {"qrcode_poll_interval_sec": 999999999},    # 极大值越上限
        {"long_poll_timeout_sec": 0},               # 0
        {"long_poll_timeout_sec": -5},              # 负数
        {"long_poll_timeout_sec": 45.5},            # 非整数
        {"long_poll_timeout_sec": 10000},           # 越上限
        {"backoff_base_sec": 0},                    # 0
        {"backoff_base_sec": -2.0},                 # 负数
        {"backoff_max_sec": 0},                     # 0
        {"backoff_max_sec": 1e12},                  # 极大值
        {"backoff_base_sec": 10, "backoff_max_sec": 5},  # max < base 跨字段
        {"qrcode_poll_interval_sec": "2"},          # 字符串（非数字类型）
        {"qrcode_poll_interval_sec": True},         # bool 不算数字
        {"qrcode_poll_interval_sec": float("inf")}, # 无穷
        {"qrcode_poll_interval_sec": float("nan")}, # NaN
    ],
)
def test_validate_wechat_numeric_params_rejects_invalid(params):
    assert _validate_wechat_numeric_params(params) is not None


@pytest.mark.parametrize(
    "params",
    [
        {},                                             # 无数值字段 → 交给默认值
        {"qrcode_poll_interval_sec": 2.0},
        {"qrcode_poll_interval_sec": 0.1},              # 下边界
        {"qrcode_poll_interval_sec": 3600},             # 上边界
        {"long_poll_timeout_sec": 1},                   # 下边界
        {"long_poll_timeout_sec": 600},                 # 上边界
        {"long_poll_timeout_sec": 45.0},                # 整数值的 float
        {"backoff_base_sec": 1.0, "backoff_max_sec": 30.0},
        {"backoff_base_sec": 5, "backoff_max_sec": 5},  # 相等允许
    ],
)
def test_validate_wechat_numeric_params_accepts_valid(params):
    assert _validate_wechat_numeric_params(params) is None


@pytest.mark.asyncio
async def test_channel_wechat_set_conf_rejects_invalid_numeric():
    """非法数值 → 返回 BAD_REQUEST，且不写入 channel manager（不落盘）。"""
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.wechat.set_conf"](
        object(), "req-bad", {"enabled": True, "backoff_base_sec": -1}, "sess-1"
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"
    assert "wechat" not in cm.configs


@pytest.mark.asyncio
async def test_channel_wechat_set_conf_accepts_valid_numeric(monkeypatch):
    """合法数值 → 保存成功并落入 channel manager。"""
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_channel_in_config",
        lambda channel_id, conf: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._clear_agent_config_cache",
        lambda *a, **kw: None,
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    params = {
        "enabled": True,
        "qrcode_poll_interval_sec": 2.0,
        "long_poll_timeout_sec": 45,
        "backoff_base_sec": 1.0,
        "backoff_max_sec": 30.0,
    }
    await channel.methods["channel.wechat.set_conf"](object(), "req-ok", params, "sess-1")

    assert channel.responses[-1]["ok"] is True
    assert cm.configs.get("wechat", {}).get("backoff_max_sec") == 30.0


def test_update_channel_subsection_in_config_persists_to_disk(tmp_path, monkeypatch):
    """验证 update_channel_subsection_in_config 确实将数据写到 config.yaml 文件。"""
    import yaml
    from jiuwenswarm.common import config as cfg

    # 1. 准备一个临时的 config.yaml，包含已有内容
    temp_config = tmp_path / "config.yaml"
    initial_data = {
        "app_version": "1.0.0",
        "channels": {
            "web": {"enabled": True},
        },
    }
    with open(temp_config, "w", encoding="utf-8") as f:
        yaml.dump(initial_data, f)

    # 2. monkeypatch CONFIG_YAML_PATH 指向临时文件
    monkeypatch.setattr(cfg, "CONFIG_YAML_PATH", temp_config)

    # 3. 调用被测试函数——写入 feishu apps 配置
    feishu_apps = [
        {"name": "应用A", "is_default": True, "app_id": "cli_a", "app_secret": "sec_a"},
        {"name": "应用B", "is_default": False, "app_id": "cli_b", "app_secret": "sec_b"},
    ]
    cfg.update_channel_subsection_in_config("feishu", "apps", feishu_apps)

    # 4. 读回文件，验证数据已落盘
    with open(temp_config, "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f)

    # 4a. 验证 channels.feishu.apps 存在且内容正确
    assert "channels" in saved
    assert "feishu" in saved["channels"]
    assert "apps" in saved["channels"]["feishu"]
    assert len(saved["channels"]["feishu"]["apps"]) == 2
    assert saved["channels"]["feishu"]["apps"][0]["name"] == "应用A"
    assert saved["channels"]["feishu"]["apps"][0]["app_id"] == "cli_a"
    assert saved["channels"]["feishu"]["apps"][1]["name"] == "应用B"

    # 4b. 验证已有内容未被破坏（round-trip 安全）
    assert saved["app_version"] == "1.0.0"
    assert saved["channels"]["web"]["enabled"] is True


def test_update_channel_subsection_in_config_creates_missing_sections(tmp_path, monkeypatch):
    """当 channels / channel_id / subsection 不存在时，应自动创建。"""
    import yaml
    from jiuwenswarm.common import config as cfg

    temp_config = tmp_path / "config.yaml"
    # 只有顶层字段，没有任何 channels
    with open(temp_config, "w", encoding="utf-8") as f:
        yaml.dump({"app_version": "2.0.0"}, f)

    monkeypatch.setattr(cfg, "CONFIG_YAML_PATH", temp_config)

    cfg.update_channel_subsection_in_config("xiaoyi", "apps", [{"name": "默认应用", "ak": "ak_1"}])

    with open(temp_config, "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f)

    assert "channels" in saved
    assert "xiaoyi" in saved["channels"]
    assert "apps" in saved["channels"]["xiaoyi"]
    assert saved["channels"]["xiaoyi"]["apps"][0]["name"] == "默认应用"
    # 原始顶层字段保留
    assert saved["app_version"] == "2.0.0"


def test_update_channel_subsection_in_config_overwrites_existing(tmp_path, monkeypatch):
    """相同 subsection 多次写入应覆盖而不是追加。"""
    import yaml
    from jiuwenswarm.common import config as cfg

    temp_config = tmp_path / "config.yaml"
    initial_data = {
        "channels": {
            "feishu": {
                "apps": [{"name": "旧应用", "app_id": "old_id"}],
            },
        },
    }
    with open(temp_config, "w", encoding="utf-8") as f:
        yaml.dump(initial_data, f)

    monkeypatch.setattr(cfg, "CONFIG_YAML_PATH", temp_config)

    # 写入新数据覆盖
    new_apps = [{"name": "新应用", "app_id": "new_id"}]
    cfg.update_channel_subsection_in_config("feishu", "apps", new_apps)

    with open(temp_config, "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f)

    assert len(saved["channels"]["feishu"]["apps"]) == 1
    assert saved["channels"]["feishu"]["apps"][0]["name"] == "新应用"
    assert saved["channels"]["feishu"]["apps"][0]["app_id"] == "new_id"
class _McpFakeAgentClient:
    """Minimal agent_client fake: returns a canned AgentResponse."""

    def __init__(self, *, ok: bool, payload: dict):
        self._ok = ok
        self._payload = payload
        self.server_ready = True
        self.sent: list = []

    async def send_request(self, envelope):
        self.sent.append(envelope)
        return SimpleNamespace(ok=self._ok, payload=self._payload)


@pytest.mark.asyncio
async def test_connector_list_is_local_and_does_not_forward() -> None:
    """mcp.list is handled in the gateway (marketplace read) and never forwarded."""
    from jiuwenswarm.server.runtime.mcp.registry import list_marketplace_mcps

    channel = FakeWebChannel()
    agent_client = _McpFakeAgentClient(ok=True, payload={"type": "list", "items": []})

    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    assert "mcp.list" in channel.methods

    # If the marketplace catalog is empty the handler still returns an empty list
    # without touching the agent client; if non-empty, every item carries a name.
    # params={} → handler 兜底 filter=builtin。
    expected = list_marketplace_mcps(mcp_filter="builtin")
    await channel.methods["mcp.list"](
        object(), "req-conn-list", {}, "sess-1",
    )
    assert agent_client.sent == []
    resp = channel.responses[-1]
    assert resp["ok"] is True
    assert resp["payload"]["type"] == "list"
    assert resp["payload"]["items"] == expected


@pytest.mark.asyncio
async def test_connector_show_forwards_to_agent() -> None:
    """mcp.show forwards to AgentServer so tools are read from the live MCP
    connection in the agent process (the gateway process has no registered MCP,
    so a local handler would always see an empty ToolMgr and force a temp
    reconnect on every detail view). The agent's _handle_mcp_show owns both
    name validation and the tools lookup."""
    channel = FakeWebChannel()
    agent_client = _McpFakeAgentClient(ok=True, payload={"type": "detail", "item": {"name": "github"}})

    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    await channel.methods["mcp.show"](
        object(), "req-conn-show", {"name": "github"}, "sess-1",
    )
    # forwarded exactly once to the agent
    assert len(agent_client.sent) == 1
    resp = channel.responses[-1]
    assert resp["ok"] is True
    assert resp["payload"]["type"] == "detail"
    assert resp["payload"]["item"]["name"] == "github"


@pytest.mark.asyncio
async def test_connector_show_propagates_agent_not_found() -> None:
    """mcp.show surfaces the agent's not-found error code (name validation +
    lookup live in the agent handler, not the gateway)."""
    channel = FakeWebChannel()
    agent_client = _McpFakeAgentClient(
        ok=False, payload={"error": "mcp 'nope' not found", "code": "MCP_NOT_FOUND"}
    )

    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    await channel.methods["mcp.show"](
        object(), "req-conn-show", {"name": "nope"}, "sess-1",
    )
    assert len(agent_client.sent) == 1
    resp = channel.responses[-1]
    assert resp["ok"] is False
    assert resp["code"] == "MCP_NOT_FOUND"


@pytest.mark.asyncio
async def test_connector_forward_propagates_agent_error() -> None:
    """Forwarded methods (mcp.connect) surface AgentServer ok=False error/code."""
    channel = FakeWebChannel()
    agent_client = _McpFakeAgentClient(
        ok=False, payload={"error": "not found", "code": "MCP_NOT_FOUND"}
    )

    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    await channel.methods["mcp.connect"](
        object(), "req-conn-err", {"name": "nope"}, "sess-1",
    )
    assert len(agent_client.sent) == 1
    resp = channel.responses[-1]
    assert resp["ok"] is False
    assert resp["error"] == "not found"
    assert resp["code"] == "MCP_NOT_FOUND"


@pytest.mark.asyncio
async def test_connector_forward_reports_when_agent_not_ready() -> None:
    """Forwarded methods return AGENT_NOT_READY and skip the agent when not ready."""
    channel = FakeWebChannel()
    agent_client = _McpFakeAgentClient(ok=True, payload={})
    agent_client.server_ready = False

    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    await channel.methods["mcp.connect"](
        object(), "req-conn-nr", {"name": "feishu"}, "sess-1",
    )
    resp = channel.responses[-1]
    assert resp["ok"] is False
    assert resp["code"] == "AGENT_NOT_READY"
    assert agent_client.sent == []
