# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mid-turn compaction processors for Code mode tool results.

agent-core already ships :class:`ToolResultWindowProcessor`: a sliding window
that keeps only the newest ``keep_last_k`` results of selected tools in the
model window and offloads the rest to the session offload directory, leaving a
``<persisted-output>`` block with a raw ``trim_size``-character preview.

These subclasses reuse that machinery unchanged and replace only the
*placeholder text* with a one-line summary produced by
:func:`~jiuwenswarm.agents.harness.code.context_diet.trim.summarize_tool_result`
(tool, key argument, key conclusion) plus the ``[[OFFLOAD: ...]]`` handle the
model can use to reload the full text. Two processor types exist so that shell
/ search results and file reads can have independent windows:

* :class:`ContextDietShellWindowProcessor` — ``bash`` / ``grep`` / ``glob``. Shell
  results that print file contents or diffs (see ``source_print.py``) stay out of
  this window when ``exempt_source_prints`` is on, like ``read_file`` results.
* :class:`ContextDietReadWindowProcessor` — ``read_file`` (off by default).

They are registered with the engine at import time and are added to the
``ContextProcessorRail`` by the Code adapter when
``modes.code.context_diet.compaction.enabled`` is true. Edit / write results are
never in ``tool_names`` and therefore never touched.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.context_engine.context.context_utils import ContextUtils
from openjiuwen.core.context_engine.processor.offloader.tool_result_window_processor import (
    WINDOWED_OUTPUT_CLOSING_TAG,
    WINDOWED_OUTPUT_TAG,
    ToolResultWindowProcessor,
    ToolResultWindowProcessorConfig,
)
from openjiuwen.core.foundation.llm.schema.message import BaseMessage, ToolMessage
from pydantic import Field

from jiuwenswarm.agents.harness.code.context_diet.config import CompactionConfig
from jiuwenswarm.agents.harness.code.context_diet.trim import (
    MARKER,
    classify_tool,
    shell_source_print_reason,
    summarize_tool_result,
)

logger = logging.getLogger(__name__)

_WINDOW_MANAGED_METADATA_KEY = "tool_result_window_managed"
_SOURCE_PRINT_CACHE_MAX = 10_000
_SUMMARY_METADATA_KEY = "context_diet_summary"

_CURRENT_MESSAGES: contextvars.ContextVar[tuple[BaseMessage, ...]] = contextvars.ContextVar(
    "context_diet_current_messages", default=()
)
"""Full message list of the ``on_add_messages`` call in progress.

The SDK loop calls ``_compact_small_result(message)`` / ``_offload_tool_message(message, context)``
without the surrounding messages, but the one-line summary needs the tool call
(name + arguments) that produced each result. A ``ContextVar`` scopes that list
to the current asyncio task, so processor instances shared between interleaved
contexts never see each other's messages.
"""

_fallback_warned = False
"""Set after the first time the SDK-private offload path failed and the base placeholder was used."""


class ContextDietWindowProcessorConfig(ToolResultWindowProcessorConfig):
    """Window config plus the size of the one-line summary placeholder."""

    summary_chars: int = Field(
        default=240,
        ge=80,
        le=2000,
        description="Maximum characters of the one-line summary that replaces an old tool result.",
    )
    exempt_source_prints: bool = Field(
        default=False,
        description=(
            "Leave shell results that print file contents or diffs (sed -n, cat, git diff…) out of the "
            "window; they are edit sources, like read_file."
        ),
    )


