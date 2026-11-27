from __future__ import annotations

import logging
import re
from pathlib import Path

from jiuwenclaw.utils import RuntimeLogFormatter, setup_logger, shutdown_logging


def _format_record(
    *,
    level: int = logging.INFO,
    msg: str = "hello",
    name: str = "jiuwenclaw.channel.vibeskill_channel",
    filename: str = "vibeskill_channel.py",
    lineno: int = 1484,
    extra: dict[str, str] | None = None,
) -> list[str]:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=f"/tmp/{filename}",
        lineno=lineno,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return RuntimeLogFormatter().format(record).split("|")


def test_runtime_log_formatter_outputs_fixed_pipe_fields() -> None:
    row = _format_record(
        msg="session_id=sid-1 sandbox_id=sandbox-1 [VibeSkillChannel] value|with\nbreak",
    )

    assert len(row) == 6
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", row[0])
    assert row[1] == "INFO"
    assert row[2] == "sid-1"
    assert row[3] == "sandbox-1"
    assert row[4] == "jiuwenclaw.channel.vibeskill_channel vibeskill_channel.py:1484:"
    assert row[5] == "[VibeSkillChannel] value with break"


def test_runtime_log_formatter_maps_python_level_names() -> None:
    assert _format_record(level=logging.WARNING)[1] == "WARN"
    assert _format_record(level=logging.CRITICAL)[1] == "FATAL"
    assert _format_record(level=logging.DEBUG)[1] == "DEBUG"


def test_runtime_log_formatter_prefers_extra_fields_and_deduplicates_message() -> None:
    row = _format_record(
        msg="[session=sid-extra] work sandbox=sandbox-extra started session_id=sid-other",
        extra={"session_id": "sid-extra", "sandboxID": "sandbox-extra"},
    )

    assert row[2] == "sid-extra"
    assert row[3] == "sandbox-extra"
    assert "[session=sid-extra]" not in row[5]
    assert "sandbox=sandbox-extra" not in row[5]
    assert "session_id=sid-other" in row[5]
    assert row[5] == "work started session_id=sid-other"


def test_runtime_log_formatter_removes_legacy_log_prefix() -> None:
    row = _format_record(
        msg=(
            "2026-06-08 16:30:22.123 [123] INFO "
            "jiuwenclaw.channel.vibeskill_channel vibeskill_channel.py:1484: "
            "sessionID=sid-2 sandboxID=sandbox-2 ready"
        )
    )

    assert row[2] == "sid-2"
    assert row[3] == "sandbox-2"
    assert row[5] == "ready"


def test_runtime_log_formatter_preserves_exception_text_in_message() -> None:
    try:
        raise ValueError("bad|value")
    except ValueError as exc:
        record = logging.LogRecord(
            name="jiuwenclaw.gateway.demo",
            level=logging.ERROR,
            pathname="/tmp/demo.py",
            lineno=42,
            msg="session_id=sid-ex failed",
            args=(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    row = RuntimeLogFormatter().format(record).split("|")

    assert len(row) == 6
    assert row[1] == "ERROR"
    assert row[2] == "sid-ex"
    assert "failed" in row[5]
    assert "ValueError: bad value" in row[5]


def test_setup_logger_uses_runtime_format_and_keeps_permissions_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LOG_ROOT_PATH", str(tmp_path))
    monkeypatch.setenv("LOG_ASYNC", "1")
    root = setup_logger("INFO")
    try:
        logging.getLogger("jiuwenclaw.gateway.demo").info(
            "session_id=sid-gw sandbox_id=sandbox-gw gateway ready"
        )
        logging.getLogger("jiuwenclaw.channel.demo").info(
            "sessionID=sid-ch channel ready"
        )
        logging.getLogger("jiuwenclaw.agentserver.demo").warning(
            "sandbox=sandbox-agent agent ready"
        )
        logging.getLogger("jiuwenclaw.agentserver.permissions.checker").info(
            '{"ok": true, "session_id": "sid-perm"}'
        )
        shutdown_logging()

        gateway = (tmp_path / "gateway.log").read_text(encoding="utf-8").splitlines()
        channel = (tmp_path / "channel.log").read_text(encoding="utf-8").splitlines()
        agent = (tmp_path / "agent_server.log").read_text(encoding="utf-8").splitlines()
        permissions = (tmp_path / "permissions.log").read_text(encoding="utf-8").splitlines()

        assert len(gateway[-1].split("|")) == 6
        gateway_row = gateway[-1].split("|")
        assert gateway_row[2] == "sid-gw"
        assert gateway_row[3] == "sandbox-gw"
        assert re.fullmatch(r"jiuwenclaw\.gateway\.demo test_logging_format\.py:\d+:", gateway_row[4])
        assert gateway_row[5] == "gateway ready"
        assert channel[-1].split("|")[2] == "sid-ch"
        assert channel[-1].split("|")[5] == "channel ready"
        agent_rows = [line.split("|") for line in agent]
        agent_row = next(row for row in agent_rows if row[4].startswith("jiuwenclaw.agentserver.demo "))
        assert agent_row[1] == "WARN"
        assert agent_row[3] == "sandbox-agent"
        assert agent_row[5] == "agent ready"
        assert permissions[-1] == '{"ok": true, "session_id": "sid-perm"}'
    finally:
        shutdown_logging()
