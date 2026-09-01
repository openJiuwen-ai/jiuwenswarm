# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import os
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
from jiuwenswarm.gateway.heartbeat import HeartbeatServiceUnavailableError


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


class _CapturingSessionListAgentClient:
    """捕获 E2A 信封并返回标准 session.list 响应（供 Web 转发断言）。"""

    def __init__(self):
        self.server_ready = True
        self.envelopes: list = []

    async def send_request(self, envelope):
        self.envelopes.append(envelope)
        if str(getattr(envelope, "method", "") or "") == "project.cron.resolve_binding":
            # AgentOS cron 绑定解析：返回用户侧项目绑定（与 ProjectAdapter 契约一致）。
            return type(
                "Resp",
                (),
                {"ok": True, "payload": {"project_id": "user-proj-1", "work_mode": "code"}},
            )()
        return type(
            "Resp",
            (),
            {
                "ok": True,
                "payload": {
                    "sessions": [
                        {
                            "session_id": "sess-team-1",
                            "mode": "team",
                            "team_name": "dev-team-swarm_sess-team-1",
                            "title": "team task",
                        }
                    ],
                    "total": 1,
                    "limit": 20,
                    "offset": 0,
                },
            },
        )()


from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient


class _OfflineRemoteAgentClient:
    server_ready = False


_OfflineRemoteAgentClient.__module__ = "jiuwenswarm.extensions.yuanrong.agent_client"


class _CapturingCronController:
    def __init__(self) -> None:
        self.params = None

    async def create_job(self, params):
        self.params = dict(params)
        return {"id": "cron-1"}


class _OwnedCronController:
    def __init__(self) -> None:
        self.job = SimpleNamespace(id="cron-owner", user_id="user-owner")
        self.update_calls = 0

    async def get_job(self, job_id):
        return self.job if job_id == self.job.id else None

    async def update_job(self, job_id, patch):
        self.update_calls += 1
        return {"id": job_id, **patch}


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

    async def set_health_check_conf(self, *, every=None, target=None, active_hours=None):
        if every is not None:
            self.config["every"] = every
        if target is not None:
            self.config["target"] = target
        if active_hours is not None:
            self.config["active_hours"] = active_hours

    def get_health_check_conf(self):
        return dict(self.config)


class FakeHeartbeatController:
    def __init__(self):
        self.calls: list[tuple] = []

    def get_meta(self):
        return {"statuses": ["scheduled"]}

    async def list_jobs(self, params, *, access_session_id=None, user_id=""):
        self.calls.append(("list", dict(params), access_session_id, user_id))
        return {"jobs": []}

    async def create_job(self, params, *, user_id=""):
        self.calls.append(("create", dict(params), user_id))
        return dict(params, id="hb-test")


@pytest.mark.asyncio
async def test_heartbeat_web_methods_preserve_health_check_aliases_and_session() -> None:
    channel = FakeWebChannel()
    controller = FakeHeartbeatController()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, heartbeat_controller=controller)
    )
    assert "health_check.get_conf" in channel.methods
    assert "health_check.set_conf" in channel.methods
    assert "health_check.get_path" not in channel.methods
    assert "heartbeat.get_conf" in channel.methods
    assert "heartbeat.set_conf" in channel.methods
    assert "heartbeat.get_path" not in channel.methods
    assert channel.methods["heartbeat.get_conf"] is channel.methods["health_check.get_conf"]
    assert channel.methods["heartbeat.set_conf"] is channel.methods["health_check.set_conf"]
    expected = {
        "heartbeat.job.list", "heartbeat.job.meta", "heartbeat.job.get",
        "heartbeat.job.create", "heartbeat.job.update", "heartbeat.job.delete",
        "heartbeat.job.toggle", "heartbeat.job.preview", "heartbeat.job.run_now",
        "heartbeat.job.cancel",
    }
    assert expected <= set(channel.methods)

    await channel.methods["heartbeat.job.list"](
        object(), "list-1", {}, "session-current", user_id="user-current"
    )
    await channel.methods["heartbeat.job.create"](
        object(),
        "create-1",
        {
            "name": "n",
            "prompt": "p",
            "channel_id": "other",
            "session_id": "other-session",
            "schedule": {"type": "interval", "interval_seconds": 120},
        },
        "session-current",
        user_id="user-current",
    )
    assert controller.calls[0] == (
        "list",
        {},
        "session-current",
        "user-current",
    )
    created = controller.calls[1][1]
    assert created["channel_id"] == "web"
    assert created["session_id"] == "session-current"
    assert created["source"] == "web_rpc"
    assert controller.calls[1][2] == "user-current"