class _ContextDietWindowBase(ToolResultWindowProcessor):
    """Shared placeholder rendering; concrete subclasses only differ by registered type."""

    @property
    def config(self) -> ContextDietWindowProcessorConfig:  # type: ignore[override]
        return self._config

    async def on_add_messages(self, context: Any, messages_to_add: list[BaseMessage], **kwargs: Any):
        # Expose the full message list to the placeholder builders for the
        # duration of this call only (task-scoped, see _CURRENT_MESSAGES).
        token = _CURRENT_MESSAGES.set(tuple(context.get_messages()) + tuple(messages_to_add))
        try:
            return await super().on_add_messages(context, messages_to_add, **kwargs)
        finally:
            _CURRENT_MESSAGES.reset(token)

    # ── which results the window manages ─────────────────────

    def _is_target_tool_result(self, message: BaseMessage, context_messages: list[BaseMessage], allowlist: set[str]):
        if not super()._is_target_tool_result(message, context_messages, allowlist):
            return False
        if not self.config.exempt_source_prints:
            return True
        return not self._is_source_print(message, context_messages)

    def _is_source_print(self, message: BaseMessage, context_messages: list[BaseMessage]) -> bool:
        call_id = str(getattr(message, "tool_call_id", "") or "")
        cache: dict[str, bool] | None = getattr(self, "_source_print_cache", None)
        if cache is None or len(cache) > _SOURCE_PRINT_CACHE_MAX:
            cache = {}
            self._source_print_cache = cache
        if call_id and call_id in cache:
            return cache[call_id]
        try:
            tool_call = ContextUtils.resolve_tool_call_from_message(message, context_messages)
            tool_name = ContextUtils.extract_tool_name(tool_call) if tool_call else getattr(message, "name", None)
            is_source = classify_tool(tool_name) == "shell" and bool(
                shell_source_print_reason(self._extract_tool_args(tool_call) if tool_call else {})
            )
        except Exception:  # noqa: BLE001 - classification must never break compaction
            is_source = False
        if call_id:
            cache[call_id] = is_source
        return is_source

    # ── placeholder rendering ────────────────────────────────

    def _summary_for(self, message: ToolMessage) -> str:
        messages = list(_CURRENT_MESSAGES.get())
        tool_call = ContextUtils.resolve_tool_call_from_message(message, messages) if messages else None
        tool_name = ContextUtils.extract_tool_name(tool_call) if tool_call else getattr(message, "name", None)
        tool_args = self._extract_tool_args(tool_call) if tool_call else {}
        content = message.content if isinstance(message.content, str) else ""
        return summarize_tool_result(tool_name, tool_args, content, max_chars=self.config.summary_chars)

    def _compact_small_result(self, message: ToolMessage) -> ToolMessage:
        content = message.content
        if not isinstance(content, str):
            return message
        metadata = dict(getattr(message, "metadata", {}) or {})
        metadata[_WINDOW_MANAGED_METADATA_KEY] = True
        summary = self._summary_for(message)
        metadata[_SUMMARY_METADATA_KEY] = summary
        compacted = (
            f"{WINDOWED_OUTPUT_TAG}\n"
            f"{MARKER} older result compacted ({len(content)} chars, not persisted): {summary}\n"
            f"{WINDOWED_OUTPUT_CLOSING_TAG}"
        )
        return message.model_copy(update={"content": compacted, "metadata": metadata})

    async def _offload_tool_message(self, message: ToolMessage, context: Any) -> ToolMessage:
        """Offload like the base class but with a one-line summary placeholder.

        The body uses agent-core internals (``_new_offload_handle_and_path``,
        ``offload_messages`` keyword set, ``ContextUtils`` helpers). If any of
        them changes shape, fall back to the base implementation — the result
        is still offloaded, only the placeholder is the SDK's raw preview — and
        warn once per process instead of failing the turn.
        """
        global _fallback_warned
        content = message.content
        if not isinstance(content, str):
            return message
        try:
            summary = self._summary_for(message)
            offload_handle, offload_path = self._new_offload_handle_and_path(context)
            placeholder = self._build_summary_placeholder(len(content), "pending", summary)
            metadata = dict(getattr(message, "metadata", {}) or {})
            metadata[_SUMMARY_METADATA_KEY] = summary
            offload_message = await self.offload_messages(
                role="tool",
                content=placeholder,
                messages=[message],
                context=context,
                tool_call_id=message.tool_call_id,
                name=message.name,
                metadata=metadata,
                sys_operation=self.sys_operation,
                offload_handle=offload_handle,
                offload_path=offload_path,
            )
            if offload_message is None:
                return message
            actual_handle = getattr(offload_message, "offload_handle", "unknown")
            actual_type = getattr(offload_message, "offload_type", "unknown")
            handle = f"[[OFFLOAD: handle={actual_handle}, type={actual_type}, path={offload_path}]]"
            offload_message.content = self._build_summary_placeholder(len(content), handle, summary)
            return offload_message  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001 - SDK-private API drift must not kill the turn
            if not _fallback_warned:
                _fallback_warned = True
                logger.warning(
                    "[ContextDiet] summary offload failed (%s: %s); falling back to the SDK placeholder "
                    "for this and later results",
                    type(exc).__name__,
                    exc,
                )
            return await super()._offload_tool_message(message, context)

    @staticmethod
    def _build_summary_placeholder(original_size: int, handle: str, summary: str) -> str:
        return (
            f"{WINDOWED_OUTPUT_TAG}\n"
            f"{MARKER} older result offloaded ({original_size} chars): {summary}\n"
            f"{handle}\n"
            f"{WINDOWED_OUTPUT_CLOSING_TAG}"
        )


@ContextEngine.register_processor()
class ContextDietShellWindowProcessor(_ContextDietWindowBase):
    """Sliding window over shell / grep / glob results."""


@ContextEngine.register_processor()
class ContextDietReadWindowProcessor(_ContextDietWindowBase):
    """Sliding window over ``read_file`` results (opt-in)."""


def build_compaction_processors(config: CompactionConfig) -> list[tuple[str, ContextDietWindowProcessorConfig]]:
    """Translate ``CompactionConfig`` into ``(processor_type, config)`` pairs.

    Returns an empty list when compaction is disabled or every window is 0, so
    the caller can register nothing and leave the preset chain untouched.
    """
    if not config.enabled:
        return []
    pairs: list[tuple[str, ContextDietWindowProcessorConfig]] = []
    if config.shell_keep_last_k > 0 and config.shell_tools:
        pairs.append(
            (
                ContextDietShellWindowProcessor.processor_type(),
                ContextDietWindowProcessorConfig(
                    tool_names=list(config.shell_tools),
                    keep_last_k=config.shell_keep_last_k,
                    min_offload_chars=config.min_offload_chars,
                    summary_chars=config.summary_chars,
                    exempt_source_prints=config.exempt_source_prints,
                ),
            )
        )
    if config.read_keep_last_k > 0 and config.read_tools:
        pairs.append(
            (
                ContextDietReadWindowProcessor.processor_type(),
                ContextDietWindowProcessorConfig(
                    tool_names=list(config.read_tools),
                    keep_last_k=config.read_keep_last_k,
                    min_offload_chars=config.min_offload_chars,
                    summary_chars=config.summary_chars,
                ),
            )
        )
    return pairs


__all__ = [
    "ContextDietReadWindowProcessor",
    "ContextDietShellWindowProcessor",
    "ContextDietWindowProcessorConfig",
    "build_compaction_processors",
]
