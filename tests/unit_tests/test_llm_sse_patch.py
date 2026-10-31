"""Regression tests for jiuwenswarm.llm_sse_patch.assemble_openai_response.

The patch rebuilds a non-streaming ``ChatCompletion`` from an SSE-only text body.
These tests lock in the behaviour discussed during review of PR #9744:

* multi-frame SSE without ``id:`` markers must not truncate (the original bug);
* non-spec streams that separate events with a single newline (instead of a
  blank line) must fall back to per-line parsing instead of yielding empty
  content;
* CRLF line endings and a leading space after ``data:`` must not corrupt text.
"""

import contextlib
import logging

from jiuwenswarm.llm_sse_patch import assemble_openai_response


@contextlib.contextmanager
def capture_warnings():
    """Capture WARNING records from the source logger.

    pytest's ``caplog`` attaches its handler to the *root* logger, but the
    project's logging setup attaches handlers to ``jiuwenswarm.*`` loggers with
    ``propagate=False``, so records never reach root and ``caplog`` stays empty
    even though the warning was emitted. Attach the recorder to the source
    logger instead, so the assertion does not depend on propagation config.
    """
    logger = logging.getLogger("jiuwenswarm.llm_sse_patch")
    captured: list[str] = []

    class _WarnRecorder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _WarnRecorder()
    handler.setLevel(logging.WARNING)
    prev_propagate = logger.propagate
    logger.addHandler(handler)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.propagate = prev_propagate


def test_multi_frame_without_id_lines_is_not_truncated():
    sse = (
        'data: {"choices":[{"message":{"token_text":"Hello "}}]}\n\n'
        'data: {"choices":[{"message":{"token_text":"World"}}]}\n\n'
    )
    result = assemble_openai_response(sse)
    assert result.choices[0].message.content == "Hello World"


def test_reasoning_and_token_split_across_frames():
    sse = (
        'data: {"choices":[{"message":{"reasoning_token_text":"think "}}]}\n\n'
        'data: {"choices":[{"message":{"token_text":"answer"}}]}\n\n'
    )
    result = assemble_openai_response(sse)
    assert result.choices[0].message.content == "answer"
    assert getattr(result.choices[0].message, "reasoning_content", None) == "think "


def test_spec_multi_line_data_field():
    sse = 'data: {"choices":[\n' 'data: {"message":{"token_text":"Joined"}}]}\n\n'
    result = assemble_openai_response(sse)
    assert result.choices[0].message.content == "Joined"


def test_non_spec_single_newline_separated_events_fall_back_to_per_line():
    # Two complete JSON objects joined only by a single newline (no blank line).
    # A whole-segment json.loads would fail; the implementation must fall back to
    # per-line parsing so content is preserved rather than dropped to "".
    sse = (
        'data: {"choices":[{"message":{"token_text":"Hello"}}]}\n'
        'data: {"choices":[{"message":{"token_text":"World"}}]}\n\n'
    )
    result = assemble_openai_response(sse)
    assert result.choices[0].message.content == "HelloWorld"


def test_crlf_line_endings_do_not_corrupt():
    sse = (
        'data: {"choices":[{"message":{"token_text":"A"}}]}\r\n\r\n'
        'data: {"choices":[{"message":{"token_text":"B"}}]}\r\n\r\n'
    )
    result = assemble_openai_response(sse)
    assert result.choices[0].message.content == "AB"


def test_leading_space_after_data_colon_is_stripped():
    sse = (
        'data: {"choices":[{"message":{"token_text":"L"}}]}\n\n'
        'data: {"choices":[{"message":{"token_text":"R"}}]}\n\n'
    )
    result = assemble_openai_response(sse)
    assert result.choices[0].message.content == "LR"


def test_trailing_event_without_final_blank_line_is_flushed():
    sse = 'data: {"choices":[{"message":{"token_text":"Tail"}}]}\n'
    result = assemble_openai_response(sse)
    assert result.choices[0].message.content == "Tail"


def test_non_spec_stream_emits_warning_on_fallback():
    """Non-spec streams that join events with a single newline must fall back
    to per-line parsing AND emit a warning."""
    with capture_warnings() as captured:
        sse = (
            'data: {"choices":[{"message":{"token_text":"Hello"}}]}\n'
            'data: {"choices":[{"message":{"token_text":"World"}}]}\n\n'
        )
        result = assemble_openai_response(sse)

    assert result.choices[0].message.content == "HelloWorld"
    assert any("回退逐行解析" in msg for msg in captured), captured


def test_done_sentinel_does_not_emit_fallback_warning():
    """A standards-compliant stream ends with ``data: [DONE]``, which is not
    JSON. It must be skipped silently instead of being reported as a parse
    failure — every OpenAI-compatible response would otherwise log a
    misleading warning that dilutes real alerts."""
    with capture_warnings() as captured:
        sse = (
            'data: {"choices":[{"message":{"token_text":"Hello"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        result = assemble_openai_response(sse)

    assert result.choices[0].message.content == "Hello"
    assert captured == [], captured

