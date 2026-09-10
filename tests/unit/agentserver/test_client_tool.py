import asyncio

import pytest

from jiuwenclaw.agentserver.tools.client_tool import ClientToolManager, normalize_tool_definitions
from jiuwenclaw.e2a.constants import E2A_RESPONSE_KIND_CLIENT_TOOL_REQUEST


@pytest.fixture
def manager():
    instance = ClientToolManager()
    instance.reset_state()
    yield instance
    instance.reset_state()


@pytest.mark.asyncio
async def test_client_tool_keeps_invocation_pending_until_matching_result(manager):
    pushed = []
    manager.set_send_push_callback(pushed.append)
    task = asyncio.create_task(
        manager.invoke(
            tool_name="document.replace",
            arguments={"from": 1, "to": 3, "text": "new"},
            invocation_id="inv-1",
            session_id="session-1",
            provider_id="shimo",
            resource_id="doc-1",
            client_session_id="client-1",
            expected_resource_version=7,
            available_tools={"document.replace"},
            channel_id="web",
            timeout=1,
        )
    )
    await asyncio.sleep(0)

    assert manager.pending_count == 1
    assert pushed[0]["response_kind"] == E2A_RESPONSE_KIND_CLIENT_TOOL_REQUEST
    event = pushed[0]["body"]
    assert event["type"] == "agent.custom_tool_call"
    assert event["invocation_id"] == "inv-1"
    assert event["client_session_id"] == "client-1"
    assert event["expected_resource_version"] == 7

    accepted, reason = manager.complete(
        {
            "tool_call_id": event["tool_call_id"],
            "invocation_id": "inv-1",
            "client_session_id": "client-1",
            "provider_id": "shimo",
            "resource_id": "doc-1",
            "success": True,
            "data": {"summary": "done"},
            "resource_version": 8,
        },
        session_id="session-1",
    )
    assert (accepted, reason) == (True, "accepted")
    result = await task
    assert result["success"] is True
    assert result["resource_version"] == 8
    assert manager.pending_count == 0


@pytest.mark.asyncio
async def test_client_tool_rejects_cross_document_result(manager):
    pushed = []
    manager.set_send_push_callback(pushed.append)
    task = asyncio.create_task(
        manager.invoke(
            tool_name="document.read",
            arguments={},
            invocation_id="inv-2",
            session_id="session-1",
            provider_id="shimo",
            resource_id="doc-1",
            client_session_id="client-1",
            expected_resource_version=None,
            available_tools={"document.read"},
            channel_id="web",
            timeout=1,
        )
    )
    await asyncio.sleep(0)
    event = pushed[0]["body"]

    accepted, reason = manager.complete(
        {
            "tool_call_id": event["tool_call_id"],
            "invocation_id": "inv-2",
            "client_session_id": "client-1",
            "provider_id": "shimo",
            "resource_id": "other-doc",
            "success": True,
        },
        session_id="session-1",
    )
    assert (accepted, reason) == (False, "resource_mismatch")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_client_tool_denies_undeclared_capability_without_push(manager):
    pushed = []
    manager.set_send_push_callback(pushed.append)
    result = await manager.invoke(
        tool_name="document.delete",
        arguments={"from": 1, "to": 2},
        invocation_id="inv-3",
        session_id="session-1",
        provider_id="shimo",
        resource_id="doc-1",
        client_session_id="client-1",
        expected_resource_version=1,
        available_tools={"document.read"},
        channel_id="web",
    )
    assert result["success"] is False
    assert result["error"]["code"] == "TOOL_NOT_FOUND"
    assert pushed == []


def test_client_tool_manifest_is_generic_and_validated():
    tools = normalize_tool_definitions([
        {"name": "any.provider.tool", "description": "A host tool", "inputSchema": {"type": "object"}}
    ])
    assert tools[0]["name"] == "any.provider.tool"
    assert normalize_tool_definitions([{"name": "bad name", "description": "x", "inputSchema": {}}]) == []
    assert normalize_tool_definitions([{"name": "bad.schema", "description": "x", "inputSchema": {"type": "string"}}]) == []
    assert normalize_tool_definitions([{"name": "huge.schema", "description": "x", "inputSchema": {"type": "object", "description": "x" * 20_000}}]) == []


@pytest.mark.asyncio
async def test_client_tool_rejects_cross_session_and_stale_client(manager):
    pushed = []
    manager.set_send_push_callback(pushed.append)
    task = asyncio.create_task(
        manager.invoke(
            tool_name="document.read",
            arguments={},
            invocation_id="inv-4",
            session_id="session-1",
            provider_id="shimo",
            resource_id="doc-1",
            client_session_id="client-1",
            expected_resource_version=None,
            available_tools={"document.read"},
            channel_id="web",
            timeout=1,
        )
    )
    await asyncio.sleep(0)
    event = pushed[0]["body"]
    result = {
        "tool_call_id": event["tool_call_id"],
        "invocation_id": "inv-4",
        "client_session_id": "client-1",
        "provider_id": "shimo",
        "resource_id": "doc-1",
        "success": True,
    }

    assert manager.complete(result, session_id="other-session") == (False, "session_mismatch")
    assert manager.complete({**result, "client_session_id": "stale-client"}, session_id="session-1") == (False, "client_session_mismatch")
    assert manager.complete(result, session_id="session-1") == (True, "accepted")
    assert (await task)["success"] is True
