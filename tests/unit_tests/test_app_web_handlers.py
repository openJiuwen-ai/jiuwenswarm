# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.web import app_web_handlers
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _chat_interrupt_ack_payload,
    _flatten_modes_team_for_config_panel,
    _flatten_symphony_for_config_panel,
    _normalize_feishu_conf,
    _normalize_xiaoyi_conf,
    _register_web_handlers,
    _validate_wechat_numeric_params,
)


class FakeWebChannel:
    def __init__(self):
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []
        self.connect_handler = None

    def register_method(self, name, handler):
        self.methods[name] = handler

    def on_connect(self, handler):
        self.connect_handler = handler

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
async def test_path_set_reloads_config_and_resets_agent_browser_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeWebChannel()
    agent_client = object()
    saved_configs: list[dict] = []
    lifecycle_calls: list[tuple[str, object]] = []

    async def fake_update_browser(config):
        saved_configs.append(config)

    monkeypatch.setattr(
        app_web_handlers,
        "update_browser_in_config",
        fake_update_browser,
    )

    async def fake_clear(client):
        lifecycle_calls.append(("reload", client))

    async def fake_restart(client):
        lifecycle_calls.append(("restart", client))

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
        {"chrome_path": "C:\\Chrome\\chrome.exe", "headless": False}
    ]
    assert lifecycle_calls == [
        ("reload", agent_client),
        ("restart", agent_client),
    ]
    assert channel.responses[-1] == {
        "id": "req-path",
        "ok": True,
        "payload": {
            "chrome_path": "C:\\Chrome\\chrome.exe",
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

    async def _record_channel(channel_id, conf):
        persisted.append((channel_id, dict(conf)))

    async def _record_subsection(channel_id, subsection, conf, keep_keys):
        persisted.append((channel_id, conf))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_channel_in_config",
        _record_channel,
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        _record_subsection,
    )
    async def _record_heartbeat(payload):
        persisted.append(("heartbeat", dict(payload)))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_heartbeat_in_config",
        _record_heartbeat,
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
@pytest.mark.parametrize(
    ("raw_config", "expected"),
    [
        ({}, "false"),
        ({"react": {"evolution": {"enabled": True}}}, "true"),
        ({"react": {"evolution": {"enabled": False}}}, "false"),
        ({"evolution": {"enabled": True}}, "true"),
    ],
)
async def test_config_get_returns_evolution_enabled_switch(monkeypatch, raw_config, expected):
    channel = FakeWebChannel()
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: raw_config)
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["config.get"](
        object(),
        "req-get-evolution",
        {},
        "sess-get-evolution",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["evolution_enabled"] == expected


@pytest.mark.asyncio
async def test_config_set_persists_evolution_enabled(monkeypatch):
    channel = FakeWebChannel()
    persisted: list[bool] = []
    reload_options_seen: list[dict] = []

    monkeypatch.setattr(
        app_web_handlers,
        "get_config_raw",
        lambda: {"react": {"evolution": {"enabled": True}}},
    )
    monkeypatch.setattr(
        app_web_handlers,
        "get_config",
        lambda: {"react": {"evolution": {"enabled": False}}},
    )
    monkeypatch.setattr(
        app_web_handlers,
        "update_evolution_enabled_in_config",
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
        "req-evolution-enabled",
        {"evolution_enabled": "false"},
        "sess-evolution-enabled",
    )

    assert persisted == [False]
    assert reload_options_seen == [{
        "target_channel_id": "web",
        "reload_scopes": ["agent_runtime"],
    }]
    assert channel.responses[-1]["payload"] == {
        "updated": ["evolution_enabled"],
        "applied_without_restart": True,
    }


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
async def test_models_save_remove_set_active_are_registered(monkeypatch):
    channel = FakeWebChannel()
    persisted: list[list[dict]] = []

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
        lambda: {"models": {"defaults": [
            {
                "model_client_config": {
                    "model_name": "keep-me",
                    "api_base": "",
                    "api_key": "",
                    "client_provider": "OpenAI",
                },
                "model_config_obj": {"temperature": 0.95},
            }
        ]}},
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
            {"get_crypto_provider": lambda self: None},
        )(),
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        return True

    _register_web_handlers(
        WebHandlersBindParams(channel=channel, on_config_saved=on_config_saved)
    )
    assert "models.save" in channel.methods
    assert "models.remove" in channel.methods
    assert "models.set_active" in channel.methods

    await channel.methods["models.save"](
        object(),
        "req-save",
        {
            "model_name": "new-model",
            "api_base": "https://example.com/v1",
            "api_key": "secret",
            "model_provider": "OpenAI",
        },
        "sess-1",
    )
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["action"] == "created"
    assert persisted[-1][-1]["model_client_config"]["model_name"] == "new-model"


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
@pytest.mark.parametrize("value", ["true", "false"])
async def test_config_set_updates_skill_create_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    value: str,
) -> None:
    channel = FakeWebChannel()
    saved_updates: list[dict[str, str]] = []

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
        lambda: {"evolution": {}},
    )
    monkeypatch.setenv("SKILL_CREATE", "")

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            on_config_saved=lambda _, **kwargs: saved_updates.append(
                kwargs["env_updates"]
            ),
        )
    )

    await channel.methods["config.set"](
        object(),
        "req-evolution",
        {"skill_create": value},
        "sess-evolution",
    )

    expected = {"SKILL_CREATE": value}
    assert saved_updates == [expected]
    from jiuwenswarm.common.local_env_config import get_process_baseline, read_env

    assert {key: read_env(key) for key in expected} == expected
    baseline = get_process_baseline()
    assert {key: baseline.get(key) for key in expected} == expected
    assert set((tmp_path / ".env").read_text(encoding="utf-8").splitlines()) == {
        f'{key}="{env_value}"' for key, env_value in expected.items()
    }
    assert channel.responses[-1]["payload"] == {
        "updated": ["skill_create"],
        "applied_without_restart": False,
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
                    "model": {"ref": "gpt-4.1#2"},
                    "skills": ["coding"],
                    "max_iterations": 12,
                    "completion_timeout": 34,
                }
            }
        }
    }

    flat = _flatten_modes_team_for_config_panel(raw)

    assert flat["agent_name_0"] == "agent_1"
    assert flat["agent_model_0"] == "gpt-4.1#2"
    assert flat["agent_skills_0"] == "coding"
    assert flat["agent_max_iterations_0"] == "12"
    assert flat["agent_completion_timeout_0"] == "34"


