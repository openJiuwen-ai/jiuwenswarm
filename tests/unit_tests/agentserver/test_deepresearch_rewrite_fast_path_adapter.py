from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage

from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_fast_path import (
    RewriteFastPathResult,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.deepresearch_rewrite_html_followup import (
    PENDING_HTML_EXPORT_STATE_KEY,
    RewriteHtmlFollowupResult,
    RewriteHtmlTarget,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


def _query() -> str:
    payload = {
        "report_path": "/workspace/report.md",
        "action": "polish",
        "selection": {
            "protocol_version": 2,
            "start_byte": 0,
            "end_byte": 9,
            "selected_text": "原句。",
            "source_sha256": "0" * 64,
        },
        "instruction": "",
    }
    return (
        "<deepresearch_rewrite_request>"
        f"{json.dumps(payload, ensure_ascii=False)}"
        "</deepresearch_rewrite_request>"
    )


def _result(**overrides) -> RewriteFastPathResult:
    values = {
        "recognized": True,
        "status": "completed",
        "action": "polish",
        "error_code": None,
        "message": (
            "本轮改写已完成。若报告已是最终版本，请回复‘生成 HTML’；"
            "如需继续改写，可直接选择下一处内容。"
        ),
        "usage_metadata": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
        "prepare_ms": 1.0,
        "model_ms": 20.0,
        "commit_ms": 2.0,
        "total_ms": 23.0,
        "model_calls": 1,
        "model_output_adjustments": (),
        "model_output_error_reason": None,
        "commit_result": {
            "status": "completed",
            "report_delivered": True,
            "report_path": "/workspace/report.rewrite.md",
            "revision_id": "rev_child",
        },
    }
    values.update(overrides)
    return RewriteFastPathResult(**values)


class _FakeContext:
    def __init__(self):
        self.messages = []
        self.pop_calls = []

    async def add_messages(self, messages):
        self.messages.extend(messages)

    def get_messages(self):
        return list(self.messages)

    def pop_messages(self, count, *, with_history):
        self.pop_calls.append((count, with_history))
        del self.messages[-count:]


class _FakeInteractionStream:
    def __init__(self, chunks=()):
        self._chunks = list(chunks)
        self.close = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _adapter_with_active_session(
    *,
    session_id: str = "session-1",
    state=None,
    save_side_effect=None,
    save_updates_state: bool = True,
    commit_side_effect=None,
):
    context = _FakeContext()
    state_store = {}
    if state is not None:
        state_store[PENDING_HTML_EXPORT_STATE_KEY] = copy.deepcopy(state)

    def get_state(key):
        return copy.deepcopy(state_store.get(key))

    def update_state(values):
        state_store.update(copy.deepcopy(values))

    save_effects = (
        iter(save_side_effect)
        if isinstance(save_side_effect, list)
        else None
    )

    async def save_contexts(active_session):
        effect = next(save_effects) if save_effects is not None else save_side_effect
        if isinstance(effect, BaseException):
            raise effect
        if callable(effect):
            outcome = effect(active_session)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        else:
            outcome = effect
        states = (
            outcome
            if outcome is not None
            else {"rewrite": {"message_count": len(context.messages)}}
        )
        if save_updates_state:
            active_session.update_state({"context": states})
        return states

    context_engine = SimpleNamespace(save_contexts=AsyncMock(side_effect=save_contexts))
    react_agent = SimpleNamespace(
        _init_context=AsyncMock(return_value=context),
        context_engine=context_engine,
    )
    session = SimpleNamespace(
        get_session_id=Mock(return_value=session_id),
        get_state=Mock(side_effect=get_state),
        update_state=Mock(side_effect=update_state),
        commit=AsyncMock(side_effect=commit_side_effect),
        state_store=state_store,
    )
    interaction_send_lock = asyncio.Lock()
    interaction_control_lock = asyncio.Lock()
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(
        _interaction_session=session,
        react_agent=react_agent,
        _interaction_send_lock=interaction_send_lock,
        _interaction_control_lock=interaction_control_lock,
        _should_keep_interaction_open_locked=Mock(return_value=False),
    )
    adapter._deepresearch_rewrite_tx_uncertain = False
    return adapter, session, context, react_agent, context_engine


@pytest.mark.asyncio
async def test_html_target_comes_only_from_exact_active_interaction_session():
    target_state = {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    adapter, session, *_ = _adapter_with_active_session(state=target_state)

    target = await adapter._load_deepresearch_rewrite_html_target("session-1")

    assert target == RewriteHtmlTarget(
        report_path="/workspace/report.rewrite.md",
        revision_id="rev_child",
    )
    session.get_state.assert_called_once_with(PENDING_HTML_EXPORT_STATE_KEY)


@pytest.mark.asyncio
async def test_html_target_rejects_mismatched_active_session_id():
    target_state = {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    adapter, session, *_ = _adapter_with_active_session(
        session_id="other-session",
        state=target_state,
    )

    target = await adapter._load_deepresearch_rewrite_html_target("session-1")

    assert target is None
    session.get_state.assert_not_called()


@pytest.mark.asyncio
async def test_persist_fast_path_turn_uses_active_session_and_commits_four_messages():
    adapter, session, context, react_agent, context_engine = (
        _adapter_with_active_session()
    )
    result = _result()

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=result,
    )

    assert persisted is True
    assert [type(message) for message in context.messages] == [
        UserMessage,
        AssistantMessage,
        ToolMessage,
        AssistantMessage,
    ]
    tool_call = context.messages[1].tool_calls[0]
    assert tool_call.name == "deepresearch_commit_rewrite"
    assert tool_call.id.startswith("rewrite-fast-path-")
    assert context.messages[2].tool_call_id == tool_call.id
    assert json.loads(context.messages[2].content)["revision_id"] == "rev_child"
    assert context.messages[3].content == result.message
    react_agent._init_context.assert_awaited_once_with(session)
    assert call(
        {
            PENDING_HTML_EXPORT_STATE_KEY: {
                "schema_version": 1,
                "report_path": "/workspace/report.rewrite.md",
                "revision_id": "rev_child",
            }
        }
    ) in session.update_state.call_args_list
    assert session.get_state("context") == {"rewrite": {"message_count": 4}}
    context_engine.save_contexts.assert_awaited_once_with(session)
    session.commit.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["memory", "commit"])
async def test_persist_fast_path_turn_rolls_back_best_effort_on_failure(failure_at):
    error = RuntimeError(failure_at)
    adapter, session, context, _react_agent, context_engine = (
        _adapter_with_active_session(
            state={"schema_version": 0},
            save_side_effect=error if failure_at == "memory" else None,
            commit_side_effect=error if failure_at == "commit" else None,
        )
    )

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert context.messages == []
    assert context.pop_calls == [(4, True)]
    assert call(
        {PENDING_HTML_EXPORT_STATE_KEY: {"schema_version": 0}}
    ) in session.update_state.call_args_list
    assert adapter._deepresearch_rewrite_tx_uncertain is True
    if failure_at == "memory":
        session.commit.assert_awaited_once_with()
    else:
        assert context_engine.save_contexts.await_count == 2
        assert context_engine.save_contexts.await_args_list[-1].args == (session,)
        assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_commit_failure_restores_and_commits_the_previous_durable_snapshot():
    snapshots = []
    adapter, session, context, _react_agent, context_engine = (
        _adapter_with_active_session(
            state={"schema_version": 0, "revision_id": "old"},
            commit_side_effect=[RuntimeError("new snapshot failed"), None],
        )
    )

    async def capture_snapshot(active_session):
        states = {"rewrite": {"message_count": len(context.messages)}}
        active_session.update_state({"context": states})
        snapshots.append(
            (
                [type(message).__name__ for message in context.messages],
                copy.deepcopy(active_session.state_store),
            )
        )
        return states

    context_engine.save_contexts.side_effect = capture_snapshot

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert snapshots[-1] == (
        [],
        {
            PENDING_HTML_EXPORT_STATE_KEY: {
                "schema_version": 0,
                "revision_id": "old",
            },
            "context": {"rewrite": {"message_count": 0}},
        },
    )
    assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_restore_failure_marks_adapter_uncertain_and_attempts_save_and_commit():
    adapter, session, _context, _react_agent, context_engine = (
        _adapter_with_active_session(
            state={"schema_version": 0},
            save_side_effect=[None, RuntimeError("restore-save-secret")],
            commit_side_effect=[
                RuntimeError("new-commit-secret"),
                RuntimeError("restore-commit-secret"),
            ],
        )
    )

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert adapter._deepresearch_rewrite_tx_uncertain is True
    assert context_engine.save_contexts.await_count == 2
    assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_partial_add_failure_removes_only_messages_owned_by_this_turn():
    adapter, _session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    existing = UserMessage(content="existing")
    context.messages.append(existing)

    async def add_two_then_raise(messages):
        context.messages.extend(messages[:2])
        raise RuntimeError("partial-add-secret")

    context.add_messages = AsyncMock(side_effect=add_two_then_raise)

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert context.messages == [existing]
    assert context.pop_calls == [(2, True)]
    assert adapter._deepresearch_rewrite_tx_uncertain is False


@pytest.mark.asyncio
async def test_partial_add_success_return_is_detected_and_rolled_back():
    adapter, session, context, _react_agent, context_engine = (
        _adapter_with_active_session()
    )
    existing = UserMessage(content="existing")
    context.messages.append(existing)

    async def add_only_two(messages):
        context.messages.extend(messages[:2])

    context.add_messages = AsyncMock(side_effect=add_only_two)

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert context.messages == [existing]
    assert context.pop_calls == [(2, True)]
    context_engine.save_contexts.assert_awaited_once_with(session)
    session.commit.assert_awaited_once_with()
    assert adapter._deepresearch_rewrite_tx_uncertain is False


@pytest.mark.asyncio
async def test_partial_add_rollback_uses_an_independent_message_snapshot():
    adapter, _session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    existing = UserMessage(content="existing")
    context.messages.append(existing)
    context.get_messages = Mock(side_effect=lambda: context.messages)

    async def add_two_then_raise(messages):
        context.messages.extend(messages[:2])
        raise RuntimeError("partial-add-live-list-secret")

    context.add_messages = AsyncMock(side_effect=add_two_then_raise)

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert context.messages == [existing]
    assert context.pop_calls == [(2, True)]
    assert adapter._deepresearch_rewrite_tx_uncertain is False


@pytest.mark.asyncio
async def test_mutate_then_raise_state_update_restores_previous_checkpoint():
    old_target = {
        "schema_version": 1,
        "report_path": "/workspace/report-old.md",
        "revision_id": "rev_old",
    }
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session(state=old_target)
    )
    calls = 0

    def mutate_then_raise_once(values):
        nonlocal calls
        calls += 1
        session.state_store.update(copy.deepcopy(values))
        if calls == 1:
            raise RuntimeError("state-update-secret")

    session.update_state.side_effect = mutate_then_raise_once

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert context.messages == []
    assert session.state_store[PENDING_HTML_EXPORT_STATE_KEY] == old_target
    assert adapter._deepresearch_rewrite_tx_uncertain is False


@pytest.mark.asyncio
async def test_partial_state_update_success_return_is_detected_and_rolled_back():
    old_target = {
        "schema_version": 1,
        "report_path": "/workspace/report-old.md",
        "revision_id": "rev_old",
    }
    old_replay_state = {"schema_version": 1, "entries": []}
    adapter, session, context, _react_agent, context_engine = (
        _adapter_with_active_session(state=old_target)
    )
    session.state_store["deepresearch_rewrite_fast_path_replays"] = copy.deepcopy(
        old_replay_state
    )
    calls = 0

    def update_only_target_once(values):
        nonlocal calls
        calls += 1
        if calls == 1:
            session.state_store[PENDING_HTML_EXPORT_STATE_KEY] = copy.deepcopy(
                values[PENDING_HTML_EXPORT_STATE_KEY]
            )
            return
        session.state_store.update(copy.deepcopy(values))

    session.update_state.side_effect = update_only_target_once
    query = _query()

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        request_id="request-partial-state",
        query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
        query=query,
        result=_result(),
    )

    assert persisted is False
    assert context.messages == []
    assert context.pop_calls == [(4, True)]
    assert session.state_store[PENDING_HTML_EXPORT_STATE_KEY] == old_target
    assert (
        session.state_store["deepresearch_rewrite_fast_path_replays"]
        == old_replay_state
    )
    assert session.update_state.call_count == 3
    context_engine.save_contexts.assert_awaited_once_with(session)
    session.commit.assert_awaited_once_with()
    assert adapter._deepresearch_rewrite_tx_uncertain is False


