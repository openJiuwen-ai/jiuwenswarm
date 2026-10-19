"""Real Core confirmations, shared rule files, and next-call activation."""

from copy import deepcopy
import asyncio
import json
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueue
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_PERMISSION_QUEUE_USER_INPUT_KEY, RootPermissionQueueRail,
    RootPermissionCompletionRail, bind_root_permission_request, reset_root_permission_request,
)
from jiuwenswarm.common import config
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID, TOOL_PERMISSION_REQUEST_ID,
)
from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    current_send_file_execution_grant, clear_send_file_execution_grant,
)
from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT, PermissionContext,
)
from tests.unit_tests.agentserver.permissions.test_native_path_alignment import native


@pytest.fixture
def storage(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    policy = {
        "enabled": True, "mode": "auto", "defaults": {"*": "allow"},
        "tools": {"unknown_lookup": "allow", "bash": "allow"},
        "file_guard": {"enabled": False},
    }
    config._dump_yaml_round_trip(path, {"permissions": policy})
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(layers, "user_permissions_path", lambda: tmp_path / "user.yaml")
    monkeypatch.setattr(layers, "session_permissions_path", lambda sid: tmp_path / sid / "session.yaml")
    return policy, path


def context(sid, name, args, call_id="call-1", state=None):
    state = {} if state is None else state
    session = SimpleNamespace(
        get_session_id=lambda: sid, get_state=state.get, update_state=state.update,
    )
    call = ToolCall(id=call_id, type="function", name=name, arguments=json.dumps(args))
    return AgentCallbackContext(
        agent=SimpleNamespace(ability_manager=SimpleNamespace(get=lambda _: None)),
        session=session, inputs=ToolCallInputs(tool_call=call, tool_name=name, tool_args=args),
        extra={},
    )


def build(policy, root, sid, smart):
    return build_permission_rail(
        {"permissions": deepcopy(policy)}, enable_auto_permission=smart,
        workspace_root=root, session_id=sid,
    )


async def approve(rail, ctx, permanent, before_answer=None, remember=True):
    """Use the existing queue reservation/claim and default Core confirmation."""
    queue = RootPermissionQueue()
    queue_rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id=ctx.session.get_session_id(), request_id="same-turn",
        enabled=True, queue=queue,
    )
    try:
        await queue_rail.before_tool_call(ctx)
        with pytest.raises(AbortError) as raised:
            await rail.before_tool_call(ctx)
        ctx.exception = raised.value
        await queue_rail.on_tool_exception(ctx)
        card = queue.pending_for_call(
            root_session_id=ctx.session.get_session_id(),
            execution_session_id=ctx.session.get_session_id(),
            tool_call_id=ctx.inputs.tool_call.id,
        )
        assert card is not None
        queue.reconcile({
            "result_type": "interrupt", "interrupt_ids": [ctx.inputs.tool_call.id],
            "state": [{"id": ctx.inputs.tool_call.id, "value": card.request}],
        }, root_session_id=ctx.session.get_session_id())
        if before_answer:
            before_answer()
        answer = InteractiveInput()
        payload = {"approved": True, "auto_confirm": remember}
        if permanent is not None:
            payload["persist_allow"] = permanent
        answer.update(card.key.invocation_id, payload)
        reserved = queue.reserve_answer(ctx.session.get_session_id(), answer)
        resumed = context(
            ctx.session.get_session_id(), ctx.inputs.tool_name, ctx.inputs.tool_args,
        )
        resumed.session = ctx.session
        resumed.agent = ctx.agent
        resumed.extra[ROOT_PERMISSION_QUEUE_USER_INPUT_KEY] = reserved.interactive_input
        await queue_rail.before_tool_call(resumed)
        await rail.before_tool_call(resumed)
        await RootPermissionCompletionRail(queue).before_tool_call(resumed)
        return resumed
    finally:
        reset_root_permission_request(token)


