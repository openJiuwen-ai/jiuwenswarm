# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from agent_gateway.common.config_panel import config_set_handlers as _gw_config_set_handlers
from agent_gateway.extensions.registry import ExtensionRegistry
from agent_gateway.channel_manager.web import app_web_handlers, web_http_routes
from agent_gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _build_external_cli_publish_url,
    _detect_external_cli_agent,
    _normalize_feishu_conf,
    _normalize_xiaoyi_conf,
    _register_web_handlers,
    _validate_wechat_numeric_params,
)
from agent_gateway.channel_manager.web.web_connect import WebChannel
from agent_gateway.heartbeat import HeartbeatServiceUnavailableError
from agent_gateway.runtime.cron import factory as _cron_factory_module

from tests.unit_tests.gateway.config_routing_harness import (
    make_config_routing_client,
    make_legacy_config_routing_client,
)
from jiuwenswarm.common.context_window import DEFAULT_CONTEXT_WINDOW_TOKENS
from jiuwenswarm.common.config_panel import config_set_handlers, models_handlers
from jiuwenswarm.common.config_panel.config_set_handlers import (
    flatten_external_cli_agents_for_config_panel as _flatten_external_cli_agents_for_config_panel,
    flatten_modes_team_for_config_panel as _flatten_modes_team_for_config_panel,
    flatten_symphony_for_config_panel as _flatten_symphony_for_config_panel,
    inject_external_cli_publish_url as _inject_external_cli_publish_url,
)
# gateway 进程的 bridge 已退役；测试进程按 server 侧同款"import 即注册"
# 装配注入点（tests 不受 R3 约束），使 config 面板等 common 消费路径
# 在本测试进程内可执行（生产形态为 gateway 经 WS 访问 AgentServer）。
import jiuwenswarm.server.runtime.a2ui.integration  # noqa: F401,E402
import jiuwenswarm.server.runtime.mcp.credential  # noqa: F401,E402
import jiuwenswarm.server.runtime.mcp.state_store  # noqa: F401,E402
import jiuwenswarm.server.runtime.session.session_metadata  # noqa: F401,E402
import jiuwenswarm.server.runtime.team_binding_store  # noqa: F401,E402
import jiuwenswarm.server.runtime.team_entity_store  # noqa: F401,E402
from jiuwenswarm.common.external_providers import register as _register_test_provider
from jiuwenswarm.server.runtime.mcp import registry as _mcp_registry_module

_register_test_provider("cron_store_factory", _cron_factory_module)
_register_test_provider("mcp_registry", _mcp_registry_module)
from agent_gateway.routing.agent_client import WebSocketAgentServerClient  # pylint: disable=wrong-import-order
from agent_gateway.routing.keys import AgentRef, RoutingKey  # pylint: disable=wrong-import-order


class FakeWebChannel:
    connection_user_id = staticmethod(WebChannel.connection_user_id)

    def __init__(self):
        self.channel_id = "web"
        self.methods: dict[str, object] = {}
        self.responses: list[dict] = []
        self.connect_handler = None
        self.disconnect_handler = None
        self.routing_keys: list[RoutingKey] = []
        self.rebounds: list[tuple[str | None, str]] = []
        self.cleared: list[str | None] = []
        self.forgotten_routes: list[tuple[RoutingKey, str]] = []

    def register_method(self, name, handler, *, local_only=False):
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

    async def routing_keys_for_ws(self, ws):
        return list(self.routing_keys)

    def record_a4p_rebound(self, session_id, ws_id):
        self.rebounds.append((session_id, ws_id))

    def clear_a4p_rebound(self, session_id):
        self.cleared.append(session_id)

    def forget_a4p_route(self, route, request_id):
        self.forgotten_routes.append((route, request_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["a4p.authorization.complete", "a4p.config.get", "a4p.config.update"])
async def test_a4p_rpc_uses_current_websocket_route(method) -> None:
    class _AgentClient:
        server_ready = True

        def __init__(self) -> None:
            self.envelope = None

        async def send_request(self, envelope):
            self.envelope = envelope
            return SimpleNamespace(ok=True, payload={"ok": True})

    channel = FakeWebChannel()
    channel.routing_keys = [
        RoutingKey(
            user_id="local",
            channel_id="web",
            app_id="app-1",
            agent_ref=AgentRef(mode="team", id="research"),
            session_id="session-1",
        )
    ]
    agent_client = _AgentClient()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=agent_client))
    ws = SimpleNamespace(_jiuwen_ws_id="ws-1")

    await channel.methods[method](
        ws,
        "rpc-1",
        {"requestId": "intent-1", "agent_ref": {"mode": "agent", "id": "untrusted"}},
        "session-1",
    )

    assert channel.responses[-1]["ok"] is True
    assert agent_client.envelope.channel_context == {
        "ws_id": "ws-1",
        "app_id": "app-1",
        "agent_ref": {"mode": "team", "id": "research"},
    }
    if method == "a4p.authorization.complete":
        # 完成授权时把恢复 turn 的输出改绑到当前连接（当前 ws_id 覆盖授权方冻结的旧 ws）。
        assert channel.rebounds == [("session-1", "ws-1")]
        assert channel.forgotten_routes == [(channel.routing_keys[0], "intent-1")]
    else:
        assert channel.rebounds == []
        assert channel.forgotten_routes == []


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


@pytest.mark.asyncio
async def test_models_get_returns_editable_detail_with_api_key(monkeypatch) -> None:
    class FakeCatalog:
        def get_public_model_detail(self, model_id, *, include_api_key=False):
            assert model_id == "mdl_a"
            assert include_api_key is True
            return {
                "model_id": model_id,
                "model_client_config": {"model_name": "a", "api_key": "test-saved-key"},
                "write_only_fields": [],
            }

    monkeypatch.setattr("jiuwenswarm.common.config_panel.model_resource_handlers.ModelCatalog", FakeCatalog)
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["models.get"](object(), "req-model", {"model_id": "mdl_a"}, "sid")

    response = channel.responses[-1]
    assert response["ok"] is True
    assert response["payload"]["model"]["model_client_config"]["api_key"] == "test-saved-key"


class _FakeModelsResponse:
    def __init__(self, model_ids):
        self.status_code = 200
        self._model_ids = model_ids

    def json(self):
        return {
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "owned_by": "system"}
                for model_id in self._model_ids
            ],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["exception", "empty"])
async def test_config_validate_model_retries_failed_probe_with_more_tokens(
    monkeypatch, first_failure
):
    channel = FakeWebChannel()
    max_tokens_calls = []

    class FakeModel:
        def __init__(self, *args, **kwargs):
            pass

        async def invoke(self, *args, **kwargs):
            max_tokens_calls.append(kwargs["max_tokens"])
            if len(max_tokens_calls) == 1:
                if first_failure == "exception":
                    raise RuntimeError("token budget too small")
                return {"content": "", "reasoning_content": ""}
            return {"content": "hello"}

    monkeypatch.setattr(models_handlers, "Model", FakeModel)
    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(models_handlers, "get_default_models", lambda _config: [])
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.validate_model"](
        object(),
        "req-validate-retry",
        {
            "model_provider": "openai",
            "model": "gpt-4.1",
            "api_base": "https://api.openai.com/v1",
            "api_key": "secret",
        },
        "sess-1",
    )

    assert max_tokens_calls == [3, 16]
    assert channel.responses[-1]["ok"] is True


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


@pytest.mark.asyncio
async def test_vendors_fetch_models_keeps_namespaced_models_for_other_vendors(monkeypatch):
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))
    monkeypatch.setattr(
        "httpx.get",
        lambda *args, **kwargs: _FakeModelsResponse(
            ["deepseek-chat", "vendor/deepseek-chat"]
        ),
    )

    await channel.methods["vendors.fetch_models"](
        object(),
        "req-fetch-models",
        {"vendor_key": "deepseek", "plan": "custom_api", "api_key": "secret"},
        "sess-1",
    )

    response = channel.responses[-1]
    assert response["ok"] is True
    assert response["payload"] == {
        "models": ["deepseek-chat", "vendor/deepseek-chat"],
        "source": "remote",
    }


class _OfflineRemoteAgentClient:
    server_ready = False


_OfflineRemoteAgentClient.__module__ = "jiuwenswarm.extensions.yuanrong.agent_client"


class _CapturingCronController:
    def __init__(self) -> None:
        self.params = None

    async def create_job(self, params):
        self.params = dict(params)
        return {"id": "cron-1"}


async def _fail_if_asked_agent_binding(**_kwargs):
    """默认项目不需要问 Agent：被调用即说明空编号走了 AgentServer 解析。"""
    raise AssertionError("must not ask the Agent to resolve the default project binding")


class _OwnedCronController:
    def __init__(self) -> None:
        self.job = SimpleNamespace(id="cron-owner", user_id="user-owner")
        self.update_calls = 0

    async def get_job(self, job_id, **_kwargs):
        return self.job if job_id == self.job.id else None

    async def update_job(self, job_id, patch, **_kwargs):
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
@pytest.mark.parametrize("enterprise", [False, True])
@pytest.mark.parametrize("client_kind", ["legacy", "remote", "missing"])
@pytest.mark.parametrize(
    "method, params",
    [
        ("session.list", {"limit": 5}),
        ("session.rename", {"session_id": "current", "title": "New title"}),
        ("session.pin", {"session_id": "current", "pinned": True}),
    ],
)
async def test_session_management_unavailable_without_server(
    monkeypatch, enterprise, client_kind, method, params,
) -> None:
    """Offline session operations must never access Gateway's local session data."""
    monkeypatch.setattr("agent_gateway.enterprise.is_enterprise", lambda: enterprise)
    clients = {
        "legacy": WebSocketAgentServerClient(),
        "remote": _OfflineRemoteAgentClient(),
        "missing": None,
    }
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=clients[client_kind]))

    def reject_local_access(*args, **kwargs):
        raise AssertionError("must not access Gateway session data")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        reject_local_access,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.set_session_pinned",
        reject_local_access,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_rename.apply_session_rename",
        reject_local_access,
    )
    await channel.methods[method](object(), "req-offline", params, "current", "user-42")

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "SERVICE_UNAVAILABLE"