@pytest.mark.asyncio
async def test_save_contexts_return_without_local_state_update_is_rejected():
    adapter, session, context, _react_agent, context_engine = (
        _adapter_with_active_session(save_updates_state=False)
    )

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        query=_query(),
        result=_result(),
    )

    assert persisted is False
    assert context.messages == []
    assert context.pop_calls == [(4, True)]
    assert context_engine.save_contexts.await_count == 2
    session.commit.assert_awaited_once_with()
    assert adapter._deepresearch_rewrite_tx_uncertain is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["memory", "commit"])
async def test_persist_fast_path_turn_rolls_back_then_propagates_cancellation(
    failure_at,
):
    cancellation = asyncio.CancelledError(f"cancel-secret-{failure_at}")
    adapter, session, context, _react_agent, context_engine = (
        _adapter_with_active_session(
            state={"schema_version": 0},
            save_side_effect=cancellation if failure_at == "memory" else None,
            commit_side_effect=cancellation if failure_at == "commit" else None,
        )
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        await adapter._persist_deepresearch_rewrite_fast_path_turn(
            session_id="session-1",
            query=_query(),
            result=_result(),
        )

    assert raised.value is cancellation
    assert context.messages == []
    assert context.pop_calls == [(4, True)]
    assert call(
        {PENDING_HTML_EXPORT_STATE_KEY: {"schema_version": 0}}
    ) in session.update_state.call_args_list
    assert adapter._deepresearch_rewrite_tx_uncertain is True
    if failure_at == "memory":
        session.commit.assert_awaited_once_with()
    else:
        assert context_engine.save_contexts.await_count == 2
        assert context_engine.save_contexts.await_args_list[-1].args == (session,)


@pytest.mark.asyncio
async def test_persist_failure_logs_only_exception_type():
    adapter, *_ = _adapter_with_active_session(
        commit_side_effect=RuntimeError("secret-commit-details"),
    )
    error_log = Mock()

    with patch.object(interface_module.logger, "error", error_log):
        persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
            session_id="session-1",
            query=_query(),
            result=_result(),
        )

    assert persisted is False
    logged = repr(error_log.call_args_list)
    assert "RuntimeError" in logged
    assert "secret-commit-details" not in logged
    assert "exc_info" not in logged


