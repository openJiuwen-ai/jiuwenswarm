# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team stream path preserves formatting whitespace for Markdown tables.

Team mode calls ``parse_stream_chunk(..., preserve_whitespace=True)`` via
``_parse_team_stream_chunk``. Default callers keep historical strip() behavior.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


@pytest.mark.parametrize(
    ("chunk_type", "content", "event_type"),
    [
        ("llm_output", " ", "chat.delta"),
        ("llm_output", "\n", "chat.delta"),
        ("llm_output", "\n\n", "chat.delta"),
        ("content_chunk", "\n\n", "chat.delta"),
        ("llm_reasoning", " ", "chat.reasoning"),
    ],
)
def test_preserve_whitespace_keeps_formatting_chunks(
    chunk_type: str,
    content: str,
    event_type: str,
) -> None:
    chunk = SimpleNamespace(type=chunk_type, payload={"content": content})
    parsed = parse_stream_chunk(chunk, preserve_whitespace=True)

    assert parsed is not None
    assert parsed["event_type"] == event_type
    assert parsed["content"] == content


def test_default_parse_stream_chunk_still_drops_whitespace_only() -> None:
    chunk = SimpleNamespace(type="llm_output", payload={"content": "\n\n"})
    assert parse_stream_chunk(chunk) is None
    assert parse_stream_chunk(chunk, preserve_whitespace=False) is None


def test_preserve_whitespace_still_skips_empty_string() -> None:
    chunk = SimpleNamespace(type="llm_output", payload={"content": ""})
    assert parse_stream_chunk(chunk, preserve_whitespace=True) is None


def test_response_chunk_default_does_not_filter_whitespace() -> None:
    """AgentResponseChunk historically forwarded empty/whitespace; keep that default."""
    chunk = SimpleNamespace(request_id="r1", payload={"content": "\n\n"})
    parsed = parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed["content"] == "\n\n"


def test_response_chunk_team_keeps_whitespace_drops_empty() -> None:
    ws = SimpleNamespace(request_id="r1", payload={"content": "\n\n"})
    empty = SimpleNamespace(request_id="r1", payload={"content": ""})
    assert parse_stream_chunk(ws, preserve_whitespace=True)["content"] == "\n\n"
    assert parse_stream_chunk(empty, preserve_whitespace=True) is None


def test_chunked_answer_preserves_whitespace_in_team_mode() -> None:
    chunk = SimpleNamespace(
        type="answer",
        payload={"output": {"output": "\n\n", "chunked": True}},
    )
    assert parse_stream_chunk(chunk) is None
    parsed = parse_stream_chunk(chunk, preserve_whitespace=True)
    assert parsed == {"event_type": "chat.delta", "content": "\n\n"}


def test_team_helpers_wires_preserve_whitespace() -> None:
    """Guard against regressing the team call-site without importing heavy deps."""
    source = Path("jiuwenswarm/server/runtime/agent_adapter/team_helpers.py").read_text(
        encoding="utf-8"
    )
    assert "preserve_whitespace" in source
    assert "_parse_team_stream_chunk" in source
    assert "parsed = _parse_team_stream_chunk(chunk)" in source
    # Must tolerate one-arg monkeypatches used by team_helpers unit tests.
    assert "inspect.signature(parse_stream_chunk)" in source
