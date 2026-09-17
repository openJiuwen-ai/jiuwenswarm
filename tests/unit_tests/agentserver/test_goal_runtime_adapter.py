"""Tests for the Goal capability adapter used by JiuwenSwarm."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Goal capability tests exercise the checked-out OpenJiuwen implementation,
# not whichever released package happens to be installed in the test venv.
_AGENT_CORE_ROOT = Path(__file__).resolve().parents[3].parent / "agent-core"
if str(_AGENT_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_CORE_ROOT))

from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.harness.goal.schema import GoalOperationError, GoalRecord, GoalStatus
from openjiuwen.harness.schema.interaction import InteractionEventType

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import (
    JiuWenSwarm,
    _should_record_user_history,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _FakeGoals:
    def __init__(self) -> None:
        self.record: GoalRecord | None = None
        self.calls: list[tuple[str, object]] = []

    async def get(self) -> GoalRecord | None:
        self.calls.append(("get", None))
        return self.record

    def get_store(self):
        return SimpleNamespace(load=lambda: self.record)

    async def set(
        self,
        objective: str,
        *,
        overwrite_confirmed: bool = False,
        token_budget: int | None = None,
        max_attempts: int | None = None,
    ) -> GoalRecord:
        self.calls.append(("set", objective))
        if not objective.strip():
            raise GoalOperationError(
                operation="set",
                code="invalid_objective",
                message="goal objective must not be empty",
            )
        if self.record is not None and not overwrite_confirmed:
            raise GoalOperationError(
                operation="set",
                code="already_exists",
                message="a goal already exists for this session",
                goal=self.record,
            )
        self.record = GoalRecord.create(session_id="session-1", objective=objective)
        return self.record

    async def pause(self) -> GoalRecord | None:
        self.calls.append(("pause", None))
        if self.record is None:
            return None
        if self.record.status is GoalStatus.ACTIVE:
            self.record.status = GoalStatus.PAUSED
        return self.record

    async def resume(self) -> GoalRecord | None:
        self.calls.append(("resume", None))
        if self.record is None:
            return None
        if self.record.status in (GoalStatus.PAUSED, GoalStatus.BLOCKED):
            self.record.status = GoalStatus.ACTIVE
        return self.record

    async def clear(self) -> GoalRecord | None:
        self.calls.append(("clear", None))
        removed = self.record
        self.record = None
        return removed


def _adapter(goal_manager: _FakeGoals) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(goal_manager=goal_manager)
    adapter._is_session_scoped_adapter = True
    return adapter


def _chunk(chunk_type: str, payload: object) -> OutputSchema:
    return OutputSchema(type=chunk_type, index=0, payload=payload)


def _delta_chunk(text: str) -> OutputSchema:
    return _chunk("llm_output", {"content": text})


def _answer_chunk(text: str) -> OutputSchema:
    return _chunk("answer", {"output": text, "result_type": "success"})


class _FakeGoalAdapter:
    async def handle_goal_command_structured(
        self,
        params: dict[str, object],
        session_id: str,
    ) -> dict[str, object]:
        return {
            "result_type": "goal_control",
            "action": params.get("action", "get"),
            "goal": None,
            "output": "No goal in this session.",
        }


class _FakeSessionManager:
    def get_session_id(self, session_id: str | None) -> str:
        return session_id or "default"


@pytest.mark.asyncio
async def test_structured_set_calls_goal_capability_and_returns_stream_intent() -> None:
    goals = _FakeGoals()
    result = await _adapter(goals).handle_goal_command_structured(
        {"action": "set", "objective": "ship the feature"},
        session_id="session-1",
    )

    assert goals.calls == [("set", "ship the feature")]
    assert result is not None
    assert result["result_type"] == "goal_stream"
    assert result["goal"]["objective"] == "ship the feature"


@pytest.mark.asyncio
async def test_resume_without_goal_is_a_normal_control_response() -> None:
    result = await _adapter(_FakeGoals()).handle_goal_command_structured(
        {"action": "resume"},
        session_id="session-1",
    )

    assert result is not None
    assert result["result_type"] == "goal_error"
    assert result["error_code"] == "no_goal"
    assert result["goal"] is None
    assert "cannot resume" in str(result["error"])


@pytest.mark.asyncio
async def test_pause_without_goal_returns_no_goal_error() -> None:
    result = await _adapter(_FakeGoals()).handle_goal_command_structured(
        {"action": "pause"},
        session_id="session-1",
    )
    assert result is not None
    assert result["result_type"] == "goal_error"
    assert result["error_code"] == "no_goal"


@pytest.mark.asyncio
async def test_clear_without_goal_returns_no_goal_error() -> None:
    result = await _adapter(_FakeGoals()).handle_goal_command_structured(
        {"action": "clear"},
        session_id="session-1",
    )
    assert result is not None
    assert result["result_type"] == "goal_error"
    assert result["error_code"] == "no_goal"


@pytest.mark.asyncio
async def test_resume_completed_goal_returns_invalid_state() -> None:
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="done")
    goals.record.status = GoalStatus.COMPLETED
    result = await _adapter(goals).handle_goal_command_structured(
        {"action": "resume"},
        session_id="session-1",
    )
    assert result is not None
    assert result["result_type"] == "goal_error"
    assert result["error_code"] == "invalid_state"
    assert result["goal"]["status"] == "completed"


@pytest.mark.asyncio
async def test_pause_non_active_goal_returns_invalid_state() -> None:
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="paused")
    goals.record.status = GoalStatus.PAUSED
    result = await _adapter(goals).handle_goal_command_structured(
        {"action": "pause"},
        session_id="session-1",
    )
    assert result is not None
    assert result["result_type"] == "goal_error"
    assert result["error_code"] == "invalid_state"


@pytest.mark.asyncio
async def test_set_existing_goal_returns_confirmation_data() -> None:
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="old goal")

    result = await _adapter(goals).handle_goal_command_structured(
        {"action": "set", "objective": "new goal"},
        session_id="session-1",
    )

    assert result is not None
    assert result["result_type"] == "goal_confirm_required"
    assert result["existing_goal"]["objective"] == "old goal"
    assert result["requested_objective"] == "new goal"


@pytest.mark.asyncio
async def test_slash_goal_parser_uses_set_argument_or_bare_objective() -> None:
    goals = _FakeGoals()
    adapter = _adapter(goals)

    explicit = await adapter._handle_goal_slash_command("/goal set write a report")
    bare = await adapter._handle_goal_slash_command("/goal write a report")
    empty = await adapter._handle_goal_slash_command("/goal set")

    assert explicit is not None
    assert explicit["goal"]["objective"] == "write a report"
    assert bare is not None
    assert bare["result_type"] == "goal_confirm_required"
    assert empty is not None
    assert empty["result_type"] == "goal_error"
    assert empty["error_code"] == "invalid_objective"


def test_runtime_events_are_adapted_without_leaking_runtime_objects() -> None:
    goal = GoalRecord.create(session_id="session-1", objective="ship the feature").to_dict()
    updated = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        OutputSchema(
            type=InteractionEventType.GOAL_UPDATED.value,
            index=0,
            payload={"goal": goal},
        )
    )
    failed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        OutputSchema(
            type=InteractionEventType.EXECUTION_ERROR.value,
            index=0,
            payload={"code": "round_execution_error", "message": "round failed"},
        )
    )

    assert updated == {"event_type": "goal.updated", "goal": goal}
    assert failed == {
        "event_type": "execution.error",
        "code": "round_execution_error",
        "message": "round failed",
        "goal": None,
    }


def test_empty_answer_chunk_is_dropped_not_emitted_as_chat_final() -> None:
    """Empty answer is not a UI closer — discard for both Goal and normal chat."""
    assert (
        JiuWenSwarmDeepAdapter._parse_stream_chunk(
            {
                "type": "answer",
                "payload": {"output": {"output": "", "chunked": False}},
            }
        )
        is None
    )
    assert (
        JiuWenSwarmDeepAdapter._parse_stream_chunk(
            {
                "type": "answer",
                "payload": {"output": {"output": "   ", "chunked": False}},
            },
            _has_streamed_content=True,
        )
        is None
    )


def test_runtime_goal_update_payload_is_always_nested_under_goal() -> None:
    goal = GoalRecord.create(session_id="session-1", objective="ship the feature").to_dict()
    updated = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        {
            "type": InteractionEventType.GOAL_UPDATED.value,
            "payload": goal,
        }
    )
    cleared = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        {
            "type": InteractionEventType.GOAL_UPDATED.value,
            "payload": {"goal": None},
        }
    )

    assert updated == {"event_type": "goal.updated", "goal": goal}
    assert cleared == {"event_type": "goal.updated", "goal": None}


@pytest.mark.asyncio
async def test_command_goal_unary_response_stays_rpc_payload() -> None:
    facade = JiuWenSwarm.__new__(JiuWenSwarm)
    facade._adapter = _FakeGoalAdapter()
    facade._session_manager = _FakeSessionManager()

    response = await facade.process_message(
        AgentRequest(
            request_id="goal-get-1",
            channel_id="tui",
            session_id="session-1",
            req_method=ReqMethod.COMMAND_GOAL,
            params={"action": "get"},
        )
    )

    assert response.ok is True
    assert response.payload is not None
    assert "event_type" not in response.payload
    assert response.payload["record"] is None
    assert response.payload["message"] == "No goal in this session."


def test_active_goal_demotes_goal_round_chat_final_to_delta() -> None:
    """Goal multi-attempt middle: demote final so the frontend does not split."""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")
    adapter._instance.interaction_started = True
    adapter._stream_content_run_kind = "goal"

    payload = adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "attempt output"}
    )

    assert payload == {
        "event_type": "chat.delta",
        "content": "attempt output",
        "goal_intermediate": True,
    }


def test_demoted_goal_final_is_dropped_when_text_was_already_streamed() -> None:
    """降级后的 final 会被前端追加：已流式过的正文必须丢掉，否则气泡/历史各留两份。

    普通轮的真 ``chat.final`` 是覆盖气泡正文，所以逐 token 流式 + final 全文不会
    重复；Goal 轮被降级成 ``chat.delta`` 后是追加，正文就会出现两遍。
    """
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")
    adapter._instance.interaction_started = True
    adapter._stream_content_run_kind = "goal"

    adapter._note_round_visible_text("测试全部")
    adapter._note_round_visible_text("通过了。")

    assert (
        adapter._adapt_goal_intermediate_final(
            {"event_type": "chat.final", "content": "测试全部通过了。"}
        )
        is None
    )
    # answer 会重新排版流式过的 token：换行/空白差异不算新内容。
    assert (
        adapter._adapt_goal_intermediate_final(
            {"event_type": "chat.final", "content": " 测试全部\n通过了。 "}
        )
        is None
    )


def test_demoted_goal_final_keeps_text_that_was_never_streamed() -> None:
    """attempt 只有 answer、没有流式正文时，降级的 final 仍要下发，别丢正文。"""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")
    adapter._instance.interaction_started = True
    adapter._stream_content_run_kind = "goal"

    adapter._note_round_visible_text("正在跑测试……")

    assert adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "本轮小结：测试通过。"}
    ) == {
        "event_type": "chat.delta",
        "content": "本轮小结：测试通过。",
        "goal_intermediate": True,
    }


def test_round_boundary_clears_streamed_text_memo() -> None:
    """上一轮流过的正文不能影响下一轮 attempt 的去重判断。"""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.interaction_started = True
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")

    adapter._track_round_output_boundary(_delta_chunk("attempt 1 输出"))
    adapter._note_round_visible_text("attempt 1 输出")
    adapter._track_round_output_boundary(_answer_chunk("attempt 1 输出"))

    adapter._track_round_output_boundary(_delta_chunk("attempt 2 输出"))
    assert adapter._stream_round_visible_text == ""
    adapter._note_round_visible_text("attempt 2 输出")
    assert (
        adapter._adapt_goal_intermediate_final(
            {"event_type": "chat.final", "content": "attempt 2 输出"}
        )
        is None
    )
    assert adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "attempt 1 输出"}
    ) == {
        "event_type": "chat.delta",
        "content": "attempt 1 输出",
        "goal_intermediate": True,
    }


def test_active_goal_keeps_user_round_chat_final_terminal() -> None:
    """User answer finishes while Goal is still ACTIVE: keep real chat.final."""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.active_round = SimpleNamespace(run_kind="user")
    adapter._instance.interaction_started = True
    adapter._stream_content_run_kind = "user"

    payload = adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "ordinary answer"}
    )

    assert payload == {
        "event_type": "chat.final",
        "content": "ordinary answer",
    }


def test_late_user_final_stays_terminal_after_goal_round_started() -> None:
    """Provenance beats live active_round when user final arrives late."""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")
    adapter._instance.interaction_started = True
    adapter._stream_content_run_kind = "user"

    payload = adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "ordinary answer"}
    )

    assert payload == {
        "event_type": "chat.final",
        "content": "ordinary answer",
    }


def test_user_to_goal_content_injects_bubble_split_final() -> None:
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")
    adapter._instance.interaction_started = True
    adapter._stream_content_run_kind = "user"

    boundary = adapter._begin_visible_chat_content()

    assert boundary == {"event_type": "chat.final", "content": ""}
    assert adapter._stream_content_run_kind == "goal"


def test_round_kind_latch_keeps_user_tail_when_goal_round_already_started() -> None:
    """Queued user tail must stay user output, so its own final closes bubble A.

    Output is drained from a queue: the scheduler can start the goal round while
    the user round's last deltas and its ``answer`` are still queued. Sampling
    ``active_round`` per chunk used to stamp that tail as goal output, which
    demoted the user round's terminal ``chat.final`` and merged the ordinary
    answer and the goal answer into one bubble.
    """
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.interaction_started = True
    adapter._instance.active_round = SimpleNamespace(run_kind="user")

    adapter._track_round_output_boundary(_delta_chunk("hello"))
    assert adapter._begin_visible_chat_content(True) is None
    assert adapter._stream_content_run_kind == "user"

    # Goal round starts at the producer; the user tail is still queued here.
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")
    adapter._track_round_output_boundary(_delta_chunk(" world"))
    assert adapter._begin_visible_chat_content(True) is None

    adapter._track_round_output_boundary(_answer_chunk("hello world"))
    assert adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "hello world"}
    ) == {"event_type": "chat.final", "content": "hello world"}
    # The forwarded terminal final closes the segment (stream loop behavior).
    adapter._stream_content_run_kind = None

    # Next round re-samples: goal output opens its own bubble and its
    # attempt-boundary final stays intermediate.
    adapter._track_round_output_boundary(_delta_chunk("goal step"))
    assert adapter._begin_visible_chat_content(True) == {
        "event_type": "chat.final",
        "content": "",
    }
    assert adapter._stream_content_run_kind == "goal"
    assert adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "goal step"}
    ) == {
        "event_type": "chat.delta",
        "content": "goal step",
        "goal_intermediate": True,
    }


def test_round_kind_latch_keeps_goal_attempts_in_one_bubble() -> None:
    """Attempt boundaries must not inject a split final between goal attempts."""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.interaction_started = True
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")

    adapter._track_round_output_boundary(_delta_chunk("attempt 1"))
    adapter._begin_visible_chat_content(True)
    adapter._track_round_output_boundary(_answer_chunk("attempt 1"))

    adapter._track_round_output_boundary(_delta_chunk("attempt 2"))
    assert adapter._begin_visible_chat_content(True) is None
    assert adapter._stream_content_run_kind == "goal"


def test_only_round_result_chunks_end_the_round_kind_latch() -> None:
    assert JiuWenSwarmDeepAdapter._is_round_terminal_chunk(_answer_chunk("done")) is True
    assert JiuWenSwarmDeepAdapter._is_round_terminal_chunk(_delta_chunk("x")) is False
    assert JiuWenSwarmDeepAdapter._is_round_terminal_chunk(_chunk("tool_call", {})) is False
    assert JiuWenSwarmDeepAdapter._is_round_terminal_chunk({"type": "answer"}) is True
    assert JiuWenSwarmDeepAdapter._is_round_terminal_chunk({"output": "x"}) is False


def test_active_goal_keeps_chat_final_when_no_goal_round_in_flight() -> None:
    """Between user end and next Goal start, do not demote (no goal round)."""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    adapter = _adapter(goals)
    adapter._instance.active_round = None
    adapter._instance.interaction_started = True

    payload = adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "ordinary answer"}
    )

    assert payload == {
        "event_type": "chat.final",
        "content": "ordinary answer",
    }


def test_interrupt_resume_dispatch_injects_even_without_output_lease() -> None:
    """Permission answers must send_input when Goal already holds the lease."""
    assert JiuWenSwarmDeepAdapter._is_interrupt_resume_dispatch(
        {
            "source": "permission_interrupt",
            "request_id": "call_1",
            "answers": [{"selected_options": ["allow_once"]}],
        }
    )
    adapter = _adapter(_FakeGoals())
    assert adapter._should_inject_into_existing_interaction(
        {
            "source": "permission_interrupt",
            "request_id": "call_1",
            "answers": [{"selected_options": ["allow_once"]}],
        }
    )
    assert not adapter._should_inject_into_existing_interaction(
        {"query": "hello", "mode": "agent"}
    )


def test_paused_goal_keeps_chat_final_terminal_even_if_round_still_unwinding() -> None:
    """User cancel pauses GoalRecord first; finals during abort must stay final."""
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    goals.record.status = GoalStatus.PAUSED
    adapter = _adapter(goals)
    # Simulate an in-flight goal round still unwinding after pause+cancel.
    adapter._instance.active_round = SimpleNamespace(run_kind="goal")
    adapter._instance.interaction_started = True

    payload = adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "cancelled output"}
    )

    assert payload == {
        "event_type": "chat.final",
        "content": "cancelled output",
    }
    assert adapter._has_active_goal_interaction() is True
    assert adapter._goal_record_is_active() is False


def test_internal_goal_resume_is_not_recorded_as_user_history() -> None:
    assert _should_record_user_history(
        {
            "query": "/goal resume",
            "runtime_mode": "follow_up",
            "log_as_user": False,
        }
    ) is False
    assert _should_record_user_history({"query": "hello"}) is True


def test_completed_goal_keeps_chat_final_terminal() -> None:
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship the feature")
    goals.record.status = GoalStatus.COMPLETED
    adapter = _adapter(goals)

    payload = adapter._adapt_goal_intermediate_final(
        {"event_type": "chat.final", "content": "final output"}
    )

    assert payload == {"event_type": "chat.final", "content": "final output"}


def test_structured_command_goal_set_maps_to_pending_op() -> None:
    req = AgentRequest(
        request_id="g1",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.COMMAND_GOAL,
        params={
            "action": "set",
            "objective": "ship it",
            "overwrite_confirmed": True,
        },
    )
    op = JiuWenSwarmDeepAdapter._structured_goal_op_from_request(req)
    assert op == {
        "action": "set",
        "objective": "ship it",
        "overwrite_confirmed": True,
    }


def test_chat_send_does_not_map_to_structured_goal_op() -> None:
    req = AgentRequest(
        request_id="c1",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "/goal set do not parse on web", "mode": "agent"},
    )
    assert JiuWenSwarmDeepAdapter._structured_goal_op_from_request(req) is None


def test_readonly_goal_get_skips_metadata_sync_flag() -> None:
    """command.goal get is read-only; set/resume/pause/clear are not."""
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    get_req = AgentRequest(
        request_id="g1",
        channel_id="web",
        session_id="ghost_goal_get",
        req_method=ReqMethod.COMMAND_GOAL,
        params={"action": "get", "mode": "agent"},
    )
    assert AgentWebSocketServer._is_readonly_goal_get_request(get_req) is True
    # Default action is get when omitted.
    assert AgentWebSocketServer._is_readonly_goal_get_request(
        AgentRequest(
            request_id="g2",
            channel_id="web",
            session_id="ghost_goal_get",
            req_method=ReqMethod.COMMAND_GOAL,
            params={},
        )
    ) is True
    for action in ("set", "resume", "pause", "clear"):
        assert AgentWebSocketServer._is_readonly_goal_get_request(
            AgentRequest(
                request_id=f"a-{action}",
                channel_id="web",
                session_id="s1",
                req_method=ReqMethod.COMMAND_GOAL,
                params={"action": action},
            )
        ) is False


def test_web_channel_does_not_treat_goal_slash_as_intent() -> None:
    assert (
        JiuWenSwarmDeepAdapter._parse_goal_slash_intent("/goal set write a report")
        == {"action": "set", "objective": "write a report"}
    )
    # Parsing exists, but stream path only applies it when channel_id == "tui".
    # Web must use command.goal; this documents the contract for callers.
    req = AgentRequest(
        request_id="w1",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "/goal set write a report"},
    )
    assert JiuWenSwarmDeepAdapter._structured_goal_op_from_request(req) is None


def test_stream_end_synthesizes_final_after_goal_cleared() -> None:
    """pause→clear ends the iterator without a final; host must synthesize one."""
    goals = _FakeGoals()
    adapter = _adapter(goals)
    goals.record = None

    assert adapter._should_emit_stream_end_chat_final(
        had_assistant_output=True,
        emitted_terminal_chat_final=False,
    )


def test_stream_end_synthesizes_final_even_before_assistant_tokens() -> None:
    """Goal.set may be processing before the first delta; clear still needs a final."""
    goals = _FakeGoals()
    adapter = _adapter(goals)
    goals.record = None

    assert adapter._should_emit_stream_end_chat_final(
        had_assistant_output=False,
        emitted_terminal_chat_final=False,
    )


def test_stream_end_skips_final_when_goal_still_active() -> None:
    goals = _FakeGoals()
    goals.record = GoalRecord.create(session_id="session-1", objective="ship")
    adapter = _adapter(goals)

    assert not adapter._should_emit_stream_end_chat_final(
        had_assistant_output=True,
        emitted_terminal_chat_final=False,
    )


def test_stream_end_skips_final_when_already_emitted() -> None:
    goals = _FakeGoals()
    adapter = _adapter(goals)
    goals.record = None

    assert not adapter._should_emit_stream_end_chat_final(
        had_assistant_output=True,
        emitted_terminal_chat_final=True,
    )


def test_record_goal_set_history_writes_objective_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import Counter

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    captured: list[dict] = []
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.append_history_record",
        lambda **kwargs: captured.append(kwargs),
    )
    interface_deep._pending_goal_objective_history.clear()
    adapter = _adapter(_FakeGoals())
    adapter._active_session_ids = Counter()
    adapter._session_agent_tasks = {}
    adapter._stream_content_run_kind = None
    adapter._stream_round_kind_latch = None
    adapter._resolve_interrupt_session_id = lambda sid: sid or "default"
    adapter._current_interaction_run_kind = lambda: None

    req = AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="s1",
        req_method=ReqMethod.COMMAND_GOAL,
        params={"mode": "agent"},
    )
    # 本用例只钉 flags；defer=False 表示空闲立刻落盘（忙碌推迟另有 defer 单测）
    adapter._record_goal_set_history_if_needed(
        req,
        action="set",
        result_type="goal_stream",
        goal_payload={"goal_id": "g1", "objective": "ship it"},
        defer=False,
    )
    assert len(captured) == 1
    assert captured[0]["role"] == "user"
    assert captured[0]["content"] == "ship it"
    assert captured[0]["extra"]["is_goal_objective_message"] is True
    assert captured[0]["extra"]["goal_id"] == "g1"

    captured.clear()
    adapter._record_goal_set_history_if_needed(
        req,
        action="pause",
        result_type="ok",
        goal_payload={"goal_id": "g1", "objective": "ship it"},
        defer=False,
    )
    assert captured == []


def test_record_goal_completed_history_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.append_history_record",
        lambda **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.load_history_records",
        lambda _sid: [],
    )
    JiuWenSwarmDeepAdapter._record_goal_completed_history_if_needed(
        session_id="s1",
        channel_id="web",
        channel_metadata=None,
        mode="agent",
        goal_payload={
            "goal_id": "g1",
            "status": "completed",
            "last_assessment": {"evidence": "all done"},
        },
    )
    assert len(captured) == 1
    assert captured[0]["extra"]["is_goal_completed_message"] is True
    assert captured[0]["extra"]["id"] == "goal-completed-g1"
    assert captured[0]["content"].startswith("goal.completed:")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.load_history_records",
        lambda _sid: [
            {
                "id": "goal-completed-g1",
                "is_goal_completed_message": True,
                "goal_id": "g1",
            }
        ],
    )
    captured.clear()
    JiuWenSwarmDeepAdapter._record_goal_completed_history_if_needed(
        session_id="s1",
        channel_id="web",
        channel_metadata=None,
        mode="agent",
        goal_payload={"goal_id": "g1", "status": "completed"},
    )
    assert captured == []

    JiuWenSwarmDeepAdapter._record_goal_completed_history_if_needed(
        session_id="s1",
        channel_id="web",
        channel_metadata=None,
        mode="agent",
        goal_payload={"goal_id": "g2", "status": "active"},
    )
    assert captured == []