@pytest.mark.asyncio
async def test_html_followup_invokes_existing_tool_with_active_target():
    adapter, *_ = _adapter_with_active_session()
    adapter._load_deepresearch_rewrite_html_target = AsyncMock(
        return_value=RewriteHtmlTarget(
            report_path="/workspace/report.rewrite.md",
            revision_id="rev_child",
        )
    )

    with patch(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.rewrite_tools."
        "deepresearch_generate_rewrite_html._func",
        new=AsyncMock(
            return_value=json.dumps(
                {"status": "completed", "html_delivered": True}
            )
        ),
    ) as html_tool:
        result = await adapter._try_deepresearch_rewrite_html_followup(
            "生成 HTML",
            "session-1",
        )

    assert result == RewriteHtmlFollowupResult(
        status="completed",
        error_code=None,
        message="已生成美化后的 HTML。",
    )
    html_tool.assert_awaited_once_with(
        report_path="/workspace/report.rewrite.md",
        revision_id="rev_child",
    )


@pytest.mark.asyncio
async def test_html_followup_missing_target_returns_adapter_guidance():
    adapter, *_ = _adapter_with_active_session()
    adapter._load_deepresearch_rewrite_html_target = AsyncMock(return_value=None)

    result = await adapter._try_deepresearch_rewrite_html_followup(
        "生成 HTML",
        "session-1",
    )

    assert result == RewriteHtmlFollowupResult(
        status="error",
        error_code="TARGET_UNAVAILABLE",
        message=(
            "未找到可生成 HTML 的已完成改写版本。"
            "建议先继续改写并生成新的 Markdown 版本，再生成 HTML。"
        ),
    )


@pytest.mark.asyncio
async def test_adapter_fast_path_plain_text_has_no_tool_or_model_side_effects():
    adapter, *_ = _adapter_with_active_session()
    adapter._model = SimpleNamespace(invoke=AsyncMock())

    with patch(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.rewrite_tools."
        "deepresearch_prepare_rewrite._func",
        new=AsyncMock(),
    ) as prepare, patch(
        "jiuwenswarm.agents.harness.common.tools.deepresearch.rewrite_tools."
        "deepresearch_commit_rewrite._func",
        new=AsyncMock(),
    ) as commit:
        result = await adapter._try_deepresearch_rewrite_fast_path("普通消息")

    assert result is None
    prepare.assert_not_awaited()
    adapter._model.invoke.assert_not_awaited()
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_disables_thinking_for_glm_rewrite_model():
    adapter, *_ = _adapter_with_active_session()
    adapter._model = SimpleNamespace(
        model_config=SimpleNamespace(model_name="glm-5.2"),
        invoke=AsyncMock(),
    )

    with patch.object(
        interface_module,
        "run_rewrite_fast_path",
        new=AsyncMock(return_value=_result()),
    ) as run_fast_path:
        await adapter._try_deepresearch_rewrite_fast_path(_query())

    assert run_fast_path.await_args.kwargs["model_call_kwargs"] == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }


@pytest.mark.asyncio
async def test_rewrite_transaction_holds_core_send_then_control_locks_through_persist():
    adapter, *_ = _adapter_with_active_session()
    result = _result()

    async def run_tool(_query):
        assert adapter._instance._interaction_send_lock.locked()
        assert adapter._instance._interaction_control_lock.locked()
        return result

    async def persist(**_kwargs):
        assert adapter._instance._interaction_send_lock.locked()
        assert adapter._instance._interaction_control_lock.locked()
        return True

    adapter._try_deepresearch_rewrite_fast_path = run_tool
    adapter._persist_deepresearch_rewrite_fast_path_turn = persist

    outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(),
        session_id="session-1",
        request_id="request-locks",
    )

    assert outcome is result
    assert not adapter._instance._interaction_send_lock.locked()
    assert not adapter._instance._interaction_control_lock.locked()


@pytest.mark.asyncio
async def test_html_transaction_holds_core_send_then_control_locks_through_tool():
    adapter, *_ = _adapter_with_active_session()
    expected = RewriteHtmlFollowupResult(
        status="completed",
        error_code=None,
        message="html done",
    )

    async def run_html(_query, _session_id):
        assert adapter._instance._interaction_send_lock.locked()
        assert adapter._instance._interaction_control_lock.locked()
        return expected

    adapter._try_deepresearch_rewrite_html_followup = run_html

    outcome = await adapter._run_deepresearch_rewrite_html_transaction(
        "生成 HTML",
        session_id="session-1",
    )

    assert outcome is expected


