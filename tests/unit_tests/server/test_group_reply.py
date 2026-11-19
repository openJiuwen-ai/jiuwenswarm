"""All tasks produce card data; ordinary replies never borrow an older execution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime import group_execution as ge
from jiuwenswarm.server.runtime import group_reply as gr
from tests.unit_tests.server.test_group_service import service as service, create


async def group_with_proxy(service):
    return await create(service, members=[{
        "kind": "agent_group", "id": "one", "member_name": "advisor",
    }])


@pytest.mark.asyncio
async def test_proxy_working_itself_pushes_the_same_card_protocol_as_a_team(service, monkeypatch):
    from jiuwenswarm.server.gateway_push import transport

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(transport.WebSocketGatewayPushTransport, "send_push", push)
    group = await group_with_proxy(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    assert record["execution_kind"] == "agent" and record["presentation"] == "card"
    assert record["member_kind"] == "agent_group"
    backend = SimpleNamespace(
        member_name="advisor",
        append_group_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id="result"))),
    )
    await store.update(record["execution_id"], status="running")
    await store.finish(record["execution_id"], backend, "Reviewed directly", [])
    payloads = [call.args[0]["payload"] for call in push.await_args_list]
    assert [item["event_type"] for item in payloads] == [
        "group.execution.created", "group.execution.updated", "group.execution.updated", "group.execution.updated",
    ]
    assert {item["execution_id"] for item in payloads} == {record["execution_id"]}
    assert payloads[-1]["result"] == "Reviewed directly"
    assert all("owner" not in item for item in payloads)


@pytest.mark.asyncio
async def test_run_team_updates_the_existing_card_instead_of_creating_another(service, monkeypatch):
    from jiuwenswarm.server.gateway_push import transport

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(transport.WebSocketGatewayPushTransport, "send_push", push)
    group = await group_with_proxy(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", "one"))[0]
    await store.update(record["execution_id"], execution_kind="team", presentation="card", run_key="run")
    payloads = [call.args[0]["payload"] for call in push.await_args_list]
    assert [item["event_type"] for item in payloads] == ["group.execution.created", "group.execution.updated"]
    assert payloads[-1]["execution_kind"] == "team"
    assert payloads[-1]["execution_id"] == record["execution_id"]


@pytest.mark.asyncio
async def test_ordinary_reply_after_an_unanswered_turn_is_not_attached_to_old_work(service, monkeypatch):
    monkeypatch.setattr(ge.GroupExecutions, "publish", AsyncMock())
    group = await group_with_proxy(service)
    gid = group["group_id"]
    store = ge.GroupExecutions(gid)
    old = (await store.create(["advisor"], "Old task", "one"))[0]
    await store.update(old["execution_id"], status="interrupted")
    message = {"sender": "advisor", "message_id": "new-message", "client_message_id": "ordinary-reply"}
    correlation = gr.correlate_public_reply(gid, message)
    assert correlation == {"reply_id": "new-message", "execution_id": None}
    assert gr.correlate_public_reply(gid, message) == correlation
    assert correlation["reply_id"] != old["reply_id"]


@pytest.mark.asyncio
async def test_final_result_is_correlated_by_execution_id_only(service, monkeypatch):
    monkeypatch.setattr(ge.GroupExecutions, "publish", AsyncMock())
    group = await group_with_proxy(service)
    gid = group["group_id"]
    record = (await ge.GroupExecutions(gid).create(["advisor"], "Review", "one"))[0]
    client_id = f'execution-result-{record["execution_id"]}'
    correlation = gr.correlate_public_reply(gid, {
        "sender": "advisor", "message_id": "result", "client_message_id": client_id,
    })
    assert correlation["execution_id"] == record["execution_id"]
    other = gr.correlate_public_reply(gid, {
        "sender": "someone-else", "message_id": "other", "client_message_id": client_id,
    })
    assert other["execution_id"] is None
    assert gr.correlate_public_reply(gid, {"sender": "user"}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["completed", "failed", "cancelled", "timed_out", "delivery_failed", "owner_lost"])
async def test_terminal_paths_use_updated_event_with_persisted_status(service, monkeypatch, path):
    from jiuwenswarm.server.gateway_push import transport

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(transport.WebSocketGatewayPushTransport, "send_push", push)
    group = await group_with_proxy(service)
    store = ge.GroupExecutions(group["group_id"])
    record = (await store.create(["advisor"], "Review", path))[0]
    key = record["execution_id"]
    if path == "completed":
        backend = SimpleNamespace(
            member_name="advisor",
            append_group_message=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(message_id="final"))),
        )
        await store.finish(key, backend, "Done", [])
    elif path == "delivery_failed":
        await store.fail_pending([key], "delivery_failed")
    elif path == "owner_lost":
        monkeypatch.setattr(ge.lifecycle, "runtime_owner_alive", lambda _: False)
        await store.reconcile()
    else:
        await store.update(key, status=path, phase=path)
    payloads = [call.args[0]["payload"] for call in push.await_args_list]
    assert {item["event_type"] for item in payloads} == {
        "group.execution.created", "group.execution.updated",
    }
    expected = {"delivery_failed": "failed", "owner_lost": "interrupted"}.get(path, path)
    assert payloads[-1]["event_type"] == "group.execution.updated"
    assert payloads[-1]["status"] == expected
    assert store.get(key)["status"] == expected
    assert payloads[-1]["revision"] > payloads[0]["revision"]
