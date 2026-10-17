import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.policy_provider import (
    set_permission_policy_provider,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.sdk.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_CHAT_ID,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def test_contributed_policy_denies_narrows_and_preserves_persistence(monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers

    saved = []
    monkeypatch.setattr(
        permissions_layers,
        "persist_user_overlay_from_effective",
        lambda permissions, session_id=None: saved.append(permissions) or True,
    )

    class Policy:
        def deny_tool(self, tool_name):
            return "[PERMISSION_DENIED] contributed policy" if tool_name == "bash" else None

        def narrow_config(self, permissions):
            return {**permissions, "tools": {"bash": "deny"}}

        def prepare_persist(self, permissions, session_id):
            return {**permissions, "approval_overrides": [{"id": "operator"}]}

    set_permission_policy_provider(Policy())
    try:
        rail = build_permission_rail({"permissions": {"enabled": True}})
        host = rail._host
        result = asyncio.run(host.permission_scene_hook(
            SimpleNamespace(normalized_tool_name="bash", user_input=None)
        ))
        assert result == ("reject", "[PERMISSION_DENIED] contributed policy")
        assert host.get_permissions_snapshot()["tools"]["bash"] == "deny"
        assert host.persist_allow_rule({"approval_overrides": []})
        assert saved[0]["approval_overrides"] == [{"id": "operator"}]
    finally:
        set_permission_policy_provider(None)


@pytest.mark.parametrize("auto", [False, True])
def test_policy_sees_request_conversation_from_scheduler_task(auto, tmp_path):
    """Host callbacks run on the scheduler and still see the request's conversation."""
    seen = []

    class Policy:
        def deny_tool(self, tool_name):
            seen.append(("deny", TOOL_PERMISSION_CHANNEL_ID.get(), TOOL_PERMISSION_CHAT_ID.get()))

        def narrow_config(self, permissions):
            seen.append(("narrow", TOOL_PERMISSION_CHANNEL_ID.get(), TOOL_PERMISSION_CHAT_ID.get()))
            return permissions

        def prepare_persist(self, permissions, session_id):
            return permissions

    permissions = {"enabled": True}
    if auto:
        permissions.update(mode="auto", defaults={"*": "allow"}, file_guard={"enabled": False})
    rail = build_permission_rail(
        {"permissions": permissions}, enable_auto_permission=auto, workspace_root=tmp_path,
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._permission_rail = rail
    adapter._is_session_scoped_adapter = True
    adapter._enable_auto_permission = False
    adapter._root_permission_queue = None
    adapter._sys_operation = None
    adapter._resolve_interrupt_session_id = lambda session_id: session_id
    agent_request = AgentRequest(
        request_id="request", session_id="session", channel_id="feishu",
        chat_id="chat-1", req_method=ReqMethod.CHAT_SEND, params={"mode": "agent"},
    )
    host = (rail.base_rail if auto else rail)._host

    async def scenario():
        # Like the agent's TaskScheduler, the worker starts before any request
        # binds its context, so tool calls run without the request's bindings.
        calls: asyncio.Queue = asyncio.Queue()

        async def scheduler():
            done = await calls.get()
            await host.permission_scene_hook(
                SimpleNamespace(normalized_tool_name="bash", user_input=None)
            )
            host.get_permissions_snapshot()
            done.set_result(None)

        worker = asyncio.create_task(scheduler())
        with adapter._bind_permission_request_context(agent_request):
            done = asyncio.get_running_loop().create_future()
            await calls.put(done)
            await done
        await worker

    set_permission_policy_provider(Policy())
    try:
        asyncio.run(scenario())
    finally:
        set_permission_policy_provider(None)
    assert seen == [("deny", "feishu", "chat-1"), ("narrow", "feishu", "chat-1")]
