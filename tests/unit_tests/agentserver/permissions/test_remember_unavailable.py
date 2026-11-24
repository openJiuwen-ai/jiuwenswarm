"""A valid exact approval remains single-use when no safe rule can be saved."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
from openjiuwen.core.runner.callback import AbortError
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.interrupt.state import INTERRUPT_AUTO_CONFIRM_KEY
from openjiuwen.harness.rails.interrupt.interrupt_base import RejectResult
from openjiuwen.harness.security import PermissionConfirmResponse

from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers as layers
from jiuwenswarm.agents.harness.common.rails.permissions import permission_interrupt_rail as exact
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import reviewer_metadata
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_persist import (
    PermissionApprovalInvalidated, PermissionRememberUnavailable, persist_exact_permission_allow_rule,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_stream_metadata import (
    peek_reviewer_tool_result_metadata,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueue, RootPermissionQueueError,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    ROOT_PERMISSION_QUEUE_USER_INPUT_KEY, RootPermissionQueueRail, RootPermissionCompletionRail,
    bind_root_permission_request, reset_root_permission_request,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from tests.unit_tests.agentserver.permissions.test_shared_remember_chain import (
    approve, build, context, storage as storage,
)


def _compound_command(tmp_path):
    return {"command": (
        f"f='{tmp_path}/file_15.txt'; if [ -f \"$f\" ]; then rm \"$f\" && "
        "echo \"DELETED: $f\"; else echo \"NOT_FOUND: $f\"; fi"
    )}


def _bytes(paths):
    return {path: path.read_bytes() if path.exists() else None for path in paths}


@pytest.mark.parametrize("permanent,remember", [(False, False), (False, True), (True, True)])
async def test_complex_approval_executes_once_without_saving_rules(storage, tmp_path, permanent, remember):
    policy, global_path = storage
    rail = build(policy, tmp_path, "session-a", True)
    paths = [global_path, layers.user_permissions_path(), layers.session_permissions_path("session-a")]
    before = _bytes(paths)
    marker = tmp_path / "executions.txt"
    manager = AbilityManager(owner_id="remember-unavailable")
    agent = SimpleNamespace(
        ability_manager=manager, agent_callback_manager=AgentCallbackManager("remember-unavailable"),
    )

    def execute(command: str) -> str:
        marker.write_text((marker.read_text() if marker.exists() else "") + "ONCE\n")
        return "executed-once"

    tool = LocalFunction(card=ToolCard(
        id="remember-unavailable-bash", name="bash", description="Controlled approval side effect",
        input_params={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    ), func=execute)
    manager.add_ability(tool.card, tool)
    first = context("session-a", "bash", _compound_command(tmp_path))
    first.agent = agent
    try:
        assert not marker.exists()
        resumed = await approve(rail, first, permanent, remember=remember)
        assert exact._EXACT_PERSIST_ATTEMPT.get() is None
        assert not resumed.extra.get("_skip_tool") and not marker.exists()
        metadata = peek_reviewer_tool_result_metadata(resumed.extra, tool_call_id="call-1")
        assert metadata["reviewer_status"] == "approved"
        assert metadata["decision_source"] == "manual_approval"
        assert (metadata.get("reviewer_reason_code") == "manual_allow_once_remember_unavailable") is remember
        results = await manager.execute(resumed, resumed.inputs.tool_call, first.session)
        assert results[0][0] == "executed-once" and marker.read_text() == "ONCE\n"
        assert _bytes(paths) == before
        assert first.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})
        following = context("session-a", "bash", first.inputs.tool_args, "next-call")
        following.agent, following.session = agent, first.session
        rejected = await approve(rail, following, False, remember=False, approved=False)
        await manager.execute(rejected, rejected.inputs.tool_call, first.session)
        assert marker.read_text() == "ONCE\n" and _bytes(paths) == before
    finally:
        manager.remove("bash")


@pytest.mark.parametrize("permanent", [False, True])
async def test_unavailable_remember_never_writes_a_nonempty_auto_confirm_key(storage, tmp_path, monkeypatch, permanent):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    base = rail.base_rail

    def persist(name, args, accesses, session_only=False, validate_only=False):
        if validate_only:
            return True
        raise PermissionRememberUnavailable("approval_rule_unavailable")

    monkeypatch.setattr(base, "_exact_persist_callback", persist)
    monkeypatch.setattr(base, "_get_auto_confirm_key", lambda call: "current-key")
    store = Mock(wraps=base._store_auto_confirm)
    monkeypatch.setattr(base, "_store_auto_confirm", store)
    first = context("session-a", "unknown_lookup", {"query": "current"},
                    state={INTERRUPT_AUTO_CONFIRM_KEY: {"sibling-key": True}})
    resumed = await approve(rail, first, permanent)
    assert not resumed.extra.get("_skip_tool")
    store.assert_not_called()
    assert first.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) == {"sibling-key": True}


@pytest.mark.parametrize("phase,error", [
    ("validate", PermissionRememberUnavailable("approval_rule_unavailable")),
    ("validate", PermissionApprovalInvalidated("approval_accesses_changed")),
    ("persist", PermissionApprovalInvalidated("installed_policy_changed")),
    ("persist", PermissionApprovalInvalidated("current_policy_denied")),
    ("persist", RuntimeError("unexpected storage failure")),
])
@pytest.mark.parametrize("permanent,scope", [(False, "session"), (True, "user")])
async def test_other_approval_failures_still_reject_and_log_the_phase(
    storage, tmp_path, monkeypatch, caplog, phase, error, permanent, scope,
):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)

    def persist(name, args, accesses, session_only=False, validate_only=False):
        if (phase == "validate") == validate_only:
            raise error
        return True

    monkeypatch.setattr(rail.base_rail, "_exact_persist_callback", persist)
    resumed = await approve(rail, context("session-a", "unknown_lookup", {"query": "private-args"}), permanent)
    assert exact._EXACT_PERSIST_ATTEMPT.get() is None
    assert resumed.extra["_skip_tool"] is True
    assert "approval_invalidated" in str(resumed.extra)
    records = [record.getMessage() for record in caplog.records if "permission.approval.invalidated" in record.getMessage()]
    assert len(records) == 1 and f"phase={phase}" in records[0]
    assert f"request_scope={scope}" in records[0]
    assert "tool_call_id=call-1" in records[0] and "session_id=session-a" in records[0]
    assert "private-args" not in records[0]
    assert not layers.user_permissions_path().exists()
    assert not layers.session_permissions_path("session-a").exists()


@pytest.mark.parametrize("missing", ["approved", "remember_requested", "validation_succeeded"])
def test_unvalidated_attempt_cannot_fall_back(storage, tmp_path, monkeypatch, missing):
    policy, _ = storage
    base = build(policy, tmp_path, "session-a", True).base_rail
    monkeypatch.setattr(base, "_exact_persist_callback", Mock(side_effect=PermissionRememberUnavailable("approval_rule_unavailable")))
    attempt = exact._ExactPersistAttempt(approved=True, remember_requested=True, validation_succeeded=True)
    setattr(attempt, missing, False)
    token = exact._EXACT_PERSIST_ATTEMPT.set(attempt)
    try:
        assert base._persist_session_allow("bash", {"command": "git status"}, session_id="session-a") is False
        assert attempt.invalidated and not attempt.remember_unavailable
    finally:
        exact._EXACT_PERSIST_ATTEMPT.reset(token)


@pytest.mark.parametrize("permanent", [False, True])
async def test_hosted_remember_without_card_validation_stays_rejected(
    storage, tmp_path, monkeypatch, caplog, permanent,
):
    policy, global_path = storage
    base = build(policy, tmp_path, "session-a", True).base_rail
    paths = [global_path, layers.user_permissions_path(), layers.session_permissions_path("session-a")]
    before = _bytes(paths)
    monkeypatch.setattr(base, "_exact_persist_callback", Mock(side_effect=PermissionRememberUnavailable("approval_rule_unavailable")))
    monkeypatch.setattr(base._host, "permission_scene_hook", None)
    hosted = AsyncMock(return_value=PermissionConfirmResponse(
        approved=True, auto_confirm=True, persist_allow=permanent,
    ))
    monkeypatch.setattr(base._host, "request_permission_confirmation", hosted)
    first = context("session-a", "bash", _compound_command(tmp_path))
    decision = await base.resolve_interrupt(first, first.inputs.tool_call, None)
    assert isinstance(decision, RejectResult) and "approval_invalidated" in str(decision.tool_result)
    hosted.assert_awaited_once()
    assert exact._EXACT_PERSIST_ATTEMPT.get() is None
    assert first.session.get_state(INTERRUPT_AUTO_CONFIRM_KEY) in (None, {})
    assert _bytes(paths) == before
    records = [
        record.getMessage() for record in caplog.records
        if "permission.approval.invalidated" in record.getMessage()
    ]
    assert len(records) == 1 and "phase=persist" in records[0]
    assert "request_scope=unknown" in records[0]
    assert "tool_call_id=call-1" in records[0] and "session_id=session-a" in records[0]


async def test_single_use_validation_failure_keeps_once_scope(storage, tmp_path, monkeypatch, caplog):
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    persist = Mock(side_effect=PermissionApprovalInvalidated("approval_accesses_changed"))
    monkeypatch.setattr(rail.base_rail, "_exact_persist_callback", persist)
    resumed = await approve(
        rail, context("session-a", "unknown_lookup", {"query": "private-args"}), False, remember=False,
    )
    assert resumed.extra["_skip_tool"] is True and "approval_invalidated" in str(resumed.extra)
    records = [
        record.getMessage() for record in caplog.records
        if "permission.approval.invalidated" in record.getMessage()
    ]
    assert len(records) == 1 and "phase=validate" in records[0]
    assert "request_scope=once" in records[0]
    assert "private-args" not in records[0]


def test_safe_rule_unavailability_is_distinct_and_does_not_write(storage, tmp_path):
    policy, path = storage
    original = path.read_bytes()
    args = _compound_command(tmp_path)
    assert persist_exact_permission_allow_rule("bash", args, auto_mode=True, workspace_root=tmp_path) is False
    with pytest.raises(PermissionRememberUnavailable, match="approval_rule_unavailable"):
        persist_exact_permission_allow_rule("bash", args, auto_mode=True, workspace_root=tmp_path, raise_on_invalid=True)
    assert persist_exact_permission_allow_rule("bash", args, auto_mode=True, workspace_root=tmp_path, validate_only=True)
    assert path.read_bytes() == original and not layers.user_permissions_path().exists()


@pytest.mark.parametrize("complex_first", [False, True])
async def test_concurrent_siblings_keep_remember_and_metadata_separate(storage, tmp_path, complex_first):
    policy, global_path = storage
    global_before = global_path.read_bytes()
    rail = build(policy, tmp_path, "session-a", True)
    queue = RootPermissionQueue()
    queue_rail = RootPermissionQueueRail(queue)
    state, executed = {}, []
    manager = AbilityManager(owner_id=f"sibling-remember-{complex_first}")
    agent = SimpleNamespace(ability_manager=manager, agent_callback_manager=AgentCallbackManager("sibling-remember"))
    tool = LocalFunction(card=ToolCard(
        id="sibling-bash", name="bash", description="Controlled sibling side effects",
        input_params={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    ), func=lambda command: executed.append(command) or "executed")
    manager.add_ability(tool.card, tool)
    calls = [context("session-a", "bash", {"command": "git status"}, "simple", state),
             context("session-a", "bash", _compound_command(tmp_path), "complex", state)]
    if complex_first:
        calls.reverse()
    token = bind_root_permission_request(root_session_id="session-a", request_id="parallel-turn", enabled=True, queue=queue)

    async def pause(call):
        call.agent = agent
        await queue_rail.before_tool_call(call)
        with pytest.raises(AbortError) as interrupted:
            await rail.before_tool_call(call)
        call.exception = interrupted.value
        await queue_rail.after_tool_call(call)
        return queue.pending_for_call(root_session_id="session-a", execution_session_id="session-a",
                                      tool_call_id=call.inputs.tool_call.id)

    try:
        cards = await asyncio.gather(*(pause(call) for call in calls))
        ids = [call.inputs.tool_call.id for call in calls]
        queue.reconcile({"result_type": "interrupt", "interrupt_ids": ids,
                         "state": [{"id": key, "value": card.request} for key, card in zip(ids, cards)]},
                        root_session_id="session-a")
        for call in calls:
            key = call.inputs.tool_call.id
            answer = InteractiveInput()
            answer.update(key, {"approved": True, "auto_confirm": True, "persist_allow": False})
            reserved = queue.reserve_answer("session-a", answer)
            resumed = context("session-a", "bash", call.inputs.tool_args, key, state)
            resumed.agent, resumed.session = agent, call.session
            resumed.extra[ROOT_PERMISSION_QUEUE_USER_INPUT_KEY] = reserved.interactive_input
            await queue_rail.before_tool_call(resumed)
            await rail.before_tool_call(resumed)
            await RootPermissionCompletionRail(queue).before_tool_call(resumed)
            assert not resumed.extra.get("_skip_tool")
            metadata = peek_reviewer_tool_result_metadata(resumed.extra, tool_call_id=key)
            assert (metadata.get("reviewer_reason_code") == "manual_allow_once_remember_unavailable") is (key == "complex")
            await manager.execute(resumed, resumed.inputs.tool_call, call.session)
            with pytest.raises(RootPermissionQueueError):
                queue.reserve_answer("session-a", answer)
        assert len(executed) == 2 and len(set(executed)) == 2
        saved = json.dumps(layers.load_session_permissions("session-a"))
        assert "git status" in saved and "file_15.txt" not in saved
        assert global_path.read_bytes() == global_before and not layers.user_permissions_path().exists()
        assert "bash:git status" in state[INTERRUPT_AUTO_CONFIRM_KEY]
        assert not queue.has_live(root_session_id="session-a")
    finally:
        reset_root_permission_request(token)
        manager.remove("bash")


@pytest.mark.parametrize("language,expected", [
    ("zh", "本次已允许，但未能记住授权"),
    ("en", "Allowed this invocation, but the permission was not remembered"),
])
async def test_remember_warning_survives_tool_result_projection(storage, tmp_path, monkeypatch, language, expected):
    monkeypatch.setattr(reviewer_metadata, "_configured_reviewer_display_language", lambda: language)
    policy, _ = storage
    rail = build(policy, tmp_path, "session-a", True)
    resumed = await approve(rail, context("session-a", "bash", _compound_command(tmp_path)), False)
    metadata = peek_reviewer_tool_result_metadata(resumed.extra, tool_call_id="call-1")
    assert expected in metadata["evidence_summary"]
    session = SimpleNamespace(events=[])

    async def emit(event):
        session.events.append(event)

    session.write_stream = emit
    await JiuSwarmStreamEventRail()._emit_tool_result(
        session, resumed.inputs.tool_call, {"success": True, "stdout": "unchanged-output"},
        reviewer_metadata=metadata,
    )
    payload = session.events[0].payload["tool_result"]
    assert payload["reviewer_metadata"]["evidence_summary"] == metadata["evidence_summary"]
    assert payload["reviewer_metadata"]["reviewer_status"] == "approved"
    assert "unchanged-output" in str(payload) and "approval_invalidated" not in str(payload)