@pytest.mark.asyncio
async def test_core_send_waits_until_rewrite_transaction_releases_both_locks():
    adapter, *_ = _adapter_with_active_session()
    tool_entered = asyncio.Event()
    allow_tool = asyncio.Event()
    send_entered = asyncio.Event()

    async def run_tool(_query):
        tool_entered.set()
        await allow_tool.wait()
        return None

    async def normal_send():
        async with adapter._instance._interaction_send_lock:
            async with adapter._instance._interaction_control_lock:
                send_entered.set()

    adapter._try_deepresearch_rewrite_fast_path = run_tool
    rewrite_task = asyncio.create_task(
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id="request-linearized"
        )
    )
    await tool_entered.wait()
    send_task = asyncio.create_task(normal_send())
    await asyncio.sleep(0)
    assert not send_entered.is_set()

    allow_tool.set()
    await rewrite_task
    await send_task

    assert send_entered.is_set()


@pytest.mark.asyncio
async def test_concurrent_rewrites_are_serialized_and_failed_a_cannot_pop_successful_b():
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session(
            state={"schema_version": 0, "revision_id": "old"},
            commit_side_effect=[RuntimeError("A failed"), None, None],
        )
    )
    result_a = _result(
        message="A",
        commit_result={
            "status": "completed",
            "report_delivered": True,
            "report_path": "/workspace/report-a.md",
            "revision_id": "rev_a",
        },
    )
    result_b = _result(
        message="B",
        commit_result={
            "status": "completed",
            "report_delivered": True,
            "report_path": "/workspace/report-b.md",
            "revision_id": "rev_b",
        },
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(
        side_effect=[result_a, result_b]
    )

    outcome_a, outcome_b = await asyncio.gather(
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id="request-a"
        ),
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id="request-b"
        ),
    )

    assert outcome_a.error_code == "CONTEXT_PERSIST_FAILED"
    assert outcome_b is result_b
    assert len(context.messages) == 4
    assert context.messages[-1].content == "B"
    assert session.state_store[PENDING_HTML_EXPORT_STATE_KEY] == {
        "schema_version": 1,
        "report_path": "/workspace/report-b.md",
        "revision_id": "rev_b",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_consumer_cancel_after_commit_tool_waits_for_durable_checkpoint(
    cancel_count,
):
    save_entered = asyncio.Event()
    allow_save = asyncio.Event()

    async def blocking_save(_session):
        save_entered.set()
        await allow_save.wait()

    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session(save_side_effect=blocking_save)
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())
    task = asyncio.create_task(
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(),
            session_id="session-1",
            request_id="request-cancel",
        )
    )
    await save_entered.wait()

    for _ in range(cancel_count):
        task.cancel()
        await asyncio.sleep(0)
    allow_save.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(context.messages) == 4
    assert context.pop_calls == []
    assert session.state_store[PENDING_HTML_EXPORT_STATE_KEY] == {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    session.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_consumer_cancel_seen_after_checkpoint_task_finished_still_propagates():
    cancellation = asyncio.CancelledError("consumer-cancel")
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())
    real_shield = asyncio.shield
    shield_calls = 0

    async def finish_then_cancel(persistence_task):
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls == 1:
            return await real_shield(persistence_task)
        await persistence_task
        raise cancellation

    with patch.object(interface_module.asyncio, "shield", side_effect=finish_then_cancel):
        with pytest.raises(asyncio.CancelledError) as raised:
            await adapter._run_deepresearch_rewrite_fast_path_transaction(
                _query(), session_id="session-1", request_id="request-finished-cancel"
            )

    assert raised.value is cancellation
    assert len(context.messages) == 4
    session.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_consumer_cancel_before_child_publish_cancels_tool_without_checkpoint():
    tool_entered = asyncio.Event()
    tool_cancelled = asyncio.Event()

    async def blocked_before_publish(_query):
        tool_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            tool_cancelled.set()

    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = blocked_before_publish
    task = asyncio.create_task(
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id="request-pre-publish"
        )
    )
    await tool_entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tool_cancelled.is_set()
    assert context.messages == []
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_consumer_cancel_after_child_publish_drains_delivery_and_checkpoint(
    cancel_count,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch import rewrite_tools

    published = asyncio.Event()
    allow_delivery = asyncio.Event()
    delivery_count = 0

    async def publish_then_deliver(_query):
        nonlocal delivery_count
        rewrite_tools._notify_rewrite_published(
            {
                "report_path": "/workspace/report.rewrite.md",
                "revision_id": "rev_child",
            }
        )
        published.set()
        delivery_count += 1
        await allow_delivery.wait()
        return _result()

    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = publish_then_deliver
    task = asyncio.create_task(
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id="request-post-publish"
        )
    )
    await published.wait()

    for _ in range(cancel_count):
        task.cancel()
        await asyncio.sleep(0)
    allow_delivery.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert delivery_count == 1
    assert len(context.messages) == 4
    assert session.state_store[PENDING_HTML_EXPORT_STATE_KEY] == {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    assert session.state_store["deepresearch_rewrite_fast_path_replays"]["entries"][
        0
    ]["request_id"] == "request-post-publish"
    session.commit.assert_awaited_once_with()
    assert adapter._deepresearch_rewrite_tx_uncertain is False


@pytest.mark.asyncio
async def test_published_child_with_invalid_terminal_persists_safe_replay():
    from jiuwenswarm.agents.harness.common.tools.deepresearch import rewrite_tools

    async def publish_then_return_invalid(_query):
        rewrite_tools._notify_rewrite_published(
            {
                "report_path": "/workspace/report.rewrite.md",
                "revision_id": "rev_child",
            }
        )
        return _result(status="error", error_code="WRITE_FAILED", commit_result=None)

    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = publish_then_return_invalid

    outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-invalid-published"
    )

    assert outcome.status == "error"
    assert outcome.error_code == "PUBLISH_STATE_UNCERTAIN"
    assert adapter._deepresearch_rewrite_tx_uncertain is False
    assert len(context.messages) == 4
    assert session.state_store[PENDING_HTML_EXPORT_STATE_KEY] == {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    replay_state = session.state_store["deepresearch_rewrite_fast_path_replays"]
    assert replay_state["entries"][0]["terminal_kind"] == "publish_uncertain"
    session.commit.assert_awaited_once_with()

    adapter._try_deepresearch_rewrite_fast_path = AsyncMock()
    replay = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-invalid-published"
    )

    assert replay.status == "error"
    assert replay.error_code == "PUBLISH_STATE_UNCERTAIN"
    adapter._try_deepresearch_rewrite_fast_path.assert_not_awaited()
    assert len(context.messages) == 4


