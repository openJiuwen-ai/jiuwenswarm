# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the context-diet compaction window processors."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import pytest
from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.foundation.llm.schema.message import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

from jiuwenswarm.agents.harness.code.context_diet import CompactionConfig
from jiuwenswarm.agents.harness.code.context_diet.processors import (
    ContextDietReadWindowProcessor,
    ContextDietShellWindowProcessor,
    ContextDietWindowProcessorConfig,
    build_compaction_processors,
)


@contextlib.contextmanager
def _capture_logger(caplog, name: str, level: int = logging.WARNING):
    """Capture records from logger ``name`` on any pytest version.

    ``setup_logger()`` sets ``propagate = False`` on the ``jiuwenswarm`` logger,
    so records never reach the root handler that ``caplog`` installs on the
    pytest pinned by uv.lock (9.0); pytest 9.1 also attaches to non-propagating
    loggers. Attach caplog's handler to the emitting logger itself (as the base
    tests do) and stop propagation meanwhile, so each record is captured once.
    """
    target = logging.getLogger(name)
    added = caplog.handler not in target.handlers
    saved = (target.propagate, target.level, caplog.handler.level)
    if added:
        target.addHandler(caplog.handler)
    target.propagate = False
    target.setLevel(level)
    caplog.handler.setLevel(level)
    try:
        yield
    finally:
        if added:
            target.removeHandler(caplog.handler)
        target.propagate = saved[0]
        target.setLevel(saved[1])
        caplog.handler.setLevel(saved[2])


class _FakeContext:
    """Minimal ModelContext stand-in: message list + identity helpers."""

    def __init__(self, messages: list[Any]):
        self._messages = list(messages)

    def get_messages(self, *args, **kwargs):
        return list(self._messages)

    def set_messages(self, messages, *args, **kwargs):
        self._messages = list(messages)

    def __len__(self):
        return len(self._messages)

    def session_id(self):
        return "sess"

    def context_id(self):
        return "ctx"

    def workspace_dir(self):
        return ""  # no filesystem: offload falls back to in-memory

    def token_counter(self):
        return None

    # in-memory offload store used when no workspace directory exists
    def offload_messages(self, handle, messages):
        self.offloaded = getattr(self, "offloaded", {})
        self.offloaded[handle] = list(messages)


def _round(idx: int, tool: str, args: dict, result: str) -> list[Any]:
    call = ToolCall(id=f"call_{idx}", type="function", name=tool, arguments=json.dumps(args))
    return [
        AssistantMessage(content="", tool_calls=[call]),
        ToolMessage(content=result, tool_call_id=f"call_{idx}", name=tool),
    ]


def test_processor_types_are_registered_with_the_engine():
    assert ContextEngine._PROCESSOR_MAP["ContextDietShellWindowProcessor"] is ContextDietShellWindowProcessor
    assert ContextEngine._PROCESSOR_MAP["ContextDietReadWindowProcessor"] is ContextDietReadWindowProcessor
    assert ContextDietShellWindowProcessor.processor_type() == "ContextDietShellWindowProcessor"


def test_build_compaction_processors_follows_config():
    default = build_compaction_processors(CompactionConfig())
    assert [name for name, _ in default] == ["ContextDietShellWindowProcessor"]
    cfg = default[0][1]
    assert cfg.keep_last_k == CompactionConfig().shell_keep_last_k == 4 and "bash" in cfg.tool_names and "grep" in cfg.tool_names
    assert "read_file" not in cfg.tool_names and "edit_file" not in cfg.tool_names

    both = build_compaction_processors(CompactionConfig(read_keep_last_k=12))
    assert [name for name, _ in both] == ["ContextDietShellWindowProcessor", "ContextDietReadWindowProcessor"]
    assert both[1][1].tool_names == ["read_file"] and both[1][1].keep_last_k == 12

    assert build_compaction_processors(CompactionConfig(enabled=False)) == []
    assert build_compaction_processors(CompactionConfig(shell_keep_last_k=0)) == []


@pytest.mark.asyncio
async def test_small_old_results_become_one_line_summaries():
    proc = ContextDietShellWindowProcessor(
        ContextDietWindowProcessorConfig(tool_names=["bash", "grep"], keep_last_k=1, min_offload_chars=10_000)
    )
    messages = [UserMessage(content="fix it")]
    messages += _round(1, "bash", {"command": "pytest -q"}, "Command: pytest -q\nStdout: 1 failed\nFAILED t.py::a\nExit Code: 1")
    messages += _round(2, "grep", {"pattern": "def foo", "path": "src"}, "src/a.py:1: def foo\nsrc/b.py:9: def foo")
    messages += _round(3, "edit_file", {"file_path": "src/a.py"}, "Updated src/a.py")
    new = _round(4, "bash", {"command": "pytest -q"}, "Command: pytest -q\nStdout: 2 passed\nExit Code: 0")
    ctx = _FakeContext(messages)

    assert await proc.trigger_add_messages(ctx, new) is True
    event, added = await proc.on_add_messages(ctx, new)

    assert event is not None and sorted(event.messages_to_modify) == [2, 4]
    stored = ctx.get_messages()
    assert "[context-diet] older result compacted" in stored[2].content
    assert "bash `pytest -q` → exit 1; FAILED t.py::a" in stored[2].content
    assert "grep /def foo/ src → 2 lines" in stored[4].content
    assert stored[2].metadata["context_diet_summary"].startswith("bash `pytest -q`")
    assert stored[6].content == "Updated src/a.py"  # edit results untouched
    assert added == new  # the newest bash result stays verbatim
    # a second pass does not touch already-compacted results
    assert await proc.trigger_add_messages(ctx, []) is False


