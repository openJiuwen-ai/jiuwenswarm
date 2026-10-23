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
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
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


async def approve(
    rail, ctx, permanent, before_answer=None, remember=True, *,
    approved=True, queue=None, request_id="same-turn",
):
    """Use the existing queue reservation/claim and default Core confirmation."""
    queue = RootPermissionQueue() if queue is None else queue
    queue_rail = RootPermissionQueueRail(queue)
    token = bind_root_permission_request(
        root_session_id=ctx.session.get_session_id(), request_id=request_id,
        enabled=True, queue=queue,
    )
    try:
        await queue_rail.before_tool_call(ctx)
        with pytest.raises(AbortError) as raised:
            await rail.before_tool_call(ctx)
        ctx.exception = raised.value
        await queue_rail.after_tool_call(ctx)
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
        payload = {"approved": approved, "auto_confirm": remember if approved else False}
        if permanent is not None:
            payload["persist_allow"] = permanent
        answer.update(card.key.invocation_id, payload)
        reserved = queue.reserve_answer(ctx.session.get_session_id(), answer)
        resumed = context(
            ctx.session.get_session_id(), ctx.inputs.tool_name, ctx.inputs.tool_args,
            call_id=ctx.inputs.tool_call.id,
        )
        resumed.session = ctx.session
        resumed.agent = ctx.agent
        resumed.extra[ROOT_PERMISSION_QUEUE_USER_INPUT_KEY] = reserved.interactive_input
        await queue_rail.before_tool_call(resumed)
        await rail.before_tool_call(resumed)
        await RootPermissionCompletionRail(queue).before_tool_call(resumed)
        assert queue.get(card.key) is None
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


