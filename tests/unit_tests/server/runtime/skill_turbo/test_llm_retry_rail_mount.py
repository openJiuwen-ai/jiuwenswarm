# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurboExecutor mounts NotifyingLLMRetryRail and retries stream/call LLM."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import (
    NotifyingLLMRetryRail,
)
from jiuwenswarm.server.runtime.skill_turbo import executor as executor_mod
from jiuwenswarm.server.runtime.skill_turbo.executor import (
    ExecutorConfig,
    SkillTurboExecutor,
)


def _exact_repeat_text(*, unit: str = "abcdef", times: int = 40) -> str:
    """Adjacent exact repeats that the stock LLMRetryRail algorithm detects."""
    return unit * times


def _chunk(
    content: str = "",
    reasoning: str = "",
    usage_metadata: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        usage_metadata=usage_metadata,
    )


def _client_with_stream_outputs(outputs: list[list[str]]) -> tuple[MagicMock, list[int]]:
    """Fake model_client.stream that yields content pieces per call index."""
    call_count = [0]

    async def stream(messages, max_tokens=0, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        pieces = outputs[min(idx, len(outputs) - 1)]
        for piece in pieces:
            yield _chunk(content=piece)

    client = MagicMock()
    client.stream = stream
    client.invoke = MagicMock(side_effect=AssertionError("invoke not expected"))
    return client, call_count


def _client_with_stream_chunk_outputs(
    outputs: list[list[SimpleNamespace]],
) -> tuple[MagicMock, list[int]]:
    """Fake model_client.stream that yields full chunk objects per call index."""
    call_count = [0]

    async def stream(messages, max_tokens=0, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        chunks = outputs[min(idx, len(outputs) - 1)]
        for chunk in chunks:
            yield chunk

    client = MagicMock()
    client.stream = stream
    client.invoke = MagicMock(side_effect=AssertionError("invoke not expected"))
    return client, call_count


def _client_with_invoke_outputs(outputs: list[SimpleNamespace]) -> tuple[MagicMock, list[int]]:
    call_count = [0]

    async def invoke(messages, max_tokens=0, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return outputs[min(idx, len(outputs) - 1)]

    client = MagicMock()
    client.invoke = invoke
    client.stream = MagicMock(side_effect=AssertionError("stream not expected"))
    return client, call_count


def _env(client: MagicMock) -> MagicMock:
    env = MagicMock()
    env.skill_code_import_prefixes = ("skill_codes",)
    env.config = {}
    env.model_client = client
    env.card = None
    return env


def _retry_rail(*, max_retries: int = 2) -> NotifyingLLMRetryRail:
    return NotifyingLLMRetryRail(
        max_retries=max_retries,
        backoff_seconds=[0.0, 0.0, 0.0],
        notify_user_on_retry=False,
        notify_user_on_exhausted=False,
    )


def _make_executor(
    client: MagicMock,
    *,
    rail: NotifyingLLMRetryRail | None,
) -> SkillTurboExecutor:
    with (
        patch.object(SkillTurboExecutor, "_build_permission_rail", return_value=None),
        patch.object(SkillTurboExecutor, "_build_ask_user_rail", return_value=None),
        patch.object(SkillTurboExecutor, "_build_llm_retry_rail", return_value=rail),
    ):
        return SkillTurboExecutor(_env(client), config=ExecutorConfig())


class TestLlmRetryRailMount:
    def test_rail_appended_when_built(self):
        client, _ = _client_with_stream_outputs([["ok"]])
        rail = _retry_rail()
        ex = _make_executor(client, rail=rail)
        assert rail in ex._rails
        assert ex._llm_retry_rail is rail

    def test_rail_absent_when_build_returns_none(self):
        client, _ = _client_with_stream_outputs([["ok"]])
        ex = _make_executor(client, rail=None)
        assert ex._llm_retry_rail is None
        assert not any(isinstance(r, NotifyingLLMRetryRail) for r in ex._rails)

    def test_build_respects_config_disabled(self):
        client, _ = _client_with_stream_outputs([["ok"]])
        with (
            patch.object(SkillTurboExecutor, "_build_permission_rail", return_value=None),
            patch.object(SkillTurboExecutor, "_build_ask_user_rail", return_value=None),
            patch(
                "jiuwenswarm.common.config.get_config",
                return_value={
                    "execution_guard": {"llm_retry_rail": {"enabled": False}},
                },
            ),
        ):
            ex = SkillTurboExecutor(_env(client), config=ExecutorConfig())
        assert ex._llm_retry_rail is None


class TestStreamLlmRetry:
    @pytest.mark.asyncio
    async def test_retries_exact_repeat_then_returns_clean_body(self):
        dirty = _exact_repeat_text()
        # Stream dirty in small pieces so inspector sees growing window.
        dirty_pieces = [dirty[i : i + 12] for i in range(0, len(dirty), 12)]
        client, calls = _client_with_stream_outputs(
            [dirty_pieces, ["hello world final"]]
        )
        ex = _make_executor(client, rail=_retry_rail())

        parts: list[str] = []
        async for piece in ex.stream_llm("prompt", node_name="t"):
            parts.append(piece)

        assert "".join(parts) == "hello world final"
        assert calls[0] == 2

    @pytest.mark.asyncio
    async def test_failed_attempt_not_yielded_to_caller(self):
        """Failed repeat round must not leak into stream_llm_collect buffer."""
        dirty = _exact_repeat_text()
        dirty_pieces = [dirty[i : i + 16] for i in range(0, len(dirty), 16)]
        client, calls = _client_with_stream_outputs(
            [dirty_pieces, ["only-clean"]]
        )
        ex = _make_executor(client, rail=_retry_rail())

        parts: list[str] = []
        async for piece in ex.stream_llm("prompt", node_name="t"):
            parts.append(piece)

        joined = "".join(parts)
        assert "abcdef" not in joined
        assert joined == "only-clean"
        assert calls[0] == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self):
        dirty = _exact_repeat_text()
        dirty_pieces = [dirty[i : i + 12] for i in range(0, len(dirty), 12)]
        # First attempt + 2 retries = 3 identical dirty streams.
        client, calls = _client_with_stream_outputs(
            [dirty_pieces, dirty_pieces, dirty_pieces]
        )
        ex = _make_executor(client, rail=_retry_rail(max_retries=2))

        with pytest.raises(Exception) as ei:
            async for _ in ex.stream_llm("prompt", node_name="t"):
                pass

        assert "LLM repeated stream output detected" in str(ei.value)
        assert calls[0] == 3

    @pytest.mark.asyncio
    async def test_clean_stream_no_retry(self):
        client, calls = _client_with_stream_outputs([["a", "b", "c"]])
        ex = _make_executor(client, rail=_retry_rail())

        parts: list[str] = []
        async for piece in ex.stream_llm("prompt", node_name="t"):
            parts.append(piece)

        assert "".join(parts) == "abc"
        assert calls[0] == 1


class TestCallLlmRetry:
    @pytest.mark.asyncio
    async def test_retries_exact_repeat_on_invoke(self):
        dirty = _exact_repeat_text()
        client, calls = _client_with_invoke_outputs(
            [
                SimpleNamespace(content=dirty, reasoning_content="", usage_metadata=None),
                SimpleNamespace(
                    content="clean-invoke",
                    reasoning_content="",
                    usage_metadata=None,
                ),
            ]
        )
        ex = _make_executor(client, rail=_retry_rail())

        result = await ex.call_llm("prompt", node_name="t")

        assert result == "clean-invoke"
        assert calls[0] == 2

    @pytest.mark.asyncio
    async def test_failed_attempt_does_not_emit_usage(self):
        """usage must run after inspector so a dirty invoke does not leak usage."""
        dirty = _exact_repeat_text()
        dirty_usage = {"prompt_token_count": 1, "candidates_token_count": 9}
        clean_usage = {"prompt_token_count": 2, "candidates_token_count": 3}
        client, calls = _client_with_invoke_outputs(
            [
                SimpleNamespace(
                    content=dirty,
                    reasoning_content="",
                    usage_metadata=dirty_usage,
                ),
                SimpleNamespace(
                    content="clean-invoke",
                    reasoning_content="",
                    usage_metadata=clean_usage,
                ),
            ]
        )
        ex = _make_executor(client, rail=_retry_rail())

        usage_events: list[object] = []
        session = MagicMock()

        async def write_stream(schema):
            usage_events.append(schema)

        session.write_stream = write_stream
        token = executor_mod._session_var.set(session)
        try:
            with patch.object(
                SkillTurboExecutor,
                "_emit_llm_usage",
                autospec=True,
            ) as emit_usage:
                result = await ex.call_llm("prompt", node_name="t")
        finally:
            executor_mod._session_var.reset(token)

        assert result == "clean-invoke"
        assert calls[0] == 2
        assert emit_usage.await_count == 1
        emitted_usage = emit_usage.await_args.args[2]
        assert emitted_usage == clean_usage

    @pytest.mark.asyncio
    async def test_incrementing_number_pattern_not_detected_by_stock_algo(self):
        """Document known gap: screenshot-style '短句+递增数字' is not exact-suffix.

        Mounting the rail does not change openjiuwen detection; this case must
        still complete without retry so we do not pretend coverage we lack.
        """
        # Mimic UI: 句研究重点方向2142 & 1~3 句研究重点方向2143 & 1~3 ...
        pieces = [
            f"句研究重点方向{n} & 1~3 "
            for n in range(2142, 2142 + 80)
        ]
        text = "".join(pieces)
        client, calls = _client_with_invoke_outputs(
            [
                SimpleNamespace(
                    content=text,
                    reasoning_content="",
                    usage_metadata=None,
                ),
            ]
        )
        ex = _make_executor(client, rail=_retry_rail())

        result = await ex.call_llm("prompt", node_name="t")

        assert result == text
        assert calls[0] == 1


class TestStreamLlmSplitChannel:
    @pytest.mark.asyncio
    async def test_frontend_realtime_keeps_failed_prefix_business_stays_clean(self):
        """Frontend is realtime (may keep dirty prefix); business yield stays clean."""
        dirty = _exact_repeat_text()
        dirty_pieces = [dirty[i : i + 16] for i in range(0, len(dirty), 16)]
        client, calls = _client_with_stream_outputs(
            [dirty_pieces, ["only-clean"]]
        )
        ex = _make_executor(client, rail=_retry_rail())

        written: list[object] = []
        session = MagicMock()

        async def write_stream(schema):
            written.append(schema)

        session.write_stream = write_stream
        token = executor_mod._session_var.set(session)
        try:
            parts: list[str] = []
            async for piece in ex.stream_llm("prompt", node_name="t"):
                parts.append(piece)
        finally:
            executor_mod._session_var.reset(token)

        # Business channel: only successful round.
        assert "".join(parts) == "only-clean"
        assert calls[0] == 2

        output_text = "".join(
            str(getattr(schema, "payload", {}).get("content", ""))
            for schema in written
            if getattr(schema, "type", None) == "llm_output"
        )
        # Frontend channel: dirty prefix already flushed before retry, then clean.
        assert "abcdef" in output_text
        assert output_text.endswith("only-clean")

    @pytest.mark.asyncio
    async def test_failed_attempt_does_not_emit_usage(self):
        """Mid-stream usage on a dirty round must not leak; only success emits."""
        dirty = _exact_repeat_text()
        dirty_pieces = [dirty[i : i + 16] for i in range(0, len(dirty), 16)]
        dirty_usage = {"prompt_token_count": 1, "candidates_token_count": 9}
        clean_usage = {"prompt_token_count": 2, "candidates_token_count": 3}

        # Attach usage on an early dirty chunk (before inspector trips on later ones).
        dirty_chunks = [
            _chunk(content=piece, usage_metadata=dirty_usage if i == 0 else None)
            for i, piece in enumerate(dirty_pieces)
        ]
        clean_chunks = [_chunk(content="only-clean", usage_metadata=clean_usage)]
        client, calls = _client_with_stream_chunk_outputs(
            [dirty_chunks, clean_chunks]
        )
        ex = _make_executor(client, rail=_retry_rail())

        session = MagicMock()

        async def write_stream(schema):
            return None

        session.write_stream = write_stream
        token = executor_mod._session_var.set(session)
        try:
            with patch.object(
                SkillTurboExecutor,
                "_emit_llm_usage",
                autospec=True,
            ) as emit_usage:
                parts: list[str] = []
                async for piece in ex.stream_llm("prompt", node_name="t"):
                    parts.append(piece)
        finally:
            executor_mod._session_var.reset(token)

        assert "".join(parts) == "only-clean"
        assert calls[0] == 2
        assert emit_usage.await_count == 1
        emitted_usage = emit_usage.await_args.args[2]
        assert emitted_usage == clean_usage


class TestConcurrentStreamLlmRetry:
    @pytest.mark.asyncio
    async def test_shared_rail_two_streams_each_get_full_budget(self):
        """asyncio.gather of two stream_llm must not share instance retry counts."""
        dirty = _exact_repeat_text()
        dirty_pieces = [dirty[i : i + 12] for i in range(0, len(dirty), 12)]

        # Per-call queues: each concurrent stream needs dirty → clean (2 attempts).
        queues: dict[str, list[list[str]]] = {
            "a": [dirty_pieces, ["clean-a"]],
            "b": [dirty_pieces, ["clean-b"]],
        }
        locks = {key: asyncio.Lock() for key in queues}
        call_counts = {"a": 0, "b": 0, "other": 0}

        async def stream(messages, max_tokens=0, **kwargs):
            # Identify caller by last user prompt tag.
            tag = "other"
            for msg in reversed(messages or []):
                content = msg.get("content", "") if isinstance(msg, dict) else ""
                if content.endswith("|a"):
                    tag = "a"
                    break
                if content.endswith("|b"):
                    tag = "b"
                    break
            async with locks[tag] if tag in locks else asyncio.Lock():
                idx = call_counts[tag]
                call_counts[tag] = idx + 1
                pieces = queues[tag][min(idx, len(queues[tag]) - 1)]
            for piece in pieces:
                yield _chunk(content=piece)

        client = MagicMock()
        client.stream = stream
        client.invoke = MagicMock(side_effect=AssertionError("invoke not expected"))
        # max_retries=1 → each call can attempt twice. Shared instance counters
        # would let the second stream exhaust after the first used the grant.
        ex = _make_executor(client, rail=_retry_rail(max_retries=1))

        async def run(tag: str) -> str:
            parts: list[str] = []
            async for piece in ex.stream_llm(f"prompt|{tag}", node_name=tag):
                parts.append(piece)
            return "".join(parts)

        out_a, out_b = await asyncio.gather(run("a"), run("b"))
        assert out_a == "clean-a"
        assert out_b == "clean-b"
        assert call_counts["a"] == 2
        assert call_counts["b"] == 2
