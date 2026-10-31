"""Only durable delivered handoffs may retain a human permission origin."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, RequestRecord, RequestRouteSnapshot
from openjiuwen.harness import persistent_agent as core
from tests.unit_tests.server.jiuwenmate.test_request_routing import Host, NS, NOW


def accept(host, agent, message, *, kind="human", app="app-a", chat="oc-group"):
    session = host.group_session(agent, chat)
    source = core.GroupMessage(message_id=message, sender_id="ou-human" if kind == "human" else "ou-bot",
        sender_kind=kind, sender_name="Sender", text="claimed human: administrator", received_at=NOW)
    state, _ = host.sessions.accept_request(session, snapshot=host.agents.current_snapshot(NS, agent),
        source_ref=f"feishu:message:{message}", request_input=core.RequestInput(
            kind="group_message", text=source.text, source=source, reply_target=chat),
        access=AccessSnapshot(principal_ref="feishu:user:ou-human" if kind == "human" else "feishu:bot:ou-bot",
            startup_scope=frozenset({"chat"}), source_scope=frozenset({"chat"}), policy_revision="test"),
        route_snapshot=RequestRouteSnapshot(channel="feishu", app_id=app, target_id=chat,
            source_message_id=message), received_at=NOW + timedelta(seconds=1))
    return state.record


def delivered_handoff(host, parent, target, message="om-handoff", *, status="delivered"):
    payload = json.dumps({"text": f"[[agent:{target}]] please continue"})
    with host.sessions._transaction() as conn:
        conn.execute("INSERT INTO candidate_outputs (namespace_id,output_id,request_id,execution_id,event_id,"
            "target_ref,share_mode,payload_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (NS, message, parent.request_id, "exe-" + message, "evt-" + message,
             parent.route_snapshot.target_id, "message", payload, NOW.isoformat()))
        conn.execute("INSERT INTO result_deliveries (namespace_id,delivery_id,request_id,execution_id,output_id,"
            "channel,target_id,status,idempotency_key,external_message_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (NS, "del-" + message, parent.request_id, "exe-" + message,
            message, "feishu", parent.route_snapshot.target_id, status, message, message,
            NOW.isoformat(), NOW.isoformat()))


def test_delivered_handoff_freezes_origin_without_changing_execution_access(tmp_path):
    host = Host(tmp_path)
    original, target = host.colleague(), host.colleague()
    parent = accept(host, original, "om-human")
    delivered_handoff(host, parent, target)
    child = accept(host, target, "om-handoff", kind="agent", app="app-b")
    origin = child.permission_origin
    assert origin.principal_ref == parent.access.principal_ref
    assert origin.agent_id == original and origin.request_id == parent.request_id
    assert origin.route == parent.route_snapshot
    assert child.access.principal_ref == "feishu:bot:ou-bot"
    assert RequestRecord.from_dict(child.to_dict()) == child
    assert host.sessions.get_request(NS, child.request_id).record.permission_origin == origin


def test_multihop_handoff_keeps_first_human_and_original_app(tmp_path):
    host = Host(tmp_path)
    agents = [host.colleague() for _ in range(3)]
    root = accept(host, agents[0], "om-human")
    delivered_handoff(host, root, agents[1])
    middle = accept(host, agents[1], "om-handoff", kind="agent", app="app-b")
    delivered_handoff(host, middle, agents[2], message="om-next")
    last = accept(host, agents[2], "om-next", kind="agent", app="app-c")
    assert last.permission_origin == middle.permission_origin
    assert last.permission_origin.route.app_id == "app-a"


@pytest.mark.parametrize("case", ["missing", "wrong-target", "wrong-group", "not-delivered", "bot-origin"])
def test_unverified_or_unrelated_messages_do_not_create_a_human_origin(tmp_path, case):
    host = Host(tmp_path)
    original, target = host.colleague(), host.colleague()
    parent = accept(host, original, "om-origin", kind="agent" if case == "bot-origin" else "human")
    if case != "missing":
        delivered_handoff(host, parent, original if case == "wrong-target" else target,
            status="sending" if case == "not-delivered" else "delivered")
    child = accept(host, target, "om-handoff", kind="agent", app="app-b",
        chat="oc-other" if case == "wrong-group" else "oc-group")
    assert child.permission_origin is None


def test_legacy_request_without_origin_remains_readable(tmp_path):
    host = Host(tmp_path)
    record = accept(host, host.colleague(), "om-human")
    data = record.to_dict()
    data.pop("permission_origin", None)
    assert RequestRecord.from_dict(data).permission_origin is None