def test_config_panel_flatten_migrates_legacy_model_with_resolved_defaults(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "gpt-4.1")
    monkeypatch.setenv("MODEL_PROVIDER", "OpenAI")
    monkeypatch.setenv("API_BASE", "https://api.openai.com/v1")
    raw = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "other-model",
                        "client_provider": "OpenAI",
                        "api_base": "https://example.com/v1",
                    },
                },
                {
                    "model_client_config": {
                        "model_name": "${MODEL_NAME}",
                        "client_provider": "${MODEL_PROVIDER}",
                        "api_base": "${API_BASE}",
                    },
                },
            ],
        },
        "web_config_panel": {
            "agent_team_agents": {
                "agent_1": {
                    "model": {
                        "provider": "${MODEL_PROVIDER}",
                        "api_base": "${API_BASE}/",
                        "api_key": "legacy-secret",
                        "model": "${MODEL_NAME}",
                    },
                },
            },
        },
    }

    flat = _flatten_modes_team_for_config_panel(raw)

    assert flat["agent_model_0"] == "gpt-4.1#1"


def test_config_panel_flatten_keeps_ambiguous_legacy_model_unresolved():
    raw = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "gpt-4.1",
                        "client_provider": "OpenAI",
                        "api_base": "https://one.example/v1",
                    },
                },
                {
                    "model_client_config": {
                        "model_name": "gpt-4.1",
                        "client_provider": "OpenAI",
                        "api_base": "https://two.example/v1",
                    },
                },
            ],
        },
        "web_config_panel": {
            "agent_team_agents": {
                "agent_1": {"model": {"model": "gpt-4.1"}},
            },
        },
    }

    flat = _flatten_modes_team_for_config_panel(raw)

    assert flat["agent_model_0"] == "gpt-4.1"


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
    assert flat["symphony_dynamic_graph_enabled"] == "false"
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

    assert recorded_symphony == [{"enabled": True, "evolution": {"enabled": False}}]
    assert recorded_skill_retrieval == [{"enabled": False, "retrieve": {"flatten_tree": True}}]
    assert channel.responses[-1] == {
        "id": "req-3",
        "ok": True,
        "payload": {
            "updated": [
                "symphony_enabled",
                "symphony_dynamic_graph_enabled",
                "skill_retrieval_enabled",
                "skill_retrieval_retrieve_flatten_tree",
            ],
            "applied_without_restart": True,
        },
        "error": None,
        "code": None,
    }