@pytest.mark.asyncio
async def test_heartbeat_web_reports_unavailable_and_missing_job_codes() -> None:
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["heartbeat.job.list"](
        object(), "list-unavailable", {}, "session-current"
    )
    assert channel.responses[-1]["code"] == "SERVICE_UNAVAILABLE"

    class Controller:
        async def get_job(self, *_args, **_kwargs):
            raise KeyError("missing")

        async def get_meta(self, **_kwargs):
            raise HeartbeatServiceUnavailableError("agentserver offline")

    channel = FakeWebChannel()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, heartbeat_controller=Controller())
    )
    await channel.methods["heartbeat.job.get"](
        object(), "get-missing", {"id": "missing"}, "session-current"
    )
    assert channel.responses[-1]["code"] == "NOT_FOUND"
    await channel.methods["heartbeat.job.meta"](
        object(), "meta-unavailable", {}, "session-current"
    )
    assert channel.responses[-1]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_session_delete_delegates_heartbeat_lifecycle_to_agentserver() -> None:
    class Agent:
        server_ready = True

        def __init__(self):
            self.requests = []

        async def send_request(self, env):
            self.requests.append(env)
            return SimpleNamespace(ok=True, payload={"session_id": "deleted"})

    channel = FakeWebChannel()
    agent = Agent()
    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=agent,
        )
    )
    await channel.methods["session.delete"](
        object(), "delete-1", {"session_id": "session-to-delete"}, "current"
    )
    assert agent.requests[0].method == "session.delete"
    assert channel.responses[-1]["ok"] is True


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
async def test_session_list_forwards_via_e2a_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web session.list 经统一薄代理 E2A 转发目标 AgentServer（Phase 1）。

    投影（to_session_info）已随迁移移入 AgentServer SessionAdapter，
    本用例验证 Gateway 侧转发契约：method / channel / user_id / params。
    """
    agent_client = _CapturingSessionListAgentClient()
    channel = FakeWebChannel()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=agent_client)
    )

    await channel.methods["session.list"](
        object(),
        "req-session-list",
        {"limit": 5},
        "current-session",
        "user-42",
    )

    assert len(agent_client.envelopes) == 1
    env = agent_client.envelopes[0]
    assert env.method == "session.list"
    assert env.channel == "web"
    assert env.user_id == "user-42"
    assert env.params == {"limit": 5}
    assert env.is_stream is False

    resp = channel.responses[-1]
    assert resp["ok"] is True
    assert resp["payload"]["sessions"][0]["team_name"] == "dev-team-swarm_sess-team-1"


@pytest.mark.asyncio
async def test_session_list_keeps_single_user_shared_directory_fallback(monkeypatch) -> None:
    """A local AgentServer restart must not hide legacy sessions from Web."""
    channel = FakeWebChannel()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=WebSocketAgentServerClient())
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        lambda *, limit, offset: ([{"session_id": "legacy", "mode": "agent"}], 1),
    )

    await channel.methods["session.list"](object(), "req-offline", {"limit": 5}, "current")

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["sessions"][0]["session_id"] == "legacy"


@pytest.mark.asyncio
async def test_session_list_does_not_fallback_for_offline_remote_client(monkeypatch) -> None:
    """Only the local shared-directory client may use Gateway's data directory."""
    channel = FakeWebChannel()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=_OfflineRemoteAgentClient())
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not read Gateway state")),
    )

    await channel.methods["session.list"](object(), "req-offline-remote", {}, "current")

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_cron_job_update_rejects_a_different_authenticated_owner() -> None:
    channel = FakeWebChannel()
    cron = _OwnedCronController()
    _register_web_handlers(WebHandlersBindParams(channel=channel, cron_controller=cron))

    await channel.methods["cron.job.update"](
        object(), "req-cron-owner", {"id": "cron-owner", "patch": {"name": "changed"}}, None, "user-other"
    )

    assert cron.update_calls == 0
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "NOT_FOUND"


class _DictOwnedCronController:
    """Mimics the real ``CronController.get_job`` which returns ``to_dict()``."""

    def __init__(self) -> None:
        self.job = {"id": "cron-dict-owner", "user_id": "user-owner"}
        self.run_now_calls = 0

    async def get_job(self, job_id):
        return self.job if job_id == self.job["id"] else None

    async def run_now_info(self, job_id):
        self.run_now_calls += 1
        return {"run_id": "run-1", "session_id": "sess-1"}


