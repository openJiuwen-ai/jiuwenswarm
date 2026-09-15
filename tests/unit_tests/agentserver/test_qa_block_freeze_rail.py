# coding: utf-8
"""Tests for JiuClawQABlockFreezeRail freeze-produce scheduling."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from openjiuwen.core.context_engine.qa_block.freezer import FreezeCommitResult
from openjiuwen.core.context_engine.qa_block.history_buffer import HistoryQABuffer
from openjiuwen.core.context_engine.qa_block.messages import load_qa_l0
from openjiuwen.core.context_engine.qa_block.registry import load_registry
from openjiuwen.core.context_engine.qa_block.schema import QABlockRegistry
from openjiuwen.core.context_engine.qa_block.store import QABlockStore
from openjiuwen.core.foundation.llm import AssistantMessage, UserMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "jiuwenclaw"
    / "agentserver"
    / "deep_agent"
    / "rails"
    / "qa_block_freeze_rail.py"
)
assert _MODULE_PATH.exists(), f"rail module path does not exist: {_MODULE_PATH}"
_spec = importlib.util.spec_from_file_location("qa_block_freeze_rail_under_test", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
JiuClawQABlockFreezeRail = _module.JiuClawQABlockFreezeRail


def _make_commit(
    *,
    qa_id: str = "qa_001",
    had_full_compact_in_qa: bool = False,
    l0_content_mode: str = "delta",
    recovery_required: bool = False,
) -> FreezeCommitResult:
    # SimpleNamespace avoids pydantic setattr rejection when installed openjiuwen
    # QABlockEntry lacks companion-PR fields (e.g. recovery_required).
    entry = SimpleNamespace(
        qa_id=qa_id,
        had_full_compact_in_qa=had_full_compact_in_qa,
        l0_content_mode=l0_content_mode,
        recovery_required=recovery_required,
    )
    return FreezeCommitResult(entry=entry, native_messages=[SimpleNamespace(content="msg")])


async def _schedule_freeze_artifact_produce_async(rail: Any, **kwargs: Any) -> None:
    await getattr(rail, "_schedule_freeze_artifact_produce_async")(**kwargs)


def _on_freeze_commit(rail: Any, session: Any, context: Any, commit: FreezeCommitResult) -> None:
    getattr(rail, "_on_freeze_commit")(session, context, commit)


def _make_freeze_ctx(
    *,
    query: Any = None,
    interruption: Any = None,
) -> tuple[AgentCallbackContext, Any, Any]:
    state: dict[str, Any] = {}
    if interruption is not None:
        state[INTERRUPTION_KEY] = interruption

    def get_state(key: str, default: Any = None) -> Any:
        return state.get(key, default)

    def update_state(updates: dict[str, Any]) -> None:
        state.update(updates)

    session = SimpleNamespace(
        get_session_id=lambda: "session-1",
        get_state=get_state,
        update_state=update_state,
        _state=state,
    )
    context = MagicMock()
    context.context_id.return_value = "ctx-1"
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.get_history_qa_buffer.return_value = []
    context_engine.save_contexts = AsyncMock()
    agent = SimpleNamespace()
    if query is None:
        query = InteractiveInput()
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=InvokeInputs(query=query),
        session=session,
    )
    return ctx, context_engine, session


def _make_interactive_freeze_ctx(
    *, interruption: Any = None
) -> tuple[AgentCallbackContext, Any, Any]:
    return _make_freeze_ctx(interruption=interruption)


class TestQABlockFreezeRailProduceSchedule(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.rail = JiuClawQABlockFreezeRail()
        self.rail.workspace = SimpleNamespace(root_path="/tmp/ws")
        self.mgr = MagicMock()
        self.mgr.schedule_freeze_artifact_produce = MagicMock(return_value=True)
        self.rail.attach_qa_artifact(self.mgr)

    async def test_schedule_async_calls_mgr_with_processor_ctx(self) -> None:
        context = MagicMock()
        context.workspace_dir.return_value = "/tmp/ws"
        context.get_session_ref.return_value = SimpleNamespace(get_session_id=lambda: "session-1")
        setattr(context, "_sys_operation", None)
        session = SimpleNamespace()
        messages = [SimpleNamespace(content="native")]

        await _schedule_freeze_artifact_produce_async(
            self.rail,
            _session=session,
            context=context,
            qa_id="qa_002",
            native_messages=messages,
        )

        self.mgr.schedule_freeze_artifact_produce.assert_called_once()
        call_kwargs = self.mgr.schedule_freeze_artifact_produce.call_args.kwargs
        self.assertEqual(call_kwargs["qa_id"], "qa_002")
        self.assertIs(call_kwargs["native_messages"], messages)
        self.assertIs(call_kwargs["workspace"], self.rail.workspace)
        artifact_ctx = self.mgr.schedule_freeze_artifact_produce.call_args.args[0]
        self.assertIs(artifact_ctx.context, context)
        self.assertEqual(artifact_ctx.workspace.root_path, "/tmp/ws")

    async def test_schedule_async_skips_when_mgr_missing(self) -> None:
        self.rail.attach_qa_artifact(None)
        await _schedule_freeze_artifact_produce_async(
            self.rail,
            _session=object(),
            context=MagicMock(),
            qa_id="qa_001",
            native_messages=[],
        )
        self.mgr.schedule_freeze_artifact_produce.assert_not_called()

    async def test_schedule_async_skips_when_workspace_missing(self) -> None:
        self.rail.workspace = None
        await _schedule_freeze_artifact_produce_async(
            self.rail,
            _session=object(),
            context=MagicMock(),
            qa_id="qa_001",
            native_messages=[],
        )
        self.mgr.schedule_freeze_artifact_produce.assert_not_called()

    async def test_on_freeze_commit_dispatches_async_task(self) -> None:
        commit = _make_commit(qa_id="qa_003")
        context = MagicMock()
        session = SimpleNamespace()
        schedule_attr = "_schedule_freeze_artifact_produce_async"

        with patch.object(self.rail, schedule_attr, autospec=True) as mock_async:
            _on_freeze_commit(self.rail, session, context, commit)
            await asyncio.sleep(0)

        mock_async.assert_awaited_once_with(
            _session=session,
            context=context,
            qa_id="qa_003",
            native_messages=commit.native_messages,
            force_produce=False,
            l0_content_mode=getattr(commit.entry, "l0_content_mode", None),
            had_full_compact_in_qa=getattr(commit.entry, "had_full_compact_in_qa", None),
        )

    async def _assert_force_produce_true(self, commit: FreezeCommitResult) -> None:
        context = MagicMock()
        session = SimpleNamespace()
        schedule_attr = "_schedule_freeze_artifact_produce_async"
        with patch.object(self.rail, schedule_attr, autospec=True) as mock_async:
            _on_freeze_commit(self.rail, session, context, commit)
            await asyncio.sleep(0)
        mock_async.assert_awaited_once_with(
            _session=session,
            context=context,
            qa_id=commit.entry.qa_id,
            native_messages=commit.native_messages,
            force_produce=True,
            l0_content_mode=getattr(commit.entry, "l0_content_mode", None),
            had_full_compact_in_qa=getattr(commit.entry, "had_full_compact_in_qa", None),
        )

    async def test_on_freeze_commit_force_produce_when_had_full_compact(self) -> None:
        await self._assert_force_produce_true(
            _make_commit(qa_id="qa_fc", had_full_compact_in_qa=True)
        )

    async def test_on_freeze_commit_force_produce_when_compact_summary_tail(self) -> None:
        await self._assert_force_produce_true(
            _make_commit(qa_id="qa_tail", l0_content_mode="compact_summary_tail")
        )

    async def test_on_freeze_commit_force_produce_when_recovery_required(self) -> None:
        await self._assert_force_produce_true(
            _make_commit(qa_id="qa_rec", recovery_required=True)
        )

    def test_on_freeze_commit_without_running_loop_is_noop(self) -> None:
        commit = _make_commit()
        with patch.object(asyncio, "get_running_loop", side_effect=RuntimeError):
            _on_freeze_commit(self.rail, object(), MagicMock(), commit)
        self.mgr.schedule_freeze_artifact_produce.assert_not_called()


class TestQABlockFreezeRailInteractiveResume(unittest.IsolatedAsyncioTestCase):
    """InteractiveInput resume: freeze only when interruption is settled."""

    def setUp(self) -> None:
        self.rail = JiuClawQABlockFreezeRail()
        self.rail.workspace = SimpleNamespace(root_path="/tmp/ws")
        self.freeze_mock = AsyncMock(return_value=_make_commit().entry)
        self.rail._freezer = SimpleNamespace(freeze=self.freeze_mock)
        self.rail._maybe_await_overview_before_freeze = AsyncMock()

    async def test_interactive_resume_with_none_result_freezes_when_no_interrupt(self) -> None:
        ctx, context_engine, _session = _make_interactive_freeze_ctx()
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "post_agent_execute_for_session", new_callable=AsyncMock):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_awaited_once()

    async def test_interactive_resume_skips_when_session_still_interrupted(self) -> None:
        ctx, context_engine, _session = _make_interactive_freeze_ctx(
            interruption=SimpleNamespace(original_query="paused"),
        )
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "QABlockStore", return_value=MagicMock()):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_not_awaited()

    async def test_interactive_resume_skips_when_result_type_interrupt(self) -> None:
        ctx, context_engine, _session = _make_interactive_freeze_ctx()
        ctx.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["id-1"]}
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "QABlockStore", return_value=MagicMock()):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_not_awaited()

    async def test_failed_freeze_clears_empty_current_qa_id(self) -> None:
        ctx, context_engine, session = _make_interactive_freeze_ctx()
        registry = QABlockRegistry(session_id="session-1", current_qa_id="qa_stale", next_qa_index=2)
        self.freeze_mock.return_value = None

        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "load_registry", return_value=registry), patch.object(
            _module, "save_registry"
        ) as mock_save, patch.object(
            _module, "post_agent_execute_for_session", new_callable=AsyncMock
        ) as mock_flush:
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_awaited_once()
        self.assertIsNone(registry.current_qa_id)
        mock_save.assert_called_once()
        mock_flush.assert_awaited_once()
        context_engine.save_contexts.assert_awaited()

    async def test_failed_freeze_skips_save_contexts_when_persist_context_false(self) -> None:
        context_engine = MagicMock()
        context_engine.get_context.return_value = MagicMock(context_id=lambda: "ctx-1")
        context_engine.get_history_qa_buffer.return_value = []
        context_engine.save_contexts = AsyncMock()
        session = SimpleNamespace(get_session_id=lambda: "session-1", get_state=lambda *_a, **_k: None)
        registry = QABlockRegistry(session_id="session-1", current_qa_id="qa_stale", next_qa_index=2)
        self.freeze_mock.return_value = None
        agent = SimpleNamespace()

        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "load_registry", return_value=registry), patch.object(
            _module, "save_registry"
        ), patch.object(_module, "post_agent_execute_for_session", new_callable=AsyncMock):
            await self.rail.freeze_current_qa_sync(
                "session-1",
                agent=agent,
                session=session,
                persist_context=False,
            )

        self.assertIsNone(registry.current_qa_id)
        context_engine.save_contexts.assert_not_awaited()
        self.assertEqual(self.freeze_mock.await_args.kwargs["persist_mode"], "sync")

    async def test_interrupt_freeze_forwards_async_persist_mode(self) -> None:
        context_engine = MagicMock()
        context_engine.get_context.return_value = MagicMock(context_id=lambda: "ctx-1")
        context_engine.get_history_qa_buffer.return_value = []
        context_engine.save_contexts = AsyncMock()
        session = SimpleNamespace(get_session_id=lambda: "session-1")

        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "post_agent_execute_for_session", new_callable=AsyncMock):
            await self.rail.freeze_current_qa_sync(
                "session-1",
                agent=SimpleNamespace(),
                session=session,
                persist_mode="async",
            )

        self.assertEqual(self.freeze_mock.await_args.kwargs["persist_mode"], "async")

    async def test_async_interrupt_freeze_eventually_persists_readable_l0(self) -> None:
        rail = JiuClawQABlockFreezeRail()
        persist_started = asyncio.Event()
        allow_persist = asyncio.Event()

        async def generate_l1(
            _user_query: str,
            _final_answer: str,
            *,
            allow_llm: bool,
            **_kwargs: Any,
        ) -> tuple[str, str]:
            if allow_llm:
                persist_started.set()
                await allow_persist.wait()
            return "summary", "inline"

        rail._freezer._summarizer.generate_l1 = generate_l1
        rail._maybe_await_overview_before_freeze = AsyncMock()

        state: dict[str, Any] = {}
        session = SimpleNamespace(
            get_session_id=lambda: "session-1",
            get_state=lambda key, default=None: state.get(key, default),
            update_state=lambda updates: state.update(updates),
        )
        messages = [
            UserMessage(content="请总结当前任务"),
            AssistantMessage(content="当前任务已完成一部分"),
        ]
        context = MagicMock()
        context.context_id.return_value = "ctx-1"
        context.get_messages.return_value = messages
        context.token_counter = None
        history = HistoryQABuffer(max_blocks=3)
        context_engine = MagicMock()
        context_engine.get_context.return_value = context
        context_engine.get_history_qa_buffer.return_value = history
        context_engine.save_contexts = AsyncMock()

        with tempfile.TemporaryDirectory() as workspace_root:
            rail.workspace = SimpleNamespace(root_path=workspace_root)
            with patch.object(
                _module, "resolve_context_engine", return_value=context_engine
            ), patch.object(
                _module, "resolve_summarizer_model", return_value=None
            ), patch.object(
                _module, "clear_assembly_committed_qa_id"
            ), patch.object(
                _module, "post_agent_execute_for_session", new_callable=AsyncMock
            ):
                await rail.freeze_current_qa_sync(
                    "session-1",
                    agent=SimpleNamespace(),
                    session=session,
                    persist_mode="async",
                )

                await asyncio.wait_for(persist_started.wait(), timeout=1.0)
                registry = load_registry(session)
                self.assertEqual(len(registry.blocks), 1)
                entry = next(iter(registry.blocks.values()))
                self.assertEqual(entry.l0_persist_status, "pending")

                allow_persist.set()

                async def wait_until_persisted() -> None:
                    while load_registry(session, force_reload=True).blocks[
                        entry.qa_id
                    ].l0_persist_status != "done":
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_until_persisted(), timeout=1.0)
                store = QABlockStore(workspace_root, "session-1")
                loaded = await load_qa_l0(
                    entry.qa_id,
                    HistoryQABuffer(max_blocks=3),
                    store,
                )

        self.assertEqual(
            [message.content for message in loaded],
            [message.content for message in messages],
        )

    async def test_sync_freeze_legacy_freezer_wraps_stream_model_for_usage(self) -> None:
        """Legacy AgentCore lacks the callback kwarg but must still report L1 usage."""
        captured: dict[str, Any] = {}

        async def legacy_freeze(
            _session: Any,
            _context: Any,
            _history: Any,
            _store: Any,
            *,
            status: str,
            persist_mode: str,
            preloaded_qa_ids: list[str] | None,
            summarizer_model: Any,
            post_commit: Any,
        ) -> Any:
            captured.update(
                status=status,
                persist_mode=persist_mode,
                preloaded_qa_ids=preloaded_qa_ids,
                summarizer_model=summarizer_model,
                post_commit=post_commit,
            )
            return _make_commit().entry

        async def stream(*_args: Any, **_kwargs: Any):
            yield SimpleNamespace(
                content="summary",
                usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            )

        self.rail._freezer = SimpleNamespace(freeze=legacy_freeze)
        ctx, context_engine, session = _make_interactive_freeze_ctx()
        session.write_stream = AsyncMock()
        model = SimpleNamespace(stream=stream)

        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=model
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "post_agent_execute_for_session", new_callable=AsyncMock):
            await self.rail.after_invoke(ctx)

        wrapped_model = captured["summarizer_model"]
        self.assertIsInstance(wrapped_model, _module.AuxiliaryUsageReportingModel)
        streamed = [chunk async for chunk in wrapped_model.stream(messages=[])]

        self.assertEqual(len(streamed), 1)
        usage_event = session.write_stream.await_args.args[0]
        self.assertEqual(usage_event.type, "llm_usage")
        self.assertEqual(usage_event.payload["usage_metadata"]["total_tokens"], 15)


class TestQABlockFreezeRailFirstAskPlainQuery(unittest.IsolatedAsyncioTestCase):
    """First permission ASK on a normal user query must not freeze (same-QA keep)."""

    def setUp(self) -> None:
        self.rail = JiuClawQABlockFreezeRail()
        self.rail.workspace = SimpleNamespace(root_path="/tmp/ws")
        self.freeze_mock = AsyncMock(return_value=_make_commit().entry)
        self.rail._freezer = SimpleNamespace(freeze=self.freeze_mock)
        self.rail._maybe_await_overview_before_freeze = AsyncMock()

    async def test_plain_query_skips_when_session_interrupted(self) -> None:
        ctx, context_engine, _session = _make_freeze_ctx(
            query="请读取桌面文件并总结",
            interruption=SimpleNamespace(original_query="paused"),
        )
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "QABlockStore", return_value=MagicMock()):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_not_awaited()

    async def test_plain_query_skips_when_result_type_interrupt(self) -> None:
        ctx, context_engine, _session = _make_freeze_ctx(query="请发送文件给我")
        ctx.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["call_1"]}
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "QABlockStore", return_value=MagicMock()):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_not_awaited()

    async def test_plain_query_freezes_when_not_interrupted(self) -> None:
        ctx, context_engine, _session = _make_freeze_ctx(query="你好")
        ctx.inputs.result = {"result_type": "answer", "output": "hi"}
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "post_agent_execute_for_session", new_callable=AsyncMock):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_awaited_once()

    async def test_after_invoke_keeps_usage_stream_open_until_freeze_persist_finishes(self) -> None:
        ctx, context_engine, session = _make_freeze_ctx(query="请生成一个 skill")
        session.write_stream = AsyncMock()
        ctx.inputs.result = {"result_type": "answer", "output": "done"}
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "post_agent_execute_for_session", new_callable=AsyncMock):
            await self.rail.after_invoke(ctx)

        freeze_kwargs = self.freeze_mock.await_args.kwargs
        self.assertEqual(freeze_kwargs["persist_mode"], "sync")
        usage_callback = freeze_kwargs["llm_usage_callback"]
        await usage_callback(
            SimpleNamespace(input_tokens=123, output_tokens=45, total_tokens=168)
        )

        usage_event = session.write_stream.await_args.args[0]
        self.assertEqual(usage_event.type, "llm_usage")
        self.assertEqual(usage_event.payload["usage_metadata"]["total_tokens"], 168)

    async def test_settled_result_with_stale_key_freezes_and_clears_key(self) -> None:
        """Explicit answer + leftover INTERRUPTION_KEY → freeze and clear (not mis-skip)."""
        stale = SimpleNamespace(original_query="stale")
        ctx, context_engine, session = _make_freeze_ctx(
            query="今天天气怎么样",
            interruption=stale,
        )
        ctx.inputs.result = {"result_type": "answer", "output": "sunny"}
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "clear_assembly_committed_qa_id"), patch.object(
            _module, "QABlockStore", return_value=MagicMock()
        ), patch.object(_module, "post_agent_execute_for_session", new_callable=AsyncMock):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_awaited_once()
        self.assertIsNone(session.get_state(INTERRUPTION_KEY))

    async def test_secondary_ask_keeps_interrupt_key(self) -> None:
        """Second ASK during resume: skip freeze and do NOT clear INTERRUPTION_KEY."""
        pending = SimpleNamespace(original_query="still waiting")
        ctx, context_engine, session = _make_freeze_ctx(
            query=InteractiveInput(),
            interruption=pending,
        )
        ctx.inputs.result = {"result_type": "interrupt", "interrupt_ids": ["call_2"]}
        with patch.object(_module, "resolve_context_engine", return_value=context_engine), patch.object(
            _module, "resolve_summarizer_model", return_value=None
        ), patch.object(_module, "QABlockStore", return_value=MagicMock()):
            await self.rail.after_invoke(ctx)

        self.freeze_mock.assert_not_awaited()
        self.assertIs(session.get_state(INTERRUPTION_KEY), pending)


if __name__ == "__main__":
    unittest.main()