@pytest.mark.asyncio
async def test_consumer_cancel_after_publish_preserves_cancel_when_runner_raises(
    caplog,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch import rewrite_tools

    published = asyncio.Event()
    allow_failure = asyncio.Event()

    async def publish_then_raise(_query):
        rewrite_tools._notify_rewrite_published(
            {
                "report_path": "/workspace/report.rewrite.md",
                "revision_id": "rev_child",
            }
        )
        published.set()
        await allow_failure.wait()
        raise RuntimeError("post-publish-runner-secret")

    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = publish_then_raise
    task = asyncio.create_task(
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id="request-publish-error"
        )
    )
    await published.wait()

    task.cancel("original-consumer-cancel")
    allow_failure.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert raised.value.args == ("original-consumer-cancel",)
    assert len(context.messages) == 4
    assert session.state_store["deepresearch_rewrite_fast_path_replays"]["entries"][
        0
    ]["terminal_kind"] == "publish_uncertain"
    assert "post-publish-runner-secret" not in caplog.text


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_core_lock_has_no_side_effects():
    adapter, *_ = _adapter_with_active_session()
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())
    await adapter._instance._interaction_send_lock.acquire()
    task = asyncio.create_task(
        adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(),
            session_id="session-1",
            request_id="request-wait-cancel",
        )
    )
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    adapter._instance._interaction_send_lock.release()

    adapter._try_deepresearch_rewrite_fast_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_replayed_request_id_returns_durable_terminal_without_second_tool_turn():
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    result = _result()
    result.commit_result["delivery_status"] = "delivered"
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=result)

    first = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-replay"
    )
    second = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-replay"
    )

    assert first == result
    assert second.status == "completed"
    assert second.message == result.message
    assert second.commit_result == {
        "status": "completed",
        "report_delivered": True,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    assert second.usage_metadata is None
    adapter._try_deepresearch_rewrite_fast_path.assert_awaited_once_with(_query())
    assert len(context.messages) == 4
    session.commit.assert_awaited_once_with()
    replay_state = session.state_store["deepresearch_rewrite_fast_path_replays"]
    assert replay_state["schema_version"] == 1
    assert replay_state["entries"][0]["request_id"] == "request-replay"
    assert replay_state["entries"][0]["target"] == {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }


@pytest.mark.asyncio
async def test_replay_state_is_bounded_and_request_ids_are_strict():
    adapter, session, _context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())

    for index in range(35):
        outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(),
            session_id="session-1",
            request_id=f"request-{index}",
        )
        assert outcome is not None

    entries = session.state_store["deepresearch_rewrite_fast_path_replays"][
        "entries"
    ]
    assert len(entries) == 32
    assert entries[0]["request_id"] == "request-3"
    assert entries[-1]["request_id"] == "request-34"
    calls_before_invalid = adapter._try_deepresearch_rewrite_fast_path.await_count
    assert (
        await adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id=""
        )
        is None
    )
    assert (
        await adapter._run_deepresearch_rewrite_fast_path_transaction(
            _query(), session_id="session-1", request_id="x" * 257
        )
        is None
    )
    assert adapter._try_deepresearch_rewrite_fast_path.await_count == calls_before_invalid


@pytest.mark.asyncio
async def test_replay_request_id_with_different_query_fails_closed_without_tool():
    adapter, _session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())
    first = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-bound-query"
    )

    replay = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query().replace('"instruction": ""', '"instruction": "different"'),
        session_id="session-1",
        request_id="request-bound-query",
    )

    assert first is not None
    assert replay.status == "error"
    assert replay.error_code == "REPLAY_CONFLICT"
    adapter._try_deepresearch_rewrite_fast_path.assert_awaited_once()
    assert len(context.messages) == 4


@pytest.mark.asyncio
async def test_replay_with_stale_target_fails_closed_without_reverting_latest_target():
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())
    first = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-stale-target"
    )
    latest_target = {
        "schema_version": 1,
        "report_path": "/workspace/report-latest.md",
        "revision_id": "rev_latest",
    }
    session.update_state({PENDING_HTML_EXPORT_STATE_KEY: latest_target})

    replay = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-stale-target"
    )

    assert first is not None
    assert replay.status == "error"
    assert replay.error_code == "REPLAY_CONFLICT"
    adapter._try_deepresearch_rewrite_fast_path.assert_awaited_once()
    assert len(context.messages) == 4
    assert session.state_store[PENDING_HTML_EXPORT_STATE_KEY] == latest_target


@pytest.mark.asyncio
async def test_replay_checkpoint_canonicalizes_tool_result_and_rejects_oversize_state():
    adapter, session, _context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    result = _result()
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=result)

    outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-canonical"
    )

    assert outcome is result
    replay_state = session.state_store["deepresearch_rewrite_fast_path_replays"]
    stored_entry = replay_state["entries"][0]
    assert set(stored_entry) == {
        "request_id",
        "query_sha256",
        "terminal_kind",
        "action",
        "target",
    }
    assert stored_entry["terminal_kind"] == "success"
    assert len(json.dumps(replay_state).encode("utf-8")) < 16_384

    replay_state["entries"][0]["action"] = "x" * 300_000
    assert adapter._decode_deepresearch_rewrite_replays(replay_state) == []


@pytest.mark.asyncio
async def test_context_persist_failure_terminal_is_not_written_to_replay_ledger():
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        request_id="request-not-replayable",
        query_sha256=hashlib.sha256(_query().encode()).hexdigest(),
        query=_query(),
        result=_result(error_code="CONTEXT_PERSIST_FAILED"),
    )

    assert persisted is False
    assert context.messages == []
    assert "deepresearch_rewrite_fast_path_replays" not in session.state_store