@pytest.mark.asyncio
async def test_cron_job_run_now_accepts_matching_owner_with_dict_job() -> None:
    """dict-shaped job (real controller output) must pass owner check for run_now."""
    channel = FakeWebChannel()
    cron = _DictOwnedCronController()
    _register_web_handlers(WebHandlersBindParams(channel=channel, cron_controller=cron))

    await channel.methods["cron.job.run_now"](
        object(), "req-cron-run", {"id": "cron-dict-owner"}, None, "user-owner"
    )

    assert cron.run_now_calls == 1
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_cron_job_run_now_rejects_dict_job_with_different_owner() -> None:
    channel = FakeWebChannel()
    cron = _DictOwnedCronController()
    _register_web_handlers(WebHandlersBindParams(channel=channel, cron_controller=cron))

    await channel.methods["cron.job.run_now"](
        object(), "req-cron-run", {"id": "cron-dict-owner"}, None, "user-other"
    )

    assert cron.run_now_calls == 0
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_agentos_cron_create_does_not_read_gateway_session_metadata(monkeypatch) -> None:
    """AgentOS project binding must stay in the routed user AgentServer directory."""
    channel = FakeWebChannel()
    cron = _CapturingCronController()
    client = _CapturingSessionListAgentClient()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=client, cron_controller=cron)
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_agentos_routing_client",
        lambda _client: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read Gateway metadata")),
    )

    await channel.methods["cron.job.create"](
        object(), "req-cron", {"name": "job", "cron_expr": "* * * * *"}, "user-session", "user-1"
    )

    assert channel.responses[-1]["ok"] is True
    assert cron.params["_agentos_project_binding_verified"] is True
    assert "project_dir" not in cron.params


@pytest.mark.asyncio
async def test_agentos_cron_update_project_fields_with_dict_job(monkeypatch) -> None:
    """AgentOS update with project fields must work with dict-shaped jobs.

    The real ``CronController.get_job`` returns ``to_dict()`` (a plain dict);
    the AgentOS binding branch must read ``work_mode`` / ``session_id`` with
    dict access instead of attribute access.
    """

    class _AgentOSClient:
        server_ready = True

    class _DictUpdateCronController:
        def __init__(self) -> None:
            self.job = {
                "id": "cron-dict-1",
                "user_id": "user-1",
                "work_mode": "work",
                "session_id": "sess-1",
            }
            self.update_calls: list[tuple[str, dict]] = []

        async def get_job(self, job_id):
            return dict(self.job) if job_id == self.job["id"] else None

        async def update_job(self, job_id, patch):
            self.update_calls.append((job_id, dict(patch)))
            return {"id": job_id, **patch}

    channel = FakeWebChannel()
    cron = _DictUpdateCronController()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=_AgentOSClient(), cron_controller=cron)
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.is_agentos_routing_client",
        lambda _client: True,
    )

    async def _fake_resolve_agent_cron_project_binding(**kwargs):
        return True, {"project_id": "user-proj-1", "work_mode": "code"}

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.resolve_agent_cron_project_binding",
        _fake_resolve_agent_cron_project_binding,
    )

    await channel.methods["cron.job.update"](
        object(),
        "req-cron-update",
        {"id": "cron-dict-1", "patch": {"project_id": "user-proj-1"}},
        None,
        "user-1",
    )

    assert channel.responses[-1]["ok"] is True, channel.responses[-1]
    assert cron.update_calls, "controller.update_job must be invoked"
    patch = cron.update_calls[0][1]
    assert patch["project_id"] == "user-proj-1"
    assert patch["work_mode"] == "code"
    assert patch["_agentos_project_binding_verified"] is True


@pytest.mark.asyncio
async def test_path_set_reloads_config_and_resets_agent_browser_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeWebChannel()
    # Match the default single-user transport so path.set follows the local
    # shared-directory branch rather than the AgentOS/remote proxy branch.
    agent_client = WebSocketAgentServerClient()
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
async def test_models_list_returns_exact_vendor_identity(monkeypatch) -> None:
    from jiuwenswarm.server.runtime import opencode_zen

    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(
        app_web_handlers,
        "get_default_models",
        lambda _config: [{
            "model_client_config": {
                "model_name": "qwen3.8-max",
                "api_base": "https://example.com/v1",
                "api_key": "secret",
                "client_provider": "OpenAI",
                "vendor_key": "alibaba",
                "plan": "token_plan",
            },
            "model_config_obj": {"temperature": 0.95},
            "alias": "qwen3.8-max",
            "is_default": True,
        }],
    )
    monkeypatch.setattr(opencode_zen, "get_zen_free_model_entries", lambda: [])
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["models.list"](object(), "req-models", {}, "session-1")

    model = channel.responses[-1]["payload"]["models"][0]
    assert channel.responses[-1]["ok"] is True
    assert model["vendor_key"] == "alibaba"
    assert model["plan"] == "token_plan"


