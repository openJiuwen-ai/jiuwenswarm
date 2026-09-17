"""Regression test for chat.error stream events carrying ``error_type``.

The streaming error aggregator in ``JiuWenSwarm.process_message_stream``
classifies the exception class on each chat.error event so that downstream
consumers (log indexers, dashboards, external evaluators) can group failures
without regexing the message text. This pins the contract on both the
yielded ``AgentResponseChunk`` and the persisted history record.
"""
from __future__ import annotations

import asyncio
import json
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

    def _capture_history(**kwargs: Any) -> None:
        recorded.append(kwargs)

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
