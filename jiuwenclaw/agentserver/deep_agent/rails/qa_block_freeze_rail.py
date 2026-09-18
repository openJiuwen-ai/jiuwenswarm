# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Freeze completed QA blocks after each DeepAgent invoke (plan mode)."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Literal

from openjiuwen.core.context_engine.qa_artifact.window import make_processor_ctx
from openjiuwen.core.context_engine.qa_block.config import QABlockConfig
from openjiuwen.core.context_engine.qa_block.freezer import FreezeCommitResult, QABlockFreezer
from openjiuwen.core.context_engine.qa_block.messages import extract_qa_native_messages
from openjiuwen.core.context_engine.qa_block.registry import load_registry, save_registry
from openjiuwen.core.context_engine.qa_block.selector import resolve_summarizer_model
from openjiuwen.core.context_engine.qa_block.store import QABlockStore
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, InvokeInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenclaw.agentserver.deep_agent.plan_pause_helpers import (
    post_agent_execute_for_session,
    resolve_actual_session,
    resolve_context_engine,
)
from jiuwenclaw.agentserver.deep_agent.rails.qa_block_assembly_rail import (
    clear_assembly_committed_qa_id,
)
from jiuwenclaw.agentserver.llm_usage import (
    AuxiliaryUsageReportingModel,
    emit_llm_usage_to_session,
)

logger = logging.getLogger(__name__)

_PRELOADED_QA_IDS_KEY = "_preloaded_qa_ids"
_FREEZE_DONE_KEY = "_qa_block_freeze_done"


def infer_qa_status(ctx: AgentCallbackContext) -> Literal["completed", "interrupted"]:
    inputs = ctx.inputs
    result = getattr(inputs, "result", None) if isinstance(inputs, InvokeInputs) else None
    if isinstance(result, dict):
        result_type = str(result.get("result_type") or "")
        if result_type == "interrupt":
            return "interrupted"
    if ctx.extra.get("_qa_block_freeze_interrupted"):
        return "interrupted"
    return "completed"


def _clear_session_interruption_key(session: Any) -> None:
    """Clear leftover HITL interrupt marker after a settled freeze.

    Skip-freeze paths must NOT call this — secondary ASK keep relies on the key.
    """
    updater = getattr(session, "update_state", None)
    if callable(updater):
        updater({INTERRUPTION_KEY: None})