class _DictOwnedCronController:
    """Mimics the real ``CronController.get_job`` which returns ``to_dict()``."""

    def __init__(self) -> None:
        self.job = {"id": "cron-dict-owner", "user_id": "user-owner"}
        self.run_now_calls = 0
        self.get_kwargs: dict = {}

    async def get_job(self, job_id, **kwargs):
        self.get_kwargs = kwargs
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
    assert cron.get_kwargs["project_id"] is None
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["run_id"] == "run-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("targets", ["web", "feishu:im-app"])
async def test_agentos_cron_create_does_not_read_gateway_session_metadata(monkeypatch, targets) -> None:
    """AgentOS project binding must stay in the routed user AgentServer directory."""
    channel = FakeWebChannel()
    cron = _CapturingCronController()
    client = _CapturingSessionListAgentClient()
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=client, cron_controller=cron)
    )
    monkeypatch.setattr(
        "agent_gateway.routing.e2a_proxy.is_agentos_routing_client",
        lambda _client: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read Gateway metadata")),
    )

    await channel.methods["cron.job.create"](
        SimpleNamespace(_jiuwen_app_id="browser-app"), "req-cron",
        {"name": "job", "cron_expr": "* * * * *", "targets": targets}, "user-session", "user-1"
    )

    assert channel.responses[-1]["ok"] is True
    # 没选项目不问 Agent，也不标记「已经在 Agent 上核对过」。
    assert "_agentos_project_binding_verified" not in cron.params
    assert "project_dir" not in cron.params
    assert cron.params.get("app_id") == ("browser-app" if targets == "web" else None)
    # 未选项目按 web 通道默认 work 模式本地物化 default（#5713，不落空串）。
    assert cron.params["project_id"] == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_jev_clear_gateway_route_persists_and_reloads_in_agentserver(tmp_path, monkeypatch, legacy):
    from unittest.mock import AsyncMock

    from dotenv import dotenv_values
    import yaml

    from agent_gateway.common import config as config_module
    from jiuwenswarm.common import config as server_config_module
    from jiuwenswarm.common.browser_settings import JEV_PROVIDER_DEFAULTS

    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    decision = {
        **JEV_PROVIDER_DEFAULTS["openrouter"],
        "mode": "hybrid", "provider": "openrouter", "display_name": "Shared Jev",
    }
    config_path.write_text(yaml.safe_dump({
        "browser": {"chrome_path": "/custom/chrome", "headless": False, "decision": decision},
        "experimental": {"multi_model_collaboration_enabled": True,
                         "multi_model_collaboration": {"jev_enabled": True, "verifier_interval": 5}},
    }), encoding="utf-8")
    env_path.write_text("OPENROUTER_API_KEY=synthetic-router\nTYPESAFE_API_KEY=synthetic-typesafe\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(config_module, "get_config_file", lambda: config_path)
    monkeypatch.setattr(server_config_module, "CONFIG_YAML_PATH", config_path)
    monkeypatch.setattr(server_config_module, "get_config_file", lambda: config_path)
    monkeypatch.setattr(config_set_handlers, "ENV_FILE", env_path)
    monkeypatch.setattr(config_set_handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-router")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-typesafe")
    reload = AsyncMock(return_value=True)
    restart = AsyncMock()
    client = make_legacy_config_routing_client(reload) if legacy else make_config_routing_client(reload)
    monkeypatch.setattr(app_web_handlers, "_clear_agent_config_cache", AsyncMock())
    monkeypatch.setattr(app_web_handlers, "_restart_agent_browser_runtime", restart)
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    await channel.methods["path.set"](
        object(), "clear-browser", {
            "chrome_path": "/custom/chrome", "headless": False,
            "decision_mode": "llm", "decision_display_name": "",
        }, "session", user_id="user-one",
    )
    assert channel.responses[-1]["ok"] is True
    await channel.methods["config.save_all"](
        object(), "clear-key", {"config": {"jev_openrouter_api_key": "", "moa_jev_enabled": "false"}},
        "session", user_id="user-one",
    )
    assert channel.responses[-1]["ok"] is True
    await channel.methods["path.get"](object(), "read-browser", {}, "session", user_id="user-one")
    response = channel.responses[-1]
    assert response["ok"] is True
    assert response["payload"]["jev_key_configured"] is False
    assert response["payload"]["decision_display_name"] == ""
    saved = yaml.safe_load(config_path.read_text())
    assert saved["browser"] == {
        "chrome_path": "/custom/chrome", "headless": False,
        "decision": {**decision, "mode": "llm", "display_name": ""},
    }
    assert saved["experimental"]["multi_model_collaboration_enabled"] is True
    assert saved["experimental"]["multi_model_collaboration"] == {"jev_enabled": False, "verifier_interval": 5}
    assert dotenv_values(env_path) == {"OPENROUTER_API_KEY": "", "TYPESAFE_API_KEY": "synthetic-typesafe"}
    reload.assert_awaited_once()
    restart.assert_awaited_once_with(client, previous_chrome_path="/custom/chrome", previous_headless=False)


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

    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(
        models_handlers,
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
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["models.list"](object(), "req-models", {}, "session-1")

    model = channel.responses[-1]["payload"]["models"][0]
    assert channel.responses[-1]["ok"] is True
    assert model["vendor_key"] == "alibaba"
    assert model["plan"] == "token_plan"
    assert model["context_window_tokens"] == DEFAULT_CONTEXT_WINDOW_TOKENS


@pytest.mark.asyncio
async def test_enterprise_no_runtime_models_list_uses_gateway_manager(
    monkeypatch,
) -> None:
    """企业版无 runtime（legacy WebSocket）走 Gateway 企业库，不代理 AS yaml。"""
    from agent_gateway.enterprise.config.effective.schemas import TemplateRefSlot

    async def _fake_load(_request, _slots):
        return SimpleNamespace(
            models={
                TemplateRefSlot.DEFAULT_MODEL.value: [
                    {
                        "model_id": "GLM-5.3",
                        "template_name": "企业默认",
                        "api_base": "https://manager.example/v1",
                        "model_provider": "OpenAI",
                        "timeout": 60,
                        "parameters": {"temperature": 0.7, "reasoning_level": "off"},
                    }
                ]
            }
        )

    monkeypatch.setattr(
        "agent_gateway.enterprise.is_enterprise",
        lambda: True,
    )
    monkeypatch.setattr(
        "agent_gateway.enterprise.config.effective.loader.load_effective_enterprise_config",
        _fake_load,
    )
    # 若误走 A4g E2A / 个人 yaml，会落到这些占位值
    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(
        models_handlers,
        "get_default_models",
        lambda _config: [{
            "model_client_config": {
                "model_name": "mdl_default_primary",
                "api_base": "https://example.com/v1",
                "api_key": "secret",
                "client_provider": "OpenAI",
            },
            "model_config_obj": {},
            "alias": "your-model-name",
            "is_default": True,
        }],
    )

    channel = FakeWebChannel()
    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_legacy_config_routing_client(),
        )
    )

    await channel.methods["models.list"](object(), "req-ent-models", {}, "session-1")

    resp = channel.responses[-1]
    assert resp["ok"] is True
    assert resp["payload"]["model_source"] == "enterprise"
    assert resp["payload"]["active_model"] == "GLM-5.3"
    assert resp["payload"]["models"][0]["model_name"] == "GLM-5.3"
    assert resp["payload"]["models"][0]["alias"] == "企业默认"
    assert resp["payload"]["models"][0]["api_key"] == ""


@pytest.mark.asyncio
async def test_personal_legacy_models_list_still_proxies_to_agent_server(
    monkeypatch,
) -> None:
    """个人版 legacy WebSocket 仍走 A4g E2A，不走企业 Gateway handler。"""
    from jiuwenswarm.server.runtime import opencode_zen

    monkeypatch.setattr(
        "agent_gateway.enterprise.is_enterprise",
        lambda: False,
    )
    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(
        models_handlers,
        "get_default_models",
        lambda _config: [{
            "model_client_config": {
                "model_name": "personal-from-yaml",
                "api_base": "https://example.com/v1",
                "api_key": "secret",
                "client_provider": "OpenAI",
            },
            "model_config_obj": {"temperature": 0.95},
            "alias": "personal-from-yaml",
            "is_default": True,
        }],
    )
    monkeypatch.setattr(opencode_zen, "get_zen_free_model_entries", lambda: [])
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.append_zen_free_models",
        lambda models: None,
    )

    channel = FakeWebChannel()
    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_legacy_config_routing_client(),
        )
    )

    await channel.methods["models.list"](object(), "req-personal", {}, "session-1")

    resp = channel.responses[-1]
    assert resp["ok"] is True
    assert resp["payload"].get("model_source") != "enterprise"
    assert resp["payload"]["models"][0]["model_name"] == "personal-from-yaml"