@pytest.mark.asyncio
async def test_report_delivery_failure_replays_fixed_safe_terminal_without_new_child():
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    result = _result(
        error_code="transport-secret-code",
        message="untrusted delivery details",
        commit_result={
            "status": "completed",
            "report_delivered": False,
            "report_path": "/workspace/report.rewrite.md",
            "revision_id": "rev_child",
            "delivery_error_code": "transport-secret-code",
        },
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=result)

    first = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-delivery-failed"
    )
    replay = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-delivery-failed"
    )

    assert first is result
    assert replay.error_code == "REPORT_DELIVERY_FAILED"
    assert replay.message == "改写版本已成功保留，但报告文件交付失败。"
    assert replay.commit_result["report_delivered"] is False
    assert len(context.messages) == 4
    adapter._try_deepresearch_rewrite_fast_path.assert_awaited_once()
    entry = session.state_store["deepresearch_rewrite_fast_path_replays"]["entries"][0]
    assert entry["terminal_kind"] == "report_delivery_failed"
    assert "untrusted delivery details" not in repr(entry)
    assert "transport-secret-code" not in repr(entry)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_result",
    [
        {"status": "error", "report_delivered": False},
        {
            "status": "completed",
            "report_delivered": "false",
            "report_path": "/workspace/report.rewrite.md",
            "revision_id": "rev_child",
        },
    ],
)
async def test_invalid_commit_terminal_is_not_persisted_or_replayed(commit_result):
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )

    persisted = await adapter._persist_deepresearch_rewrite_fast_path_turn(
        session_id="session-1",
        request_id="request-invalid-terminal",
        query_sha256=hashlib.sha256(_query().encode()).hexdigest(),
        query=_query(),
        result=_result(commit_result=commit_result),
    )

    assert persisted is False
    assert context.messages == []
    assert "deepresearch_rewrite_fast_path_replays" not in session.state_store


@pytest.mark.asyncio
async def test_replay_corrupt_state_and_target_read_fail_closed_without_tool():
    adapter, session, _context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())
    cyclic = {"schema_version": 1}
    cyclic["entries"] = cyclic
    session.state_store["deepresearch_rewrite_fast_path_replays"] = cyclic

    assert adapter._decode_deepresearch_rewrite_replays(cyclic) == []
    outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-corrupt"
    )

    assert outcome.status == "error"
    assert outcome.error_code == "REPLAY_CONFLICT"
    adapter._try_deepresearch_rewrite_fast_path.assert_not_awaited()

    adapter, session, *_ = _adapter_with_active_session()
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())
    session.get_state.side_effect = [None, RuntimeError("target-read-secret")]
    outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-target-read"
    )

    assert outcome.status == "error"
    assert outcome.error_code == "REPLAY_CONFLICT"
    adapter._try_deepresearch_rewrite_fast_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_surrogate_query_fails_closed_without_tool_or_checkpoint():
    adapter, session, _context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(return_value=_result())

    outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        "\ud800", session_id="session-1", request_id="request-surrogate"
    )

    assert outcome is None
    adapter._try_deepresearch_rewrite_fast_path.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_scoped_adapter_nonstream_fails_closed_before_core():
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._deepresearch_rewrite_tx_uncertain = True
    adapter._instance = None
    request = AgentRequest(
        request_id="request-uncertain",
        session_id="session-1",
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "continue"},
    )

    response = await adapter.process_message_impl(request, {})

    assert response.ok is False
    assert response.payload["error_code"] == "DEEPRESEARCH_CHECKPOINT_UNCERTAIN"


@pytest.mark.asyncio
async def test_uncertain_scoped_adapter_stream_fails_closed_before_core():
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._deepresearch_rewrite_tx_uncertain = True
    adapter._instance = None
    request = AgentRequest(
        request_id="request-uncertain",
        session_id="session-1",
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "continue"},
    )

    chunks = [chunk async for chunk in adapter.process_message_stream_impl(request, {})]

    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    assert chunks[0].payload == {
        "event_type": "chat.error",
        "error_code": "DEEPRESEARCH_CHECKPOINT_UNCERTAIN",
        "error": "会话检查点状态不确定，请重试以重新加载会话。",
    }


@pytest.mark.asyncio
async def test_parent_evicts_uncertain_child_without_waiting_for_idle_ttl():
    parent = object.__new__(JiuWenSwarmDeepAdapter)
    parent._is_session_scoped_adapter = False
    child = SimpleNamespace(
        _deepresearch_rewrite_tx_uncertain=True,
        is_session_active=Mock(return_value=False),
        is_deep_agent_executing_for_session=Mock(return_value=False),
        cleanup=AsyncMock(),
    )
    parent._session_adapters = {"session-1": child}
    parent._session_adapter_locks = {}
    parent._session_adapter_last_used = {"session-1": interface_module.time.time()}
    parent._session_adapter_versions = {"session-1": 0}
    parent._session_adapter_reload_failures = {}
    parent._sessions_awaiting_tool_permission = set()

    await parent._evict_idle_session_adapters()

    child.cleanup.assert_awaited_once_with()
    assert "session-1" not in parent._session_adapters


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"action": "x" * 65},
    ],
)
async def test_invalid_terminal_result_is_not_written_to_replay_checkpoint(overrides):
    adapter, session, context, _react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    adapter._try_deepresearch_rewrite_fast_path = AsyncMock(
        return_value=_result(**overrides)
    )

    outcome = await adapter._run_deepresearch_rewrite_fast_path_transaction(
        _query(), session_id="session-1", request_id="request-invalid-result"
    )

    assert outcome.error_code == "CONTEXT_PERSIST_FAILED"
    assert "deepresearch_rewrite_fast_path_replays" not in session.state_store
    assert context.messages == []


@pytest.mark.parametrize(
    "params",
    [
        {"query": _query(), "mode": "agent", "attach_goal": True},
        {
            "query": _query(),
            "mode": "agent",
            "source": "ask_user_interrupt",
            "request_id": "pending",
            "answers": [],
        },
        {"query": _query(), "mode": "agent", "answers": [{"answer": "A"}]},
        {"query": _query(), "mode": "agent", "answers": []},
    ],
)
def test_stream_rewrite_fast_path_excludes_attach_and_interrupt_requests(params):
    adapter, *_ = _adapter_with_active_session()

    eligible = adapter._is_stream_rewrite_fast_path_eligible(
        AgentRequest(
            request_id="request-1",
            channel_id="web",
            session_id="session-1",
            params=params,
            metadata={},
            is_stream=True,
        ),
        pending_goal_op=None,
        attach_goal_request=bool(params.get("attach_goal")),
        goal_stream_request=False,
    )

    assert eligible is False