@pytest.mark.asyncio
async def test_large_old_results_are_offloaded_with_handle_and_summary():
    proc = ContextDietShellWindowProcessor(
        ContextDietWindowProcessorConfig(tool_names=["bash"], keep_last_k=1, min_offload_chars=100)
    )
    big = "Command: cat log\nStdout: " + "\n".join(f"row {i}" for i in range(300)) + "\nExit Code: 0"
    messages = _round(1, "bash", {"command": "cat log"}, big)
    new = _round(2, "bash", {"command": "ls"}, "Command: ls\nStdout: a\nExit Code: 0")
    ctx = _FakeContext(messages)

    event, _ = await proc.on_add_messages(ctx, new)

    assert event is not None and event.messages_to_modify == [1]
    placeholder = ctx.get_messages()[1].content
    assert "[context-diet] older result offloaded" in placeholder
    assert "bash `cat log` → exit 0" in placeholder
    assert "[[OFFLOAD: handle=" in placeholder and "type=in_memory" in placeholder
    assert len(placeholder) < len(big) // 4
    assert len(ctx.offloaded) == 1 and next(iter(ctx.offloaded.values()))[0].content == big


@pytest.mark.asyncio
async def test_window_does_not_fire_below_keep_last_k():
    proc = ContextDietShellWindowProcessor(ContextDietWindowProcessorConfig(tool_names=["bash"], keep_last_k=8))
    messages = []
    for i in range(5):
        messages += _round(i, "bash", {"command": "ls"}, f"Command: ls\nStdout: {i}\nExit Code: 0")
    ctx = _FakeContext(messages)
    assert await proc.trigger_add_messages(ctx, []) is False
    event, added = await proc.on_add_messages(ctx, [])
    assert event is None and added == []


@pytest.mark.asyncio
async def test_private_sdk_api_failure_falls_back_to_base_offload(monkeypatch, caplog):
    """If an agent-core internal changes shape, the result is still offloaded via the base class."""
    from openjiuwen.core.context_engine.context.context_utils import ContextUtils

    from jiuwenswarm.agents.harness.code.context_diet import processors as mod

    monkeypatch.setattr(mod, "_fallback_warned", False)

    def _boom(*args, **kwargs):
        raise AttributeError("SDK helper changed shape")

    # Every ContextUtils / offloader helper the override calls is also used by
    # the base class, so a failure is injected at the one seam that belongs to
    # the override alone: the summary builder. Any exception raised inside the
    # override must reach the base implementation.
    monkeypatch.setattr(mod, "summarize_tool_result", _boom)
    assert ContextUtils.extract_tool_name  # the SDK helper itself stays intact for the base path
    proc = ContextDietShellWindowProcessor(
        ContextDietWindowProcessorConfig(tool_names=["bash"], keep_last_k=1, min_offload_chars=100)
    )
    big = "Command: cat log\nStdout: " + "\n".join(f"row {i}" for i in range(300)) + "\nExit Code: 0"
    messages = _round(1, "bash", {"command": "cat log"}, big) + _round(2, "bash", {"command": "cat log"}, big)
    new = _round(3, "bash", {"command": "ls"}, "Command: ls\nStdout: a\nExit Code: 0")
    ctx = _FakeContext(messages)

    with _capture_logger(caplog, mod.__name__):
        event, _ = await proc.on_add_messages(ctx, new)

    assert event is not None and event.messages_to_modify == [1, 3]
    stored = ctx.get_messages()
    for idx in (1, 3):
        placeholder = stored[idx].content
        assert "[[OFFLOAD: handle=" in placeholder  # base-class placeholder still carries the handle
        assert "[context-diet] older result offloaded" not in placeholder
    assert len(ctx.offloaded) == 2
    warnings = [r for r in caplog.records if "summary offload failed" in r.getMessage()]
    assert len(warnings) == 1  # warned once, not per message


@pytest.mark.asyncio
async def test_message_lookup_is_task_scoped_not_instance_state():
    """Interleaved contexts sharing one processor instance never see each other's messages."""
    import asyncio

    from jiuwenswarm.agents.harness.code.context_diet import processors as mod

    proc = ContextDietShellWindowProcessor(
        ContextDietWindowProcessorConfig(tool_names=["bash"], keep_last_k=1, min_offload_chars=10_000)
    )
    seen: list[tuple[str, int]] = []
    original = proc._summary_for

    def spy(message):
        seen.append((message.tool_call_id, len(mod._CURRENT_MESSAGES.get())))
        return original(message)

    proc._summary_for = spy  # type: ignore[method-assign]
    ctx_a = _FakeContext(_round(1, "bash", {"command": "a"}, "Command: a\nExit Code: 0") * 1)
    ctx_b = _FakeContext(
        _round(10, "bash", {"command": "b"}, "Command: b\nExit Code: 0")
        + _round(11, "bash", {"command": "b2"}, "Command: b2\nExit Code: 0")
    )
    new_a = _round(2, "bash", {"command": "a2"}, "Command: a2\nExit Code: 0")
    new_b = _round(12, "bash", {"command": "b3"}, "Command: b3\nExit Code: 0")
    await asyncio.gather(proc.on_add_messages(ctx_a, new_a), proc.on_add_messages(ctx_b, new_b))
    assert not hasattr(proc, "_context_messages")
    by_call = dict(seen)
    assert by_call["call_1"] == 4  # ctx_a: 2 old + 2 new messages
    assert by_call["call_10"] == 6 and by_call.get("call_11", 6) == 6  # ctx_b: 4 old + 2 new
    assert mod._CURRENT_MESSAGES.get() == ()