@pytest.mark.asyncio
async def test_models_list_omits_context_for_empty_template_model(monkeypatch) -> None:
    from jiuwenswarm.server.runtime import opencode_zen

    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(
        models_handlers,
        "get_default_models",
        lambda _config: [{
            "model_client_config": {
                "model_name": "",
                "api_base": "",
                "api_key": "",
                "client_provider": "",
            },
            "model_config_obj": {},
            "is_default": True,
        }],
    )
    monkeypatch.setattr(opencode_zen, "get_zen_free_model_entries", lambda: [])
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["models.list"](object(), "req-empty-model", {}, "session-1")

    model = channel.responses[-1]["payload"]["models"][0]
    assert model["model_name"] == ""
    assert "context_window_tokens" not in model


@pytest.mark.asyncio
async def test_models_list_builds_the_list_off_the_event_loop(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_available(config, auth_session=None):
        try:
            asyncio.get_running_loop()
            seen["on_event_loop"] = True
        except RuntimeError:
            seen["on_event_loop"] = False
        seen["auth_session"] = auth_session
        return [{"model_client_config": {"model_name": "m", "api_key": "k"}, "model_config_obj": {}}]

    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(models_handlers, "get_available_models", fake_available)
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))
    ws = SimpleNamespace(_jiuwen_auth_session="auth-sess-1")

    await channel.methods["models.list"](ws, "req-models", {}, "session-1")

    assert channel.responses[-1]["ok"] is True
    assert seen["on_event_loop"] is False, "get_available_models 不能在事件循环线程上执行"
    assert seen["auth_session"] == "auth-sess-1", "登录会话仍要按这条连接传进去"


@pytest.mark.asyncio
async def test_models_list_includes_cached_zen_free_models(monkeypatch) -> None:
    """Free models stay selectable: Gateway merges rows fetched from AgentServer."""
    zen_row = {
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
    }

    async def _rows(_client):
        return [zen_row]

    monkeypatch.setattr(models_handlers, "get_config", lambda: {"models": {}})
    monkeypatch.setattr(models_handlers, "get_default_models", lambda _config: [])
    # A4g：E2A 路径的 zen 免费模型合并在 server 侧 ConfigAdapter（等价于原
    # gateway wrapper 的 fetch_zen_free_model_rows + zen_rows 注入）
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.opencode_zen.append_zen_free_models",
        lambda models: models.append(zen_row),
    )
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

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
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("channel.feishu.set_conf", {"apps": [{"app_id": "app-1"}]}),
        ("channel.dingtalk.set_conf", {"apps": [{"client_id": "client-1", "enabled": False, "allow_from": ["tester"]}]}),
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
        "agent_gateway.channel_manager.web.app_web_handlers.update_channel_in_config",
        lambda channel_id, conf: persisted.append((channel_id, dict(conf))),
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers.replace_channel_subsection_with_cleanup",
        lambda channel_id, subsection, conf, keep_keys: persisted.append((channel_id, conf)),
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers.update_health_check_in_config",
        lambda payload: persisted.append(("health_check", dict(payload))),
    )
    async def edit_channel(channel_id, edit):
        saved = edit({})
        persisted.append((channel_id, saved))
        return saved

    monkeypatch.setattr("agent_gateway.channel_manager.web.persistent_bindings.edit_channel_body", edit_channel)

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
@pytest.mark.parametrize(
    "params,keys,scopes",
    [
        ({"api_base": "https://example.com/one"}, {"API_BASE"}, ["model"]),
        ({"bocha_api_key": "test-key"}, {"BOCHA_API_KEY"}, ["search"]),
        ({"serper_api_key": ""}, {"SERPER_API_KEY"}, ["search"]),
        ({"free_search_ddg_enabled": "true"}, {"FREE_SEARCH_DDG_ENABLED"}, ["search"]),
        ({"free_search_bing_enabled": "true"}, {"FREE_SEARCH_BING_ENABLED"}, ["search"]),
        (
            {"bocha_api_key": "test-key", "vision_api_key": "test-vision-key"},
            {"BOCHA_API_KEY", "VISION_API_KEY"},
            ["multimodal", "search"],
        ),
    ],
)
async def test_config_set_applies_scoped_reload_before_responding(monkeypatch, tmp_path, params, keys, scopes):
    channel = FakeWebChannel()
    reload_started = asyncio.Event()
    release_first_reload = asyncio.Event()
    reload_calls: list[tuple[set[str], dict, dict]] = []

    monkeypatch.setattr(
        "agent_gateway.extensions.registry.ExtensionRegistry.get_instance",
        lambda: SimpleNamespace(get_crypto_provider=lambda: None, get_crypto_utility_extension=lambda: None),
    )
    for key in keys:
        monkeypatch.setenv(key, "")

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.ENV_FILE",
        tmp_path / ".env",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
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
            agent_client=make_config_routing_client(on_config_saved),
            on_config_saved=on_config_saved,
        )
    )

    task = asyncio.create_task(channel.methods["config.set"](
        object(),
        "req-1",
        params,
        "sess-1",
    ))

    try:
        await asyncio.wait_for(reload_started.wait(), timeout=1)
        assert channel.responses == []
    finally:
        release_first_reload.set()
        await task

    assert reload_calls[0][0] == keys
    assert reload_calls[0][2]["target_channel_id"] == "web"
    assert reload_calls[0][2]["reload_scopes"] == scopes
    assert channel.responses[-1]["id"] == "req-1"
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["applied_without_restart"] is True


@pytest.mark.asyncio
async def test_config_set_reports_saved_when_hot_reload_callback_fails(monkeypatch, tmp_path):
    channel = FakeWebChannel()

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.ENV_FILE",
        tmp_path / ".env",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"models": {"defaults": []}},
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        raise RuntimeError("agent unreachable at 10.0.0.1")

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_config_routing_client(on_config_saved),
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
        config_set_handlers,
        "get_config_raw",
        lambda: {"setup_guide": {"enabled": True}},
    )
    monkeypatch.setattr(
        config_set_handlers,
        "get_config",
        lambda: {"setup_guide": {"enabled": False}},
    )
    monkeypatch.setattr(
        config_set_handlers,
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
            agent_client=make_config_routing_client(on_config_saved),
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
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(config_set_handlers, "get_config", lambda: raw_config)
    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        lambda: SimpleNamespace(get_crypto_provider=lambda: None, get_crypto_utility_extension=lambda: None),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.get"](
        object(),
        "req-get-setup-guide",
        {},
        "sess-get-setup-guide",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["setup_guide_enabled"] == expected


@pytest.mark.asyncio
async def test_config_get_returns_jev_credentials_like_model_settings(monkeypatch):
    """The authorized settings editor can reload the existing provider credentials."""
    channel = FakeWebChannel()
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-openrouter-secret")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-typesafe-secret")
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {})
    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        lambda: SimpleNamespace(get_crypto_provider=lambda: None, get_crypto_utility_extension=lambda: None),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.get"](object(), "req-get-jev", {}, "sess-get-jev")

    payload = channel.responses[-1]["payload"]
    assert payload["jev_openrouter_api_key"] == "synthetic-openrouter-secret"
    assert payload["jev_typesafe_api_key"] == "synthetic-typesafe-secret"
    assert config_set_handlers.CONFIG_SET_ENV_MAP["jev_openrouter_api_key"] == "OPENROUTER_API_KEY"
    assert config_set_handlers.CONFIG_SET_ENV_MAP["jev_typesafe_api_key"] == "TYPESAFE_API_KEY"


@pytest.mark.asyncio
async def test_trajectory_diagnosis_switch_round_trips_through_config_rpc(monkeypatch):
    channel = FakeWebChannel()
    persisted: list[bool] = []
    monkeypatch.setattr(
        app_web_handlers,
        "get_config_raw",
        lambda: {"trajectory_ui": {"enabled": False, "diagnosis_enabled": False}},
    )
    monkeypatch.setattr(
        app_web_handlers,
        "get_config",
        lambda: {"trajectory_ui": {"enabled": False, "diagnosis_enabled": False}},
    )
    # config.set 路径实现已下沉 config_set_handlers，config.get 仍读 gateway 侧，双patch
    monkeypatch.setattr(
        config_set_handlers,
        "get_config_raw",
        lambda: {"trajectory_ui": {"enabled": False, "diagnosis_enabled": False}},
    )
    monkeypatch.setattr(
        config_set_handlers,
        "get_config",
        lambda: {"trajectory_ui": {"enabled": False, "diagnosis_enabled": False}},
    )
    monkeypatch.setattr(
        config_set_handlers,
        "update_trajectory_diagnosis_in_config",
        lambda enabled: persisted.append(enabled),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.get"](object(), "req-trajectory-get", {}, "session")
    assert channel.responses[-1]["payload"]["trajectory_ui_diagnosis_enabled"] == "false"

    await channel.methods["config.set"](
        object(),
        "req-trajectory-set",
        {"trajectory_ui_diagnosis_enabled": "true"},
        "session",
    )
    assert persisted == [True]
    assert channel.responses[-1]["payload"]["updated"] == ["trajectory_ui_diagnosis_enabled"]
    # Diagnosis owns the data plane and chat/trajectory tabs.
    diagnosis_change_set = config_set_handlers.ConfigChangeSet({}, ["trajectory_ui_diagnosis_enabled"])
    assert diagnosis_change_set.reload_scopes == {"agent_runtime", "web_ui"}


@pytest.mark.asyncio
async def test_task_full_duplex_switch_round_trips_through_config_rpc(monkeypatch):
    channel = FakeWebChannel()
    persisted: list[bool] = []
    raw_config = {"experimental": {"task_full_duplex_enabled": False}}
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: raw_config)
    # config.set 路径实现已下沉 config_set_handlers，config.get 仍读 gateway 侧，双patch
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(config_set_handlers, "get_config", lambda: raw_config)
    monkeypatch.setattr(
        config_set_handlers,
        "update_task_full_duplex_in_config",
        lambda enabled: persisted.append(enabled),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.get"](object(), "req-duplex-get", {}, "session")
    assert channel.responses[-1]["payload"]["task_full_duplex_enabled"] == "false"

    await channel.methods["config.set"](
        object(),
        "req-duplex-set",
        {"task_full_duplex_enabled": "true"},
        "session",
    )
    assert persisted == [True]
    assert channel.responses[-1]["payload"]["updated"] == ["task_full_duplex_enabled"]
    change_set = config_set_handlers.ConfigChangeSet({}, ["task_full_duplex_enabled"])
    assert change_set.reload_scopes == {"web_ui"}


@pytest.mark.asyncio
async def test_progressive_tool_optimize_switch_round_trips_through_config_rpc(monkeypatch):
    channel = FakeWebChannel()
    persisted: list[bool] = []
    raw_config = {"progressive_tool_enabled": True, "progressive_tool_optimize": False}
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: raw_config)
    # config.set 路径实现已下沉 config_set_handlers，config.get 仍读 gateway 侧，双patch
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(config_set_handlers, "get_config", lambda: raw_config)
    monkeypatch.setattr(
        config_set_handlers,
        "update_progressive_tool_optimize_in_config",
        lambda enabled: persisted.append(enabled),
    )
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client())
    )

    await channel.methods["config.get"](object(), "req-tool-optimize-get", {}, "session")
    assert channel.responses[-1]["payload"]["progressive_tool_optimize"] == "false"

    await channel.methods["config.set"](
        object(),
        "req-tool-optimize-set",
        {"progressive_tool_optimize": "true"},
        "session",
    )
    assert persisted == [True]
    assert channel.responses[-1]["payload"]["updated"] == ["progressive_tool_optimize"]
    change_set = config_set_handlers.ConfigChangeSet({}, ["progressive_tool_optimize"])
    assert change_set.reload_scopes == {"agent_runtime"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_config", "expected"),
    [
        ({}, "true"),
        ({"rsi": {"enabled": False}}, "false"),
    ],
)
async def test_config_get_returns_rsi_switch(monkeypatch, raw_config, expected):
    channel = FakeWebChannel()
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(config_set_handlers, "get_config", lambda: raw_config)
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.get"](
        object(),
        "req-get-rsi",
        {},
        "sess-get-rsi",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["rsi_enabled"] == expected


@pytest.mark.asyncio
async def test_config_save_all_persists_rsi_switch(monkeypatch):
    channel = FakeWebChannel()
    persisted: list[bool] = []
    reload_options_seen: list[dict] = []

    monkeypatch.setattr(
        config_set_handlers,
        "get_config_raw",
        lambda: {"rsi": {"enabled": True}},
    )
    monkeypatch.setattr(
        config_set_handlers,
        "get_config",
        lambda: {"rsi": {"enabled": False}},
    )
    monkeypatch.setattr(
        config_set_handlers,
        "update_rsi_enabled_in_config",
        lambda enabled: persisted.append(enabled),
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        del updated_keys, env_updates, config_payload
        reload_options_seen.append(dict(reload_options))
        return True

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_config_routing_client(on_config_saved),
            on_config_saved=on_config_saved,
        )
    )

    await channel.methods["config.save_all"](
        object(),
        "req-set-rsi",
        {"config": {"rsi_enabled": False}},
        "sess-set-rsi",
    )

    assert persisted == [False]
    assert reload_options_seen == [
        {
            "target_channel_id": "web",
            "reload_scopes": ["agent_runtime"],
        }
    ]
    assert channel.responses[-1]["payload"] == {
        "updated": ["rsi_enabled"],
        "applied_without_restart": True,
        "models_count": None,
    }


def test_media_capability_config_uses_multimodal_hot_reload_scope():
    for env_key in config_set_handlers.MULTIMODAL_RELOAD_ENV_KEYS:
        change_set = config_set_handlers.ConfigChangeSet({env_key: "true"}, [])
        assert change_set.reload_scopes == {"multimodal"}


def test_media_capability_provider_identity_has_exact_env_contract():
    for modality in ("vision", "audio", "video"):
        prefix = modality.upper()
        assert config_set_handlers.CONFIG_SET_ENV_MAP[f"{modality}_endpoint_profile"] == f"{prefix}_ENDPOINT_PROFILE"
        assert config_set_handlers.CONFIG_SET_ENV_MAP[f"{modality}_vendor_key"] == f"{prefix}_VENDOR_KEY"
        assert config_set_handlers.CONFIG_SET_ENV_MAP[f"{modality}_plan"] == f"{prefix}_PLAN"
        assert f"{prefix}_ENDPOINT_PROFILE" in config_set_handlers.MULTIMODAL_RELOAD_ENV_KEYS
        assert f"{prefix}_VENDOR_KEY" not in config_set_handlers.MULTIMODAL_RELOAD_ENV_KEYS
        assert f"{prefix}_PLAN" not in config_set_handlers.MULTIMODAL_RELOAD_ENV_KEYS


def test_task_chat_uses_general_asr_config_independent_from_joyai():
    assert config_set_handlers.CONFIG_SET_ENV_MAP["asr_api_base"] == "ASR_API_BASE"
    assert config_set_handlers.CONFIG_SET_ENV_MAP["asr_api_key"] == "ASR_API_KEY"
    assert config_set_handlers.CONFIG_SET_ENV_MAP["asr_model"] == "ASR_MODEL_NAME"
    assert not any(value.startswith("VOICE_ASR_") for value in config_set_handlers.ASR_ENV_KEYS)
    assert not any(value.startswith("JOYAI_") for value in config_set_handlers.ASR_ENV_KEYS)
    for env_key in config_set_handlers.ASR_ENV_KEYS:
        change_set = config_set_handlers.ConfigChangeSet({env_key: "value"}, [])
        assert change_set.reload_scopes == {"web_ui"}


@pytest.mark.asyncio
async def test_task_asr_rpc_returns_transcript(monkeypatch):
    channel = FakeWebChannel()

    async def fake_transcribe(params, *, environ):
        assert params == {"audio_base64": "YXVkaW8=", "mime_type": "audio/webm"}
        # 8ce9b60d 起远端配置路径会把 AgentServer 的 ASR 配置透传进来
        assert set(environ) >= {"ASR_API_BASE", "ASR_API_KEY", "ASR_MODEL_NAME"}
        return "transcribed speech"

    monkeypatch.setattr(app_web_handlers, "transcribe_task_audio", fake_transcribe)
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["task.asr.transcribe"](
        object(),
        "req-task-asr",
        {"audio_base64": "YXVkaW8=", "mime_type": "audio/webm"},
        "session",
    )

    assert channel.responses[-1] == {
        "id": "req-task-asr",
        "ok": True,
        "payload": {"text": "transcribed speech"},
        "error": None,
        "code": None,
    }


@pytest.mark.asyncio
async def test_media_capability_provider_identity_round_trips_through_config_rpc(monkeypatch, tmp_path):
    channel = FakeWebChannel()
    monkeypatch.setattr(config_set_handlers, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(app_web_handlers, "get_config", lambda: {"models": {"defaults": []}})
    monkeypatch.setattr(app_web_handlers, "get_config_raw", lambda: {})
    # config.set 路径实现已下沉 config_set_handlers，config.get 仍读 gateway 侧，双patch
    monkeypatch.setattr(config_set_handlers, "get_config", lambda: {"models": {"defaults": []}})
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: {})

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        del updated_keys, env_updates, config_payload
        assert reload_options["reload_scopes"] == ["multimodal"]
        return True

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_config_routing_client(on_config_saved),
            on_config_saved=on_config_saved,
        )
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
@pytest.mark.parametrize(
    ("permissions", "expected_profile", "expected_enabled"),
    [
        ({"enabled": True, "mode": "manual"}, "default", "true"),
        ({"enabled": True, "mode": "auto"}, "automatic", "true"),
        ({"enabled": False, "mode": "auto"}, "full_access", "false"),
        ({"enabled": True, "mode": "future"}, "default", "true"),
    ],
)
async def test_config_get_returns_canonical_permission_profile(
    monkeypatch, permissions, expected_profile, expected_enabled
):
    channel = FakeWebChannel()
    raw_config = {"permissions": permissions}
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: raw_config)
    monkeypatch.setattr(config_set_handlers, "get_config", lambda: raw_config)
    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        lambda: SimpleNamespace(get_crypto_provider=lambda: None, get_crypto_utility_extension=lambda: None),
    )
    monkeypatch.setattr(
        config_set_handlers, "flatten_modes_team_for_config_panel", lambda raw: {}
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.get"](object(), "req-profile", {}, "sess-profile")

    payload = channel.responses[-1]["payload"]
    assert payload["permissions_profile"] == expected_profile
    assert payload["permissions_enabled"] == expected_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"permissions_profile": "invalid"},
        {"permissions_mode": "auto"},
        {"permissions_profile": "automatic", "permissions_enabled": "true"},
    ],
)
async def test_config_set_rejects_invalid_permission_facade(monkeypatch, params):
    channel = FakeWebChannel()
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: {})
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.set"](object(), "req-profile", params, "sess-profile")

    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "saved_profile", "canonical"),
    [
        (
            {"permissions_profile": "automatic"},
            "automatic",
            {"permissions_profile": "automatic", "permissions_enabled": "true"},
        ),
        (
            {"permissions_enabled": "false"},
            "full_access",
            {"permissions_profile": "full_access", "permissions_enabled": "false"},
        ),
    ],
)
async def test_config_set_returns_canonical_permission_facade(
    monkeypatch, params, saved_profile, canonical
):
    channel = FakeWebChannel()
    saved: list[str] = []
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(
        config_set_handlers,
        "update_permissions_profile_in_config",
        lambda profile: saved.append(profile),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.set"](object(), "req-profile", params, "sess-profile")

    assert saved == [saved_profile]
    assert channel.responses[-1]["payload"]["canonical_config"] == canonical


@pytest.mark.asyncio
async def test_config_save_all_returns_canonical_permission_facade(monkeypatch):
    channel = FakeWebChannel()
    saved: list[str] = []
    reload_options_seen: list[dict] = []
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(
        config_set_handlers,
        "update_permissions_profile_in_config",
        lambda profile: saved.append(profile),
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        del updated_keys, env_updates, config_payload
        reload_options_seen.append(dict(reload_options))
        return True

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_config_routing_client(on_config_saved),
            on_config_saved=on_config_saved,
        )
    )

    await channel.methods["config.save_all"](
        object(),
        "req-save-all-profile",
        {"config": {"permissions_enabled": "false"}},
        "sess-profile",
    )

    assert saved == ["full_access"]
    assert reload_options_seen == [{
        "target_channel_id": "web",
        "reload_scopes": ["permissions"],
    }]
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["updated"] == ["permissions_enabled"]
    assert channel.responses[-1]["payload"]["applied_without_restart"] is True
    assert channel.responses[-1]["payload"]["canonical_config"] == {
        "permissions_profile": "full_access",
        "permissions_enabled": "false",
    }


