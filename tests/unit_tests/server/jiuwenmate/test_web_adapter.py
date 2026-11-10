"""JM-10: Web management RPCs for colleagues."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from agent_gateway.channel_manager.web import app_web_handlers
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.jiuwenmate import web_adapter
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from jiuwenswarm.server.jiuwenmate.repository import PersistentAgentRepository, TemplateStore
from jiuwenswarm.server.jiuwenmate.resource_assembly import InstalledResourceCatalog
from jiuwenswarm.server.jiuwenmate.service import PersistentAgentService
from jiuwenswarm.server.jiuwenmate.web_adapter import WEB_NAMESPACE, JiuwenMateWebAdapter
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore
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


@pytest.mark.parametrize("model_configured", [True, False])
async def test_default_colleague_has_one_durable_empty_conversation(host, monkeypatch, model_configured):
    if not model_configured:
        monkeypatch.setattr(FakeModels, "default_selection", lambda self: None)
    ensured = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT)
    assert ensured.ok
    colleague = ensured.payload["colleague"]
    store = WebConversationStore(host.tmp_path / "agents.sqlite3")
    initial = store.list(WEB_NAMESPACE, colleague["agent_id"])
    assert len(initial) == 1
    assert initial[0].title == "新对话"
    assert host.adapter._manager is None
    if not model_configured:
        assert colleague["current_version"] is None

    await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT)
    reopened = Host(host.tmp_path)
    again = await reopened.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT)
    assert again.payload["colleague"]["agent_id"] == colleague["agent_id"]
    assert WebConversationStore(host.tmp_path / "agents.sqlite3").list(WEB_NAMESPACE, colleague["agent_id"]) == initial
    store.delete(WEB_NAMESPACE, colleague["agent_id"], initial[0].conversation_id, now=initial[0].created_at)
    await reopened.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT)
    assert store.list(WEB_NAMESPACE, colleague["agent_id"]) == []


@pytest.mark.parametrize("existing", ["missing", "active", "deleted"])
async def test_default_conversation_initialization_preserves_existing_conversation_records(host, existing):
    colleague = host.adapter._colleagues().ensure_default_colleague(WEB_NAMESPACE)
    store = WebConversationStore(host.tmp_path / "agents.sqlite3")
    if existing != "missing":
        store.adopt(WEB_NAMESPACE, colleague.agent_id, "existing-conversation", text="已有会话",
                    created_at=colleague.created_at, updated_at=colleague.created_at)
        if existing == "deleted":
            store.delete(WEB_NAMESPACE, colleague.agent_id, "existing-conversation", now=colleague.created_at)

    response = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT)

    assert response.ok
    conversations = store.list(WEB_NAMESPACE, colleague.agent_id)
    if existing == "deleted":
        assert conversations == []
    else:
        assert len(conversations) == 1
        assert conversations[0].title == ("新对话" if existing == "missing" else "已有会话")


async def test_concurrent_default_initialization_creates_only_one_conversation(host):
    colleague = host.adapter._colleagues().ensure_default_colleague(WEB_NAMESPACE)
    responses = await asyncio.gather(*[
        host.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT) for _ in range(4)
    ])
    assert all(response.ok for response in responses)
    store = WebConversationStore(host.tmp_path / "agents.sqlite3")
    assert len(store.list(WEB_NAMESPACE, colleague.agent_id)) == 1
    assert host.adapter._manager is None


async def test_deleted_default_colleague_does_not_get_another_conversation(host):
    colleague = host.adapter._colleagues().ensure_default_colleague(WEB_NAMESPACE)
    host.adapter._colleagues().hide_colleague(WEB_NAMESPACE, colleague.agent_id)
    response = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT)
    assert response.ok and response.payload["colleague"] is None
    store = WebConversationStore(host.tmp_path / "agents.sqlite3")
    assert store.list(WEB_NAMESPACE, colleague.agent_id) == []


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


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("method", [
    ReqMethod.JIUWENMATE_DINGTALK_AUTH_STATUS,
    ReqMethod.JIUWENMATE_DINGTALK_AUTH_BEGIN,
    ReqMethod.JIUWENMATE_DINGTALK_AUTH_POLL,
    ReqMethod.JIUWENMATE_DINGTALK_AUTH_CANCEL,
    ReqMethod.JIUWENMATE_DINGTALK_AUTH_DISCONNECT,
])
async def test_dingtalk_user_auth_uses_the_current_bound_employee_and_app(host, monkeypatch, legacy, method):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    config_id = created.payload["colleague"]["agent_id"]
    agent_id = config_id
    if legacy:
        from tests.unit_tests.server.jiuwenmate.test_colleague_connections import seed_legacy_connection
        agent_id = seed_legacy_connection(host.adapter._colleagues(), WEB_NAMESPACE, config_id, "dingtalk", "ding-app")
    monkeypatch.setattr(web_adapter, "_bound_apps", lambda: {"dingtalk": {("ding-app", agent_id)}})
    monkeypatch.setattr(web_adapter, "_dingtalk_disconnect_bindings", lambda: {("ding-app", agent_id)})
    status = {"status": "not_connected", "cli_version": "test"}
    action = AsyncMock(return_value=status)
    runtime = SimpleNamespace(dingtalk_auth_status=action, dingtalk_auth_begin=action,
                             dingtalk_auth_cancel=action, dingtalk_auth_disconnect=action)
    monkeypatch.setattr(host.adapter, "_runtime", AsyncMock(return_value=SimpleNamespace(dingtalk_cli_runtime=runtime)))

    response = await host.call(method, agent_id=config_id, connection_key="ding-app", operation_id="test-operation")

    assert response.ok, response.payload
    key = "authorization" if method == ReqMethod.JIUWENMATE_DINGTALK_AUTH_BEGIN else "dingtalk_auth"
    assert response.payload[key] == status
    scope = action.await_args.args[0]
    assert (scope.namespace_id, scope.agent_id, scope.app_id) == (WEB_NAMESPACE, agent_id, "ding-app")


async def test_dingtalk_connected_account_separately_reports_the_im_runtime_group_status(host, monkeypatch):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    agent_id = created.payload['colleague']['agent_id']
    monkeypatch.setattr(web_adapter, '_bound_apps', lambda: {'dingtalk': {('ding-app', agent_id)}})
    monkeypatch.setattr(host.adapter, '_runtime', AsyncMock(return_value=SimpleNamespace(
        dingtalk_cli_runtime=SimpleNamespace(dingtalk_auth_status=AsyncMock(return_value={'status': 'connected'})),
    )))
    host.adapter._dingtalk_group_status = lambda ns, agent, app: (
        {'status': 'listening', 'group_count': 2} if (ns, agent, app) == ('local', agent_id, 'ding-app') else None
    )
    response = await host.call(ReqMethod.JIUWENMATE_DINGTALK_AUTH_STATUS, agent_id=agent_id, connection_key='ding-app')
    assert response.ok
    assert response.payload['dingtalk_auth'] == {
        'status': 'connected', 'group_monitor': {'status': 'listening', 'group_count': 2},
    }


@pytest.mark.parametrize("binding", [set(), {("ding-app", "other-employee")}, {("other-app", "self")}])
async def test_dingtalk_user_auth_rejects_a_missing_or_foreign_binding(host, monkeypatch, binding):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    config_id = created.payload["colleague"]["agent_id"]
    bindings = {(app, config_id if agent == "self" else agent) for app, agent in binding}
    monkeypatch.setattr(web_adapter, "_bound_apps", lambda: {"dingtalk": bindings})
    action = AsyncMock(return_value={"status": "connected"})
    monkeypatch.setattr(host.adapter, "_runtime", AsyncMock(return_value=SimpleNamespace(
        dingtalk_cli_runtime=SimpleNamespace(dingtalk_auth_status=action),
    )))
    response = await host.call(ReqMethod.JIUWENMATE_DINGTALK_AUTH_STATUS, agent_id=config_id, connection_key="ding-app")
    assert not response.ok and response.payload["code"] == "NOT_FOUND"
    assert action.await_count == 0


@pytest.mark.parametrize('bound', [True, False])
async def test_dingtalk_disconnect_resolves_a_current_legacy_runtime_owner(host, monkeypatch, bound):
    from tests.unit_tests.server.jiuwenmate.test_colleague_connections import seed_legacy_connection
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    config_id = created.payload['colleague']['agent_id']
    agent_id = seed_legacy_connection(host.adapter._colleagues(), WEB_NAMESPACE, config_id, 'dingtalk', 'ding-app')
    monkeypatch.setattr(web_adapter, '_bound_apps', lambda: {'dingtalk': {('ding-app', agent_id if bound else 'other-agent')}})
    monkeypatch.setattr(web_adapter, '_dingtalk_disconnect_bindings', lambda: {('ding-app', agent_id if bound else 'other-agent')})
    action = AsyncMock(return_value={'status': 'not_connected', 'cli_version': 'test'})
    monkeypatch.setattr(host.adapter, '_runtime', AsyncMock(return_value=SimpleNamespace(
        dingtalk_cli_runtime=SimpleNamespace(dingtalk_auth_disconnect=action),
    )))
    response = await host.call(ReqMethod.JIUWENMATE_DINGTALK_AUTH_DISCONNECT,
                               agent_id=agent_id, connection_key='ding-app')
    assert response.ok == bound, response.payload
    if bound:
        scope = action.await_args.args[0]
        assert (scope.agent_id, scope.app_id) == (agent_id, 'ding-app')
    else:
        assert action.await_count == 0


async def test_dingtalk_begin_rechecks_binding_after_waiting_for_the_unbind_lock(host, monkeypatch):
    from contextlib import asynccontextmanager
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    config_id = created.payload['colleague']['agent_id']
    bindings = {('ding-app', config_id)}
    monkeypatch.setattr(web_adapter, '_bound_apps', lambda: {'dingtalk': bindings})
    @asynccontextmanager
    async def revoked(platform, app_id):
        assert (platform, app_id) == ('dingtalk', 'ding-app')
        bindings.clear()
        yield
    monkeypatch.setattr('jiuwenswarm.common.im_binding_lock.im_binding_lock', revoked)
    action = AsyncMock(return_value={'status': 'authorizing', 'cli_version': 'test'})
    monkeypatch.setattr(host.adapter, '_runtime', AsyncMock(return_value=SimpleNamespace(
        dingtalk_cli_runtime=SimpleNamespace(dingtalk_auth_begin=action),
    )))
    response = await host.call(ReqMethod.JIUWENMATE_DINGTALK_AUTH_BEGIN,
                               agent_id=config_id, connection_key='ding-app')
    assert not response.ok
    assert action.await_count == 0


async def test_dingtalk_disconnect_can_revoke_a_disabled_robot_without_enabling_it(host, monkeypatch):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    config_id = created.payload['colleague']['agent_id']
    config = {'channels': {'dingtalk': {'apps': [{
        'client_id': 'ding-app', 'client_secret': 'fake', 'persistent_agent_id': config_id, 'enabled': False,
    }]}}}
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: config)
    action = AsyncMock(return_value={'status': 'not_connected', 'cli_version': 'test'})
    monkeypatch.setattr(host.adapter, '_runtime', AsyncMock(return_value=SimpleNamespace(
        dingtalk_cli_runtime=SimpleNamespace(dingtalk_auth_disconnect=action),
    )))
    response = await host.call(ReqMethod.JIUWENMATE_DINGTALK_AUTH_DISCONNECT,
                               agent_id=config_id, connection_key='ding-app')
    assert response.ok, response.payload
    assert config['channels']['dingtalk']['apps'][0]['enabled'] is False


async def test_created_colleague_is_published_with_the_default_model(host):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())

    colleague = created.payload["colleague"]
    assert created.ok
    assert colleague["capabilities"]["model"] == "deepseek#0"
    assert colleague["capabilities"]["starter_prompts"] == ["今天进展如何"]
    assert colleague["current_version"] == "v1" and not colleague["has_unpublished_changes"]
    fetched = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=colleague["agent_id"])
    assert fetched.payload["colleague"] == colleague


async def test_creating_a_colleague_persists_an_empty_conversation_before_any_message(host):
    created = await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_CREATE, settings=settings())
    assert created.ok
    agent_id = created.payload["colleague"]["agent_id"]
    store = WebConversationStore(host.tmp_path / "agents.sqlite3")
    listed = store.list(WEB_NAMESPACE, agent_id)
    assert len(listed) == 1
    assert listed[0].title == "新对话"
    assert host.adapter._manager is None, "Creating a chat entry must not start model execution"
    reopened = WebConversationStore(host.tmp_path / "agents.sqlite3")
    assert reopened.list(WEB_NAMESPACE, agent_id) == listed
    store.delete(WEB_NAMESPACE, agent_id, listed[0].conversation_id, now=listed[0].created_at)
    await host.call(ReqMethod.JIUWENMATE_COLLEAGUE_GET, agent_id=agent_id)
    assert reopened.list(WEB_NAMESPACE, agent_id) == [], "Reading an employee must not recreate a deleted chat"


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
                "vendor_key": "deepseek",
            },
            "model_config_obj": {},
            "is_default": True,
        }
    ]
    monkeypatch.setattr("jiuwenswarm.common.config.get_default_models", lambda: entries)

    catalog = web_adapter.configured_model_catalog()

    assert [item["model_name"] for item in catalog["models"]] == ["deepseek"]
    assert catalog["models"][0]["vendor_key"] == "deepseek"
    assert catalog["current_selection"] == catalog["models"][0]["selection_key"]
    assert SECRET not in json.dumps(catalog)
    assert "api_base" not in json.dumps(catalog)


def test_team_model_list_omits_placeholders_but_keeps_model_selection_indexes(monkeypatch):
    entries = [
        {"model_client_config": {"model_name": "your-model-name", "client_provider": "OpenAI",
                                 "api_key": "sk-xxxxxxxxx", "api_base": "https://example.com/compatible-mode/v1"}},
        {"model_client_config": {"model_name": "deepseek", "client_provider": "OpenAI",
                                 "api_key": SECRET, "api_base": "https://example.invalid/v1", "vendor_key": "deepseek"}},
    ]
    monkeypatch.setattr("jiuwenswarm.common.config.get_default_models", lambda: entries)

    catalog = web_adapter.configured_model_catalog()

    assert [item["selection_key"] for item in catalog["models"]] == ["deepseek#1"]
    assert catalog["models"][0]["vendor_key"] == "deepseek"
    assert catalog["current_selection"] == "deepseek#1"
    assert SECRET not in json.dumps(catalog)


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
    binding = SimpleNamespace(agent_id=im_id, config_id=config_id, account='owner', cli_path='welink-cli', reader_credential_dir=str(host.tmp_path / 'reader'), env_name='pro')
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