@pytest.mark.asyncio
async def test_models_list_includes_cached_zen_free_models(monkeypatch) -> None:
    """Free models are in-memory entries but must remain selectable in new sessions."""
    from jiuwenswarm.server.runtime import opencode_zen

    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(app_web_handlers, "get_default_models", lambda _config: [])
    monkeypatch.setattr(
        opencode_zen,
        "get_zen_free_model_entries",
        lambda: [{
            "model_client_config": {
                "model_name": "deepseek-v4-flash-free",
                "api_base": "https://opencode.ai/zen/v1",
                "api_key": "public",
                "client_provider": "OpenAI",
            },
            "model_config_obj": {"temperature": 0.95},
            "alias": "DeepSeek V4 Flash",
            "context_window_tokens": 200000,
            "is_free": True,
        }],
    )
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["models.list"](object(), "req-models", {}, "session-1")

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["models"] == [{
        "model_name": "deepseek-v4-flash-free",
        "api_base": "https://opencode.ai/zen/v1",
        "api_key": "public",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "reasoning_level": "",
        "is_default": None,
        "is_agentos": False,
        "is_free": True,
        "alias": "DeepSeek V4 Flash",
        "context_window_tokens": 200000,
    }]


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
        ("health_check.set_conf", {"every": 30, "target": "web"}),
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
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_health_check_in_config",
        lambda payload: persisted.append(("health_check", dict(payload))),
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


def test_media_capability_config_uses_multimodal_hot_reload_scope():
    for env_key in app_web_handlers._MULTIMODAL_RELOAD_ENV_KEYS:
        change_set = app_web_handlers._ConfigChangeSet({env_key: "true"}, [])
        assert change_set.reload_scopes == {"multimodal"}


@pytest.mark.parametrize(
    ("param_key", "env_key"),
    [
        ("video_gen_api_base", "VIDEO_GEN_API_BASE"),
        ("video_gen_api_key", "VIDEO_GEN_API_KEY"),
        ("video_gen_model", "VIDEO_GEN_MODEL_NAME"),
        ("video_gen_provider", "VIDEO_GEN_PROVIDER"),
    ],
)
def test_video_gen_keys_are_settable_from_config_panel(param_key, env_key):
    """video_gen must be readable/writable via config.get / config.set."""
    assert app_web_handlers._CONFIG_SET_ENV_MAP.get(param_key) == env_key
    assert param_key in app_web_handlers.CONFIG_KEYS


@pytest.mark.parametrize(
    "env_key",
    [
        "VIDEO_GEN_API_BASE",
        "VIDEO_GEN_API_KEY",
        "VIDEO_GEN_MODEL_NAME",
        "VIDEO_GEN_PROVIDER",
    ],
)
def test_video_gen_keys_trigger_multimodal_reload(env_key):
    """Saving video_gen config must yield the multimodal reload scope."""
    assert env_key in app_web_handlers._MULTIMODAL_RELOAD_ENV_KEYS
    change = app_web_handlers._ConfigChangeSet(env_updates={env_key: "x"}, yaml_updated=[])
    assert change.reload_scopes == {"multimodal"}


@pytest.mark.asyncio
async def test_config_set_persists_video_gen_keys_to_env_file(tmp_path, monkeypatch):
    """VIDEO_GEN_* written by config.set must land in .env and read back via config.get."""
    env_file = tmp_path / ".env"
    env_file.write_text('API_KEY="existing"\n', encoding="utf-8")
    monkeypatch.setattr(app_web_handlers, "_ENV_FILE", env_file)
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {"models": {"defaults": []}})
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(
        "jiuwenswarm.extensions.registry.ExtensionRegistry.get_instance",
        lambda: type(
            "Registry",
            (),
            {"get_crypto_provider": lambda self: None},
        )(),
    )
    for env_key in ("VIDEO_GEN_API_BASE", "VIDEO_GEN_API_KEY", "VIDEO_GEN_MODEL_NAME", "VIDEO_GEN_PROVIDER"):
        monkeypatch.setenv(env_key, "")

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["config.set"](
        object(),
        "req-set",
        {
            "video_gen_api_base": "https://dashscope.aliyuncs.com/api/v1",
            "video_gen_api_key": "sk-video-gen",
            "video_gen_model": "wan2.6-t2v",
            "video_gen_provider": "DashScope",
        },
        "sess-1",
    )

    assert channel.responses[-1]["ok"] is True
    updated = channel.responses[-1]["payload"]["updated"]
    assert set(updated) == {
        "video_gen_api_base",
        "video_gen_api_key",
        "video_gen_model",
        "video_gen_provider",
    }

    written = env_file.read_text(encoding="utf-8")
    assert 'VIDEO_GEN_API_KEY="sk-video-gen"' in written
    assert 'VIDEO_GEN_PROVIDER="DashScope"' in written
    assert 'API_KEY="existing"' in written
    assert os.environ["VIDEO_GEN_API_KEY"] == "sk-video-gen"

    await channel.methods["config.get"](object(), "req-get", {}, "sess-1")
    payload = channel.responses[-1]["payload"]
    assert payload["video_gen_model"] == "wan2.6-t2v"
    assert payload["video_gen_provider"] == "DashScope"


@pytest.mark.asyncio
async def test_config_set_rejects_unknown_video_gen_provider(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_web_handlers, "_ENV_FILE", env_file)
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {"models": {"defaults": []}})
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(
        "jiuwenswarm.extensions.registry.ExtensionRegistry.get_instance",
        lambda: type(
            "Registry",
            (),
            {"get_crypto_provider": lambda self: None},
        )(),
    )

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["config.set"](
        object(), "req-set", {"video_gen_provider": "NotAProvider"}, "sess-1",
    )

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"


def test_media_capability_provider_identity_has_exact_env_contract():
    for modality in ("vision", "audio", "video"):
        prefix = modality.upper()
        assert app_web_handlers._CONFIG_SET_ENV_MAP[f"{modality}_endpoint_profile"] == f"{prefix}_ENDPOINT_PROFILE"
        assert app_web_handlers._CONFIG_SET_ENV_MAP[f"{modality}_vendor_key"] == f"{prefix}_VENDOR_KEY"
        assert app_web_handlers._CONFIG_SET_ENV_MAP[f"{modality}_plan"] == f"{prefix}_PLAN"
        assert f"{prefix}_ENDPOINT_PROFILE" in app_web_handlers._MULTIMODAL_RELOAD_ENV_KEYS
        assert f"{prefix}_VENDOR_KEY" not in app_web_handlers._MULTIMODAL_RELOAD_ENV_KEYS
        assert f"{prefix}_PLAN" not in app_web_handlers._MULTIMODAL_RELOAD_ENV_KEYS


@pytest.mark.asyncio
async def test_media_capability_provider_identity_round_trips_through_config_rpc(monkeypatch, tmp_path):
    channel = FakeWebChannel()
    monkeypatch.setattr(app_web_handlers, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {"models": {"defaults": []}})
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: {})

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        del updated_keys, env_updates, config_payload
        assert reload_options["reload_scopes"] == ["multimodal"]
        return True

    _register_web_handlers(
        WebHandlersBindParams(channel=channel, on_config_saved=on_config_saved)
    )

    await channel.methods["config.set"](
        object(),
        "req-media-identity-set",
        {
            "vision_endpoint_profile": "dashscope",
            "vision_vendor_key": "alibaba",
            "vision_plan": "token_plan",
        },
        "sess-media-identity",
    )
    assert channel.responses[-1]["payload"] == {
        "updated": [
            "vision_endpoint_profile",
            "vision_vendor_key",
            "vision_plan",
        ],
        "applied_without_restart": True,
    }

    await channel.methods["config.get"](
        object(),
        "req-media-identity-get",
        {},
        "sess-media-identity",
    )
    payload = channel.responses[-1]["payload"]
    assert payload["vision_endpoint_profile"] == "dashscope"
    assert payload["vision_vendor_key"] == "alibaba"
    assert payload["vision_plan"] == "token_plan"


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
                    "vendor_key": "alibaba",
                    "plan": "token_plan",
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
    persisted_mcc = persisted[0][0]["model_client_config"]
    assert persisted_mcc["vendor_key"] == "alibaba"
    assert persisted_mcc["plan"] == "token_plan"
    assert reload_options_seen[-1]["target_channel_id"] == "web"
    assert reload_options_seen[-1]["reload_scopes"] == ["model"]
    assert channel.responses[-1]["id"] == "req-models"
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["applied_without_restart"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vendor_key", "plan", "expected_error"),
    [
        ("alibaba", "unsupported_plan", "plan must be one of"),
        (None, "token_plan", "vendor_key is required when plan is set"),
    ],
)
async def test_models_replace_all_rejects_invalid_vendor_identity(vendor_key, plan, expected_error):
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["models.replace_all"](
        object(),
        "req-models-invalid-provider",
        {
            "models": [{
                "model_name": "model-one",
                "api_base": "https://example.com/v1",
                "api_key": "secret",
                "model_provider": "OpenAI",
                "is_default": True,
                "vendor_key": vendor_key,
                "plan": plan,
            }],
        },
        "sess-1",
    )

    response = channel.responses[-1]
    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"
    assert expected_error in response["error"]