class JiuClawQABlockFreezeRail(DeepAgentRail):
    priority = 75

    def __init__(self, config: QABlockConfig | None = None):
        super().__init__()
        self._config = config or QABlockConfig()
        self._freezer = QABlockFreezer(self._config)
        self._qa_artifact_mgr: Any | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def attach_qa_artifact(self, mgr: Any | None) -> None:
        """Wire QAArtifactManager for bounded overview await before freeze."""
        self._qa_artifact_mgr = mgr

    async def _maybe_await_overview_before_freeze(self, session: Any) -> None:
        mgr = self._qa_artifact_mgr
        if mgr is None or session is None:
            return
        registry = load_registry(session)
        qa_id = registry.current_qa_id
        if not qa_id:
            return
        finished = await mgr.await_pending_overview(
            qa_id,
            session_id=registry.session_id,
            timeout_s=self._config.freeze_overview_await_s,
        )
        if not finished:
            logger.info(
                "[QABlockFreezeRail] overview await timeout before freeze qa_id=%s",
                qa_id,
            )

    def init(self, agent) -> None:
        super().init(agent)
        config = getattr(getattr(agent, "react_agent", None), "_config", None)
        if config is None:
            config = getattr(getattr(agent, "_react_agent", None), "_config", None)
        if config is not None:
            model_config = getattr(config, "model_config_obj", None)
            model_client_config = getattr(config, "model_client_config", None)
            self.bind_summarizer_model_defaults(model_config, model_client_config)

    def bind_summarizer_model_defaults(
        self,
        model_config: Any,
        model_client_config: Any,
    ) -> None:
        self._freezer.bind_summarizer_model_defaults(model_config, model_client_config)

    def _prepare_freeze_usage_reporting(
        self,
        session: Any,
        persist_mode: Literal["async", "sync"],
        summarizer_model: Any | None,
    ) -> tuple[Any | None, dict[str, Any]]:
        """Bridge usage reporting across AgentCore freezer API versions."""
        if persist_mode != "sync":
            return summarizer_model, {}

        async def report_usage(usage_metadata: Any) -> None:
            await emit_llm_usage_to_session(session, usage_metadata)

        try:
            parameters = inspect.signature(self._freezer.freeze).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        accepts_usage_callback = any(
            parameter.name == "llm_usage_callback"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if accepts_usage_callback:
            return summarizer_model, {"llm_usage_callback": report_usage}

        if not callable(getattr(summarizer_model, "stream", None)):
            return summarizer_model, {}

        logger.info(
            "[QABlockFreezeRail] legacy AgentCore freezer lacks llm_usage_callback; "
            "wrapping the summarizer model"
        )
        return AuxiliaryUsageReportingModel(summarizer_model, report_usage), {}

    async def _schedule_freeze_artifact_produce_async(
        self,
        *,
        _session: Any,
        context: Any,
        qa_id: str,
        native_messages: list,
        force_produce: bool = False,
        l0_content_mode: str | None = None,
        had_full_compact_in_qa: bool | None = None,
    ) -> None:
        mgr = self._qa_artifact_mgr
        if mgr is None or self.workspace is None:
            return
        artifact_ctx = make_processor_ctx(context, sys_operation=self.sys_operation)
        produce = mgr.schedule_freeze_artifact_produce
        call_kwargs: dict[str, Any] = {
            "workspace": self.workspace,
            "qa_id": qa_id,
            "native_messages": native_messages,
        }
        # Compatible with older agent-core that lacks force_produce kwargs.
        try:
            params = inspect.signature(produce).parameters
        except (TypeError, ValueError):
            params = {}
        if "force_produce" in params:
            call_kwargs["force_produce"] = force_produce
        if "l0_content_mode" in params:
            call_kwargs["l0_content_mode"] = l0_content_mode
        if "had_full_compact_in_qa" in params:
            call_kwargs["had_full_compact_in_qa"] = had_full_compact_in_qa
        produce(artifact_ctx, **call_kwargs)

    async def _clear_empty_current_qa_after_failed_freeze(
        self,
        session: Any,
        context_engine: Any,
        *,
        session_id: str,
        context: Any | None = None,
        persist_context: bool = True,
    ) -> None:
        """Drop stale in-progress pointer when freeze had nothing to commit."""
        registry = load_registry(session)
        qa_id = registry.current_qa_id
        if not qa_id:
            return

        if context is not None:
            getter = getattr(context, "get_messages", None)
            if callable(getter):
                messages = getter() or []
                native = extract_qa_native_messages(messages, registry)
                if native:
                    roles = {getattr(message, "role", None) for message in native}
                    if "user" in roles or "tool" in roles:
                        logger.info(
                            "[QABlockFreezeRail] skip clear after failed freeze: "
                            "native user/tool still present session_id=%s qa_id=%s",
                            session_id,
                            qa_id,
                        )
                        return

        registry.current_qa_id = None
        save_registry(session, registry)
        clear_assembly_committed_qa_id(session)
        if persist_context:
            await context_engine.save_contexts(session)
            await self._persist_freeze_checkpoint(session, session_id=session_id)
        else:
            # Registry pointer cleared; caller (orphan salvage) restores messages first,
            # then persists context to avoid checkpointing a stripped-without-user buffer.
            await self._persist_freeze_checkpoint(session, session_id=session_id)
        logger.info(
            "[QABlockFreezeRail] cleared empty current_qa_id after failed freeze "
            "session_id=%s qa_id=%s persist_context=%s",
            session_id,
            qa_id,
            persist_context,
        )

    async def _persist_freeze_checkpoint(self, session: Any, *, session_id: str) -> None:
        """Flush registry after freeze; inner ReAct post_run may have checkpointed early."""
        try:
            await post_agent_execute_for_session(session)
        except Exception as exc:
            logger.warning(
                "[QABlockFreezeRail] freeze checkpoint flush failed session_id=%s: %s",
                session_id,
                exc,
            )

    def _on_freeze_commit(self, session: Any, context: Any, commit: FreezeCommitResult) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "[QABlockFreezeRail] no event loop for freeze produce schedule qa_id=%s",
                commit.entry.qa_id,
            )
            return
        loop.create_task(
            self._schedule_freeze_artifact_produce_async(
                _session=session,
                context=context,
                qa_id=commit.entry.qa_id,
                native_messages=commit.native_messages,
                # Force-produce responsibility split (P0-2):
                # - Rail: extract flags from QABlockEntry and set force_produce.
                #   recovery_required is entry-only and cannot be inferred by manager.
                # - Manager: final gate; also ORs had_full_compact / compact_summary_tail
                #   from kwargs or message inference (defense-in-depth / non-rail callers).
                force_produce=bool(
                    getattr(commit.entry, "had_full_compact_in_qa", False)
                    or getattr(commit.entry, "l0_content_mode", "") == "compact_summary_tail"
                    or getattr(commit.entry, "recovery_required", False)
                ),
                l0_content_mode=getattr(commit.entry, "l0_content_mode", None),
                had_full_compact_in_qa=getattr(commit.entry, "had_full_compact_in_qa", None),
            )
        )

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        if not self._config.enabled:
            return
        # Keep the request stream alive until the L1 summarizer reports its
        # usage. The answer chunk has already been emitted, but an async
        # freeze would finish after Runner closes the stream and its tokens
        # could no longer reach the adapter's usage accumulator.
        await self._freeze_session(ctx, persist_mode="sync")

    async def freeze_current_qa_sync(
        self,
        session_id: str,
        *,
        agent: Any,
        session: Any = None,
        status: Literal["completed", "interrupted"] = "interrupted",
        persist_context: bool = True,
        persist_mode: Literal["async", "sync"] = "sync",
    ) -> None:
        """Emergency freeze before plan cancel checkpoint.

        Async persistence requires the caller to keep ``session`` alive until
        the background freeze task finishes. Callers that own a temporary
        session must use the default sync mode before closing it.

        ``persist_context=False`` skips ``save_contexts`` so callers (orphan salvage)
        can restore temporarily stripped current-round user messages before persisting.
        Registry/checkpoint flush still runs so ``current_qa_id`` updates are durable.
        """
        if not self._config.enabled:
            return
        context_engine = resolve_context_engine(agent)
        if context_engine is None:
            logger.info("[QABlockFreezeRail] cancel freeze skipped: no context_engine")
            return

        if session is None:
            logger.info("[QABlockFreezeRail] cancel freeze skipped: no session session_id=%s", session_id)
            return

        actual_session = resolve_actual_session(session)
        context = context_engine.get_context(session_id=session_id)
        if context is None:
            logger.info("[QABlockFreezeRail] cancel freeze skipped: no context session_id=%s", session_id)
            return

        workspace_root = ""
        if self.workspace is not None:
            workspace_root = getattr(self.workspace, "root_path", "") or ""
        store = QABlockStore(workspace_root, session_id, self.sys_operation)
        history = context_engine.get_history_qa_buffer(
            session_id,
            context.context_id(),
            max_blocks=self._config.history_qa_buffer_size,
        )

        await self._maybe_await_overview_before_freeze(actual_session)
        summarizer_model = resolve_summarizer_model(agent)
        summarizer_model, usage_reporting_kwargs = self._prepare_freeze_usage_reporting(
            actual_session,
            persist_mode,
            summarizer_model,
        )
        freeze_kwargs = {
            "status": status,
            "persist_mode": persist_mode,
            "summarizer_model": summarizer_model,
            "post_commit": lambda commit, s=actual_session, c=context: self._on_freeze_commit(s, c, commit),
        }
        freeze_kwargs.update(usage_reporting_kwargs)
        entry = await self._freezer.freeze(
            actual_session,
            context,
            history,
            store,
            **freeze_kwargs,
        )
        if entry is not None:
            clear_assembly_committed_qa_id(actual_session)
            if persist_context:
                await context_engine.save_contexts(actual_session)
                await self._persist_freeze_checkpoint(actual_session, session_id=session_id)
            else:
                await self._persist_freeze_checkpoint(actual_session, session_id=session_id)
            logger.info(
                "[QABlockFreezeRail] cancel freeze done session_id=%s qa_id=%s status=%s "
                "persist_context=%s persist_mode=%s",
                session_id,
                entry.qa_id,
                status,
                persist_context,
                persist_mode,
            )
        else:
            await self._clear_empty_current_qa_after_failed_freeze(
                actual_session,
                context_engine,
                session_id=session_id,
                context=context,
                persist_context=persist_context,
            )

    async def _freeze_session(
        self,
        ctx: AgentCallbackContext,
        *,
        persist_mode: Literal["async", "sync"],
        status: Literal["completed", "interrupted"] | None = None,
    ) -> None:
        if ctx.extra.get(_FREEZE_DONE_KEY):
            logger.info("[QABlockFreezeRail] freeze skipped: already done this invoke")
            return

        session = resolve_actual_session(ctx.session)
        agent = ctx.agent
        if session is None or agent is None:
            return

        context_engine = resolve_context_engine(agent)
        if context_engine is None:
            return

        session_id = session.get_session_id() if hasattr(session, "get_session_id") else ""
        context = context_engine.get_context(session_id=session_id)
        if context is None:
            logger.info("[QABlockFreezeRail] freeze skipped: context not in pool session_id=%s", session_id)
            return

        # 弹窗/权限 HITL 中断期间不卸载 QA，沿用当前上下文（同轮 keep）。
        # 覆盖：首次普通 query ASK、InteractiveInput 续跑中再次中断。
        # 成功收尾：result 明确非 interrupt 时不因残留 INTERRUPTION_KEY 误 skip（stale key）；
        # 且 freeze 成功后清 key。二次 ASK（result=interrupt 或 result 未决 + key）仍 skip，不清 key。
        # stream 外层 result 常为 None，故未决时仍看 session INTERRUPTION_KEY。
        if isinstance(ctx.inputs, InvokeInputs):
            result = getattr(ctx.inputs, "result", None)
            result_is_interrupt = (
                isinstance(result, dict) and result.get("result_type") == "interrupt"
            )
            result_settled = (
                isinstance(result, dict)
                and bool(result.get("result_type"))
                and result.get("result_type") != "interrupt"
            )
            has_interrupt_key = bool(session.get_state(INTERRUPTION_KEY))
            still_interrupted = result_is_interrupt or (
                has_interrupt_key and not result_settled
            )
            if still_interrupted:
                if isinstance(result, dict) and "interrupt_ids" in result:
                    logger.info(
                        "[QABlockFreezeRail] skip freeze for ask_user_question interrupt "
                        "session_id=%s",
                        session_id,
                    )
                else:
                    logger.info(
                        "[QABlockFreezeRail] skip freeze while session interrupted "
                        "session_id=%s",
                        session_id,
                    )
                # Shared rail: leave QA mounted across HITL skip. Team pause
                # persistence runs in TeamManager.freeze_leader_qa_before_pause.
                return

        workspace_root = ""
        if self.workspace is not None:
            workspace_root = getattr(self.workspace, "root_path", "") or ""
        store = QABlockStore(workspace_root, session_id, self.sys_operation)
        history = context_engine.get_history_qa_buffer(
            session_id,
            context.context_id(),
            max_blocks=self._config.history_qa_buffer_size,
        )
        freeze_status = status or infer_qa_status(ctx)
        preloaded = ctx.extra.get(_PRELOADED_QA_IDS_KEY)
        await self._maybe_await_overview_before_freeze(session)
        summarizer_model = resolve_summarizer_model(agent)
        summarizer_model, usage_reporting_kwargs = self._prepare_freeze_usage_reporting(
            session,
            persist_mode,
            summarizer_model,
        )
        freeze_kwargs = {
            "status": freeze_status,
            "persist_mode": persist_mode,
            "preloaded_qa_ids": preloaded if isinstance(preloaded, list) else None,
            "summarizer_model": summarizer_model,
            "post_commit": lambda commit, s=session, c=context: self._on_freeze_commit(s, c, commit),
        }
        freeze_kwargs.update(usage_reporting_kwargs)
        entry = await self._freezer.freeze(
            session,
            context,
            history,
            store,
            **freeze_kwargs,
        )
        if entry is None:
            await self._clear_empty_current_qa_after_failed_freeze(
                session,
                context_engine,
                session_id=session_id,
                context=context,
            )
            # Turn is settled enough to attempt freeze; drop stale interrupt marker.
            _clear_session_interruption_key(session)
            return

        clear_assembly_committed_qa_id(session)
        ctx.extra[_FREEZE_DONE_KEY] = entry.qa_id
        await context_engine.save_contexts(session)
        await self._persist_freeze_checkpoint(session, session_id=session_id)
        _clear_session_interruption_key(session)
        logger.info(
            "[QABlockFreezeRail] freeze committed session_id=%s qa_id=%s status=%s persist=%s",
            session_id,
            entry.qa_id,
            freeze_status,
            persist_mode,
        )