def test_web_does_not_expose_symphony_evolution_rpc_methods():
    assert "symphony.evolution_status" not in app_web_handlers._FORWARD_REQ_METHODS
    assert "symphony.evolution_record_outcome" not in app_web_handlers._FORWARD_REQ_METHODS
    assert "symphony.evolution_rebuild" not in app_web_handlers._FORWARD_REQ_METHODS


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

    async def _record_subsection(cid, sub, conf, keep_keys):
        recorded_subsection.append((cid, sub, conf, keep_keys))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        _record_subsection,
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

    async def _record_subsection(cid, sub, conf, keep_keys):
        recorded_subsection.append((cid, sub, conf, keep_keys))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        _record_subsection,
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

    async def _record_subsection(cid, sub, conf, keep_keys):
        recorded_subsection.append((cid, sub, conf, keep_keys))

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        _record_subsection,
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

    async def _noop_update_channel(channel_id, conf):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_channel_in_config",
        _noop_update_channel,
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

    # 2. monkeypatch get_config_file 指向临时文件
    monkeypatch.setattr(cfg, "get_config_file", lambda: temp_config)

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

    monkeypatch.setattr(cfg, "get_config_file", lambda: temp_config)

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

    monkeypatch.setattr(cfg, "get_config_file", lambda: temp_config)

    # 写入新数据覆盖
    new_apps = [{"name": "新应用", "app_id": "new_id"}]
    cfg.update_channel_subsection_in_config("feishu", "apps", new_apps)

    with open(temp_config, "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f)

    assert len(saved["channels"]["feishu"]["apps"]) == 1
    assert saved["channels"]["feishu"]["apps"][0]["name"] == "新应用"
    assert saved["channels"]["feishu"]["apps"][0]["app_id"] == "new_id"


def test_chat_interrupt_ack_payload_ws_shape_unchanged():
    payload = _chat_interrupt_ack_payload("s1", {"intent": "pause"}, for_http=False)
    assert payload == {"accepted": True, "session_id": "s1", "intent": "pause"}
    empty = _chat_interrupt_ack_payload("s1", {}, for_http=False)
    assert empty == {"accepted": True, "session_id": "s1"}
    assert "event_type" not in empty


def test_chat_interrupt_ack_payload_http_carries_interrupt_result():
    payload = _chat_interrupt_ack_payload("s1", {"intent": "cancel"}, for_http=True)
    assert payload["accepted"] is True
    assert payload["session_id"] == "s1"
    assert payload["intent"] == "cancel"
    assert payload["event_type"] == "chat.interrupt_result"
    assert payload["success"] is True
    assert payload["message"] == "任务已取消"

    defaulted = _chat_interrupt_ack_payload("s1", {}, for_http=True)
    assert defaulted["intent"] == "cancel"
    assert defaulted["event_type"] == "chat.interrupt_result"

    paused = _chat_interrupt_ack_payload("s1", {"intent": "pause"}, for_http=True)
    assert paused["intent"] == "pause"
    assert paused["message"] == "任务已暂停"


def test_inject_connection_identity_overrides_frame_params() -> None:
    """连接级身份无条件覆盖帧 params（防伪造），供 history 回调落库归属。"""
    from jiuwenswarm.gateway.channel_manager.web import web_ws_transport as wsm

    transport = object.__new__(wsm.WebWsTransport)
    ws = SimpleNamespace(
        **{wsm._WEB_CONNECTION_USER_ID_ATTR: "u1"},
        _web_routing={"group_id": "g1", "bot_id": "b1"},
    )
    raw = json.dumps({
        "type": "req", "id": "r1", "method": "chat.send",
        "params": {
            "session_id": "s1", "query": "hi",
            "user": "forged", "user_id": "forged",
            "group_id": "forged", "bot_id": "forged",
        },
    })
    out = json.loads(transport._inject_user_id_into_frame(ws, raw))
    assert out["params"]["user"] == "u1"
    assert out["params"]["user_id"] == "u1"
    assert out["params"]["group_id"] == "g1"
    assert out["params"]["bot_id"] == "b1"


def test_inject_identity_noop_without_connection_identity() -> None:
    from jiuwenswarm.gateway.channel_manager.web import web_ws_transport as wsm

    transport = object.__new__(wsm.WebWsTransport)
    ws = SimpleNamespace()
    raw = json.dumps({
        "type": "req", "id": "r1", "method": "chat.send",
        "params": {"session_id": "s1"},
    })
    assert transport._inject_user_id_into_frame(ws, raw) == raw


def test_capture_session_create_row_on_ok_response(monkeypatch) -> None:
    """create 成功响应经下行拦截异步落行（remote 转发主路径）；失败/未知 rid 不动。"""
    import threading

    from jiuwenswarm.channels.web.history_store import api as history_api
    from jiuwenswarm.gateway.channel_manager.web import web_ws_transport as wsm

    captured: dict[str, object] = {}
    done = threading.Event()

    def fake_ensure(sid, store, **kwargs):
        captured["sid"] = sid
        captured.update(kwargs)
        done.set()
        return True

    monkeypatch.setattr(history_api, "ensure_session_row_sync", fake_ensure)

    transport = object.__new__(wsm.WebWsTransport)
    transport._pending_session_creates = {}
    ws = SimpleNamespace(
        **{wsm._WEB_CONNECTION_USER_ID_ATTR: "u1"},
        _web_routing={"group_id": "g1", "bot_id": "b1", "user_id": ""},
    )
    transport._pending_session_creates["rid1"] = (ws, "新会话标题")

    ok_frame = {
        "type": "res", "id": "rid1", "ok": True,
        "payload": {"session_id": "web_new", "projectId": "p1", "workMode": "work"},
    }
    transport._capture_session_create_row(ws, ok_frame)
    assert done.wait(timeout=5), "ensure_session_row_sync 未被调用"
    assert captured["sid"] == "web_new"
    assert captured["user"] == "u1"
    assert captured["group_id"] == "g1"
    assert captured["bot_id"] == "b1"
    assert captured["project_id"] == "p1"
    assert captured["work_mode"] == "work"
    assert captured["title"] == "新会话标题"
    assert "rid1" not in transport._pending_session_creates

    # 失败响应 / 未知 rid / 非会话帧：不触发、不误清
    transport._pending_session_creates["rid2"] = (ws, "t")
    transport._capture_session_create_row(ws, {"type": "res", "id": "rid2", "ok": False, "payload": {}})
    assert "rid2" in transport._pending_session_creates
    transport._capture_session_create_row(ws, {"type": "event", "id": "rid2", "payload": {}})
    assert "rid2" in transport._pending_session_creates