@pytest.mark.asyncio
async def test_models_replace_all_rejects_reasoning_level_not_supported_by_model():
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))

    await channel.methods["models.replace_all"](
        object(),
        "req-models-invalid-reasoning",
        {
            "models": [{
                "model_name": "kimi-k3",
                "api_base": "https://api.moonshot.cn/v1",
                "api_key": "secret",
                "model_provider": "OpenAI",
                "reasoning_level": "off",
                "is_default": True,
            }],
        },
        "sess-1",
    )

    response = channel.responses[-1]
    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"
    assert "reasoning_level must be one of: low, high, max" in response["error"]


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
    dependency_checks: list[None] = []
    recorded: list[dict] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: dependency_checks.append(None) or None,
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

    assert dependency_checks == [None]
    assert recorded and recorded[0]["team"][0]["external_cli_agents"] == [{"cli_agent": "codex"}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_does_not_install_codex_for_claude_only(monkeypatch):
    channel = FakeWebChannel()
    dependency_checks: list[None] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel))

    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.gateway.channel_manager.web.app_web_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: dependency_checks.append(None) or None,
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

    assert dependency_checks == []
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_updates_external_cli_switches_without_team_save(monkeypatch):
    channel = FakeWebChannel()
    dependency_checks: list[None] = []
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
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: dependency_checks.append(None) or None,
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

    assert dependency_checks == [None]
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
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ensure_claude_dependency_available_or_start_install",
        lambda: None,
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
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: None,
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
async def test_config_set_starts_codex_dependency_install_without_saving_codex(monkeypatch):
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
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: {"status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0},
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )

    await channel.methods["config.set"](
        object(),
        "req-codex-installing",
        {
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "true",
        },
        "sess-codex-installing",
    )

    assert updates == [([], "ws://127.0.0.1:19000/ws")]
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["codex_dependency_install"]["status"] == "running"


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
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: {"status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0},
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
    assert channel.responses[-1]["payload"]["codex_dependency_install"]["status"] == "running"


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

    with app_web_handlers._CODEX_DEPENDENCY_INSTALL_LOCK:
        app_web_handlers._CODEX_DEPENDENCY_INSTALL_STATUS.update({
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
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._install_codex_dependency",
        install_once,
    )

    first = app_web_handlers._ensure_codex_dependency_available_or_start_install()
    assert install_started.wait(timeout=5)
    second = app_web_handlers._ensure_codex_dependency_available_or_start_install()
    release_install.set()

    assert first and first["status"] == "running"
    assert second and second["status"] == "running"
    assert len(install_calls) == 1


@pytest.mark.parametrize(
    ("cli_agent", "required_module"),
    [
        ("claude", "claude_agent_sdk"),
        ("codex", "openai_codex"),
    ],
)
def test_external_cli_dependency_install_starts_managed_runtime_in_frozen_desktop(
    monkeypatch: pytest.MonkeyPatch,
    cli_agent: str,
    required_module: str,
) -> None:
    lock = app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS[cli_agent]
    status = app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES[cli_agent]
    with lock:
        status.update({
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
        lambda name: None if name == required_module else original_find_spec(name),
    )
    monkeypatch.setattr(app_web_handlers.sys, "frozen", True, raising=False)

    created_threads: list[SimpleNamespace] = []

    class _Thread:
        def __init__(
            self,
            *,
            target: object,
            args: tuple[str, ...],
            name: str,
            daemon: bool,
        ) -> None:
            created_threads.append(
                SimpleNamespace(target=target, args=args, name=name, daemon=daemon, started=False)
            )

        def start(self) -> None:
            created_threads[-1].started = True

    monkeypatch.setattr(app_web_handlers.threading, "Thread", _Thread)

    ensure_dependency = (
        app_web_handlers._ensure_claude_dependency_available_or_start_install
        if cli_agent == "claude"
        else app_web_handlers._ensure_codex_dependency_available_or_start_install
    )
    first = ensure_dependency()
    second = ensure_dependency()

    assert first and first["status"] == "running"
    assert first["phase"] == "preparing"
    assert second and second["status"] == "running"
    assert len(created_threads) == 1
    assert created_threads[0].target is app_web_handlers._run_managed_external_cli_runtime_install
    assert created_threads[0].args == (cli_agent,)
    assert created_threads[0].name == f"{cli_agent}-managed-runtime-install"
    assert created_threads[0].daemon is True
    assert created_threads[0].started is True


def test_optional_dependency_install_times_out_after_one_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Process:
        stdout = None

        def __init__(self) -> None:
            self.killed = False
            self.waited = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> int:
            self.waited = True
            return 1

    class _Thread:
        def __init__(self, **_kwargs: object) -> None:
            self.joined = False

        def start(self) -> None:
            return None

        def join(self, *, timeout: int) -> None:
            assert timeout == 1
            self.joined = True

    process = _Process()
    monotonic_values = iter(
        [
            10.0,
            10.0 + app_web_handlers._OPTIONAL_DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
        ],
    )
    monkeypatch.setattr(app_web_handlers, "_is_frozen_runtime", lambda: False)
    monkeypatch.setattr(
        app_web_handlers,
        "_build_optional_dependency_install_args",
        lambda _package: ["installer"],
    )
    monkeypatch.setattr(
        app_web_handlers.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(app_web_handlers.threading, "Thread", _Thread)
    monkeypatch.setattr(
        app_web_handlers.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(
        RuntimeError,
        match="failed to install codex dependency: timed out",
    ):
        app_web_handlers._install_optional_dependency("codex", "package", "openai_codex")

    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_external_cli_codex_install_status_returns_snapshot():
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel))
    with app_web_handlers._CODEX_DEPENDENCY_INSTALL_LOCK:
        app_web_handlers._CODEX_DEPENDENCY_INSTALL_STATUS.update({
            "status": "running",
            "phase": "downloading",
            "error": "",
            "last_log": "Collecting openjiuwen",
            "log_tail": ["Collecting openjiuwen"],
            "started_at": 1.0,
            "finished_at": 0.0,
            "updated_at": 2.0,
            "downloaded_bytes": 1024,
            "total_bytes": 4096,
            "bytes_per_second": 512.0,
            "eta_seconds": 6.0,
            "artifact_index": 1,
            "artifact_count": 2,
            "current_package": "openai-codex-cli-bin",
            "current_version": "0.144.4",
            "download_attempt": 3,
            "download_max_attempts": 5,
            "switching_source": False,
        })

    await channel.methods["external_cli.codex_install_status"](
        object(),
        "req-codex-install-status",
        {},
        "sess-codex-install-status",
    )

    payload = channel.responses[-1]["payload"]
    assert channel.responses[-1]["ok"] is True
    assert payload["status"] == "running"
    assert payload["phase"] == "downloading"
    assert payload["log_tail"] == ["Collecting openjiuwen"]
    assert payload["log_tail"] is not app_web_handlers._CODEX_DEPENDENCY_INSTALL_STATUS["log_tail"]
    assert payload["downloaded_bytes"] == 1024
    assert payload["total_bytes"] == 4096
    assert payload["bytes_per_second"] == 512.0
    assert payload["eta_seconds"] == 6.0
    assert payload["download_attempt"] == 3
    assert payload["download_max_attempts"] == 5
    assert payload["switching_source"] is False


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
                "index": {"enabled": True},
                "discovery": {"max_results": 17},
            },
        }
    }

    flat = _flatten_symphony_for_config_panel(raw)

    assert flat["symphony_enabled"] == "true"
    assert "symphony_dynamic_graph_enabled" not in flat
    assert "symphony_orchestration_mode" not in flat
    assert flat["skill_retrieval_enabled"] == "true"
    assert flat["skill_retrieval_index_enabled"] == "true"
    assert flat["skill_retrieval_max_results"] == "17"
    assert "skill_retrieval_build_branching_factor" not in flat


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
            "skill_retrieval_index_enabled": "true",
        },
        "sess-3",
    )

    assert recorded_symphony == [{"enabled": True}]
    assert recorded_skill_retrieval == [
        {"enabled": False, "index": {"enabled": True}}
    ]
    assert channel.responses[-1] == {
        "id": "req-3",
        "ok": True,
        "payload": {
            "updated": [
                "symphony_enabled",
                "skill_retrieval_enabled",
                "skill_retrieval_index_enabled",
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


def test_web_forwards_only_canonical_personal_context_rpc_methods():
    methods = {
        "personal_context.runtime.status",
        "personal_context.runtime.start_collection",
        "personal_context.runtime.stop_collection",
        "personal_context.runtime.start_agent_use",
        "personal_context.runtime.stop_agent_use",
        "personal_context.runtime.get_config",
        "personal_context.runtime.patch_config",
        "personal_context.runtime.select_model",
        "personal_context.fetch.list_services",
        "personal_context.fetch.create_service",
        "personal_context.fetch.delete_service",
        "personal_context.fetch.patch_service",
        "personal_context.fetch.start_service",
        "personal_context.fetch.stop_service",
        "personal_context.fetch.run_all",
        "personal_context.fetch.run_one",
        "personal_context.fetch.get_run_status",
        "personal_context.fetch.get_authorization_status",
        "personal_context.fetch.authorize_provider",
        "personal_context.context.stream_graph",
        "personal_context.context.stream_tree",
        "personal_context.context.search_pages",
        "personal_context.context.get_node",
        "personal_context.context.get_source",
    }
    forwarded = {
        method
        for method in app_web_handlers._FORWARD_REQ_METHODS
        if method.startswith("personal_context.")
    }
    no_local = {
        method
        for method in app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS
        if method.startswith("personal_context.")
    }

    assert forwarded == methods
    assert no_local == methods
    assert len(methods) == 24


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


# ── media.persist 大图 HTTP bridge 分流（Phase 2 传输取舍） ──────────────────


def test_persist_media_locally_concurrent_same_name_does_not_clobber(tmp_path, monkeypatch):
    """单用户大图本地落盘在同名并发请求下必须各自占用唯一文件。"""
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )

    def _persist(index: int):
        return app_web_handlers._persist_media_locally(
            f"image-{index}".encode(), "session-1", "image.png"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_persist, range(16)))

    assert all(ok for ok, _payload in results)
    paths = [Path(payload["path"]) for _ok, payload in results]
    assert len(set(paths)) == 16
    assert {path.read_bytes() for path in paths} == {
        f"image-{index}".encode() for index in range(16)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_succeeds", [True, False])
async def test_pre_persist_large_media_splits_or_keeps_oversized_images(
    monkeypatch, upload_succeeds,
):
    """超预算（>4MB）的 base64 图片：HTTP 上传成功时转 ``_persisted``（不再携带
    base64，由 AgentServer 透传落盘记录）；上传失败时保留原 base64 项（由下游
    链路返回可重试错误），不静默丢图。小图始终保留 base64 走 E2A。"""
    import base64

    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
        _pre_persist_large_media,
    )
    from jiuwenswarm.gateway.routing.agent_http_bridge import E2A_PAYLOAD_MAX_BYTES

    big_b64 = base64.b64encode(b"x" * (E2A_PAYLOAD_MAX_BYTES + 1)).decode("ascii")
    small_b64 = base64.b64encode(b"small-image-bytes").decode("ascii")
    uploaded = {}

    async def fake_upload(item, data, *, session_id, index, agent_client, user_id):
        if not upload_succeeds:
            return None
        uploaded[index] = data
        return {
            "type": "image",
            "filename": "big.png",
            "mime_type": "image/png",
            "path": f"/tmp/uploads/big-{index}.png",
            "size_bytes": len(data),
            "_persisted": True,
        }

    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.app_web_handlers._upload_media_item_via_http",
        fake_upload,
    )

    params = await _pre_persist_large_media(
        {
            "content": "x",
            "media_items": [
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "filename": "big.png",
                    "base64Data": big_b64,
                },
                {
                    "type": "image",
                    "mimeType": "image/png",
                    "filename": "small.png",
                    "base64Data": small_b64,
                },
            ],
        },
        session_id="sess-1",
        agent_client=None,
        user_id=None,
    )

    items = params["media_items"]
    assert len(items) == 2
    if upload_succeeds:
        # 大图已转 HTTP 上传，不再携带 base64
        assert items[0]["_persisted"] is True
        assert "base64Data" not in items[0] and "base64_data" not in items[0]
        assert items[0]["path"] == "/tmp/uploads/big-0.png"
        assert 0 in uploaded
    else:
        # 上传失败：保留原 base64，交由下游链路返回可重试错误
        assert items[0]["base64Data"] == big_b64
        assert "_persisted" not in items[0]
        assert 0 not in uploaded
    # 小图始终保留 base64 走 E2A
    assert "_persisted" not in items[1]
    assert items[1]["base64Data"] == small_b64
    assert 1 not in uploaded
