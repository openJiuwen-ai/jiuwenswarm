"""Best-effort enrichment for AgentCore-owned observability spans."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from threading import RLock
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span

try:
    from openjiuwen.agent_teams.observability import abort_current_llm_span
except ImportError:
    def abort_current_llm_span(error: BaseException) -> bool:
        return False
from openjiuwen.agent_teams.observability.span_context import (
    get_current_agent_span,
    get_current_llm_span,
    get_team_span,
)
from openjiuwen.core.runner.callback import AgentEvents, LLMCallEvents, ToolCallEvents

from jiuwenswarm.extensions.identity_provider import IdentityStore
from jiuwenswarm.telemetry.attributes import (
    APP_ID,
    DOMAIN_ID,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_DECISION_TOOL_COUNT,
    GEN_AI_DECISION_TOOL_NAMES,
    GEN_AI_DECISION_TYPE,
    GEN_AI_INPUT_MESSAGES,
    GEN_AI_INPUT_MESSAGES_COUNT,
    GEN_AI_INPUT_MESSAGES_TOTAL_LENGTH,
    GEN_AI_OPERATION_NAME,
    GEN_AI_OUTPUT_MESSAGES,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_REQUEST_STREAMING,
    GEN_AI_REQUEST_TEMPERATURE,
    GEN_AI_REQUEST_TOP_P,
    GEN_AI_RESPONSE_FINISH_REASON,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_RESPONSE_MODEL,
    GEN_AI_SPAN_TYPE,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_ARGUMENTS,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_DEFINITIONS,
    GEN_AI_TOOL_NAME,
    GEN_AI_TOOL_RESULT,
    GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
    GEN_AI_USAGE_CACHE_CREATION_TOKENS,
    GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
    GEN_AI_USAGE_CACHE_READ_TOKENS,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
    GEN_AI_USAGE_TOTAL_TOKENS,
    GEN_AI_CONTEXT_ASSISTANT_MESSAGES,
    GEN_AI_CONTEXT_SKILL,
    GEN_AI_CONTEXT_SYSTEM_PROMPT,
    GEN_AI_CONTEXT_TOOL_DEFINITIONS,
    GEN_AI_CONTEXT_TOOL_RESULTS,
    GEN_AI_CONTEXT_USER_MESSAGES,
    GEN_AI_STREAMING_FIRST_TOKEN,
    GEN_AI_SKILL_ID,
    GEN_AI_SKILL_NAME,
    GEN_AI_SKILL_VERSION,
    ERROR_TYPE,
    JIUWENCLAW_AGENT_NAME,
    JIUWENCLAW_CANCELED,
    JIUWENCLAW_AGENT_PARENT,
    JIUWENCLAW_APP_ID,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_CLAW_ID,
    JIUWENCLAW_DOMAIN_ID,
    JIUWENCLAW_ITERATION,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_USER_ID,
    USER_ID,
)
from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.enrichment.messages import (
    classify_decision,
    message_content,
    message_role,
    serialize_input_messages,
    serialize_output_message,
    serialize_tool_definitions,
)
from jiuwenswarm.telemetry.enrichment.tokens import extract_usage
from jiuwenswarm.telemetry.enrichment.tokens import (
    ContextTokenBreakdown,
    UsageBreakdown,
    count_context_tokens,
)
from jiuwenswarm.telemetry.enrichment.skills import SkillObservation, extract_skill
from jiuwenswarm.telemetry.metrics import TelemetryMetrics, metrics_channel_id
from jiuwenswarm.telemetry.span_registry import SpanRegistryProcessor


NAMESPACE = "jiuwenswarm.telemetry.enrichment"
CORE_NAMESPACE = "extensions.observability"
INPUT_PRIORITY = -100
OUTPUT_PRIORITY = 100
_LOGGER = logging.getLogger(__name__)

_TOKEN_COUNT_POOL = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="jiuwen-tlm-tok"
)


@dataclass
class _CallState:
    started_at: float
    model: str = ""
    provider: str = ""
    channel_id: str = ""
    agent_name: str = ""
    context_tokens: ContextTokenBreakdown | None = None
    ttft_seconds: float | None = None
    sequence: int = 0
    tool_name: str = ""
    inputs: Any = None
    skill: SkillObservation | None = None
    span_key: tuple[int, int] | None = None
    trace_id: int = 0
    session_id: str = ""


@dataclass(frozen=True)
class _SkillSession:
    started_at: float
    version: str
    channel_id: str


class EnrichmentStateRegistry:
    """Bounded TTL state keyed strictly by ``(trace_id, span_id)``."""

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than zero")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[int, int], _CallState] = {}
        self._lock = RLock()

    def put(self, key: tuple[int, int], state: _CallState) -> None:
        with self._lock:
            self._prune_locked(time.monotonic())
            self._entries[key] = state
            while len(self._entries) > self._max_entries:
                oldest = min(
                    self._entries, key=lambda item: self._entries[item].sequence
                )
                self._entries.pop(oldest, None)

    def get(self, key: tuple[int, int]) -> _CallState | None:
        with self._lock:
            self._prune_locked(time.monotonic())
            return self._entries.get(key)

    def pop(self, key: tuple[int, int]) -> _CallState | None:
        with self._lock:
            self._prune_locked(time.monotonic())
            return self._entries.pop(key, None)

    def active_count(self) -> int:
        with self._lock:
            self._prune_locked(time.monotonic())
            return len(self._entries)

    def prune(self, now: float | None = None) -> int:
        with self._lock:
            return self._prune_locked(time.monotonic() if now is None else now)

    def _prune_locked(self, now: float) -> int:
        expired = [
            key
            for key, state in self._entries.items()
            if now - state.started_at >= self._ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)


class MetricCallStateRegistry:
    """Correlate sampled and unsampled callback pairs without shadow spans."""

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than zero")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, str, int], _CallState] = {}
        self._task_keys: dict[tuple[str, int], list[tuple[str, str, int]]] = {}
        self._identity_keys: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
        self._sequence = 0
        self._lock = RLock()

    def start(self, kind: str, identity: str, state: _CallState) -> None:
        task_id = self._task_id()
        with self._lock:
            self._prune_locked(time.monotonic())
            self._sequence += 1
            state.sequence = self._sequence
            correlation = identity or f"task:{task_id}"
            key = (kind, correlation, self._sequence)
            self._entries[key] = state
            self._task_keys.setdefault((kind, task_id), []).append(key)
            if identity:
                self._identity_keys.setdefault((kind, identity), []).append(key)
            self._limit_locked()

    def finish(self, kind: str, identity: str) -> _CallState | None:
        task_id = self._task_id()
        with self._lock:
            self._prune_locked(time.monotonic())
            task_index = self._task_keys.get((kind, task_id), [])
            task_state = self._take_latest_locked(
                task_index,
                identity=identity if identity else None,
            )
            if task_state is not None:
                return task_state
            if identity:
                return self._take_latest_locked(
                    self._identity_keys.get((kind, identity), [])
                )
            return None

    def active_count(self) -> int:
        with self._lock:
            self._prune_locked(time.monotonic())
            return len(self._entries)

    def prune(self, now: float | None = None) -> int:
        with self._lock:
            return self._prune_locked(time.monotonic() if now is None else now)

    @staticmethod
    def _task_id() -> int:
        task = asyncio.current_task()
        return id(task) if task is not None else 0

    def _remove_indexes_locked(self, key: tuple[str, str, int]) -> None:
        for task_index_key, task_keys in list(self._task_keys.items()):
            task_remaining = [item for item in task_keys if item != key]
            if task_remaining:
                self._task_keys[task_index_key] = task_remaining
            else:
                self._task_keys.pop(task_index_key, None)
        for identity_index_key, identity_keys in list(self._identity_keys.items()):
            identity_remaining = [item for item in identity_keys if item != key]
            if identity_remaining:
                self._identity_keys[identity_index_key] = identity_remaining
            else:
                self._identity_keys.pop(identity_index_key, None)

    def _take_latest_locked(
        self,
        index: list[tuple[str, str, int]],
        *,
        identity: str | None = None,
    ) -> _CallState | None:
        for position in range(len(index) - 1, -1, -1):
            key = index[position]
            if identity is not None and key[1] != identity:
                continue
            state = self._entries.pop(key, None)
            if state is None:
                index.pop(position)
                continue
            self._remove_indexes_locked(key)
            return state
        return None

    def _prune_locked(self, now: float) -> int:
        expired = [
            key
            for key, state in self._entries.items()
            if now - state.started_at >= self._ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
            self._remove_indexes_locked(key)
        return len(expired)

    def _limit_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key].sequence)
            self._entries.pop(oldest, None)
            self._remove_indexes_locked(oldest)


class SkillSessionRegistry:
    """Bounded activation state keyed by trace, session, and skill."""

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than zero")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[int, str, str], _SkillSession] = {}
        self._lock = RLock()

    def start(
        self,
        trace_id: int,
        session_id: str,
        skill_name: str,
        session: _SkillSession,
    ) -> None:
        key = (trace_id, session_id, skill_name)
        with self._lock:
            self._prune_locked(time.monotonic())
            self._entries.pop(key, None)
            self._entries[key] = session
            while len(self._entries) > self._max_entries:
                oldest = min(
                    self._entries,
                    key=lambda item: self._entries[item].started_at,
                )
                self._entries.pop(oldest, None)

    def finish(
        self,
        trace_id: int,
        session_id: str,
        skill_name: str,
    ) -> _SkillSession | None:
        with self._lock:
            self._prune_locked(time.monotonic())
            return self._entries.pop((trace_id, session_id, skill_name), None)

    def active_count(self) -> int:
        with self._lock:
            self._prune_locked(time.monotonic())
            return len(self._entries)

    def prune(self, now: float | None = None) -> int:
        with self._lock:
            return self._prune_locked(time.monotonic() if now is None else now)

    def _prune_locked(self, now: float) -> int:
        expired = [
            key
            for key, session in self._entries.items()
            if now - session.started_at >= self._ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
        return len(expired)


class RichTelemetryCallbacks:
    """Attach Enterprise-compatible data without creating or ending spans."""

    def __init__(
        self,
        *,
        span_registry: SpanRegistryProcessor,
        metrics: TelemetryMetrics,
        config: TelemetryConfig,
    ) -> None:
        self._span_registry = span_registry
        self._metrics = metrics
        self._config = config
        self._span_state = EnrichmentStateRegistry(max_entries=2048, ttl_seconds=900)
        self._metric_state = MetricCallStateRegistry(max_entries=2048, ttl_seconds=900)
        self._skill_sessions = SkillSessionRegistry(
            max_entries=2048,
            ttl_seconds=900,
        )
        self._registered: list[tuple[str, Callable[..., Any]]] = []
        self._framework: Any | None = None

    async def register(self, framework: Any) -> None:
        if self._registered:
            return
        pairs = (
            (LLMCallEvents.LLM_INVOKE_INPUT, self._on_llm_invoke_input, INPUT_PRIORITY),
            (LLMCallEvents.LLM_STREAM_INPUT, self._on_llm_stream_input, INPUT_PRIORITY),
            (
                LLMCallEvents.LLM_STREAM_OUTPUT,
                self._on_llm_stream_output,
                OUTPUT_PRIORITY,
            ),
            (
                LLMCallEvents.LLM_INVOKE_OUTPUT,
                self._on_llm_invoke_output,
                OUTPUT_PRIORITY,
            ),
            (LLMCallEvents.LLM_OUTPUT, self._on_llm_output, OUTPUT_PRIORITY),
            (LLMCallEvents.LLM_CALL_ERROR, self._on_llm_error, OUTPUT_PRIORITY),
            (ToolCallEvents.TOOL_CALL_STARTED, self._on_tool_input, INPUT_PRIORITY),
            (ToolCallEvents.TOOL_CALL_FINISHED, self._on_tool_output, OUTPUT_PRIORITY),
            (ToolCallEvents.TOOL_CALL_ERROR, self._on_tool_error, OUTPUT_PRIORITY),
            (AgentEvents.AGENT_INVOKE_INPUT, self._on_agent_input, INPUT_PRIORITY),
            (AgentEvents.AGENT_STREAM_INPUT, self._on_agent_input, INPUT_PRIORITY),
            (
                AgentEvents.AGENT_INVOKE_OUTPUT,
                self._on_agent_invoke_output,
                OUTPUT_PRIORITY,
            ),
            (
                AgentEvents.AGENT_STREAM_OUTPUT,
                self._on_agent_stream_output,
                OUTPUT_PRIORITY,
            ),
        )
        try:
            for event, callback, priority in pairs:
                await framework.register(
                    event,
                    callback,
                    priority=priority,
                    namespace=NAMESPACE,
                )
                self._registered.append((event, callback))
            self._framework = framework
        except BaseException:
            registered, self._registered = self._registered, []
            failed: set[tuple[str, int]] = set()
            for event, callback in reversed(registered):
                try:
                    await framework.unregister(event, callback)
                except BaseException:
                    failed.add((event, id(callback)))
            self._registered = [
                (event, callback)
                for event, callback in registered
                if (event, id(callback)) in failed
            ]
            self._framework = framework if self._registered else None
            raise

    async def unregister(self, framework: Any) -> None:
        if self._registered and framework is not self._framework:
            raise ValueError("cannot unregister from a different callback framework")
        registered = self._registered
        failed: set[tuple[str, int]] = set()
        first_error: BaseException | None = None
        for event, callback in reversed(registered):
            try:
                await framework.unregister(event, callback)
            except BaseException as error:
                failed.add((event, id(callback)))
                if first_error is None:
                    first_error = error
        self._registered = [
            (event, callback)
            for event, callback in registered
            if (event, id(callback)) in failed
        ]
        self._framework = framework if self._registered else None
        if first_error is not None:
            raise first_error

    @staticmethod
    def validate_core_ordering(framework: Any) -> bool:
        expected = {
            LLMCallEvents.LLM_INVOKE_INPUT: INPUT_PRIORITY,
            LLMCallEvents.LLM_STREAM_INPUT: INPUT_PRIORITY,
            LLMCallEvents.LLM_STREAM_OUTPUT: OUTPUT_PRIORITY,
            LLMCallEvents.LLM_INVOKE_OUTPUT: OUTPUT_PRIORITY,
            LLMCallEvents.LLM_OUTPUT: OUTPUT_PRIORITY,
            LLMCallEvents.LLM_CALL_ERROR: OUTPUT_PRIORITY,
            ToolCallEvents.TOOL_CALL_STARTED: INPUT_PRIORITY,
            ToolCallEvents.TOOL_CALL_FINISHED: OUTPUT_PRIORITY,
            ToolCallEvents.TOOL_CALL_ERROR: OUTPUT_PRIORITY,
            AgentEvents.AGENT_INVOKE_INPUT: INPUT_PRIORITY,
            AgentEvents.AGENT_STREAM_INPUT: INPUT_PRIORITY,
            AgentEvents.AGENT_INVOKE_OUTPUT: OUTPUT_PRIORITY,
            AgentEvents.AGENT_STREAM_OUTPUT: OUTPUT_PRIORITY,
        }
        for event, priority in expected.items():
            callbacks = framework.list_callbacks(event)
            if not any(
                item["namespace"] == CORE_NAMESPACE and item["priority"] == 0
                for item in callbacks
            ):
                return False
            if not any(
                item["namespace"] == NAMESPACE and item["priority"] == priority
                for item in callbacks
            ):
                return False
        return True

    async def _on_llm_invoke_input(self, *args: Any, **kwargs: Any) -> None:
        await self._enrich_llm_input(
            self._llm_input_payload(args, kwargs),
            streaming=False,
        )

    async def _on_llm_stream_input(self, *args: Any, **kwargs: Any) -> None:
        await self._enrich_llm_input(
            self._llm_input_payload(args, kwargs),
            streaming=True,
        )

    async def _on_llm_stream_output(self, *args: Any, **kwargs: Any) -> Any:
        del args
        result = kwargs.get("result")
        span = None
        try:
            span = get_current_llm_span()
            if span is not None and span.is_recording():
                span_key = self._span_key(span)
                state = self._span_state.get(span_key)
                core_state = getattr(span, "otel_llm_state", None)
                if state is not None and core_state is not None:
                    if core_state.first_chunk_ns is None:
                        core_state.first_chunk_ns = time.monotonic_ns()
                    if state.ttft_seconds is None:
                        state.ttft_seconds = max(
                            (core_state.first_chunk_ns - core_state.start_ns)
                            / 1_000_000_000,
                            0.0,
                        )
                        self._set_if_empty(span, GEN_AI_STREAMING_FIRST_TOKEN, True)
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(
                    "LLM telemetry stream output enrichment failed",
                    exc_info=True,
                )
                return result
            settled = await self._report_llm_control_error(
                error,
                kwargs,
                streaming=True,
            )
            if not settled:
                state = self._metric_state.finish(
                    "llm",
                    self._call_identity(kwargs),
                )
                if state is not None and state.span_key is not None:
                    self._span_state.pop(state.span_key)
                elif span is not None:
                    try:
                        self._span_state.pop(self._span_key(span))
                    except BaseException:
                        pass
            raise
        return result

    async def _on_llm_output(self, *args: Any, **kwargs: Any) -> Any:
        del args
        # AgentCore's extension runtime closes an LLM span from every terminal
        # ``LLM_OUTPUT`` event, including non-streaming calls. Enrich here at
        # our higher callback priority while the core-owned span is still live;
        # waiting for ``LLM_INVOKE_OUTPUT`` would observe an already-ended span.
        return await self._finish_llm_output(kwargs)

    async def _on_llm_invoke_output(self, *args: Any, **kwargs: Any) -> Any:
        del args
        return await self._finish_llm_output(kwargs)

    async def _finish_llm_output(self, kwargs: dict[str, Any]) -> Any:
        warning = "LLM telemetry output enrichment failed"
        result = kwargs.get("result")
        if result is None:
            result = kwargs.get("response")
        state = self._metric_state.finish("llm", self._call_identity(kwargs))
        span_state = None
        span = None
        pending_error: BaseException | None = None
        try:
            if state is not None and state.span_key is not None:
                span_state = self._span_state.pop(state.span_key)
            span = get_current_llm_span()
            if span is not None and span_state is None:
                span_state = self._span_state.pop(self._span_key(span))
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
            else:
                pending_error = error
        state = state or span_state

        observation = result
        usage = UsageBreakdown()
        try:
            observation = self._best_effort(
                warning,
                lambda: self._llm_output_observation(kwargs),
                default=result,
            )
            usage = self._best_effort(
                warning,
                lambda: extract_usage(kwargs.get("usage") or observation),
                default=UsageBreakdown(),
            )
            if span is not None and span.is_recording():
                self._enrich_llm_output_span(
                    span,
                    kwargs,
                    observation,
                    usage,
                    state,
                )
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
            elif pending_error is None:
                pending_error = error

        try:
            if state is not None:
                self._record_llm_metrics(state, usage)
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
            elif pending_error is None:
                pending_error = error
        if pending_error is not None:
            await self._report_llm_control_error(pending_error, kwargs)
            raise pending_error
        return result

    @staticmethod
    def _is_streaming_llm_output(kwargs: dict[str, Any]) -> bool:
        is_stream = kwargs.get("is_stream")
        if is_stream is not None:
            return is_stream is True
        try:
            span = get_current_llm_span()
            core_state = getattr(span, "otel_llm_state", None) if span else None
            return bool(getattr(core_state, "is_streaming", False))
        except Exception:
            _LOGGER.warning(
                "LLM telemetry output kind detection failed",
                exc_info=True,
            )
            return False

    def _enrich_llm_output_span(
        self,
        span: Span,
        kwargs: dict[str, Any],
        observation: Any,
        usage: UsageBreakdown,
        state: _CallState | None,
    ) -> None:
        warning = "LLM telemetry output enrichment failed"
        self._best_effort(warning, lambda: self._write_common_attributes(span))
        self._best_effort(
            warning,
            lambda: self._write_llm_type_attributes(span, kwargs, state),
        )
        decision_data = self._best_effort(
            warning,
            lambda: classify_decision(observation),
        )
        if decision_data is not None:
            decision, tool_names = decision_data
            self._best_effort(
                warning,
                lambda: self._set_if_empty(span, GEN_AI_DECISION_TYPE, decision),
            )
            self._best_effort(
                warning,
                lambda: self._set_if_empty(
                    span,
                    GEN_AI_DECISION_TOOL_NAMES,
                    tool_names,
                ),
            )
            self._best_effort(
                warning,
                lambda: self._set_if_empty(
                    span,
                    GEN_AI_DECISION_TOOL_COUNT,
                    len(tool_names),
                ),
            )
        if self._config.log_messages:
            output_value: Any = observation
            if self._config.redact_completions:
                output_value = self._best_effort(
                    warning,
                    lambda: self._redacted_llm_output(observation),
                )
            serialized_output = self._best_effort(
                warning,
                lambda: serialize_output_message(
                    output_value,
                    max_chars=self._config.attribute_value_max_length,
                ),
            )
            if serialized_output is not None:
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(
                        span,
                        GEN_AI_OUTPUT_MESSAGES,
                        serialized_output,
                    ),
                )
                self._best_effort(
                    warning,
                    lambda: span.add_event(
                        "gen_ai.assistant.message",
                        {"content": serialized_output},
                    ),
                )
        finish_reason = self._best_effort(
            warning,
            lambda: self._value(observation, "finish_reason"),
        )
        if finish_reason:
            finish_reason_value = self._best_effort(
                warning,
                lambda: str(finish_reason),
            )
            if finish_reason_value is not None:
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(
                        span,
                        GEN_AI_RESPONSE_FINISH_REASONS,
                        [finish_reason_value],
                    ),
                )
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(
                        span,
                        GEN_AI_RESPONSE_FINISH_REASON,
                        finish_reason_value,
                    ),
                )
        usage_attributes = (
            (GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens),
            (GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens),
            (GEN_AI_USAGE_TOTAL_TOKENS, usage.total_tokens),
            (
                GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
                usage.cache_read_input_tokens,
            ),
            (
                GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
                usage.cache_creation_input_tokens,
            ),
            (
                GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
                usage.reasoning_output_tokens,
            ),
        )
        if any(value for _, value in usage_attributes):
            for key, value in usage_attributes:
                self._best_effort(
                    warning,
                    lambda: span.set_attribute(key, value),
                )
        self._best_effort(
            warning,
            lambda: self._mirror_alias(
                span,
                GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
                GEN_AI_USAGE_CACHE_READ_TOKENS,
            ),
        )
        self._best_effort(
            warning,
            lambda: self._mirror_alias(
                span,
                GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
                GEN_AI_USAGE_CACHE_CREATION_TOKENS,
            ),
        )
        parent = self._best_effort(
            warning,
            lambda: self._span_registry.find_parent(span, name_prefix="agent."),
        )
        parent_recording = (
            self._best_effort(warning, parent.is_recording, default=False)
            if parent is not None
            else False
        )
        if parent_recording:
            for key, value in (
                (GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens),
                (GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens),
                (GEN_AI_USAGE_TOTAL_TOKENS, usage.total_tokens),
            ):
                self._best_effort(
                    warning,
                    lambda: self._add_number(parent, key, value),
                )
        if state is not None and state.context_tokens is not None:
            context_tokens = state.context_tokens
            for key, value in (
                (GEN_AI_CONTEXT_SYSTEM_PROMPT, context_tokens.system_prompt),
                (GEN_AI_CONTEXT_USER_MESSAGES, context_tokens.user_messages),
                (
                    GEN_AI_CONTEXT_ASSISTANT_MESSAGES,
                    context_tokens.assistant_messages,
                ),
                (GEN_AI_CONTEXT_TOOL_RESULTS, context_tokens.tool_results),
                (GEN_AI_CONTEXT_SKILL, context_tokens.skill),
                (
                    GEN_AI_CONTEXT_TOOL_DEFINITIONS,
                    context_tokens.tool_definitions,
                ),
            ):
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(span, key, value),
                )

    def _record_llm_metrics(
        self,
        state: _CallState,
        usage: UsageBreakdown,
        *,
        error: bool = False,
    ) -> None:
        duration_attributes = self._filtered_metric_attributes(
            {
                GEN_AI_REQUEST_MODEL: state.model,
                GEN_AI_SYSTEM: state.provider,
                JIUWENCLAW_CHANNEL_ID: state.channel_id,
            }
        )
        count_attributes = self._filtered_metric_attributes(
            {
                GEN_AI_REQUEST_MODEL: state.model,
                "status": "error" if error else "success",
                JIUWENCLAW_CHANNEL_ID: state.channel_id,
            }
        )
        self._metric_record(
            "gen_ai.client.operation.duration",
            max(time.monotonic() - state.started_at, 0.0),
            duration_attributes,
        )
        self._metric_add("gen_ai.client.operation.count", 1, count_attributes)
        for token_type, value in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cache_read_input_tokens),
            ("cache_creation", usage.cache_creation_input_tokens),
            ("reasoning", usage.reasoning_output_tokens),
        ):
            if value:
                self._metric_add(
                    "gen_ai.client.token.usage",
                    value,
                    {
                        **duration_attributes,
                        "gen_ai.token.type": token_type,
                    },
                )
        if state.ttft_seconds is not None:
            self._metric_record(
                "gen_ai.client.token.first_token_duration",
                state.ttft_seconds,
                duration_attributes,
            )

    async def _on_llm_error(self, *args: Any, **kwargs: Any) -> None:
        del args
        state = self._metric_state.finish("llm", self._call_identity(kwargs))
        try:
            span = get_current_llm_span()
            if span is not None:
                if state is not None and state.span_key is not None:
                    self._span_state.pop(state.span_key)
                else:
                    span_key = self._span_key(span)
                    state = state or self._span_state.get(span_key)
                    self._span_state.pop(span_key)
            error = kwargs.get("error") or kwargs.get("exception")
            if span is not None and span.is_recording():
                self._write_common_attributes(span)
                error_name = (
                    type(error).__name__
                    if isinstance(error, BaseException)
                    else "Error"
                )
                self._set_if_empty(span, ERROR_TYPE, error_name)
                if isinstance(error, asyncio.CancelledError):
                    self._set_if_empty(span, JIUWENCLAW_CANCELED, True)
                if isinstance(error, TimeoutError):
                    self._set_if_empty(span, "jiuwenclaw.timeout", True)
        except BaseException as error:
            if state is not None and state.span_key is not None:
                try:
                    self._span_state.pop(state.span_key)
                except BaseException:
                    pass
            if isinstance(error, Exception):
                _LOGGER.warning("LLM telemetry error enrichment failed", exc_info=True)
        try:
            if state is not None:
                self._record_llm_metrics(
                    state,
                    UsageBreakdown(),
                    error=True,
                )
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning("LLM telemetry error metrics failed", exc_info=True)

    async def _on_tool_input(self, *args: Any, **kwargs: Any) -> None:
        del args
        warning = "Tool telemetry input enrichment failed"
        state: _CallState | None = None
        identity = ""
        state_started = False
        try:
            tool_name = str(kwargs.get("tool_name") or "")
            state = _CallState(
                started_at=time.monotonic(),
                tool_name=tool_name,
                inputs=kwargs.get("inputs"),
                session_id=self._callback_label(
                    kwargs.get("inputs"),
                    kwargs,
                    "session_id",
                ),
            )
            state.channel_id = self._request_metric_channel()
            identity = self._call_identity(kwargs)
            self._metric_state.start("tool", identity, state)
            state_started = True
            state.skill = self._best_effort(
                warning,
                lambda: extract_skill(tool_name, kwargs.get("inputs"), None),
            )
            span = self._find_tool_span(kwargs)
            state.trace_id = self._valid_trace_id(span)
            state.session_id = (
                self._state_span_value(span, JIUWENCLAW_SESSION_ID)
                or state.session_id
            )
            state.channel_id = (
                self._state_span_value(span, JIUWENCLAW_CHANNEL_ID)
                or state.channel_id
            )
            state.agent_name = self._state_agent_name(span) or state.agent_name
            if span is None:
                return
            state.span_key = self._span_key(span)
            if not span.is_recording():
                return
            self._span_state.put(state.span_key, state)
            self._best_effort(warning, lambda: self._write_common_attributes(span))
            self._best_effort(
                warning,
                lambda: self._set_if_empty(span, GEN_AI_SPAN_TYPE, "tool"),
            )
            tool_id = kwargs.get("tool_id")
            self._best_effort(
                warning,
                lambda: self._set_if_empty(span, GEN_AI_TOOL_NAME, tool_name),
            )
            if tool_id not in (None, ""):
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(
                        span,
                        GEN_AI_TOOL_CALL_ID,
                        str(tool_id),
                    ),
                )
            self._best_effort(
                warning,
                lambda: self._write_skill_attributes(
                    span,
                    state.skill,
                    emit_events=False,
                ),
            )
            if self._config.log_messages:
                arguments = self._best_effort(
                    warning,
                    lambda: self._bounded_json(
                        kwargs.get("inputs"),
                        redact=self._config.redact_prompts,
                    ),
                )
                if arguments is not None:
                    self._best_effort(
                        warning,
                        lambda: self._set_if_empty(
                            span,
                            GEN_AI_TOOL_ARGUMENTS,
                            arguments,
                        ),
                    )
                    self._best_effort(
                        warning,
                        lambda: span.add_event(
                            "tool.arguments",
                            {"content": arguments},
                        ),
                    )
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
                return
            if state_started:
                self._metric_state.finish("tool", identity)
            if state is not None and state.span_key is not None:
                self._span_state.pop(state.span_key)
            raise

    async def _on_tool_output(self, *args: Any, **kwargs: Any) -> Any:
        del args
        warning = "Tool telemetry output enrichment failed"
        result_value = kwargs.get("result")
        tool_failed = self._value(result_value, "success") is False or self._value(
            result_value, "error"
        ) not in (None, "", False)
        state = self._metric_state.finish("tool", self._call_identity(kwargs))
        span_state = None
        span = None
        pending_error: BaseException | None = None
        try:
            if state is not None and state.span_key is not None:
                span_state = self._span_state.pop(state.span_key)
            span = self._resolve_tool_span(state, kwargs)
            if span is not None and span_state is None:
                span_state = self._span_state.pop(self._span_key(span))
        except BaseException as error:
            if state is not None and state.span_key is not None:
                self._span_state.pop(state.span_key)
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
            else:
                pending_error = error
        state = state or span_state
        skill = self._best_effort(
            warning,
            lambda: extract_skill(
                kwargs.get("tool_name"),
                kwargs.get("inputs")
                if kwargs.get("inputs") is not None
                else getattr(state, "inputs", None),
                result_value,
            ),
        )
        if state is not None and skill is not None:
            state.skill = skill
        try:
            if span is not None and span.is_recording():
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(span, GEN_AI_SPAN_TYPE, "tool"),
                )
                if tool_failed:
                    self._best_effort(
                        warning,
                        lambda: self._set_if_empty(span, ERROR_TYPE, "ToolError"),
                    )
                if self._config.log_messages and result_value is not None:
                    result = self._best_effort(
                        warning,
                        lambda: self._bounded_json(
                            result_value,
                            redact=self._config.redact_completions,
                        ),
                    )
                    if result is not None:
                        self._best_effort(
                            warning,
                            lambda: self._set_if_empty(
                                span,
                                GEN_AI_TOOL_RESULT,
                                result,
                            ),
                        )
                        self._best_effort(
                            warning,
                            lambda: span.add_event(
                                "tool.result",
                                {"content": result},
                            ),
                        )
                self._best_effort(
                    warning,
                    lambda: self._write_skill_attributes(span, skill),
                )
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
            elif pending_error is None:
                pending_error = error
        if state is not None:
            self._record_tool_metrics(state, kwargs, error=tool_failed)
        if pending_error is not None:
            raise pending_error
        return result_value

    async def _on_tool_error(self, *args: Any, **kwargs: Any) -> None:
        del args
        state = self._metric_state.finish("tool", self._call_identity(kwargs))
        span = self._resolve_tool_span(state, kwargs)
        if span is not None:
            state = state or self._span_state.get(self._span_key(span))
            self._span_state.pop(self._span_key(span))
        error = kwargs.get("error") or kwargs.get("exception")
        if span is not None and span.is_recording():
            error_name = (
                type(error).__name__ if isinstance(error, BaseException) else "Error"
            )
            self._set_if_empty(span, ERROR_TYPE, error_name)
            is_cancelled = isinstance(error, asyncio.CancelledError)
            if is_cancelled:
                self._set_if_empty(span, JIUWENCLAW_CANCELED, True)
            if isinstance(error, TimeoutError):
                self._set_if_empty(span, "jiuwenclaw.timeout", True)
        if state is not None:
            self._record_tool_metrics(state, kwargs, error=True)

    async def _on_agent_input(self, *args: Any, **kwargs: Any) -> None:
        warning = "Agent telemetry input enrichment failed"
        inputs = args[0] if args else kwargs.get("inputs")
        state = _CallState(
            started_at=time.monotonic(),
            channel_id=(
                self._callback_label(inputs, kwargs, "channel_id")
                or self._request_metric_channel()
            ),
            agent_name=self._callback_label(
                inputs,
                kwargs,
                "agent_name",
                "agent_id",
            ),
        )
        self._metric_state.start("agent", "", state)
        try:
            span = self._find_agent_span()
            if span is None or not span.is_recording():
                return
            state.span_key = self._span_key(span)
            self._span_state.put(state.span_key, state)
            self._best_effort(warning, lambda: self._write_common_attributes(span))
            self._best_effort(
                warning,
                lambda: self._set_if_empty(span, GEN_AI_SPAN_TYPE, "agent"),
            )
            self._best_effort(warning, lambda: self._write_agent_name(span, inputs))
            session = kwargs.get("session")
            session_id = self._best_effort(
                warning,
                lambda: self._value(inputs, "session_id"),
            )
            if not session_id and session is not None:
                getter = getattr(session, "get_session_id", None)
                if callable(getter):
                    session_id = self._best_effort(warning, getter)
            for key, name in (
                (GEN_AI_CONVERSATION_ID, "conversation_id"),
                (JIUWENCLAW_REQUEST_ID, "request_id"),
                (JIUWENCLAW_CHANNEL_ID, "channel_id"),
                (JIUWENCLAW_AGENT_PARENT, "parent_session_id"),
                (JIUWENCLAW_ITERATION, "iteration"),
                ("jiuwenclaw.agent.mode", "mode"),
            ):
                value = self._best_effort(
                    warning,
                    lambda: self._value(inputs, name),
                )
                if value not in (None, ""):
                    self._best_effort(
                        warning,
                        lambda: self._set_if_empty(span, key, value),
                    )
            state.channel_id = (
                self._state_span_value(span, JIUWENCLAW_CHANNEL_ID)
                or state.channel_id
            )
            state.agent_name = self._state_agent_name(span) or state.agent_name
            if session_id not in (None, ""):
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(
                        span,
                        JIUWENCLAW_SESSION_ID,
                        session_id,
                    ),
                )
            query = self._best_effort(
                warning,
                lambda: (
                    self._value(inputs, "query") or self._value(inputs, "user_input")
                ),
            )
            if query and self._config.log_messages:
                prompt = "[REDACTED]" if self._config.redact_prompts else str(query)
                serialized_prompt = self._best_effort(
                    warning,
                    lambda: serialize_input_messages(
                        [{"role": "user", "content": prompt}],
                        max_chars=self._config.attribute_value_max_length,
                    ),
                )
                if serialized_prompt is not None:
                    self._best_effort(
                        warning,
                        lambda: self._set_if_empty(
                            span,
                            GEN_AI_INPUT_MESSAGES,
                            serialized_prompt,
                        ),
                    )
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
                return
            self._metric_state.finish("agent", "")
            if state.span_key is not None:
                self._span_state.pop(state.span_key)
            raise

    async def _on_agent_invoke_output(self, *args: Any, **kwargs: Any) -> Any:
        del args
        return await self._finish_agent_output(kwargs)

    async def _on_agent_stream_output(self, *args: Any, **kwargs: Any) -> Any:
        del args
        result = kwargs.get("result")
        try:
            if not bool(self._value(result, "last_chunk")):
                return result
        except BaseException as error:
            state = self._metric_state.finish("agent", "")
            if state is not None and state.span_key is not None:
                self._span_state.pop(state.span_key)
            if isinstance(error, Exception):
                _LOGGER.warning(
                    "Agent telemetry stream output enrichment failed",
                    exc_info=True,
                )
                return result
            raise
        return await self._finish_agent_output(kwargs)

    async def _finish_agent_output(self, kwargs: dict[str, Any]) -> Any:
        result = kwargs.get("result")
        state = self._metric_state.finish("agent", "")
        span_state = None
        pending_error: BaseException | None = None
        try:
            span = None
            if state is not None and state.span_key is not None:
                span_state = self._span_state.pop(state.span_key)
                span = self._span_registry.find(*state.span_key)
            if span is None:
                span = self._find_agent_span()
            if span is not None and span_state is None:
                span_state = self._span_state.pop(self._span_key(span))
            state = state or span_state
        except BaseException as error:
            if state is not None and state.span_key is not None:
                self._span_state.pop(state.span_key)
            if isinstance(error, Exception):
                _LOGGER.warning(
                    "Agent telemetry output enrichment failed",
                    exc_info=True,
                )
            else:
                pending_error = error
        state = state or span_state
        if state is not None:
            self._metric_record(
                "jiuwenclaw.agent.duration",
                max(time.monotonic() - state.started_at, 0.0),
                self._filtered_metric_attributes(
                    {
                        JIUWENCLAW_AGENT_NAME: state.agent_name,
                        JIUWENCLAW_CHANNEL_ID: state.channel_id,
                    }
                ),
            )
        if pending_error is not None:
            raise pending_error
        return result

    async def _enrich_llm_input(
        self, kwargs: dict[str, Any], *, streaming: bool
    ) -> None:
        warning = "LLM telemetry input enrichment failed"
        started_at = time.monotonic()
        model = self._llm_model(kwargs)
        span = self._best_effort(warning, get_current_llm_span)
        provider = self._llm_provider(kwargs) or self._normalized_provider(
            self._state_span_value(span, "gen_ai.provider.name")
        )
        state = _CallState(
            started_at=started_at,
            model=model,
            provider=provider or "unknown",
            channel_id=self._state_span_value(span, JIUWENCLAW_CHANNEL_ID)
            or self._request_metric_channel(),
            agent_name=self._state_agent_name(span),
        )
        identity = self._call_identity(kwargs)
        self._metric_state.start("llm", identity, state)
        messages = kwargs.get("messages") or []
        tools = kwargs.get("tools") or []
        try:
            try:
                timeout = float(
                    getattr(self._config, "token_count_timeout_seconds", 0.5)
                )
                token_task = asyncio.get_running_loop().run_in_executor(
                    _TOKEN_COUNT_POOL,
                    count_context_tokens,
                    messages,
                    tools,
                )
                try:
                    state.context_tokens = await asyncio.wait_for(
                        token_task, timeout=timeout
                    )
                except BaseException:
                    token_task.cancel()
                    raise
            except Exception:
                _LOGGER.warning(warning, exc_info=True)
                state.context_tokens = None
            if state.context_tokens is not None:
                for tool_name, tokens in state.context_tokens.per_tool_tokens:
                    if tokens:
                        self._metric_add(
                            "gen_ai.tool.token.usage",
                            tokens,
                            self._filtered_metric_attributes(
                                {
                                    GEN_AI_TOOL_NAME: tool_name,
                                    GEN_AI_REQUEST_MODEL: state.model,
                                    JIUWENCLAW_CHANNEL_ID: state.channel_id,
                                }
                            ),
                        )
                for skill_name, tokens in state.context_tokens.per_skill_tokens:
                    if tokens:
                        self._metric_add(
                            "gen_ai.skill.token.usage",
                            tokens,
                            self._filtered_metric_attributes(
                                {
                                    GEN_AI_SKILL_NAME: skill_name,
                                    GEN_AI_REQUEST_MODEL: state.model,
                                    JIUWENCLAW_CHANNEL_ID: state.channel_id,
                                }
                            ),
                        )
            if span is None or not span.is_recording():
                return
            state.span_key = self._span_key(span)
            self._span_state.put(state.span_key, state)
            self._best_effort(warning, lambda: self._write_common_attributes(span))
            self._best_effort(
                warning,
                lambda: self._write_llm_type_attributes(span, kwargs, state),
            )
            message_items = self._best_effort(
                warning,
                lambda: list(messages),
                default=[],
            )
            if model:
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(span, GEN_AI_REQUEST_MODEL, model),
                )
            self._best_effort(
                warning,
                lambda: self._set_if_empty(
                    span,
                    GEN_AI_REQUEST_STREAMING,
                    streaming,
                ),
            )
            for name, key in (
                ("temperature", GEN_AI_REQUEST_TEMPERATURE),
                ("top_p", GEN_AI_REQUEST_TOP_P),
            ):
                source = self._best_effort(
                    warning,
                    partial(self._llm_config_value, kwargs, name),
                )
                if source is not None:
                    self._best_effort(
                        warning,
                        partial(self._set_if_empty, span, key, source),
                    )
            self._best_effort(
                warning,
                lambda: self._set_if_empty(
                    span,
                    GEN_AI_INPUT_MESSAGES_COUNT,
                    len(message_items),
                ),
            )
            total_length = self._best_effort(
                warning,
                lambda: sum(len(message_content(message)) for message in message_items),
            )
            if total_length is not None:
                self._best_effort(
                    warning,
                    lambda: self._set_if_empty(
                        span,
                        GEN_AI_INPUT_MESSAGES_TOTAL_LENGTH,
                        total_length,
                    ),
                )
            if not self._config.log_messages:
                return
            max_chars = self._config.attribute_value_max_length
            serializable_messages: list[Any] | None = message_items
            if self._config.redact_prompts:
                try:
                    serializable_messages = await asyncio.to_thread(
                        lambda: [
                            {"role": message_role(message), "content": "[REDACTED]"}
                            for message in message_items
                        ]
                    )
                except Exception:
                    _LOGGER.warning(warning, exc_info=True)
                    serializable_messages = None
            if serializable_messages is not None:
                try:
                    serialized_messages = await asyncio.to_thread(
                        serialize_input_messages,
                        serializable_messages,
                        max_chars=max_chars,
                    )
                except Exception:
                    _LOGGER.warning(warning, exc_info=True)
                    serialized_messages = None
                if serialized_messages is not None:
                    self._best_effort(
                        warning,
                        lambda: self._set_if_empty(
                            span,
                            GEN_AI_INPUT_MESSAGES,
                            serialized_messages,
                        ),
                    )
            if tools:
                try:
                    tool_definitions = await asyncio.to_thread(
                        lambda: (
                            "[REDACTED]"
                            if self._config.redact_prompts
                            else serialize_tool_definitions(
                                tools, max_chars=max_chars
                            )
                        )
                    )
                except Exception:
                    _LOGGER.warning(warning, exc_info=True)
                    tool_definitions = None
                if tool_definitions is not None:
                    self._best_effort(
                        warning,
                        lambda: self._set_if_empty(
                            span,
                            GEN_AI_TOOL_DEFINITIONS,
                            tool_definitions,
                        ),
                    )
            for message in serializable_messages or []:
                self._best_effort(
                    warning,
                    partial(self._add_input_message_event, span, message),
                )
        except BaseException as error:
            if isinstance(error, Exception):
                _LOGGER.warning(warning, exc_info=True)
                return
            settled = await self._report_llm_control_error(
                error,
                kwargs,
                streaming=streaming,
            )
            if not settled:
                self._metric_state.finish("llm", identity)
                if state.span_key is not None:
                    self._span_state.pop(state.span_key)
            raise

    async def _report_llm_control_error(
        self,
        error: BaseException,
        kwargs: dict[str, Any],
        *,
        streaming: bool | None = None,
    ) -> bool:
        """Settle Rich state, then ask the Core owner to close its LLM span."""
        payload: dict[str, Any] = {}
        for key in (
            "call_id",
            "model",
            "model_name",
            "model_provider",
            "model_config",
            "model_client_config",
            "is_stream",
        ):
            if key in kwargs:
                payload[key] = kwargs[key]
        if streaming is not None:
            payload["is_stream"] = streaming
        payload["error"] = error
        try:
            await self._on_llm_error(**payload)
        except BaseException as cleanup_error:
            if isinstance(cleanup_error, Exception):
                _LOGGER.warning(
                    "LLM telemetry control-error state cleanup failed",
                    exc_info=True,
                )
        try:
            return abort_current_llm_span(error)
        except BaseException as cleanup_error:
            if isinstance(cleanup_error, Exception):
                _LOGGER.warning(
                    "LLM telemetry owner cleanup failed",
                    exc_info=True,
                )
            return False

    def _add_input_message_event(self, span: Span, message: Any) -> None:
        role = message_role(message)
        event_role = role if role in {"system", "user", "assistant", "tool"} else "user"
        span.add_event(
            f"gen_ai.{event_role}.message",
            {
                "content": message_content(message)[
                    : self._config.attribute_value_max_length
                ]
            },
        )

    @staticmethod
    def _llm_input_payload(
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(kwargs)
        if args and payload.get("messages") is None:
            payload["messages"] = args[0]
        return payload

    @staticmethod
    def _llm_output_observation(kwargs: dict[str, Any]) -> Any:
        result = kwargs.get("result")
        if result is not None:
            return result
        response = kwargs.get("response")
        if response is not None and not isinstance(response, (str, bytes)):
            if not any(
                name in kwargs
                for name in ("reasoning_content", "tool_calls", "finish_reason")
            ):
                return response
        return {
            "content": response,
            "reasoning_content": kwargs.get("reasoning_content"),
            "tool_calls": kwargs.get("tool_calls"),
            "finish_reason": kwargs.get("finish_reason"),
            "usage_metadata": kwargs.get("usage"),
        }

    def _llm_config_value(self, kwargs: dict[str, Any], name: str) -> Any:
        direct = kwargs.get(name)
        if direct is not None:
            return direct
        return self._value(kwargs.get("model_config"), name)

    def _llm_model(self, kwargs: dict[str, Any]) -> str:
        value = kwargs.get("model") or kwargs.get("model_name")
        if not value:
            model_config = kwargs.get("model_config")
            value = self._value(model_config, "model") or self._value(
                model_config,
                "model_name",
            )
        try:
            return str(value or "")
        except Exception:
            return ""

    def _llm_provider(self, kwargs: dict[str, Any]) -> str:
        for source in (
            kwargs,
            kwargs.get("model_client_config"),
            kwargs.get("model_config"),
        ):
            for name in ("model_provider", "client_provider", "provider"):
                value = self._value(source, name)
                provider = self._normalized_provider(value)
                if provider:
                    return provider
        return ""

    def _state_span_value(self, span: Span | None, key: str) -> str:
        current = span
        value = self._span_attribute(current, key)
        if value:
            return value
        parent = None
        if current is not None:
            try:
                parent = self._span_registry.find_parent(
                    current,
                    name_prefix="agent.",
                )
            except (AttributeError, TypeError, ValueError):
                parent = None
        if parent is None:
            try:
                candidate = get_current_agent_span()
            except Exception:
                candidate = None
            if candidate is not current:
                parent = candidate
        return self._span_attribute(parent, key)

    def _state_agent_name(self, span: Span | None) -> str:
        return self._state_span_value(
            span,
            JIUWENCLAW_AGENT_NAME,
        ) or self._state_span_value(span, GEN_AI_AGENT_NAME)

    @staticmethod
    def _request_metric_channel() -> str:
        try:
            return str(metrics_channel_id.get() or "")
        except Exception:
            return ""

    @classmethod
    def _callback_label(
        cls,
        inputs: Any,
        kwargs: Mapping[str, Any],
        *names: str,
    ) -> str:
        for source in (inputs, kwargs):
            for name in names:
                value = cls._value(source, name)
                if value in (None, ""):
                    continue
                try:
                    return str(value)
                except Exception as error:
                    _LOGGER.debug(
                        "[TelemetryEnrichment] callback label conversion failed: %s",
                        error,
                    )
        return ""

    @staticmethod
    def _span_attribute(span: Span | None, key: str) -> str:
        attributes = getattr(span, "attributes", None) or {}
        try:
            value = attributes.get(key)
        except Exception:
            return ""
        if value in (None, ""):
            return ""
        try:
            return str(value)
        except Exception:
            return ""

    @classmethod
    def _normalized_provider(cls, value: Any) -> str:
        if hasattr(value, "value"):
            value = cls._value(value, "value")
        try:
            return str(value or "").strip().lower()
        except Exception:
            return ""

    def _write_llm_type_attributes(
        self,
        span: Span,
        kwargs: dict[str, Any],
        state: _CallState | None,
    ) -> None:
        self._set_if_empty(span, GEN_AI_SPAN_TYPE, "model")
        self._set_if_empty(span, GEN_AI_OPERATION_NAME, "chat")
        provider = (
            state.provider
            if state is not None
            else self._llm_provider(kwargs)
            or self._normalized_provider(
                self._state_span_value(span, "gen_ai.provider.name")
            )
            or "unknown"
        )
        self._set_if_empty(span, GEN_AI_SYSTEM, provider)
        response_model = kwargs.get("model_name")
        is_output = "response" in kwargs or "result" in kwargs
        if not response_model and is_output and state is not None:
            response_model = state.model
        if response_model:
            self._set_if_empty(span, GEN_AI_RESPONSE_MODEL, str(response_model))

    def _redacted_llm_output(self, observation: Any) -> dict[str, Any]:
        raw_tool_calls = self._value(observation, "tool_calls")
        if raw_tool_calls is None:
            tool_calls: list[Any] = []
        elif isinstance(raw_tool_calls, (str, bytes, Mapping)):
            tool_calls = [raw_tool_calls]
        else:
            try:
                tool_calls = list(raw_tool_calls)
            except Exception:
                tool_calls = []
        redacted_calls: list[dict[str, Any]] = []
        for call in tool_calls:
            function = self._value(call, "function")
            name = self._value(call, "name") or self._value(function, "name")
            entry: dict[str, Any] = {
                "id": self._value(call, "id") or "",
                "name": name or "",
            }
            arguments = self._value(call, "arguments")
            if arguments is None:
                arguments = self._value(function, "arguments")
            if arguments not in (None, ""):
                entry["arguments"] = "[REDACTED]"
            redacted_calls.append(entry)
        reasoning = self._value(observation, "reasoning_content")
        return {
            "content": "[REDACTED]",
            "tool_calls": redacted_calls,
            "reasoning_content": "[REDACTED]" if reasoning else None,
        }

    def _write_skill_attributes(
        self,
        span: Span,
        skill: SkillObservation | None,
        *,
        emit_events: bool = True,
    ) -> None:
        if skill is None:
            return
        self._set_if_empty(span, GEN_AI_SKILL_NAME, skill.name)
        self._set_if_empty(span, GEN_AI_SKILL_ID, skill.skill_id)
        if skill.version:
            self._set_if_empty(span, GEN_AI_SKILL_VERSION, skill.version)
        if skill.loaded:
            self._set_if_empty(span, GEN_AI_OPERATION_NAME, "load_skill")
            if emit_events:
                span.add_event(
                    "skill.loaded",
                    {"skill.name": skill.name, "skill.id": skill.skill_id},
                )
        if skill.released:
            self._set_if_empty(span, GEN_AI_OPERATION_NAME, "release_skill")
            if emit_events:
                span.add_event(
                    "skill.released",
                    {"skill.name": skill.name, "skill.id": skill.skill_id},
                )

    def _write_agent_name(self, span: Span, inputs: Any) -> None:
        attributes = span.attributes or {}
        name = (
            self._value(inputs, "agent_name")
            or self._value(inputs, "agent_id")
            or attributes.get("agentteam.member.name")
            or attributes.get("agentteam.agent.id")
        )
        if not name:
            parts = str(getattr(span, "name", "")).split(".")
            if len(parts) > 2 and parts[0] == "agent":
                name = parts[1]
        self._sync_primary_alias(
            span,
            GEN_AI_AGENT_NAME,
            JIUWENCLAW_AGENT_NAME,
            name,
        )

    @staticmethod
    def _span_key(span: Span) -> tuple[int, int]:
        context = span.get_span_context()
        return context.trace_id, context.span_id

    @staticmethod
    def _valid_trace_id(span: Span | None) -> int:
        if span is None:
            return 0
        try:
            context = span.get_span_context()
            return context.trace_id if context.is_valid else 0
        except Exception:
            return 0

    @staticmethod
    def _call_identity(kwargs: dict[str, Any]) -> str:
        value = kwargs.get("call_id") or kwargs.get("tool_id")
        return str(value).strip() if value not in (None, "") else ""

    def _metric_add(
        self, name: str, value: int | float, attributes: dict[str, Any]
    ) -> None:
        try:
            self._metrics.add(name, value, attributes)
        except Exception:
            _LOGGER.debug("Telemetry metric add failed", exc_info=True)

    @staticmethod
    def _best_effort(
        warning: str,
        operation: Callable[[], Any],
        *,
        default: Any = None,
    ) -> Any:
        try:
            return operation()
        except Exception:
            _LOGGER.warning(warning, exc_info=True)
            return default

    def _metric_record(
        self, name: str, value: int | float, attributes: dict[str, Any]
    ) -> None:
        try:
            self._metrics.record(name, value, attributes)
        except Exception:
            _LOGGER.debug("Telemetry metric record failed", exc_info=True)

    def _record_tool_metrics(
        self, state: _CallState, kwargs: dict[str, Any], *, error: bool
    ) -> None:
        attributes = self._filtered_metric_attributes(
            {
                GEN_AI_TOOL_NAME: state.tool_name,
                JIUWENCLAW_CHANNEL_ID: state.channel_id,
            }
        )
        duration = max(time.monotonic() - state.started_at, 0.0)
        self._metric_add("gen_ai.tool.call.count", 1, attributes)
        self._metric_record("gen_ai.tool.duration", duration, attributes)
        if error:
            self._metric_add("gen_ai.tool.error.count", 1, attributes)
        self._record_skill_metrics(state, error=error)

    def _record_skill_metrics(self, state: _CallState, *, error: bool) -> None:
        skill = state.skill
        if skill is None:
            return
        kind = self._skill_tool_kind(state.tool_name)
        if kind == "skill_tool":
            skill_attributes = self._skill_metric_attributes(
                skill.name,
                version=skill.version,
                channel_id=state.channel_id,
            )
            self._metric_add("gen_ai.skill.call.count", 1, skill_attributes)
            if error:
                self._metric_add("gen_ai.skill.error.count", 1, skill_attributes)
                return
            if skill.loaded:
                self._skill_sessions.start(
                    state.trace_id,
                    state.session_id,
                    skill.name,
                    _SkillSession(
                        started_at=state.started_at,
                        version=skill.version,
                        channel_id=state.channel_id,
                    ),
                )
            return
        if kind != "skill_complete":
            return

        activation = self._skill_sessions.finish(
            state.trace_id,
            state.session_id,
            skill.name,
        )
        skill_attributes = self._skill_metric_attributes(
            skill.name,
            version=activation.version if activation is not None else skill.version,
            channel_id=(
                activation.channel_id if activation is not None else state.channel_id
            ),
        )
        if error:
            self._metric_add("gen_ai.skill.error.count", 1, skill_attributes)
        elif activation is not None:
            self._metric_record(
                "gen_ai.skill.duration",
                max(time.monotonic() - activation.started_at, 0.0),
                skill_attributes,
            )

    @staticmethod
    def _skill_tool_kind(tool_name: str) -> str:
        normalized = tool_name.strip().lower()
        for separator in ("/", ":", "."):
            normalized = normalized.rsplit(separator, 1)[-1]
        return normalized if normalized in {"skill_tool", "skill_complete"} else ""

    def _skill_metric_attributes(
        self,
        skill_name: str,
        *,
        version: str,
        channel_id: str,
    ) -> dict[str, Any]:
        return self._filtered_metric_attributes(
            {
                GEN_AI_SKILL_NAME: skill_name,
                GEN_AI_SKILL_VERSION: version,
                GEN_AI_SYSTEM: "jiuwenclaw",
                JIUWENCLAW_CHANNEL_ID: channel_id,
            }
        )

    @staticmethod
    def _filtered_metric_attributes(
        attributes: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            key: value
            for key, value in attributes.items()
            if value is not None and value != ""
        }

    @staticmethod
    def _set_if_empty(span: Span, key: str, value: Any) -> None:
        existing = span.attributes.get(key) if span.attributes is not None else None
        if existing not in (None, "", (), []):
            return
        try:
            span.set_attribute(key, value)
        except (TypeError, ValueError):
            return

    @staticmethod
    def _mirror_alias(span: Span, primary: str, alias: str) -> None:
        value = span.attributes.get(primary) if span.attributes is not None else None
        if value in (None, "", (), []):
            return
        try:
            span.set_attribute(alias, value)
        except (TypeError, ValueError):
            return

    def _sync_primary_alias(
        self,
        span: Span,
        primary: str,
        alias: str,
        candidate: Any,
    ) -> None:
        current = span.attributes.get(primary) if span.attributes is not None else None
        if current in (None, "", (), []):
            if candidate in (None, "", (), []):
                return
            self._set_if_empty(span, primary, candidate)
        self._mirror_alias(span, primary, alias)

    @staticmethod
    def _add_number(span: Span, key: str, value: int | float) -> None:
        if not value:
            return
        current = span.attributes.get(key, 0) if span.attributes is not None else 0
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            current = 0
        try:
            span.set_attribute(key, current + value)
        except (TypeError, ValueError):
            return

    def _write_common_attributes(self, span: Span) -> None:
        resource = getattr(span, "resource", None)
        resource_attributes = getattr(resource, "attributes", {})
        claw_id = self._config.claw_id or resource_attributes.get(JIUWENCLAW_CLAW_ID)
        if claw_id:
            self._set_if_empty(span, JIUWENCLAW_CLAW_ID, claw_id)
        service_version = resource_attributes.get("service.version")
        if service_version:
            self._set_if_empty(span, "service.version", service_version)
        try:
            identity = IdentityStore.get_identity()
        except Exception:
            identity = None
        for primary, alias, value in (
            (USER_ID, JIUWENCLAW_USER_ID, self._value(identity, "user_id")),
            (DOMAIN_ID, JIUWENCLAW_DOMAIN_ID, self._value(identity, "domain_id")),
            (APP_ID, JIUWENCLAW_APP_ID, self._value(identity, "app_id")),
        ):
            self._sync_primary_alias(span, primary, alias, value)

    def _find_agent_span(self) -> Span | None:
        current = trace.get_current_span()
        context = current.get_span_context()
        if not context.is_valid:
            return None
        if current.is_recording() and current.name.startswith("agent."):
            return current
        return self._span_registry.find_latest(context.trace_id, name_prefix="agent.")

    @staticmethod
    def _value(source: Any, name: str) -> Any:
        if isinstance(source, Mapping):
            try:
                return source.get(name)
            except Exception:
                return None
        try:
            return getattr(source, name, None)
        except Exception:
            return None

    def _find_tool_span(self, kwargs: dict[str, Any]) -> Span | None:
        current = trace.get_current_span()
        current_context = current.get_span_context()
        if not current_context.is_valid:
            for parent in (get_current_agent_span(), get_team_span()):
                if parent is None:
                    continue
                parent_context = parent.get_span_context()
                if parent_context.is_valid:
                    current_context = parent_context
                    break
            else:
                return None
        tool_name = str(kwargs.get("tool_name") or "unknown")
        return self._span_registry.find_latest(
            current_context.trace_id,
            name=f"tool.{tool_name}",
        )

    def _resolve_tool_span(
        self,
        state: _CallState | None,
        kwargs: dict[str, Any],
    ) -> Span | None:
        if state is not None and state.span_key is not None:
            span = self._span_registry.find(*state.span_key)
            if span is not None:
                return span
        return self._find_tool_span(kwargs)

    @staticmethod
    def _bounded_json(value: Any, *, redact: bool) -> str:
        if redact:
            return "[REDACTED]"
        try:
            serialized = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = str(value)
        return serialized[:4096]


__all__ = [
    "EnrichmentStateRegistry",
    "INPUT_PRIORITY",
    "NAMESPACE",
    "OUTPUT_PRIORITY",
    "MetricCallStateRegistry",
    "RichTelemetryCallbacks",
    "SkillSessionRegistry",
]