def _entry_adapter(monkeypatch):
    interaction_send_lock = asyncio.Lock()
    interaction_control_lock = asyncio.Lock()
    adapter = JiuWenSwarmDeepAdapter()
    instance = SimpleNamespace(
        active_round=None,
        attach_output=AsyncMock(),
        send_input=AsyncMock(),
        get_context_usage=Mock(return_value={}),
        _interaction_send_lock=interaction_send_lock,
        _interaction_control_lock=interaction_control_lock,
        _should_keep_interaction_open_locked=Mock(return_value=False),
    )
    adapter._instance = instance
    adapter._is_session_scoped_adapter = True
    adapter._permission_rail = None
    adapter._stream_event_rail = None
    adapter._request_summary_rail = None
    adapter._model = SimpleNamespace(model_config=SimpleNamespace(model_name="test"))
    adapter._model_request_config = None
    monkeypatch.setattr(adapter, "_inject_extension_config_into_inputs", Mock())
    monkeypatch.setattr(adapter, "_has_valid_model_config", Mock(return_value=True))
    monkeypatch.setattr(adapter, "_handle_slash_command", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_resolve_model_for_request", Mock(return_value=adapter._model))
    monkeypatch.setattr(adapter, "_resolve_model_name", Mock(return_value="test"))
    monkeypatch.setattr(adapter, "_apply_model_to_react_agent", Mock())
    monkeypatch.setattr(adapter, "_maybe_apply_pending_reload", AsyncMock())
    monkeypatch.setattr(adapter, "_bind_request_env_overlay", Mock(return_value=()))
    monkeypatch.setattr(adapter, "_reset_request_env_bindings", Mock())
    monkeypatch.setattr(adapter, "_mark_session_active", Mock())
    monkeypatch.setattr(adapter, "_unmark_session_active", Mock())
    monkeypatch.setattr(adapter, "_register_session_agent_task", Mock())
    monkeypatch.setattr(adapter, "_unregister_session_agent_task", Mock())
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(adapter, "_goal_record_is_active", Mock(return_value=False))
    monkeypatch.setattr(interface_module, "setup_permission_context", Mock(return_value=object()))
    monkeypatch.setattr(interface_module, "cleanup_permission_context", Mock())
    monkeypatch.setattr(interface_module, "setup_permissions_session_scope", Mock(return_value=object()))
    monkeypatch.setattr(interface_module, "reset_permissions_session_scope", Mock())
    monkeypatch.setattr(interface_module, "set_perf_summary_context", Mock())
    monkeypatch.setattr(interface_module, "finalize_perf_summary_request", Mock())
    monkeypatch.setattr(interface_module, "clear_perf_summary_context", Mock())
    return adapter, instance


def _request(
    query: str,
    *,
    stream: bool,
    params: dict | None = None,
) -> AgentRequest:
    return AgentRequest(
        request_id="request-1",
        channel_id="web",
        session_id="session-1",
        params=params or {"query": query, "mode": "agent"},
        metadata={},
        is_stream=stream,
    )


@pytest.mark.asyncio
async def test_nonstream_html_followup_returns_without_attach_or_fast_path(
    monkeypatch,
):
    adapter, instance = _entry_adapter(monkeypatch)
    html_result = RewriteHtmlFollowupResult(
        status="completed",
        error_code=None,
        message="已生成美化后的 HTML。",
    )
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        AsyncMock(return_value=html_result),
    )
    fast_path = AsyncMock()
    monkeypatch.setattr(adapter, "_try_deepresearch_rewrite_fast_path", fast_path)

    response = await adapter.process_message_impl(
        _request("生成 HTML", stream=False),
        {"query": "生成 HTML", "conversation_id": "session-1"},
    )

    assert response.ok is True
    assert response.payload == {"content": html_result.message}
    instance.attach_output.assert_not_awaited()
    instance.send_input.assert_not_awaited()
    fast_path.assert_not_awaited()


async def _collect_stream(adapter, query: str):
    return [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            _request(query, stream=True),
            {"query": query, "conversation_id": "session-1"},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["corrupt", "different_query", "stale_target"])
async def test_stream_replay_conflict_is_terminal_without_core_fallback(
    monkeypatch,
    scenario,
):
    adapter, instance = _entry_adapter(monkeypatch)
    _unused, session, _context, react_agent, _context_engine = (
        _adapter_with_active_session()
    )
    instance._interaction_session = session
    instance.react_agent = react_agent
    target = {
        "schema_version": 1,
        "report_path": "/workspace/report.rewrite.md",
        "revision_id": "rev_child",
    }
    query = _query()
    if scenario == "corrupt":
        replay_state = {"schema_version": 1}
        replay_state["entries"] = replay_state
    else:
        replay_state = {
            "schema_version": 1,
            "entries": [
                {
                    "request_id": "request-1",
                    "query_sha256": hashlib.sha256(_query().encode()).hexdigest(),
                    "message": "completed",
                    "action": "polish",
                    "target": target,
                }
            ],
        }
        session.state_store[PENDING_HTML_EXPORT_STATE_KEY] = target
        if scenario == "different_query":
            query = _query().replace(
                '"instruction": ""',
                '"instruction": "different"',
            )
        else:
            session.state_store[PENDING_HTML_EXPORT_STATE_KEY] = {
                "schema_version": 1,
                "report_path": "/workspace/report-latest.md",
                "revision_id": "rev_latest",
            }
    session.state_store["deepresearch_rewrite_fast_path_replays"] = replay_state
    rewrite_tool = AsyncMock(return_value=_result())
    monkeypatch.setattr(adapter, "_try_deepresearch_rewrite_fast_path", rewrite_tool)
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        AsyncMock(return_value=None),
    )

    chunks = await _collect_stream(adapter, query)

    assert [chunk.payload for chunk in chunks] == [
        {
            "event_type": "chat.error",
            "error": "改写失败（REPLAY_CONFLICT）：无法安全重放改写请求，请重新提交。",
            "status": "error",
            "error_code": "REPLAY_CONFLICT",
        },
        None,
    ]
    rewrite_tool.assert_not_awaited()
    instance.attach_output.assert_not_awaited()
    instance.send_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_html_followup_has_priority_and_never_attaches(monkeypatch):
    adapter, instance = _entry_adapter(monkeypatch)
    html_result = RewriteHtmlFollowupResult(
        status="completed",
        error_code=None,
        message="已生成美化后的 HTML。",
    )
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        AsyncMock(return_value=html_result),
    )
    fast_path = AsyncMock()
    monkeypatch.setattr(adapter, "_try_deepresearch_rewrite_fast_path", fast_path)

    chunks = await _collect_stream(adapter, "生成 HTML")

    assert [chunk.payload for chunk in chunks] == [
        {"event_type": "chat.final", "content": html_result.message},
        None,
    ]
    instance.attach_output.assert_not_awaited()
    instance.send_input.assert_not_awaited()
    fast_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonstream_html_text_in_hitl_resume_goes_to_core_attach_and_send(monkeypatch):
    adapter, instance = _entry_adapter(monkeypatch)
    instance.attach_output.return_value = None
    html_followup = AsyncMock(
        return_value=RewriteHtmlFollowupResult(
            status="completed",
            error_code=None,
            message="must not bypass HITL",
        )
    )
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        html_followup,
    )
    params = {
        "query": "生成 HTML",
        "mode": "agent",
        "source": "ask_user_interrupt",
        "request_id": "pending-ask",
        "answers": [],
    }

    response = await adapter.process_message_impl(
        _request("生成 HTML", stream=False, params=params),
        {"query": "生成 HTML", "conversation_id": "session-1"},
    )

    assert response.payload["event_type"] == "runtime.accepted"
    html_followup.assert_not_awaited()
    instance.attach_output.assert_awaited_once_with()
    instance.send_input.assert_awaited_once()


