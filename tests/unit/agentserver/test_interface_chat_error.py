"""Regression test for chat.error stream events carrying ``error_type``.

The streaming error aggregator in ``JiuWenSwarm.process_message_stream``
classifies the exception class on each chat.error event so that downstream
consumers (log indexers, dashboards, external evaluators) can group failures
without regexing the message text. This pins the contract on both the
yielded ``AgentResponseChunk`` and the persisted history record.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, List

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk


class _RaisingStream:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __aiter__(self) -> "_RaisingStream":
        return self

    async def __anext__(self) -> AgentResponseChunk:
        raise self._exc


class _RaisingAdapter:
    """Fake AgentAdapter whose stream impl raises immediately."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def create_instance(self, config: dict[str, Any] | None = None) -> None:
        return None

    async def reload_agent_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def process_message_impl(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._exc

    def process_message_stream_impl(
        self, _request: AgentRequest, _inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        return _RaisingStream(self._exc)

    async def process_interrupt(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def handle_user_answer(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def handle_heartbeat(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


def _patch_facade(
    monkeypatch: pytest.MonkeyPatch,
    facade: JiuWenSwarm,
    adapter: _RaisingAdapter,
    recorded: List[dict[str, Any]],
) -> None:
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")

    def _capture_history(**kwargs: Any) -> bool:
        recorded.append(kwargs)
        return True

    monkeypatch.setattr(interface_module, "append_history_record", _capture_history)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    # Bypass the user prompt builder; the test only cares about the error
    # aggregator after the inner stream raises.
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)


@pytest.mark.asyncio
async def test_chat_error_chunk_includes_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    facade = JiuWenSwarm()
    adapter = _RaisingAdapter(ValueError("boom from agent"))
    history: List[dict[str, Any]] = []
    _patch_facade(monkeypatch, facade, adapter, history)

    request = AgentRequest(
        request_id="req-err-1",
        channel_id="acp",
        session_id="acp_test_sess",
        params={"query": "hello", "mode": "agent.plan"},
    )

    chunks: List[AgentResponseChunk] = []
    async for chunk in facade.process_message_stream(request):
        chunks.append(chunk)

    error_chunks = [
        c for c in chunks
        if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"
    ]
    assert len(error_chunks) == 1, f"expected 1 chat.error chunk, got {len(error_chunks)}"
    payload = error_chunks[0].payload
    assert payload["error_type"] == "ValueError"
    assert "boom from agent" in payload["error"]


@pytest.mark.asyncio
async def test_chat_error_history_record_carries_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    facade = JiuWenSwarm()
    adapter = _RaisingAdapter(RuntimeError("rate limited"))
    history: List[dict[str, Any]] = []
    _patch_facade(monkeypatch, facade, adapter, history)

    request = AgentRequest(
        request_id="req-err-2",
        channel_id="acp",
        session_id="acp_test_sess",
        params={"query": "hi", "mode": "agent.plan"},
    )
    async for _chunk in facade.process_message_stream(request):
        pass

    error_records = [
        r for r in history if r.get("event_type") == "chat.error"
    ]
    assert len(error_records) == 1
    extra = error_records[0].get("extra")
    assert isinstance(extra, dict)
    assert extra.get("error_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_chat_error_history_record_persists_error_type_at_top_level(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Pin that append_history_record flattens extra["error_type"] to the
    # persisted record's top level.
    from jiuwenswarm.server.runtime.session import session_history
    from jiuwenswarm.server.runtime.session import session_metadata

    facade = JiuWenSwarm()
    adapter = _RaisingAdapter(LookupError("token bucket exhausted"))
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")

    # Redirect session_history's sessions dir into the tempdir; do NOT mock
    # append_history_record itself — we want the real flatten-and-write path.
    # session_metadata.py imports get_agent_sessions_dir into its own module
    # namespace and is invoked transitively by append_history_record, so it
    # needs the same redirect or it writes metadata.json to the real
    # ~/.jiuwenswarm sessions dir.
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers._shared._sessions_dir_for_request",
        lambda _request: sessions_root,
    )
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    sid = "tempsess1"
    request = AgentRequest(
        request_id="req-err-disk",
        channel_id="acp",
        session_id=sid,
        params={"query": "hi", "mode": "agent.plan"},
    )
    async for _chunk in facade.process_message_stream(request):
        pass

    history_file = session_history.get_write_history_path(sid)
    chat_errors: list[dict[str, Any]] = []
    for _ in range(100):
        if history_file.exists():
            persisted = session_history.load_history_records(sid)
            chat_errors = [r for r in persisted if r.get("event_type") == "chat.error"]
            if chat_errors:
                break
        await asyncio.sleep(0.05)

    assert history_file.exists(), f"history file not written at {history_file}"
    assert len(chat_errors) == 1, f"expected 1 chat.error record, got {len(chat_errors)}"
    # The doc claims this field is at the top level (not nested under
    # event_payload or extra). Pin it.
    assert chat_errors[0].get("error_type") == "LookupError"


@pytest.mark.asyncio
async def test_cancelled_error_propagates_without_chat_error_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    # asyncio.CancelledError must propagate as cancellation, not be classified
    # and yielded as a chat.error event.
    facade = JiuWenSwarm()
    adapter = _RaisingAdapter(asyncio.CancelledError())
    history: List[dict[str, Any]] = []
    _patch_facade(monkeypatch, facade, adapter, history)

    request = AgentRequest(
        request_id="req-cancel",
        channel_id="acp",
        session_id="acp_test_sess",
        params={"query": "hi", "mode": "agent.plan"},
    )

    with pytest.raises(asyncio.CancelledError):
        async for _chunk in facade.process_message_stream(request):
            pass

    # No history record for cancellation
    assert not [r for r in history if r.get("event_type") == "chat.error"]


@pytest.mark.asyncio
async def test_duplicate_identical_error_suppresses_yield_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two consecutive CRON-style streams in the same session with identical
    error text: the first yields an unmarked chat.error; the second drops
    chat.error and pads with chat.done so an unmodified gateway neither
    bubbles nor synthesizes busy. Interactive (non-cron) requests must NOT
    be suppressed — that previously left users with a silent empty round.
    """
    from jiuwenswarm.server.runtime.session import session_history as sh
    from jiuwenswarm.server.runtime.session import session_metadata
    sh._error_dedup_state.clear()

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers._shared._sessions_dir_for_request",
        lambda _request: sessions_root,
    )

    facade = JiuWenSwarm()
    adapter = _RaisingAdapter(ValueError("gpu quota exhausted"))
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    sid = "dedup-wire-session"

    async def run_cron(req_id: str) -> List[AgentResponseChunk]:
        request = AgentRequest(
            request_id=req_id,
            channel_id="acp",
            session_id=sid,
            params={
                "query": "hello",
                "mode": "agent.plan",
                "cron": {"job_id": "job-1", "run_id": req_id},
            },
        )
        chunks: List[AgentResponseChunk] = []
        async for chunk in facade.process_message_stream(request):
            chunks.append(chunk)
        return chunks

    chunks1 = await run_cron("cron-req-1")
    err1 = [c for c in chunks1 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    assert len(err1) == 1, "first cron stream must yield exactly 1 chat.error"
    assert err1[0].payload.get("suppressed_error") is not True, (
        "first error must NOT carry the suppressed marker"
    )

    chunks2 = await run_cron("cron-req-2")
    err2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    done2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.done"]
    assert len(err2) == 0, "second identical cron error must not yield chat.error (gateway would bubble it)"
    assert len(done2) >= interface_module._SUPPRESSED_ERROR_PADDING_FRAMES, (
        "duplicate cron error must pad with chat.done so unmodified gateway skips busy synthesis"
    )

    sh._error_dedup_state.clear()


class _ChatErrorChunkStream:
    def __init__(self, error_text: str) -> None:
        self._error_text = error_text
        self._n = 0

    def __aiter__(self) -> "_ChatErrorChunkStream":
        return self

    async def __anext__(self) -> AgentResponseChunk:
        self._n += 1
        if self._n == 1:
            return AgentResponseChunk(
                request_id="stub",
                channel_id="acp",
                payload={"event_type": "context.usage"},
                is_complete=False,
            )
        if self._n == 2:
            # Mimic DeepAdapter: the LLM failure is caught inside the adapter
            # and surfaced as a normal chat.error chunk (not a raised exception).
            return AgentResponseChunk(
                request_id="stub",
                channel_id="acp",
                payload={"event_type": "chat.error", "error": self._error_text},
                is_complete=False,
            )
        raise StopAsyncIteration


class _ChatErrorChunkAdapter:
    """Fake AgentAdapter whose stream impl yields chat.error as a normal chunk."""

    def __init__(self, error_text: str) -> None:
        self._error_text = error_text

    async def create_instance(self, config: dict[str, Any] | None = None) -> None:
        return None

    async def reload_agent_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def process_message_impl(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("not used")

    def process_message_stream_impl(
        self, _request: AgentRequest, _inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        return _ChatErrorChunkStream(self._error_text)

    async def process_interrupt(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def handle_user_answer(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def handle_heartbeat(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


@pytest.mark.asyncio
async def test_duplicate_chat_error_chunk_suppressed_on_wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """DeepAdapter-style errors (chat.error emitted as a normal chunk): first
    reaches history and the wire unmarked; consecutive identical failures in
    one session drop chat.error and pad with chat.done.
    """
    from jiuwenswarm.server.runtime.session import session_history as sh
    from jiuwenswarm.server.runtime.session import session_metadata
    sh._error_dedup_state.clear()

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers._shared._sessions_dir_for_request",
        lambda _request: sessions_root,
    )

    facade = JiuWenSwarm()
    adapter = _ChatErrorChunkAdapter(
        "[181001] model call failed, reason: openAI API async stream error: "
        "AuthenticationError: Error code: 401"
    )
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    sid = "dedup-chunk-error-session"

    async def run_stream(req_id: str) -> List[AgentResponseChunk]:
        request = AgentRequest(
            request_id=req_id,
            channel_id="acp",
            session_id=sid,
            params={
                "query": "hello",
                "mode": "agent.plan",
                "cron": {"job_id": "job-1", "run_id": req_id},
            },
        )
        chunks: List[AgentResponseChunk] = []
        async for chunk in facade.process_message_stream(request):
            chunks.append(chunk)
        return chunks

    chunks1 = await run_stream("cron-chunk-req-1")
    err1 = [c for c in chunks1 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    assert len(err1) == 1, "first chunk-style error must reach the wire"
    assert err1[0].payload.get("suppressed_error") is not True, (
        "first chunk-style error must NOT carry the suppressed marker"
    )

    chunks2 = await run_stream("cron-chunk-req-2")
    err2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    done2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.done"]
    assert len(err2) == 0, "duplicate chunk-style error must not yield chat.error"
    assert len(done2) >= interface_module._SUPPRESSED_ERROR_PADDING_FRAMES, (
        "duplicate chunk-style error must pad with chat.done"
    )

    sh._force_flush_all_pending()
    records = sh.load_history_records(sid, sessions_root=str(sessions_root))
    persisted_errors = [r for r in records if r.get("event_type") == "chat.error"]
    assert len(persisted_errors) == 1, "only the first error must be persisted to history"

    sh._error_dedup_state.clear()


@pytest.mark.asyncio
async def test_interactive_user_retry_after_duplicate_error_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """After the cron gate was removed from facade dedup, identical chat.error
    is suppressed regardless of source — cron / proactive / interactive
    all share the per-session fingerprint counter. First failure surfaces;
    subsequent identical failures pad with chat.done.
    """
    from jiuwenswarm.server.runtime.session import session_history as sh
    from jiuwenswarm.server.runtime.session import session_metadata
    sh._error_dedup_state.clear()

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers._shared._sessions_dir_for_request",
        lambda _request: sessions_root,
    )

    facade = JiuWenSwarm()
    adapter = _RaisingAdapter(ValueError("401 unauthorized"))
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    sid = "interactive-retry-session"
    chunks1: List[AgentResponseChunk] = []
    async for chunk in facade.process_message_stream(
        AgentRequest(
            request_id="interactive-1",
            channel_id="acp",
            session_id=sid,
            params={"query": "hello", "mode": "agent.plan"},
        )
    ):
        chunks1.append(chunk)

    err1 = [c for c in chunks1 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    assert len(err1) == 1

    # Same error again, same session — must be suppressed (cron gate removed).
    chunks2: List[AgentResponseChunk] = []
    async for chunk in facade.process_message_stream(
        AgentRequest(
            request_id="interactive-2",
            channel_id="acp",
            session_id=sid,
            params={"query": "hello again", "mode": "agent.plan"},
        )
    ):
        chunks2.append(chunk)

    err2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    done2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.done"]
    assert len(err2) == 0, "second identical chat.error must not reach the wire"
    assert len(done2) >= interface_module._SUPPRESSED_ERROR_PADDING_FRAMES, (
        "duplicate error must pad with chat.done to avoid busy synthesis"
    )

    sh._error_dedup_state.clear()


@pytest.mark.asyncio
async def test_dict_branch_duplicate_chat_error_suppressed_for_cron(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The dict-event branch (interface.py ~3825) of process_message_stream
    also goes through the dedup gate. Pin it for cron so a duplicate cron
    tick's chat.error dict event does not leak onto the wire.
    """
    from jiuwenswarm.server.runtime.session import session_history as sh
    from jiuwenswarm.server.runtime.session import session_metadata
    sh._error_dedup_state.clear()

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers._shared._sessions_dir_for_request",
        lambda _request: sessions_root,
    )

    facade = JiuWenSwarm()
    adapter = _ChatErrorChunkAdapter("stale model credential")
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    sid = "dict-branch-cron-session"
    chunks1: List[AgentResponseChunk] = []
    async for chunk in facade.process_message_stream(
        AgentRequest(
            request_id="cron-1",
            channel_id="acp",
            session_id=sid,
            params={"query": "x", "mode": "agent.plan", "cron": {"job_id": "j", "run_id": "cron-1"}},
        )
    ):
        chunks1.append(chunk)
    assert any(
        isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"
        for c in chunks1
    )

    chunks2: List[AgentResponseChunk] = []
    async for chunk in facade.process_message_stream(
        AgentRequest(
            request_id="cron-2",
            channel_id="acp",
            session_id=sid,
            params={"query": "x", "mode": "agent.plan", "cron": {"job_id": "j", "run_id": "cron-2"}},
        )
    ):
        chunks2.append(chunk)
    err2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    done2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.done"]
    assert len(err2) == 0, "dict-branch cron duplicate must not yield chat.error"
    assert len(done2) >= interface_module._SUPPRESSED_ERROR_PADDING_FRAMES

    sh._error_dedup_state.clear()


@pytest.mark.asyncio
async def test_heartbeat_session_error_is_not_swallowed_by_dedup_padding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: heartbeat sessions return SKIPPED_HEARTBEAT from
    append_history_record — facade MUST NOT mis-classify that as
    SUPPRESSED_DUPLICATE and pad with chat.done, otherwise an interactive
    user retry inside a heartbeat session sees no error AND no busy
    synthesis, leaving them with nothing.
    """
    from jiuwenswarm.server.runtime.session import session_history as sh

    assert sh.append_history_record(
        session_id="heartbeat-foo",
        request_id="r1",
        channel_id="acp",
        role="assistant",
        event_type="chat.error",
        content="anything",
        timestamp=1000.0,
    ) is sh.HistoryAppendStatus.SKIPPED_HEARTBEAT

    assert sh.append_history_record(
        session_id="dedup-empty",
        request_id="r1",
        channel_id="acp",
        role="assistant",
        event_type="chat.error",
        content="",
        timestamp=1000.0,
    ) is sh.HistoryAppendStatus.SKIPPED_EMPTY


@pytest.mark.asyncio
async def test_duplicate_chat_error_chunk_suppressed_for_any_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Chunk-style chat.error (e.g. DeepAdapter) must dedup identically to
    the exception branch — second identical chunk in the same session
    is replaced with chat.done padding, regardless of source.
    """
    from jiuwenswarm.server.runtime.session import session_history as sh
    from jiuwenswarm.server.runtime.session import session_metadata
    sh._error_dedup_state.clear()

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers._shared._sessions_dir_for_request",
        lambda _request: sessions_root,
    )

    facade = JiuWenSwarm()
    adapter = _ChatErrorChunkAdapter("401 unauthorized")
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    sid = "non-cron-dedup-session"

    async def run_stream(req_id: str) -> List[AgentResponseChunk]:
        request = AgentRequest(
            request_id=req_id,
            channel_id="acp",
            session_id=sid,
            params={"query": "hello", "mode": "agent.plan"},
        )
        chunks: List[AgentResponseChunk] = []
        async for chunk in facade.process_message_stream(request):
            chunks.append(chunk)
        return chunks

    chunks1 = await run_stream("interactive-1")
    err1 = [c for c in chunks1 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    assert len(err1) == 1

    chunks2 = await run_stream("interactive-2")
    err2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.error"]
    done2 = [c for c in chunks2 if isinstance(c.payload, dict) and c.payload.get("event_type") == "chat.done"]
    assert len(err2) == 0, "second identical chat.error chunk must not reach the wire"
    assert len(done2) >= interface_module._SUPPRESSED_ERROR_PADDING_FRAMES, (
        "duplicate chunk-style error must pad with chat.done"
    )

    sh._error_dedup_state.clear()