@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("name,args", [
    ("unknown_lookup", {"query": "approval"}),
    ("bash", {"command": "git status"}),
])
async def test_remember_activates_next_call_and_survives_rebuild(storage, tmp_path, smart, permanent, name, args):
    policy, global_path = storage
    policy["mode"] = "auto" if smart else "manual"
    policy["defaults"] = {"*": "ask"}
    if not smart:
        policy["tools"] = {name: "ask"}
    config._dump_yaml_round_trip(global_path, {"permissions": policy})
    original = global_path.read_bytes()
    rail = build(policy, tmp_path, "session-a", smart)
    if smart:
        rail._maybe_run_reviewer = AsyncMock(wraps=rail._maybe_run_reviewer)
    first = context("session-a", name, args)
    resumed = await approve(rail, first, permanent)
    assert not resumed.extra.get("_skip_tool")
    target = layers.user_permissions_path() if permanent else layers.session_permissions_path("session-a")
    assert target.is_file()
    assert global_path.read_bytes() == original
    review_count = rail._maybe_run_reviewer.await_count if smart else 0
    # Same rail, same session, same logical turn; Core markers are deliberately absent.
    first.session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: {}})
    next_call = context("session-a", name, args, call_id="call-2")
    next_call.session = first.session
    await rail.before_tool_call(next_call)
    assert not next_call.extra.get("_skip_tool")
    if smart:
        assert rail._maybe_run_reviewer.await_count == review_count
    rebuilt = build(policy, tmp_path, "session-a", smart)
    rebuilt_call = context("session-a", name, args, "rebuilt")
    await rebuilt.before_tool_call(rebuilt_call)
    assert not rebuilt_call.extra.get("_skip_tool")
    if name == "bash":
        with pytest.raises(AbortError):
            await rebuilt.before_tool_call(context("session-a", name, {"command": "git diff"}, "different-command"))
    other = build(policy, tmp_path, "session-b", smart)
    if permanent:
        other_call = context("session-b", name, args, "other")
        await other.before_tool_call(other_call)
        assert not other_call.extra.get("_skip_tool")
    else:
        with pytest.raises(AbortError):
            await other.before_tool_call(context("session-b", name, args, "other"))


@pytest.mark.parametrize("permanent", [False, True])
async def test_latest_deny_invalidates_pending_approval(storage, tmp_path, permanent):
    policy, global_path = storage
    rail = build(policy, tmp_path, "session-a", True)

    def deny():
        latest = deepcopy(policy)
        latest["tools"]["unknown_lookup"] = "deny"
        config._dump_yaml_round_trip(global_path, {"permissions": latest})

    resumed = await approve(rail, context("session-a", "unknown_lookup", {}), permanent, deny)
    assert resumed.extra["_skip_tool"] is True
    assert not layers.user_permissions_path().exists()
    assert not layers.session_permissions_path("session-a").exists()


@pytest.mark.parametrize("permanent", [False, True])
async def test_send_remember_uses_shared_files_and_exact_read_axes(storage, tmp_path, permanent):
    policy, global_path = storage
    root = tmp_path / "workspace"
    root.mkdir()
    first, second = tmp_path / "one.txt", tmp_path / "two.txt"
    first.write_text("one")
    second.write_text("two")
    policy["tools"] = {"send_file_to_user": "allow"}
    policy["file_guard"] = {
        "enabled": True, "mode": "native",
        "workspace": {"read": "allow", "write": "allow", "exec": "ask"},
        "defaults": {"read": "ask", "write": "ask", "exec": "ask"},
    }
    config._dump_yaml_round_trip(global_path, {"permissions": policy})
    rail = build(policy, root, "session-a", True)
    args = {"abs_file_path_list": json.dumps([str(first), str(second)]), "target_channels": ["web"]}
    channel = TOOL_PERMISSION_CHANNEL_ID.set("web")
    request = TOOL_PERMISSION_REQUEST_ID.set("same-turn")
    try:
        resumed = await approve(rail, context("session-a", "send_file_to_user", args), permanent)
        assert not resumed.extra.get("_skip_tool")
        assert current_send_file_execution_grant() is not None
        saved = layers.load_user_permissions() if permanent else layers.load_session_permissions("session-a")
        paths = saved["file_guard"]["paths"]
        assert {entry["path"] for entry in paths} == {str(first), str(second)}
        assert all(entry["read"] == "allow" and entry["write"] == "ask" and entry["exec"] == "ask" for entry in paths)
        clear_send_file_execution_grant()
        next_call = context("session-a", "send_file_to_user", args, "next-send")
        await rail.before_tool_call(next_call)
        assert not next_call.extra.get("_skip_tool")
        assert current_send_file_execution_grant() is not None
        third = tmp_path / "three.txt"
        third.write_text("three")
        changed = {**args, "abs_file_path_list": json.dumps([str(first), str(third)])}
        with pytest.raises(AbortError):
            await rail.before_tool_call(context("session-a", "send_file_to_user", changed, "changed-send"))
        assert current_send_file_execution_grant() is None
    finally:
        clear_send_file_execution_grant()
        TOOL_PERMISSION_REQUEST_ID.reset(request)
        TOOL_PERMISSION_CHANNEL_ID.reset(channel)


