"""One colleague identity with independent platform connections and Sessions."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.models import ConnectionRecord, PersistentAgentError
from jiuwenswarm.server.jiuwenmate.web_adapter import _colleague_wire
from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_persistent_agent_service import make_service

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
NS = "local"


def legacy_connection(service, config_id, app_id="ding-app"):
    definition = service._repository.get(NS, config_id)
    legacy = replace(definition, agent_id="legacy-ding", is_default=False)
    service._repository.insert_connection_agent(legacy, ConnectionRecord(
        namespace_id=NS, agent_id=legacy.agent_id, config_id=config_id,
        channel="dingtalk", connection_key=app_id, created_at=NOW,
    ))
    return legacy.agent_id


def test_binding_three_channels_does_not_copy_an_agent_or_publication(tmp_path):
    service = make_service(tmp_path)
    agent = service.ensure_default_colleague(NS)
    before = service.current_snapshot(NS, agent.agent_id).config_version
    assert service.attach_connection(NS, agent.agent_id, "feishu", "feishu-app") == agent.agent_id
    assert service.attach_connection(NS, agent.agent_id, "dingtalk", "ding-app") == agent.agent_id
    assert service.attach_connection(NS, agent.agent_id, "dingtalk", "ding-app") == agent.agent_id
    assert [a.agent_id for a in service.list_agents(NS)] == [agent.agent_id]
    assert service.connections(NS, agent.agent_id) == []
    assert service.current_snapshot(NS, agent.agent_id).config_version == before


def test_connection_view_preserves_platform_and_shares_agent_and_workspace(tmp_path):
    service = make_service(tmp_path)
    agent = service.ensure_default_colleague(NS)
    result = _colleague_wire(agent, service, {
        "feishu": {("f-app", agent.agent_id)}, "dingtalk": {("d-app", agent.agent_id)},
    })
    assert {c["channel"] for c in result["connections"]} == {"web", "feishu", "dingtalk"}
    assert {c["agent_id"] for c in result["connections"]} == {agent.agent_id}
    assert len({c["workspace_dir"] for c in result["connections"]}) == 1


def test_rebind_preserves_idle_legacy_agent_and_its_data(tmp_path):
    service = make_service(tmp_path)
    agent = service.ensure_default_colleague(NS)
    old = legacy_connection(service, agent.agent_id)
    assert service.attach_connection(NS, agent.agent_id, "dingtalk", "ding-app") == agent.agent_id
    assert service.connection_of(NS, old).config_id == agent.agent_id
    assert service.get_colleague(NS, old).agent_id == old


@pytest.mark.parametrize("paused", [False, True])
def test_rebind_refuses_unfinished_legacy_requests(tmp_path, paused):
    service = make_service(tmp_path)
    agent = service.ensure_default_colleague(NS)
    old = legacy_connection(service, agent.agent_id)
    sessions = ExecutionRepository(service._repository.db_path)
    session = sessions.resolve_session(NS, old, ConversationRef.im_direct("dingtalk", "staff"), now=NOW)
    state, _ = sessions.accept_request(
        session, snapshot=replace(service.current_snapshot(NS, agent.agent_id), agent_id=old),
        source_ref="dingtalk:legacy-message", access=AccessSnapshot(
            principal_ref="dingtalk:ding-app:staff", startup_scope=frozenset({"chat"}),
            source_scope=frozenset({"chat"}), policy_revision="test",
        ), request_input=core.RequestInput(
            kind="direct_message", text="hello", reply_target="staff",
            source=core.DirectMessageSource(
                message_id="legacy-message", sender_id="staff", sender_name="Tester", received_at=NOW,
            ),
        ), received_at=NOW,
    )
    if paused:
        sessions.start_execution(NS, state.record.request_id, now=NOW)
        sessions.finish_request(NS, state.record.request_id, "paused", now=NOW)
    with pytest.raises(PersistentAgentError) as error:
        service.attach_connection(NS, agent.agent_id, "dingtalk", "ding-app")
    assert error.value.code == "CONFLICT"