@pytest.mark.asyncio
async def test_invalid_combined_payload_does_not_persist_permission_profile(
    monkeypatch,
):
    channel = FakeWebChannel()
    saved: list[str] = []
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(
        config_set_handlers,
        "update_permissions_profile_in_config",
        lambda profile: saved.append(profile),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.set"](
        object(),
        "req-invalid-combined-profile",
        {"permissions_profile": "full_access", "model_provider": "invalid"},
        "sess-profile",
    )

    assert saved == []
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_permission_profile_write_failure_returns_no_canonical_success(
    monkeypatch,
):
    channel = FakeWebChannel()
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: {})

    def fail_update(_profile: str) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(
        config_set_handlers,
        "update_permissions_profile_in_config",
        fail_update,
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.set"](
        object(),
        "req-profile-write-failure",
        {"permissions_profile": "automatic"},
        "sess-profile",
    )

    response = channel.responses[-1]
    assert response["ok"] is False
    assert response["code"] == "INTERNAL_ERROR"
    assert response["payload"] is None


@pytest.mark.asyncio
async def test_config_save_all_kvc_failure_does_not_persist_permission_profile(
    monkeypatch,
):
    channel = FakeWebChannel()
    saved: list[str] = []
    affinity_checks: list[None] = []

    def invalid_affinity(_models, *, affinity_enabled):
        assert affinity_enabled is True
        affinity_checks.append(None)
        raise ValueError("invalid affinity")

    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        lambda: SimpleNamespace(get_crypto_provider=lambda: None, get_crypto_utility_extension=lambda: None),
    )
    monkeypatch.setattr(config_set_handlers, "get_config_raw", lambda: {})
    monkeypatch.setattr(config_set_handlers, "update_default_models_in_config", invalid_affinity)
    monkeypatch.setattr(
        config_set_handlers,
        "update_kv_cache_affinity_enabled_in_config",
        lambda _enabled: None,
    )
    monkeypatch.setattr(
        config_set_handlers,
        "update_permissions_profile_in_config",
        lambda profile: saved.append(profile),
    )
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    await channel.methods["config.save_all"](
        object(),
        "req-save-all-invalid-kvc",
        {
            "config": {"permissions_profile": "automatic", "kv_cache_affinity_enabled": "true"},
            "models": [
                {
                    "model_name": "model-one",
                    "api_base": "https://example.invalid/v1",
                    "api_key": "TEST_ONLY_API_KEY",
                    "model_provider": "OpenAI",
                    "is_default": True,
                }
            ],
        },
        "sess-profile",
    )

    assert affinity_checks == [None]
    assert saved == []
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_models_replace_all_applies_scoped_reload_before_responding(monkeypatch):
    channel = FakeWebChannel()
    reload_started = asyncio.Event()
    release_reload = asyncio.Event()
    persisted: list[list[dict]] = []
    reload_options_seen: list[dict] = []

    def save_models(models, *, cron_references_checked):
        assert cron_references_checked is True
        persisted.append(list(models))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.models_handlers.get_config_raw",
        lambda: {"models": {"defaults": []}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.models_handlers.get_default_models",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.models_handlers.update_default_models_in_config",
        save_models,
    )
    monkeypatch.setattr(
        models_handlers,
        "_get_crypto_provider",
        lambda: SimpleNamespace(encrypt=lambda value: value),
    )

    async def on_config_saved(updated_keys, *, env_updates, config_payload, reload_options):
        reload_options_seen.append(dict(reload_options))
        reload_started.set()
        await release_reload.wait()
        return True

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_config_routing_client(on_config_saved),
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
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

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
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

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

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr("jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.common.config_panel.config_set_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.replace_teams_in_config",
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

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_legacy_config_routing_client()))

    monkeypatch.setattr("jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.common.config_panel.config_set_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: dependency_checks.append(None) or None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.replace_teams_in_config",
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

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr("jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr("jiuwenswarm.common.config_panel.config_set_handlers.get_config",
                        lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: dependency_checks.append(None) or None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.replace_teams_in_config",
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
    histories_updates: list[dict[str, list[str]]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_legacy_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: dependency_checks.append(None) or None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_path_histories_in_config",
        lambda histories: histories_updates.append(dict(histories)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.replace_teams_in_config",
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
    # A builtin switch carries no cli_path change, so the history stays empty.
    assert histories_updates == [{}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_saves_external_cli_path_after_detection(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []
    histories_updates: list[dict[str, list[str]]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
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
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_claude_dependency_available_or_start_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_path_histories_in_config",
        lambda histories: histories_updates.append(dict(histories)),
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
    # No previous path existed, so there is nothing to append to the history.
    assert histories_updates == [{}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_claude_switch_keeps_codex_entry_verbatim(monkeypatch):
    """Toggling one card must not re-detect or rewrite the other agent's entry."""
    channel = FakeWebChannel()
    updates: list[tuple[list[dict], str | None]] = []
    histories_updates: list[dict[str, list[str]]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {
            "preferred_language": "zh",
            "modes": {
                "team": {
                    "jiuwen_team": {
                        "external_cli_agents": [
                            {"cli_agent": "codex", "cli_path": "/custom/codex"},
                        ],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
        lambda cli_agent, cli_path="": pytest.fail("untouched agents must not be re-detected"),
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_claude_dependency_available_or_start_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_path_histories_in_config",
        lambda histories: histories_updates.append(dict(histories)),
    )

    await channel.methods["config.set"](
        object(),
        "req-claude-switch-keeps-codex",
        {
            "external_cli_agent_claude_enabled": "true",
            "external_cli_agent_claude_use_builtin": "true",
        },
        "sess-claude-switch-keeps-codex",
    )

    assert updates == [
        (
            [{"cli_agent": "claude"}, {"cli_agent": "codex", "cli_path": "/custom/codex"}],
            "ws://127.0.0.1:19000/ws",
        )
    ]
    # The untouched codex path must not leak into any history write.
    assert histories_updates == [{}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_uses_builtin_codex_without_validating_stale_windows_script(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []
    histories_updates: list[dict[str, list[str]]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
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
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
        lambda cli_agent, cli_path="": pytest.fail("built-in mode must not validate cli_path"),
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_path_histories_in_config",
        lambda histories: histories_updates.append(dict(histories)),
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
    # This save carries no cli_path key, so nothing is appended here; the
    # switch-to-builtin append is covered by its own dedicated test.
    assert histories_updates == [{}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_appends_replaced_path_when_switching_to_builtin(monkeypatch):
    """Switching from a custom path to the built-in CLI keeps the old path.

    The built-in mode reports an empty effective path, but the replaced
    custom path must still join the history instead of being lost.
    """
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []
    histories_updates: list[dict[str, list[str]]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {
            "preferred_language": "zh",
            "modes": {
                "team": {
                    "jiuwen_team": {
                        "external_cli_agents": [
                            {"cli_agent": "codex", "cli_path": "/custom/codex"},
                        ],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
        lambda cli_agent, cli_path="": pytest.fail("built-in mode must not validate cli_path"),
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_path_histories_in_config",
        lambda histories: histories_updates.append(dict(histories)),
    )

    await channel.methods["config.set"](
        object(),
        "req-codex-switch-builtin",
        {
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "true",
            "external_cli_agent_codex_cli_path": "",
        },
        "sess-codex-switch-builtin",
    )

    # The built-in entry carries no cli_path, and the replaced custom path
    # joins the history instead of being lost.
    assert updates == [([{"cli_agent": "codex"}], "ws://127.0.0.1:19000/ws")]
    assert histories_updates == [{"codex": ["/custom/codex"]}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_appends_replaced_cli_path_to_history(monkeypatch):
    """Saving a different cli_path must persist the previous path as history."""
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []
    histories_updates: list[dict[str, list[str]]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {
            "preferred_language": "zh",
            "modes": {
                "team": {
                    "jiuwen_team": {
                        "external_cli_agents": [
                            {"cli_agent": "codex", "cli_path": "/custom/codex"},
                        ],
                        "external_cli_path_histories": {"codex": ["/older/codex"]},
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
        lambda cli_agent, cli_path="": {
            "cli_agent": cli_agent,
            "status": "ok",
            "path": "/new/codex",
            "version": "1.2.3",
            "reference_version": "1.2.3",
            "message": "",
        },
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_path_histories_in_config",
        lambda histories: histories_updates.append(dict(histories)),
    )

    await channel.methods["config.set"](
        object(),
        "req-codex-path-history",
        {
            "external_cli_agent_codex_enabled": "true",
            "external_cli_agent_codex_use_builtin": "false",
            "external_cli_agent_codex_cli_path": "/new/codex",
            "external_cli_agent_codex_path_histories": '["/older/codex"]',
        },
        "sess-codex-path-history",
    )

    assert updates == [([{"cli_agent": "codex", "cli_path": "/new/codex"}], "ws://127.0.0.1:19000/ws")]
    assert histories_updates == [{"codex": ["/older/codex", "/custom/codex"]}]
    assert channel.responses[-1]["ok"] is True


@pytest.mark.asyncio
async def test_config_set_history_only_save_rewrites_config_yaml(monkeypatch, tmp_path):
    """A history-only save must rewrite config.yaml for built-in agents too.

    The cleanup flow sends only ``path_histories`` keys; those keys must hit
    the config.set handling branch, or the UI reports a cleanup that the
    next config reload silently reverts.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "modes": {
                    "team": {
                        "jiuwen_team": {
                            "external_cli_agents": [
                                {"cli_agent": "claude", "cli_path": "/custom/claude"},
                            ],
                        },
                    },
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", config_file)

    channel = FakeWebChannel()
    agent_writes: list[list[dict[str, str]]] = []
    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {
            "preferred_language": "zh",
            "modes": {
                "team": {
                    "jiuwen_team": {
                        "external_cli_agents": [
                            {"cli_agent": "claude", "cli_path": "/custom/claude"},
                        ],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
        lambda cli_agent, cli_path="": pytest.fail("a history-only save must not detect any cli_path"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: agent_writes.append(list(agents)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.refresh_external_cli_builtin_models",
        lambda agents, **_: None,
    )

    await channel.methods["config.set"](
        object(),
        "req-claude-history-only",
        {"external_cli_agent_claude_path_histories": '["/old/claude", "/custom/claude"]'},
        "sess-claude-history-only",
    )

    assert channel.responses[-1]["ok"] is True
    # The entry is kept verbatim; the histories key is what changes on disk.
    assert agent_writes == [[{"cli_agent": "claude", "cli_path": "/custom/claude"}]]
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    histories = saved["modes"]["team"]["jiuwen_team"]["external_cli_path_histories"]
    assert histories == {"claude": ["/old/claude", "/custom/claude"]}


@pytest.mark.asyncio
async def test_config_set_rejects_codex_windows_script_when_manual_path(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[dict[str, str]], str | None]] = []

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
        lambda cli_agent, cli_path="": {
            "cli_agent": cli_agent,
            "status": "unsupported",
            "path": cli_path,
            "reason": "windows_script",
            "message": "windows_script",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
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

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.detect_external_cli_agent",
        lambda cli_agent, cli_path="": {
            "cli_agent": cli_agent,
            "status": "missing",
            "path": "",
            "message": "not found",
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
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
async def test_config_save_all_nested_shape_triggers_install_gate(monkeypatch):
    """A4g review P1 回归：save_all 嵌套键形状（前端真实链路）必须触发安装门控。

    前端外部 CLI 卡片的唯一保存通道是 config.save_all 且键嵌套在
    params.config 下；门控若只扫顶层键则对真实流量永不触发（安装不启动、
    agent 以启用态直接落盘）。
    """
    channel = FakeWebChannel()

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=make_legacy_config_routing_client())
    )

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: {"status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0},
    )

    await channel.methods["config.save_all"](
        object(),
        "req-save-all-nested-install",
        {
            "config": {
                "external_cli_agent_codex_enabled": "true",
                "external_cli_agent_codex_use_builtin": "true",
            }
        },
        "sess-save-all-nested",
    )

    assert channel.responses[-1]["ok"] is True
    installs = channel.responses[-1]["payload"]["external_cli_dependency_installs"]
    assert installs["codex"]["status"] == "running"
    # 安装启动即短路：嵌套键未被落盘（updated 为空）
    assert channel.responses[-1]["payload"]["updated"] == []


@pytest.mark.asyncio
async def test_install_gate_team_collision_top_level_wins(monkeypatch):
    """碰撞形状回归：顶层 team 与嵌套 config.team 并存时顶层优先（与
    config_save_all_handler 的合并顺序一致）。

    反向合并（嵌套覆盖顶层）时门控看到不含 codex 的嵌套 team 而漏判，
    落盘的却是顶层含 codex 的 team——codex 未经安装门控直接启用。
    """
    channel = FakeWebChannel()

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(
        WebHandlersBindParams(channel=channel, agent_client=make_legacy_config_routing_client())
    )

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    ensure_calls: list[None] = []
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: ensure_calls.append(None) or {
            "status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0,
        },
    )

    await channel.methods["config.save_all"](
        object(),
        "req-team-collision",
        {
            # 顶层 team 含 codex（save_all 落盘的正是这份）；嵌套 config.team 不含
            "team": [
                {"team_name": "alpha", "external_cli_agents": [{"cli_agent": "codex"}]},
            ],
            "config": {
                "team": [
                    {"team_name": "alpha", "external_cli_agents": [{"cli_agent": "claude"}]},
                ],
            },
        },
        "sess-team-collision",
    )

    # 门控按合并后（顶层优先）的 team 载荷判定：codex 命中 → ensure 被调、短路
    assert ensure_calls, "碰撞形状下门控必须按顶层 team 判定并触发安装"
    assert channel.responses[-1]["ok"] is True
    installs = channel.responses[-1]["payload"]["external_cli_dependency_installs"]
    assert installs["codex"]["status"] == "running"
    assert channel.responses[-1]["payload"]["updated"] == []


@pytest.mark.asyncio
async def test_config_set_starts_codex_dependency_install_without_saving_codex(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[str], str | None]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_legacy_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: {"status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
        lambda agents, publish_url=None: updates.append((agents, publish_url)),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_permissions_profile_in_config",
        lambda profile: saved_profiles.append(profile),
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

    # A4g 门控语义：安装启动时整条 config.set 短路（不写配置、不发 E2A），
    # 响应携带安装状态；安装完成后前端重发，配置才落盘（终态与原
    # "过滤安装中 agent 的部分写入"一致）。
    assert updates == []
    assert channel.responses[-1]["ok"] is True
    installs = channel.responses[-1]["payload"]["external_cli_dependency_installs"]
    assert installs["codex"]["status"] == "running"


@pytest.mark.asyncio
async def test_config_set_saves_claude_while_codex_dependency_is_installing(monkeypatch):
    channel = FakeWebChannel()
    updates: list[tuple[list[str], str | None]] = []

    monkeypatch.setenv("WEB_PORT", "19000")
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_legacy_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh", "modes": {"team": {"jiuwen_team": {}}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_codex_dependency_available_or_start_install",
        lambda: {"status": "running", "error": "", "started_at": 1.0, "finished_at": 0.0},
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._ensure_claude_dependency_available_or_start_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_external_cli_agents_in_config",
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

    # A4g 门控语义：codex 安装中即短路整条请求（claude 待安装完成后随重发
    # 一起落盘，终态与原部分写入一致）。
    assert updates == []
    assert channel.responses[-1]["ok"] is True
    installs = channel.responses[-1]["payload"]["external_cli_dependency_installs"]
    assert installs["codex"]["status"] == "running"
    assert "claude" not in installs


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
        "agent_gateway.channel_manager.web.app_web_handlers.importlib.util.find_spec",
        lambda name: None if name == "openai_codex" else original_find_spec(name),
    )

    def install_once(force_reinstall: bool = False) -> None:
        install_calls.append(None)
        install_started.set()
        release_install.wait(timeout=5)

    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._install_codex_dependency",
        install_once,
    )

    first = app_web_handlers._ensure_codex_dependency_available_or_start_install()
    assert install_started.wait(timeout=5)
    second = app_web_handlers._ensure_codex_dependency_available_or_start_install()
    release_install.set()

    assert first and first["status"] == "running"
    assert first["progress_kind"] == "installer_activity"
    assert second and second["status"] == "running"
    assert len(install_calls) == 1


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
        "jiuwenswarm.common.config_panel.config_set_handlers.ENV_FILE",
        tmp_path / ".env",
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"react": {"evolution": {"skill_evolution": value == "true"}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_skill_evolution_enabled_in_config",
        lambda enabled: evolution_updates.append(enabled),
    )

    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel,
            agent_client=make_config_routing_client(
                lambda _, **kwargs: (saved_updates.append(kwargs) or True)
            ),
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

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: current_config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.replace_teams_in_config",
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

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr("jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
                        lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"modes": {"team": {}}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.replace_teams_in_config",
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

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config_raw",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.get_config",
        lambda: {"symphony": {}},
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_symphony_in_config",
        lambda updates: recorded_symphony.append(updates),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config_panel.config_set_handlers.update_skill_retrieval_in_config",
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


# =====================================================================
# _normalize_xiaoyi_conf 纯函数测试
# =====================================================================


# =====================================================================
# get_conf 处理程序 — 验证归一化在读取时生效
# =====================================================================


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


# =====================================================================
# set_conf 处理程序 — 多应用模式（apps 键）
# =====================================================================


@pytest.mark.asyncio
async def test_channel_feishu_bind_agent_persists_one_to_one_persistent_agent_id(monkeypatch, tmp_path):
    from agent_gateway.common import config as config_module

    channel = FakeWebChannel()
    cm = FakeChannelManager()
    cm.configs["feishu"] = {
        "apps": [
            {
                "name": "现有应用",
                "app_id": "cli_existing",
                "app_secret": "existing-secret",
                "persistent_agent_id": "mate_existing",
            },
            {
                "name": "同事的旧应用",
                "app_id": "cli_old_for_new",
                "app_secret": "old-secret",
                "persistent_agent_id": "mate_new",
            },
        ]
    }
    config_path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(config_path, {"channels": {"feishu": cm.configs["feishu"]}})
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", config_path)

    async def send_request(envelope):
        # AgentServer answers with the agent it keeps for this app of the colleague.
        payload = {"agent_id": "mate_new"} if envelope.method == "jiuwenmate.connection.attach" else {}
        return SimpleNamespace(ok=True, payload=payload)

    _register_web_handlers(WebHandlersBindParams(
        channel=channel, channel_manager=cm, agent_client=SimpleNamespace(send_request=send_request),
    ))

    await channel.methods["channel.feishu.bind_agent"](
        object(),
        "req-bind-agent",
        {"app_id": "cli_new", "app_secret": "new-secret", "config_id": "mate_colleague"},
        "sess-1",
    )

    assert channel.responses[-1]["ok"] is True
    saved_config = config_module.load_yaml_round_trip(config_path)
    apps = saved_config["channels"]["feishu"]["apps"]
    assert [(app["app_id"], app.get("persistent_agent_id")) for app in apps] == [
        ("cli_existing", "mate_existing"),
        ("cli_new", "mate_new"),
    ]
    new_app = apps[1]
    assert new_app["app_secret"] == "new-secret"
    assert "agent_id" not in new_app
    assert cm.configs["feishu"]["apps"] == apps
    assert channel.responses[-1]["payload"]["binding"] == {
        "app_id": "cli_new",
        "persistent_agent_id": "mate_new",
    }


@pytest.mark.asyncio
async def test_channel_feishu_get_bindings_returns_ids_without_credentials():
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    cm.configs["feishu"] = {
        "apps": [
            {
                "app_id": "cli_bound",
                "app_secret": "must-not-leak",
                "persistent_agent_id": "mate_bound",
                "enabled": True,
            },
            {
                "app_id": "cli_unbound",
                "app_secret": "must-not-leak-either",
                "enabled": True,
            },
        ]
    }
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.feishu.get_bindings"](object(), "req-bindings", {}, "sess-1")

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {"agent_ids": ["mate_bound"]}


@pytest.mark.asyncio
async def test_channel_feishu_revoke_disables_only_the_bound_colleague(monkeypatch, tmp_path):
    from agent_gateway.common import config as config_module

    channel = FakeWebChannel()
    cm = FakeChannelManager()
    cm.configs["feishu"] = {
        "apps": [
            {
                "app_id": "cli_keep",
                "app_secret": "keep-secret",
                "persistent_agent_id": "mate_keep",
                "enabled": True,
            },
            {
                "app_id": "cli_drop",
                "app_secret": "drop-secret",
                "persistent_agent_id": "mate_drop",
                "enabled": True,
            },
        ]
    }
    config_path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(config_path, {"channels": {"feishu": cm.configs["feishu"]}})
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", config_path)
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.feishu.revoke"](
        object(), "req-revoke-feishu", {"persistent_agent_id": "mate_drop"}, "sess-1",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {"revoked": True}
    assert "drop-secret" not in str(channel.responses[-1])
    saved = config_module.load_yaml_round_trip(config_path)["channels"]["feishu"]["apps"]
    by_agent = {app["persistent_agent_id"]: app for app in saved}
    assert by_agent["mate_drop"]["enabled"] is False
    assert by_agent["mate_drop"]["app_secret"] == "drop-secret"
    assert by_agent["mate_keep"]["enabled"] is True
    assert cm.configs["feishu"]["apps"] == saved


@pytest.mark.asyncio
async def test_channel_welink_revoke_disables_polling_and_keeps_the_account(monkeypatch, tmp_path):
    from agent_gateway.common import config as config_module

    channel = FakeWebChannel()
    cm = FakeChannelManager()
    cm.configs["welink"] = {
        "enabled": True,
        "account": "h00557917",
        "persistent_agent_id": "mate_xiaojiu",
        "credential_dir": "C:/welink/mate_xiaojiu",
        "poll_interval_seconds": 5,
    }
    config_path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(config_path, {"channels": {"welink": cm.configs["welink"]}})
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", config_path)
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.welink.revoke"](
        object(), "req-revoke-other", {"persistent_agent_id": "mate_other"}, "sess-1",
    )
    assert channel.responses[-1]["ok"] is False
    assert cm.configs["welink"]["enabled"] is True

    await channel.methods["channel.welink.revoke"](
        object(), "req-revoke-welink", {"persistent_agent_id": "mate_xiaojiu"}, "sess-1",
    )

    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"] == {"revoked": True}
    saved = config_module.load_yaml_round_trip(config_path)["channels"]["welink"]
    assert saved["enabled"] is False
    assert saved["account"] == "h00557917"
    assert saved["bindings"] == []
    assert saved["reader_credential_dir"] == "C:/welink/mate_xiaojiu"
    assert cm.configs["welink"]["enabled"] is False
    assert cm.configs["welink"]["account"] == "h00557917"


# =====================================================================
# set_conf 处理程序 — 边界场景
# =====================================================================


@pytest.mark.asyncio
async def test_channel_feishu_set_conf_apps_empty_list(monkeypatch, tmp_path):
    """空 apps 列表 → 保存并返回空列表。"""
    channel = FakeWebChannel()
    cm = FakeChannelManager()
    from agent_gateway.common import config as config_module

    config_path = tmp_path / "config.yaml"
    config_path.write_text("channels: {}\n")
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", config_path)

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))

    await channel.methods["channel.feishu.set_conf"](
        object(), "req-empty", {"apps": []}, "sess-1"
    )

    assert config_module.load_yaml_round_trip(config_path)["channels"]["feishu"]["apps"] == []
    assert channel.responses[-1]["ok"] is True
    assert channel.responses[-1]["payload"]["config"]["apps"] == []


@pytest.mark.asyncio
async def test_channel_set_conf_channel_manager_unavailable(monkeypatch):
    """cm 为 None → 返回 SERVICE_UNAVAILABLE。"""
    channel = FakeWebChannel()
    # 不传 channel_manager，_resolve 返回 None
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))

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
        "agent_gateway.channel_manager.web.app_web_handlers.update_channel_in_config",
        lambda channel_id, conf: None,
    )
    monkeypatch.setattr(
        "agent_gateway.channel_manager.web.app_web_handlers._clear_agent_config_cache",
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


def test_update_channel_subsection_in_config_creates_missing_sections(tmp_path, monkeypatch):
    """当 channels / channel_id / subsection 不存在时，应自动创建。"""
    import yaml
    from agent_gateway.common import config as cfg

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
    from agent_gateway.common import config as cfg

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


def _a4p_config_request(method, params, channel="web", session="session"):
    from e2a.agent_models import AgentRequest
    from e2a.message import ReqMethod
    return AgentRequest(request_id="rpc", channel_id=channel, session_id=session,
                        req_method=ReqMethod(method), params=params)


@pytest.mark.asyncio
async def test_a4p_config_disabled_partial_update_and_get(monkeypatch):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc
    config = {"enabled": False, "require_user_signature": True, "server_id": "private"}
    events = []
    monkeypatch.setattr(a4p_rpc, "get_a4p_config", lambda: dict(config))
    def persist(updates):
        events.append("persist")
        config.update(updates)
    async def apply():
        events.append("apply")
    monkeypatch.setattr(a4p_rpc, "update_a4p_in_config", persist)
    monkeypatch.setattr(a4p_rpc, "reconfigure_a4p_runtime", apply)
    response = await a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.get", {"session_id": "session"}))
    assert response.ok and response.payload == {"enabled": False}
    response = await a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.update", {"enabled": True, "session_id": "session"}))
    assert response.ok and response.payload == {"enabled": True}
    assert events == ["persist", "apply"]
    assert config["server_id"] == "private"


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"session_id": "session"}, {"enabled": "false"}, {"enabled": 1}, {"enabled": None}, {"unknown": True}, {"enabled": True, "unknown": False}, {"require_user_signature": True}, {"require_user_signature": False}])
async def test_a4p_config_rejects_invalid_updates_without_writing(monkeypatch, params):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc
    monkeypatch.setattr(a4p_rpc, "update_a4p_in_config", lambda _: pytest.fail("unexpected write"))
    response = await a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.update", params))
    assert not response.ok and response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["a4p.config.get", "a4p.config.update"])
async def test_a4p_config_requires_web_route(monkeypatch, method):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc
    monkeypatch.setattr(a4p_rpc, "get_a4p_config", lambda: pytest.fail("unexpected config access"))
    response = await a4p_rpc.dispatch_a4p_request(_a4p_config_request(method, {"enabled": True}, channel="cli"))
    assert not response.ok and response.payload["code"] == "A4P_WEB_SESSION_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,code", [("persist", "INTERNAL_ERROR"), ("apply", "A4P_CONFIG_APPLY_FAILED")])
async def test_a4p_config_errors_are_reported(monkeypatch, stage, code):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc
    events = []
    def persist(updates):
        events.append("persist")
        if stage == "persist":
            raise OSError("disk unavailable")
    async def apply():
        events.append("apply")
        raise RuntimeError("apply failed")
    monkeypatch.setattr(a4p_rpc, "update_a4p_in_config", persist)
    monkeypatch.setattr(a4p_rpc, "reconfigure_a4p_runtime", apply)
    response = await a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.update", {"enabled": False}))
    assert not response.ok and response.payload["code"] == code
    assert events == (["persist"] if stage == "persist" else ["persist", "apply"])


@pytest.mark.asyncio
async def test_a4p_config_serializes_updates_and_reads(monkeypatch):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc
    monkeypatch.setattr(a4p_rpc, "_CONFIG_LOCK", asyncio.Lock())
    entered, release = asyncio.Event(), asyncio.Event()
    events = []
    config = {"enabled": False}
    def persist(updates):
        events.append(dict(updates))
        config.update(updates)
    async def apply():
        entered.set()
        await release.wait()
    monkeypatch.setattr(a4p_rpc, "update_a4p_in_config", persist)
    monkeypatch.setattr(a4p_rpc, "get_a4p_config", lambda: dict(config))
    monkeypatch.setattr(a4p_rpc, "reconfigure_a4p_runtime", apply)
    first = asyncio.create_task(a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.update", {"enabled": True})))
    await entered.wait()
    second = asyncio.create_task(a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.update", {"enabled": False})))
    read = asyncio.create_task(a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.get", {})))
    await asyncio.sleep(0)
    assert events == [{"enabled": True}]
    assert not second.done() and not read.done()
    release.set()
    responses = await asyncio.gather(first, second, read)
    assert all(response.ok for response in responses)
    assert responses[-1].payload == {"enabled": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["enabled"])
@pytest.mark.parametrize("value", ["true", "false", 0, 1, 0.0, None, [], {}])
async def test_a4p_config_boolean_fields_are_strict(monkeypatch, field, value):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc

    monkeypatch.setattr(a4p_rpc, "update_a4p_in_config", lambda _: pytest.fail("unexpected write"))
    response = await a4p_rpc.dispatch_a4p_request(
        _a4p_config_request("a4p.config.update", {field: value})
    )
    assert not response.ok and response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["a4p.config.get", "a4p.config.update"])
@pytest.mark.parametrize("params", [None, [], "invalid"])
async def test_a4p_config_requires_object_params(monkeypatch, method, params):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc

    monkeypatch.setattr(a4p_rpc, "update_a4p_in_config", lambda _: pytest.fail("unexpected write"))
    response = await a4p_rpc.dispatch_a4p_request(_a4p_config_request(method, params))
    assert not response.ok and response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"enabled": True}, {"unknown": True}, {"a4p_enabled": True}])
async def test_a4p_config_get_rejects_business_fields(monkeypatch, params):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc

    monkeypatch.setattr(a4p_rpc, "get_a4p_config", lambda: pytest.fail("unexpected read"))
    response = await a4p_rpc.dispatch_a4p_request(_a4p_config_request("a4p.config.get", params))
    assert not response.ok and response.payload["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"enabled": True},
    {"enabled": False},
])
async def test_a4p_config_round_trip_uses_persisted_values(monkeypatch, tmp_path, updates):
    from jiuwenswarm.agents.harness.common.a4p import rpc as a4p_rpc, runtime as a4p_runtime
    from jiuwenswarm.common import config as config_module

    path = tmp_path / "config.yaml"
    path.write_text("a4p:\n  enabled: false\n  require_user_signature: false\n  server_id: private\nother: preserved\n")
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(a4p_runtime, "get_config", config_module.get_config_raw)
    applied = []

    async def apply():
        applied.append(a4p_runtime.get_a4p_config())

    monkeypatch.setattr(a4p_rpc, "reconfigure_a4p_runtime", apply)
    response = await a4p_rpc.dispatch_a4p_request(
        _a4p_config_request("a4p.config.update", {**updates, "session_id": "session"})
    )
    expected = {"enabled": False, **updates}
    assert response.ok and response.payload == expected
    assert applied == [{**expected, "require_user_signature": False, "server_id": "private"}]
    persisted = config_module.get_config_raw()
    assert persisted == {"a4p": {**expected, "require_user_signature": False, "server_id": "private"}, "other": "preserved"}
    read = await a4p_rpc.dispatch_a4p_request(
        _a4p_config_request("a4p.config.get", {"session_id": "session"})
    )
    assert read.ok and read.payload == expected


def test_a4p_gateway_registers_only_configuration_and_authorization():
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=None))
    assert {name for name in channel.methods if name.startswith("a4p.")} == {
        "a4p.config.get", "a4p.config.update", "a4p.authorization.complete",
        "a4p.authorization.pending", "a4p.authorization.reject", "a4p.authorization.reprepare",
    }


def _use_managed_provider_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frozen: bool,
    runtime_state: str = "not_installed",
) -> list[SimpleNamespace]:
    """Expose a third-party ``acme`` runtime and capture install threads instead of running them."""
    from agent_gateway.common import external_cli_runtime

    monkeypatch.setattr(
        app_web_handlers,
        "_EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS",
        dict(app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_LOCKS),
    )
    monkeypatch.setattr(
        app_web_handlers,
        "_EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES",
        dict(app_web_handlers._EXTERNAL_CLI_DEPENDENCY_INSTALL_STATUSES),
    )
    monkeypatch.setattr(external_cli_runtime, "external_cli_runtime_names", lambda: ("claude", "codex", "acme"))
    monkeypatch.setattr(
        external_cli_runtime,
        "get_external_cli_runtime_status",
        lambda cli_agent: {"runtime_state": runtime_state, "managed": True, "cli_agent": cli_agent},
    )
    monkeypatch.setattr(app_web_handlers, "_is_frozen_runtime", lambda: frozen)
    created_threads: list[SimpleNamespace] = []

    class _Thread:
        def __init__(self, *, target: object, args: tuple[str, ...], name: str, daemon: bool) -> None:
            created_threads.append(SimpleNamespace(target=target, args=args, name=name, daemon=daemon))

        def start(self) -> None:
            return None

    monkeypatch.setattr(app_web_handlers.threading, "Thread", _Thread)
    return created_threads


async def _call_external_cli(method: str, cli_agent: str) -> dict:
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=make_config_routing_client()))
    await channel.methods[method](object(), f"req-{method}", {"cli_agent": cli_agent}, "sess-external-cli")
    return channel.responses[-1]


@pytest.mark.asyncio
async def test_welink_multi_binding_status_and_revoke_preserve_shared_reader(monkeypatch, tmp_path):
    from agent_gateway.common import config as config_module

    channel, cm = FakeWebChannel(), FakeChannelManager()
    cm.configs['welink'] = {
        'account': 'reader', 'reader_credential_dir': '/reader', 'cli_authorized': True,
        'bindings': [
            {'persistent_agent_id': 'mate_1', 'config_id': 'config_1', 'assistant_credential_id': 'ak1', 'assistant_secret': 'secret1', 'credential_dir': '/assistant1'},
            {'persistent_agent_id': 'mate_2', 'config_id': 'config_2', 'assistant_credential_id': 'ak2', 'assistant_secret': 'secret2', 'credential_dir': '/assistant2'},
        ],
    }
    path = tmp_path / 'config.yaml'
    config_module.dump_yaml_round_trip(path, {'channels': cm.configs})
    monkeypatch.setattr(config_module, 'CONFIG_YAML_PATH', path)
    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm))
    await channel.methods['channel.welink.get_bindings'](object(), 'status', {'config_id': 'config_2'}, 'session')
    response = channel.responses[-1]
    assert response['ok'] is True
    assert response['payload']['persistent_agent_id'] == 'mate_2'
    assert response['payload']['config_id'] == 'config_2'
    assert len(response['payload']['bindings']) == 2
    assert 'secret1' not in str(response)
    await channel.methods['channel.welink.revoke'](object(), 'revoke', {'config_id': 'config_2'}, 'session')
    assert channel.responses[-1]['ok'] is True
    saved = config_module.load_yaml_round_trip(path)['channels']['welink']
    assert saved['cli_authorized'] is True
    assert saved['reader_credential_dir'] == '/reader'
    assert [b['persistent_agent_id'] for b in saved['bindings']] == ['mate_1']
    assert saved['bindings'][0]['enabled'] is True


@pytest.mark.asyncio
async def test_cron_session_selection_rpc_rejects_group_before_resolving(monkeypatch):
    from jiuwenswarm.common.config_panel.model_resource_handlers import session_selection_handler
    from jiuwenswarm.common.external_providers import _providers

    monkeypatch.setitem(
        _providers, "session_metadata",
        SimpleNamespace(get_session_metadata=lambda session_id: {"cron_id": "job"}),
    )
    channel = SimpleNamespace(send_response=AsyncMock())
    await session_selection_handler(
        channel, None, "select", {"model_selection": {"type": "model_group", "id": "group"}}, "cron-session",
    )
    response = channel.send_response.call_args.kwargs
    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"
    assert "only support a single model" in response["error"]