@pytest.mark.parametrize("permanent", [False, True])
async def test_write_failure_uses_core_fallback_without_claiming_a_file(storage, tmp_path, monkeypatch, permanent):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    monkeypatch.setattr(layers, "_dump_yaml_dict", lambda *_: False)
    first = context("session-a", "unknown_lookup", {})
    resumed = await approve(rail, first, permanent)
    assert not resumed.extra.get("_skip_tool")
    assert not layers.user_permissions_path().exists()
    assert not layers.session_permissions_path("session-a").exists()
    assert first.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) == {"unknown_lookup": True}
    with pytest.raises(AbortError):
        await rail.before_tool_call(context("session-a", "unknown_lookup", {}, "next-call"))


@pytest.mark.parametrize("permanent", [False, True])
async def test_unreadable_policy_blocks_instead_of_using_write_fallback(storage, tmp_path, permanent):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    resumed = await approve(
        rail, context("session-a", "unknown_lookup", {}), permanent,
        lambda: layers.user_permissions_path().write_text("broken: ["),
    )
    assert resumed.extra["_skip_tool"] is True
    assert resumed.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})


async def test_legacy_remember_payload_uses_core_permanent_semantics(storage, tmp_path):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    resumed = await approve(rail, context("session-a", "unknown_lookup", {}), None)
    assert not resumed.extra.get("_skip_tool")
    assert layers.user_permissions_path().is_file()
    assert not layers.session_permissions_path("session-a").exists()


async def test_allow_once_uses_same_core_chain_without_writing_rules(storage, tmp_path):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    resumed = await approve(rail, context("session-a", "unknown_lookup", {}), False, remember=False)
    assert not resumed.extra.get("_skip_tool")
    assert not layers.user_permissions_path().exists()
    assert not layers.session_permissions_path("session-a").exists()
    assert resumed.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})
    with pytest.raises(AbortError):
        await rail.before_tool_call(context("session-a", "unknown_lookup", {}, "next"))


@pytest.mark.parametrize("permanent", [False, True])
async def test_changed_pending_arguments_cannot_gain_a_rule(storage, tmp_path, permanent):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    first = context("session-a", "unknown_lookup", {"query": "original"})
    def change():
        first.inputs.tool_args["query"] = "changed"
        first.inputs.tool_call.arguments = json.dumps(first.inputs.tool_args)
    resumed = await approve(rail, first, permanent, change)
    assert resumed.extra["_skip_tool"] is True
    assert not layers.user_permissions_path().exists()
    assert not layers.session_permissions_path("session-a").exists()


@pytest.mark.parametrize("permanent", [False, True])
async def test_permanent_success_clears_old_core_marker(storage, tmp_path, permanent):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    first = context("session-a", "unknown_lookup", {})
    resumed = await approve(rail, first, permanent, lambda: first.session.update_state({
        INTERRUPT_AUTO_CONFIRM_KEY: {"unknown_lookup": True},
    }))
    assert not resumed.extra.get("_skip_tool")
    assert first.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) == ({} if permanent else {"unknown_lookup": True})


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("run_policy", [{}, {"unknown_lookup": "ask"}])
async def test_run_scoped_policy_cannot_be_remembered(storage, tmp_path, permanent, run_policy):
    from jiuwenswarm.runtime.run_permissions import RUN_PERMISSIONS
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    token = RUN_PERMISSIONS.set(run_policy)
    try:
        resumed = await approve(rail, context("session-a", "unknown_lookup", {}), permanent)
        assert resumed.extra["_skip_tool"] is True
        assert not layers.user_permissions_path().exists()
        assert not layers.session_permissions_path("session-a").exists()
    finally:
        RUN_PERMISSIONS.reset(token)