@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("existing_rules", [False, True])
async def test_reject_and_allow_once_apply_only_to_current_call(storage, tmp_path, smart, existing_rules):
    policy, global_path = storage
    policy["mode"] = "auto" if smart else "manual"
    policy["defaults"] = {"*": "ask"}
    policy["tools"] = {"unknown_lookup": "ask"}
    config.dump_yaml_round_trip(global_path, {"permissions": policy})
    paths = [global_path, layers.user_permissions_path(), layers.session_permissions_path("session-a")]
    if existing_rules:
        for path in paths[1:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            config.dump_yaml_round_trip(path, {"deny_tools": ["unrelated_tool"]})
    original = {path: path.read_bytes() if path.exists() else None for path in paths}
    rail = build(policy, tmp_path, "session-a", smart)
    queue, executed = RootPermissionQueue(), []
    manager = AbilityManager(owner_id="reject-once")
    agent = SimpleNamespace(
        ability_manager=manager, agent_callback_manager=AgentCallbackManager("reject-once"),
    )
    tool = LocalFunction(
        card=ToolCard(
            id="reject-once-target", name="unknown_lookup", description="Approval probe",
            input_params={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        func=lambda query: executed.append(query) or "executed-once",
    )
    manager.add_ability(tool.card, tool)
    first = context("session-a", "unknown_lookup", {"query": "same"}, "rejected-call")
    first.agent = agent
    try:
        for approved, call_id in ((False, "rejected-call"), (True, "allowed-call")):
            call = context("session-a", "unknown_lookup", {"query": "same"}, call_id)
            call.agent, call.session = agent, first.session
            resumed = await approve(
                rail, call, None, remember=False, approved=approved,
                queue=queue, request_id=f"request-{call_id}",
            )
            results = await manager.execute(resumed, resumed.inputs.tool_call, first.session)
            assert executed == (["same"] if approved else [])
            if approved:
                assert results[0][0] == "executed-once"
            assert not queue.has_live(root_session_id="session-a")
            assert first.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})
            assert {path: path.read_bytes() if path.exists() else None for path in paths} == original
        third = context("session-a", "unknown_lookup", {"query": "same"}, "third-call")
        third.agent, third.session = agent, first.session
        token = bind_root_permission_request(
            root_session_id="session-a", request_id="request-third-call", enabled=True, queue=queue,
        )
        try:
            queue_rail = RootPermissionQueueRail(queue)
            await queue_rail.before_tool_call(third)
            with pytest.raises(AbortError) as raised:
                await rail.before_tool_call(third)
            third.exception = raised.value
            await queue_rail.after_tool_call(third)
            card = queue.pending_for_call(
                root_session_id="session-a", execution_session_id="session-a", tool_call_id="third-call",
            )
            assert card is not None and card.key.request_id == "request-third-call"
            assert executed == ["same"]
            assert {path: path.read_bytes() if path.exists() else None for path in paths} == original
        finally:
            reset_root_permission_request(token)
    finally:
        manager.remove("unknown_lookup")


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


@pytest.mark.parametrize("permanent", [False, True])
async def test_remember_restores_in_a_fresh_process(storage, tmp_path, permanent):
    policy, global_path = storage
    rail = build(policy, tmp_path, "session-a", True)
    await approve(rail, context("session-a", "unknown_lookup", {}), permanent)
    program = textwrap.dedent("""
        import asyncio, sys
        from pathlib import Path
        from jiuwenswarm.common import config
        from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers
        from tests.unit_tests.agentserver.permissions.test_shared_remember_chain import build, context
        global_path, user_path, session_path, root = map(Path, sys.argv[1:])
        config.CONFIG_YAML_PATH = config._CONFIG_YAML_PATH = global_path
        layers.user_permissions_path = lambda: user_path
        layers.session_permissions_path = lambda _: session_path
        policy = config.load_yaml_round_trip(global_path)["permissions"]
        rail = build(policy, root, "session-a", True)
        ctx = context("session-a", "unknown_lookup", {}, "fresh-process")
        asyncio.run(rail.before_tool_call(ctx))
        assert not ctx.extra.get("_skip_tool")
        print("restored_from_shared_files")
    """)
    result = subprocess.run([
        sys.executable, "-c", program, str(global_path),
        str(layers.user_permissions_path()), str(layers.session_permissions_path("session-a")),
        str(tmp_path),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "restored_from_shared_files" in result.stdout


@pytest.mark.parametrize("change", ["remove", "ask", "deny"])
async def test_user_rule_changes_apply_on_next_call(storage, tmp_path, change):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    await approve(rail, context("session-a", "unknown_lookup", {}), True)
    if change == "remove":
        layers.user_permissions_path().unlink()
    else:
        config.dump_yaml_round_trip(layers.user_permissions_path(), {
            f"{change}_tools": ["unknown_lookup"],
        })
    following = context("session-a", "unknown_lookup", {}, "next")
    if change == "deny":
        await rail.before_tool_call(following)
        assert following.extra["_skip_tool"] is True
    else:
        with pytest.raises(AbortError):
            await rail.before_tool_call(following)


async def test_each_callback_loads_shared_layers_once_including_owner_scene(storage, tmp_path, monkeypatch):
    policy, _ = storage
    config.dump_yaml_round_trip(layers.user_permissions_path(), {"allow_tools": ["unknown_lookup"]})
    counts = {"user": 0, "session": 0}
    reader = layers.read_permission_layers_locked
    def read_layers(sid=None, **kwargs):
        counts["user"] += 1
        counts["session"] += 1
        return reader(sid, **kwargs)
    monkeypatch.setattr(layers, "read_permission_layers_locked", read_layers)
    rail = build(policy, tmp_path, "session-a", True)
    counts.update(user=0, session=0)
    owner = TOOL_PERMISSION_CONTEXT.set(PermissionContext(channel_id="web", principal_user_id="owner"))
    try:
        await rail.before_tool_call(context("session-a", "unknown_lookup", {}))
        assert counts == {"user": 1, "session": 1}
        config.dump_yaml_round_trip(layers.user_permissions_path(), {"deny_tools": ["unknown_lookup"]})
        following = context("session-a", "unknown_lookup", {}, "next")
        await rail.before_tool_call(following)
        assert following.extra["_skip_tool"] is True
        assert counts == {"user": 2, "session": 2}
    finally:
        TOOL_PERMISSION_CONTEXT.reset(owner)


@pytest.mark.parametrize("permanent", [False, True])
async def test_native_read_remember_reloads_exact_file_axis(storage, native, permanent, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_stream_metadata import (
        REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY,
    )
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata._configured_reviewer_display_language",
        lambda: "en",
    )
    _, global_path = storage
    config.dump_yaml_round_trip(global_path, {"permissions": native.permissions})
    external = native.root.parent / "approved.txt"
    external.write_text("approved")
    first = context("native-session", "read_file", {"file_path": str(external)})
    first.agent = SimpleNamespace(ability_manager=native.manager)
    resumed = await approve(native.rail, first, permanent)
    assert not resumed.extra.get("_skip_tool")
    saved = layers.load_user_permissions() if permanent else layers.load_session_permissions("native-session")
    assert saved["file_guard"]["paths"] == [{
        "path": str(external), "read": "allow", "write": "ask", "exec": "ask",
        "match": "prefix",
    }]
    first.session.update_state({INTERRUPT_AUTO_CONFIRM_KEY: {}})
    following = context("native-session", "read_file", {"file_path": str(external)}, "next")
    following.agent, following.session = first.agent, first.session
    native.rail._maybe_run_reviewer = AsyncMock(wraps=native.rail._maybe_run_reviewer)
    await native.rail.before_tool_call(following)
    assert not following.extra.get("_skip_tool")
    assert native.rail._maybe_run_reviewer.await_count == 0
    metadata = following.extra[REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY]["next"]
    assert metadata["decision_source"] == "policy_evaluator"
    assert metadata["reviewer_status"] == metadata["final_reviewer_status"] == "approved"
    assert metadata["evidence_summary"] == (
        "Your permission settings already allow this action. No approval needed."
    )
    assert "reviewer_called" not in metadata

    events = []

    async def capture(event):
        events.append(event)

    following.session.write_stream = capture
    following.extra[REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY]["other-call"] = {"reviewer_status": "denied"}
    stream = JiuSwarmStreamEventRail()
    await stream.before_tool_call(following)
    tool_args = following.inputs.tool_args
    if isinstance(tool_args, str):
        tool_args = json.loads(tool_args)
    following.inputs.tool_result = await native.tools["read_file"].invoke(tool_args)
    assert following.inputs.tool_result.success
    await stream.after_tool_call(following)
    progress = [
        event.payload["tool_update"] for event in events
        if event.type == "tool_update" and "reviewer_metadata" in event.payload["tool_update"]
    ]
    terminal = [event.payload["tool_result"] for event in events if event.type == "tool_result"]
    assert progress[0]["tool_call_id"] == terminal[0]["tool_call_id"] == "next"
    assert progress[0]["reviewer_metadata"] == terminal[0]["reviewer_metadata"] == metadata
    assert terminal[0]["success"] is True
    assert following.extra[REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY] == {
        "other-call": {"reviewer_status": "denied"},
    }

    rebuilt = build(native.permissions, native.root, "other-session", True)
    rebuilt._maybe_run_reviewer = AsyncMock(wraps=rebuilt._maybe_run_reviewer)
    cross = context("other-session", "read_file", {"file_path": str(external)}, "cross")
    cross.agent = first.agent
    if permanent:
        await rebuilt.before_tool_call(cross)
        assert not cross.extra.get("_skip_tool")
        assert (cross.extra[REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY]["cross"]["evidence_summary"]
                == metadata["evidence_summary"])
        assert rebuilt._maybe_run_reviewer.await_count == 0
    else:
        with pytest.raises(AbortError):
            await rebuilt.before_tool_call(cross)
    other = native.root.parent / "other.txt"
    other.write_text("other")
    changed = context("native-session", "read_file", {"file_path": str(other)}, "other")
    changed.agent = first.agent
    with pytest.raises(AbortError):
        await native.rail.before_tool_call(changed)


@pytest.mark.parametrize("failure", ["malformed", "read_error"])
async def test_damaged_user_deny_never_falls_through_to_session_allow(storage, tmp_path, monkeypatch, failure):
    policy, _ = storage
    layers.session_permissions_path("session-a").parent.mkdir(parents=True, exist_ok=True)
    config.dump_yaml_round_trip(layers.user_permissions_path(), {"deny_tools": ["unknown_lookup"]})
    layers.session_permissions_path("session-a").parent.mkdir(parents=True, exist_ok=True)
    config.dump_yaml_round_trip(layers.session_permissions_path("session-a"), {"allow_tools": ["unknown_lookup"]})
    rail = build(policy, tmp_path, "session-a", True)
    first = context("session-a", "unknown_lookup", {})
    await rail.before_tool_call(first)
    assert first.extra["_skip_tool"] is True
    if failure == "malformed":
        layers.user_permissions_path().write_text("broken: [")
    else:
        original = layers._load_yaml_dict
        def fail_read(path, **kwargs):
            if path == layers.user_permissions_path():
                raise OSError("cannot read User policy")
            return original(path, **kwargs)
        monkeypatch.setattr(layers, "_load_yaml_dict", fail_read)
    following = context("session-a", "unknown_lookup", {}, "next")
    await rail.before_tool_call(following)
    assert following.extra["_skip_tool"] is True


@pytest.mark.parametrize("permanent", [False, True])
@pytest.mark.parametrize("change", ["mode", "global_allow"])
async def test_pending_global_change_cannot_report_empty_persist_success(storage, tmp_path, permanent, change):
    policy, global_path = storage
    rail = build(policy, tmp_path, "session-a", True)
    def modify_global():
        latest = deepcopy(policy)
        if change == "mode":
            latest["mode"] = "manual"
        else:
            latest["approval_overrides"] = [{
                "id": "new-rule", "tools": ["bash"],
                "match_type": "command", "pattern": "git status *", "action": "allow",
            }]
        config.dump_yaml_round_trip(global_path, {"permissions": latest})
    first = context("session-a", "bash", {"command": "git status"}) if change == "global_allow" else context("session-a", "unknown_lookup", {})
    resumed = await approve(rail, first, permanent, modify_global)
    assert resumed.extra["_skip_tool"] is True
    assert not layers.user_permissions_path().exists()
    assert not layers.session_permissions_path("session-a").exists()


async def test_pending_card_uses_captured_asks_after_reviewer_await(storage, native, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import PermissionHandlingResult
    from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import build_manual_approval_required_response
    _, global_path = storage
    config.dump_yaml_round_trip(global_path, {"permissions": native.permissions})
    path = native.root.parent / "pending.txt"
    path.write_text("pending")
    first = context("native-session", "read_file", {"file_path": str(path)})
    first.agent = SimpleNamespace(ability_manager=native.manager)
    entered, release = asyncio.Event(), asyncio.Event()
    async def review(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return PermissionHandlingResult(True, build_manual_approval_required_response("reviewer_wait"), "reviewer")
    monkeypatch.setattr(native.rail, "_maybe_run_reviewer", review)
    queue = RootPermissionQueue()
    token = bind_root_permission_request(root_session_id="native-session", request_id="pending", enabled=True, queue=queue)
    try:
        await RootPermissionQueueRail(queue).before_tool_call(first)
        pending = asyncio.create_task(native.rail.before_tool_call(first))
        await asyncio.wait_for(entered.wait(), 5)
        different = deepcopy(native.permissions)
        different["file_guard"]["defaults"]["read"] = "allow"
        native.rail.base_rail.update_config(different)
        release.set()
        with pytest.raises(AbortError) as interrupted:
            await pending
        assert interrupted.value.cause.request.metadata["permission_ask_accesses"] == [(str(path), "read")]
    finally:
        release.set()
        reset_root_permission_request(token)


@pytest.mark.parametrize("permanent", [False, True])
def test_real_handler_sqlite_and_restart_chain(tmp_path, permanent):
    root = tmp_path / "checkpoint-chain"
    for phase in ("approve", "restart"):
        result = subprocess.run([
            sys.executable, "-m", "tests.unit_tests.agentserver.permissions.shared_remember_checkpoint_probe",
            str(root), str(int(permanent)), phase,
        ], capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
    approved = json.loads((root / "approve.json").read_text())
    restored = json.loads((root / "restart.json").read_text())
    assert approved["handler_calls"] == 1 and approved["persisted"] is True
    assert restored["sqlite_restored"] is True and restored["file_rule_allowed_without_marker"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("permanent", [False, True])
async def test_adapter_reload_keeps_global_separate_before_remember(storage, tmp_path, smart, permanent):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    policy, global_path = storage
    policy["mode"] = "auto" if smart else "manual"
    policy["tools"]["unknown_lookup"] = "ask"
    config._dump_yaml_round_trip(global_path, {"permissions": policy})
    original_global = global_path.read_bytes()
    user_path = layers.user_permissions_path()
    session_path = layers.session_permissions_path("session-a")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    config._dump_yaml_round_trip(user_path, {"tools": {"previous_user": "allow"}})
    config._dump_yaml_round_trip(session_path, {"tools": {"previous_session": "allow"}})
    rail = build(policy, tmp_path, "session-a", smart)
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("session-a")
    adapter._permission_rail = rail
    adapter._update_permission_rail({"permissions": deepcopy(policy)})
    resumed = await approve(rail, context("session-a", "unknown_lookup", {}), permanent)
    assert not resumed.extra.get("_skip_tool")
    saved = layers.load_user_permissions() if permanent else layers.load_session_permissions("session-a")
    assert saved.get("approval_overrides") or "unknown_lookup" in saved.get("allow_tools", [])
    next_call = context("session-a", "unknown_lookup", {}, "next-call")
    await rail.before_tool_call(next_call)
    assert not next_call.extra.get("_skip_tool")
    assert global_path.read_bytes() == original_global
    for path, overlay in (
        (user_path, layers.load_user_permissions()),
        (session_path, layers.load_session_permissions("session-a")),
    ):
        tools = overlay.get("tools", {})
        tools.pop("previous_user", None)
        tools.pop("previous_session", None)
        if "allow_tools" in overlay:
            overlay["allow_tools"] = [
                name for name in overlay["allow_tools"]
                if name not in {"previous_user", "previous_session"}
            ]
        config._dump_yaml_round_trip(path, overlay)
    base = rail.base_rail if smart else rail
    snapshot = base.get_permission_snapshot(session_id="session-a")
    assert "previous_user" not in snapshot["tools"]
    assert "previous_session" not in snapshot["tools"]


@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("failure", [
    "user_invalid", "session_invalid", "user_unreadable", "session_unreadable",
    "lock", "compose", "update",
])
def test_adapter_reload_failure_preserves_global_and_installed_denies(
    storage, tmp_path, monkeypatch, smart, failure,
):
    from unittest.mock import Mock
    from jiuwenswarm.agents.harness.common.rails.permissions import permission_compose
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    policy, _ = storage
    policy["mode"] = "auto" if smart else "manual"
    policy["tools"]["blocked_tool"] = "deny"
    paths = (layers.user_permissions_path(), layers.session_permissions_path("session-a"))
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        config._dump_yaml_round_trip(path, {"tools": {"overlay_blocked": "deny"}})
    originals = {path: path.read_bytes() for path in paths}
    rail = build(policy, tmp_path, "session-a", smart)
    base = rail.base_rail if smart else rail
    installed = base.installed_permission_config()
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("session-a")
    adapter._permission_rail = rail
    desired = deepcopy(policy)
    desired["tools"]["blocked_tool"] = "allow"
    try:
        with monkeypatch.context() as patch:
            if failure.endswith("invalid"):
                paths[int(failure.startswith("session"))].write_text("tools: [broken")
            elif failure.endswith("unreadable"):
                unreadable = paths[int(failure.startswith("session"))]
                read = config._load_yaml_round_trip

                def fail_read(path):
                    if path == unreadable:
                        raise PermissionError("unreadable_overlay")
                    return read(path)

                patch.setattr(config, "_load_yaml_round_trip", fail_read)
            else:
                target, attribute = {
                    "lock": (layers, "permission_storage_lock"),
                    "compose": (permission_compose, "compose_host_effective_permissions"),
                    "update": (base, "update_config"),
                }[failure]
                patch.setattr(target, attribute, Mock(side_effect=RuntimeError(failure)))
            with pytest.raises(Exception):
                adapter._update_permission_rail({"permissions": desired})
            assert base.installed_permission_config() == installed
            if smart:
                token = TOOL_PERMISSION_CONTEXT.set(PermissionContext(group_digital_avatar=True))
                try:
                    assert base.get_permission_snapshot()["tools"]["blocked_tool"] == "deny"
                finally:
                    TOOL_PERMISSION_CONTEXT.reset(token)
    finally:
        for path, content in originals.items():
            path.write_bytes(content)
    current = base.get_permission_snapshot(session_id="session-a")
    assert current["tools"]["blocked_tool"] == "deny"
    assert current["tools"]["overlay_blocked"] == "deny"
