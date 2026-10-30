"""JM-10: Web management RPCs for colleagues."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import AsyncMock, patch

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.channel_manager.web import app_web_handlers
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.jiuwenmate import web_adapter
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.resource_assembly import InstalledResourceCatalog
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_NAMESPACE, JiuwenMateWebAdapter
from jiuwenswarm.server.runtime.gateway_adapter import AdapterRegistry

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

SECRET = "sk-test-secret-value"
CATALOG = {
    "models": [{"selection_key": "deepseek#0", "display_name": "DeepSeek", "model_name": "deepseek"}],
    "current_selection": "deepseek#0",
    "current_display_name": "DeepSeek",
}


class FakeModels:
    def default_selection(self):
        return "deepseek#0"

    def resolve(self, selection, *, client_id):
        if selection not in ("deepseek#0", "kimi#1"):
            raise PersistentAgentError("model is not available", code="MODEL_NOT_FOUND")
        name = selection.partition("#")[0]
        return TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=client_id,
                client_provider="OpenAI",
                api_key=SECRET,
                api_base="https://example.invalid/v1",
                model_name=name,
            ),
            model_request_config=ModelRequestConfig(model=name),
        )


class Host:
    def __init__(self, tmp_path) -> None:
        self.created = 0
        self.tmp_path = tmp_path
        self.adapter = JiuwenMateWebAdapter(self._service, model_catalog=lambda: CATALOG)

    def _service(self) -> PersistentAgentService:
        self.created += 1
        ticks = count()
        base = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
        library = self.tmp_path / "skill-library"
        skill = library / "report-writer"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("---\nname: report-writer\n---\n写报告。\n", encoding="utf-8")
        return PersistentAgentService(
            PersistentAgentRepository(self.tmp_path / "agents.sqlite3"),
            TemplateStore(self.tmp_path / "templates"),
            models=FakeModels(),
            template_source=lambda _: None,
            clock=lambda: base + timedelta(seconds=next(ticks)),
            windows=WindowBackendStore(self.tmp_path / "agents.sqlite3"),
            resources=InstalledResourceCatalog(
                skills_dir=library,
                builtin_dir=self.tmp_path / "no-builtin",
                state_file=self.tmp_path / "no-state.json",
            ),
        )

    async def call(self, method: ReqMethod, **params):
        request = AgentRequest(request_id="req-1", channel_id="web", req_method=method, params=params)
        response = await self.adapter.handle(request)
        assert SECRET not in json.dumps(response.payload, ensure_ascii=False)
        return response


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def settings(**changes):
    fields = {
        "name": "研发同事",
        "description": "负责整理研发进展。",
        "tags": ["研发工程"],
        "category": "development",
        "capabilities": {"model": None, "skills": ["report-writer"], "starter_prompts": ["今天进展如何"]},
    }
    fields.update(changes)
    return fields


# JM-10-V-01 colleague management through the Web RPCs


async def test_opening_the_page_shows_the_default_colleague(host):
    ensured = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT)
    listed = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_LIST)

    colleague = ensured.payload["colleague"]
    assert ensured.ok and colleague["name"] == "小九" and colleague["is_default"]
    assert [item["agent_id"] for item in listed.payload["colleagues"]] == [colleague["agent_id"]]


async def test_colleague_list_restores_im_binding_status_from_owner_config(host, monkeypatch):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    agent_id = created.payload["colleague"]["agent_id"]
    connected = (await host.adapter.attach_connection(
        {"config_id": agent_id, "channel": "feishu", "connection_key": "app-feishu"},
    ))["agent_id"]
    # The owner's Feishu configuration binds the app to the agent created for it.
    monkeypatch.setattr(web_adapter, "_bound_apps", lambda: {"feishu": {("app-feishu", connected)}})

    listed = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_LIST)
    fetched = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=agent_id)

    assert [item["agent_id"] for item in listed.payload["colleagues"]] == [agent_id]
    assert listed.payload["colleagues"][0]["im_configured"] is True
    assert fetched.payload["colleague"]["im_configured"] is True
    assert [(item["channel"], item["agent_id"]) for item in fetched.payload["colleague"]["connections"]] == [
        ("web", agent_id), ("feishu", connected),
    ]


async def test_created_colleague_is_published_with_the_default_model(host):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())

    colleague = created.payload["colleague"]
    assert created.ok
    assert colleague["capabilities"]["model"] == "deepseek#0"
    assert colleague["capabilities"]["starter_prompts"] == ["今天进展如何"]
    assert colleague["current_version"] == "v1" and not colleague["has_unpublished_changes"]
    fetched = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=colleague["agent_id"])
    assert fetched.payload["colleague"] == colleague


async def test_settings_change_waits_for_publication(host):
    agent_id = (await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())).payload["colleague"][
        "agent_id"
    ]
    current = (await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_GET, agent_id=agent_id)).payload["settings"]
    assert current["persona"]

    current["description"] = "负责研发周报。"
    updated = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_SETTINGS_UPDATE, agent_id=agent_id, settings=current)
    assert updated.payload["colleague"]["has_unpublished_changes"]

    published = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_PUBLISH, agent_id=agent_id)
    assert published.payload["version"]["config_version"] == "v2"
    refreshed = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=agent_id)
    assert refreshed.payload["colleague"]["description"] == "负责研发周报。"
    assert not refreshed.payload["colleague"]["has_unpublished_changes"]


async def test_colleague_enabled_state_is_persisted_by_the_web_rpc(host):
    agent_id = (await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())).payload["colleague"][
        "agent_id"
    ]

    disabled = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_SET_ENABLED, agent_id=agent_id, enabled=False)
    fetched = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=agent_id)
    listed = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_LIST)
    enabled = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_SET_ENABLED, agent_id=agent_id, enabled=True)

    assert disabled.ok and disabled.payload["colleague"]["enabled"] is False
    assert fetched.payload["colleague"]["enabled"] is False
    assert listed.payload["colleagues"][0]["enabled"] is False
    assert enabled.payload["colleague"]["enabled"] is True


async def test_deleted_colleague_is_gone(host):
    agent_id = (await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())).payload["colleague"][
        "agent_id"
    ]

    deleted = await host.adapter.remove_colleague({"config_id": agent_id, "mode": "purge"})
    missing = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=agent_id)

    assert deleted == {"removed": True}
    assert not missing.ok and missing.payload["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    ("method", "params", "code"),
    [
        (ReqMethod.JIUWENMATE_COLLEAGUE_GET, {}, "BAD_REQUEST"),
        (ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, {}, "BAD_REQUEST"),
        (ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, {"settings": settings(secret="x")}, "BAD_REQUEST"),
        (
            ReqMethod.JIUWENMATE_COLLEAGUE_CREATE,
            {"settings": settings(capabilities={"model": "missing#9"})},
            "MODEL_NOT_FOUND",
        ),
        (ReqMethod.JIUWENMATE_COLLEAGUE_PUBLISH, {"agent_id": "mate_missing"}, "NOT_FOUND"),
    ],
)
async def test_refused_operations_keep_their_error_code(host, method, params, code):
    response = await host.call(method, **params)

    assert not response.ok
    assert response.payload["code"] == code
    assert response.payload["error"]


async def test_model_list_does_not_open_the_colleague_store(host):
    response = await host.call(ReqMethod.JIUWENMATE_MODELS_LIST)

    assert response.ok and response.payload == CATALOG
    assert host.created == 0


async def test_colleague_store_is_opened_once(host):
    await asyncio.gather(*(host.call(ReqMethod.JIUWENMATE_COLLEAGUE_LIST) for _ in range(5)))

    assert host.created == 1


async def test_agent_mail_web_rpcs_use_the_created_employees_runtime(host, monkeypatch):
    class MailRuntime:
        async def status(self, scope):
            return {"status": "not_connected", "cli_version": "1.0.18"}

        async def begin_auth(self, scope):
            return {
                "status": "authorizing",
                "cli_version": "1.0.18",
                "operation_id": "op-1",
                "auth_url": "https://agent.qq.com/oauth?ticket=opaque",
            }

        async def poll_auth(self, scope, operation_id):
            assert operation_id == "op-1"
            return {"status": "connected", "cli_version": "1.0.18", "email": "employee@example.com"}

        async def cancel_auth(self, scope, operation_id):
            return {"status": "not_connected", "cli_version": "1.0.18"}

        async def disconnect(self, scope):
            return {"status": "not_connected", "cli_version": "1.0.18"}

        async def execute(self, scope, operation, payload):
            return {"ok": True}

        async def remove_runtime(self, scope):
            return None

    monkeypatch.setattr(
        "jiuwenswarm.server.jiuwenmate.agent_manager.load_agent_mail_runtime_provider", lambda: MailRuntime()
    )
    agent_id = (await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())).payload["colleague"]["agent_id"]

    status = await host.call(ReqMethod.JIUWENMATE_AGENT_MAIL_STATUS, agent_id=agent_id)
    authorization = await host.call(ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_BEGIN, agent_id=agent_id)
    connected = await host.call(
        ReqMethod.JIUWENMATE_AGENT_MAIL_AUTH_POLL, agent_id=agent_id, operation_id="op-1"
    )

    assert status.payload["agent_mail"]["status"] == "not_connected"
    assert authorization.payload["authorization"]["auth_url"] == "https://agent.qq.com/oauth?ticket=opaque"
    assert connected.payload["agent_mail"] == {
        "status": "connected", "cli_version": "1.0.18", "email": "employee@example.com"
    }


def test_configured_model_catalog_carries_no_secret(monkeypatch):
    entries = [
        {
            "model_client_config": {
                "model_name": "deepseek",
                "client_provider": "OpenAI",
                "api_key": SECRET,
                "api_base": "https://example.invalid/v1",
            },
            "model_config_obj": {},
            "is_default": True,
        }
    ]
    monkeypatch.setattr("jiuwenswarm.common.config.get_default_models", lambda: entries)

    catalog = web_adapter.configured_model_catalog()

    assert [item["model_name"] for item in catalog["models"]] == ["deepseek"]
    assert catalog["current_selection"] == catalog["models"][0]["selection_key"]
    assert SECRET not in json.dumps(catalog)
    assert "api_base" not in json.dumps(catalog)


def test_web_gateway_does_not_forward_internal_jiuwenmate_methods():
    # Refresh status has its own worker proxy, not the jiuwenmate.* forwarder.
    methods = JiuwenMateWebAdapter.methods - {ReqMethod.MODELS_REFRESH_STATUS.value}
    internal_methods = {
        ReqMethod.JIUWENMATE_FEISHU_INBOUND.value,
        ReqMethod.JIUWENMATE_DINGTALK_INBOUND.value,
        ReqMethod.JIUWENMATE_CONNECTION_ATTACH.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_CONNECTIONS.value,
        ReqMethod.JIUWENMATE_COLLEAGUE_REMOVE.value,
        ReqMethod.JIUWENMATE_THINKING_NOTICE_ACK.value,
        ReqMethod.JIUWENMATE_FEISHU_PREPARE.value,
        ReqMethod.JIUWENMATE_DELIVERY_ACK.value,
        ReqMethod.JIUWENMATE_GROUP_RESPONSE.value,
        ReqMethod.JIUWENMATE_THINKING_NOTICE_ACK.value,
    }
    # Gateway handles deletion itself: it removes the IM apps first, then asks AgentServer.
    gateway_methods = {ReqMethod.JIUWENMATE_COLLEAGUE_DELETE.value}
    web_methods = {
        method.value
        for method in ReqMethod
        if method.value.startswith("jiuwenmate.")
    } - internal_methods - gateway_methods

    assert web_methods == methods
    assert gateway_methods.isdisjoint(methods)
    assert gateway_methods.isdisjoint(app_web_handlers._FORWARD_REQ_METHODS)
    assert gateway_methods.isdisjoint(app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS)
    assert internal_methods.isdisjoint(methods)
    assert methods <= app_web_handlers._FORWARD_REQ_METHODS
    assert methods <= app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS
    assert internal_methods.isdisjoint(app_web_handlers._FORWARD_REQ_METHODS)
    assert internal_methods.isdisjoint(app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS)


async def test_agent_server_dispatches_the_methods_to_the_adapter(host):
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._adapter_registry = AdapterRegistry()
    server._adapter_registry.register(host.adapter)
    request = AgentRequest(request_id="req-1", channel_id="web", req_method=ReqMethod.JIUWENMATE_COLLEAGUE_LIST)

    with patch("jiuwenswarm.server.agent_ws_server.send_wire_payload", new=AsyncMock()) as send:
        handled = await server._dispatch_gateway_adapter_request(object(), request, asyncio.Lock())

    assert handled
    wire = json.dumps(send.await_args.args[1], ensure_ascii=False, default=str)
    assert '"colleagues"' in wire and "req-1" in wire


async def test_colleagues_live_in_the_local_namespace(host):
    await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())

    service = host.adapter._colleagues()
    assert [view.name for view in service.list_colleagues(WEB_NAMESPACE)] == ["研发同事"]
    assert service.list_colleagues("other") == []


async def test_welink_mail_settings_follow_colleague_connection_and_are_live(host, monkeypatch):
    from types import SimpleNamespace
    from jiuwenswarm.server.jiuwenmate import welink_office_settings as module
    from jiuwenswarm.server.jiuwenmate import welink_inbound
    from jiuwenswarm.server.jiuwenmate.welink_office_tool import ConfiguredOfficeRuntime
    config_id = (await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())).payload['colleague']['agent_id']
    before = await host.call(ReqMethod.JIUWENMATE_WELINK_OFFICE_STATUS, agent_id=config_id)
    assert before.ok and before.payload['welink_office']['reason'] == 'WELINK_NOT_BOUND'
    im_id = host.adapter._colleagues().attach_connection('local', config_id, 'welink', 'owner:'+config_id)
    binding = SimpleNamespace(agent_id=im_id, account='owner', cli_path='welink-cli', reader_credential_dir=str(host.tmp_path / 'reader'), env_name='pro')
    monkeypatch.setattr(welink_inbound, 'configured_welink_bindings', lambda: (binding,))
    monkeypatch.setattr(welink_inbound, 'welink_binding_for_agent', lambda _: binding)
    monkeypatch.setattr(module, 'parse_auth_status', lambda *args: (True, 'owner'))
    class Cli:
        calls = []
        def __init__(self, *args, **kwargs): pass
        def auth_status(self): return SimpleNamespace(ok=True, stdout='', stderr='')
        def run(self, args, timeout=30):
            self.calls.append(args)
            return SimpleNamespace(ok=True, stdout='{"success":true,"events":[]}')
    monkeypatch.setattr(module, 'WelinkCli', Cli)
    monkeypatch.setattr('jiuwenswarm.server.jiuwenmate.welink_office_tool.WelinkCli', Cli)
    connected = await host.call(ReqMethod.JIUWENMATE_WELINK_OFFICE_CONNECT, agent_id=config_id, email='owner@example.com')
    assert connected.ok, connected.payload
    assert connected.payload['welink_office']['status'] == 'connected'
    runtime = ConfiguredOfficeRuntime(host.adapter._manager.db_path, binding)
    calendar_result = runtime.execute('req-calendar', 'calendar.list', {'start': '2026-11-02', 'end': '2026-11-03'})
    assert calendar_result['status'] == 'ok', (calendar_result, host.adapter._manager.db_path)
    assert ['calendar', 'list', '--start', '2026-11-02', '--end', '2026-11-03'] in Cli.calls
    disconnected = await host.call(ReqMethod.JIUWENMATE_WELINK_OFFICE_DISCONNECT, agent_id=config_id)
    assert disconnected.ok
    assert runtime.execute('req-calendar2', 'calendar.list', {'start': '2026-11-02', 'end': '2026-11-03'})['code'] == 'WELINK_OFFICE_NOT_CONNECTED'
    missing = await host.call(ReqMethod.JIUWENMATE_WELINK_OFFICE_CONNECT, agent_id='missing', email='owner@example.com')
    assert not missing.ok


def test_welink_office_rpcs_are_forwarded_to_runtime():
    for method in (ReqMethod.JIUWENMATE_WELINK_OFFICE_STATUS,
                   ReqMethod.JIUWENMATE_WELINK_OFFICE_CONNECT,
                   ReqMethod.JIUWENMATE_WELINK_OFFICE_DISCONNECT):
        assert method.value in JiuwenMateWebAdapter.methods
        assert method.value in app_web_handlers._FORWARD_REQ_METHODS
        assert method.value in app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS
