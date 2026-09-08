from __future__ import annotations

import logging

from jiuwenswarm.extensions.agentos.agentos_router.logutil import (
    agentos_extra,
    format_agentos,
    log_agentos,
)


def test_format_agentos_omits_empty_and_keeps_primary_order() -> None:
    line = format_agentos(
        "route.stream",
        method="chat.send",
        user_id="u1",
        sandbox_id="sbx",
        session_id="s1",
        request_id="r1",
        created=False,
        extra_flag="ok",
    )
    assert line.startswith("[AgentOS] route.stream ")
    assert line.split() == [
        "[AgentOS]",
        "route.stream",
        "user_id=u1",
        "session_id=s1",
        "request_id=r1",
        "sandbox_id=sbx",
        "method=chat.send",
        "created=false",
        "extra_flag=ok",
    ]


def test_format_agentos_redacts_token_kwargs() -> None:
    line = format_agentos(
        "auth.ok",
        user_id="u1",
        token="secret",
        authorization="Bearer secret",
        payload={"query": "hi"},
    )
    assert "secret" not in line
    assert "token=" not in line
    assert "authorization=" not in line
    assert "payload=" not in line
    assert "user_id=u1" in line


def test_format_agentos_puts_trace_id_after_request_id() -> None:
    line = format_agentos(
        "agent.ws.ready",
        user_id="u1",
        request_id="r1",
        trace_id="t-ws-1",
        sandbox_id="sbx",
    )
    keys = [part.split("=", 1)[0] for part in line.split()[2:]]
    assert keys[:4] == ["user_id", "request_id", "trace_id", "sandbox_id"]
    assert "trace_id=t-ws-1" in line


def test_agentos_extra_skips_empty() -> None:
    assert agentos_extra() == {}
    assert agentos_extra(session_id="s1", sandbox_id="") == {"session_id": "s1"}
    assert agentos_extra(session_id="", sandbox_id="sbx") == {"sandbox_id": "sbx"}


def test_log_agentos_sets_record_extra() -> None:
    logger = logging.getLogger("jiuwenswarm.extensions.agentos.test_logutil")
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_agentos(
            logger,
            logging.INFO,
            "sandbox.create.ok",
            user_id="u1",
            session_id="s1",
            sandbox_id="sbx",
        )
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 1
    record = captured[0]
    assert record.session_id == "s1"
    assert record.sandbox_id == "sbx"
    assert "[AgentOS] sandbox.create.ok" in record.getMessage()
    assert "session_id=s1" in record.getMessage()
    assert "sandbox_id=sbx" in record.getMessage()