@pytest.mark.asyncio
async def test_nonstream_structured_goal_never_enters_html_direct_path(monkeypatch):
    adapter, instance = _entry_adapter(monkeypatch)
    instance.attach_output.return_value = None
    html_followup = AsyncMock()
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        html_followup,
    )
    request = _request(
        "生成 HTML",
        stream=False,
        params={
            "query": "生成 HTML",
            "mode": "agent",
            "action": "set",
            "objective": "keep researching",
        },
    )
    request.req_method = ReqMethod.COMMAND_GOAL

    await adapter.process_message_impl(
        request,
        {"query": "生成 HTML", "conversation_id": "session-1"},
    )

    html_followup.assert_not_awaited()
    instance.attach_output.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stream_html_text_in_hitl_resume_goes_to_core_attach_and_send(monkeypatch):
    adapter, instance = _entry_adapter(monkeypatch)
    instance.attach_output.return_value = None
    reattach = AsyncMock(return_value=None)
    monkeypatch.setattr(adapter, "_reattach_interrupt_output", reattach)
    html_followup = AsyncMock(
        return_value=RewriteHtmlFollowupResult(
            status="completed",
            error_code=None,
            message="must not bypass HITL",
        )
    )
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        html_followup,
    )
    params = {
        "query": "生成 HTML",
        "mode": "agent",
        "source": "ask_user_interrupt",
        "request_id": "pending-ask",
        "answers": [],
    }

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            _request("生成 HTML", stream=True, params=params),
            {"query": "生成 HTML", "conversation_id": "session-1"},
        )
    ]

    assert chunks[-1].is_complete is True
    html_followup.assert_not_awaited()
    instance.attach_output.assert_awaited_once_with()
    reattach.assert_awaited_once_with("session-1")
    instance.send_input.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [True, False])
async def test_stream_rewrite_fast_path_persists_before_yield_and_reports_usage(
    monkeypatch,
    persisted,
):
    adapter, instance = _entry_adapter(monkeypatch)
    result = _result()
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_fast_path",
        AsyncMock(return_value=result),
    )
    persist = AsyncMock(return_value=persisted)
    monkeypatch.setattr(
        adapter,
        "_persist_deepresearch_rewrite_fast_path_turn",
        persist,
    )

    chunks = await _collect_stream(adapter, _query())

    payloads = [chunk.payload for chunk in chunks]
    assert payloads[0]["event_type"] == "chat.final"
    assert payloads[0]["status"] == "completed"
    if persisted:
        assert payloads[0].get("error_code") is None
        assert "若报告已是最终版本" in payloads[0]["content"]
    else:
        assert payloads[0]["error_code"] == "CONTEXT_PERSIST_FAILED"
        assert "若报告已是最终版本" not in payloads[0]["content"]
    assert payloads[1] == {
        "event_type": "chat.usage_summary",
        "session_id": "session-1",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
        "model": "test",
    }
    assert payloads[2] is None
    persist.assert_awaited_once()
    instance.attach_output.assert_not_awaited()
    instance.send_input.assert_not_awaited()
    assert interface_module.finalize_perf_summary_request.call_args.kwargs["status"] == (
        "ok" if persisted else "error"
    )


@pytest.mark.asyncio
async def test_stream_fast_path_error_does_not_synthesize_empty_final(monkeypatch):
    adapter, instance = _entry_adapter(monkeypatch)
    result = _result(
        status="error",
        error_code="REVISION_CONFLICT",
        message="the report revision changed",
        usage_metadata=None,
        model_calls=0,
        commit_result=None,
    )
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_fast_path",
        AsyncMock(return_value=result),
    )
    persist = AsyncMock()
    monkeypatch.setattr(
        adapter,
        "_persist_deepresearch_rewrite_fast_path_turn",
        persist,
    )

    chunks = await _collect_stream(adapter, _query())

    assert [chunk.payload for chunk in chunks] == [
        {
            "event_type": "chat.error",
            "error": "改写失败（REVISION_CONFLICT）：the report revision changed",
            "status": "error",
            "error_code": "REVISION_CONFLICT",
        },
        None,
    ]
    assert all(
        payload is None or payload.get("event_type") != "chat.final"
        for payload in [chunk.payload for chunk in chunks]
    )
    persist.assert_not_awaited()
    instance.attach_output.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_fast_path_cancellation_propagates_without_yield_or_attach(
    monkeypatch,
):
    adapter, instance = _entry_adapter(monkeypatch)
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_html_followup",
        AsyncMock(return_value=None),
    )
    cancellation = asyncio.CancelledError("cancel-fast-path-secret")
    monkeypatch.setattr(
        adapter,
        "_try_deepresearch_rewrite_fast_path",
        AsyncMock(side_effect=cancellation),
    )
    yielded = []

    with pytest.raises(asyncio.CancelledError) as raised:
        async for chunk in adapter.process_message_stream_impl(
            _request(_query(), stream=True),
            {"query": _query(), "conversation_id": "session-1"},
        ):
            yielded.append(chunk)

    assert raised.value is cancellation
    assert yielded == []
    instance.attach_output.assert_not_awaited()
    instance.send_input.assert_not_awaited()
