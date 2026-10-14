"""Unit tests for CronSchedulerService stream_gen best-effort aclose logging."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.gateway.cron.scheduler import _safe_aclose


@pytest.mark.asyncio
async def test_safe_aclose_success_does_not_log(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = MagicMock()
    stream.aclose = AsyncMock(return_value=None)
    warning = MagicMock()
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.scheduler.logger.warning",
        warning,
    )

    await _safe_aclose(stream, request_id="cron-1")

    stream.aclose.assert_awaited_once_with()
    warning.assert_not_called()


@pytest.mark.asyncio
async def test_safe_aclose_logs_failure_with_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = MagicMock()
    stream.aclose = AsyncMock(side_effect=RuntimeError("boom"))
    warning = MagicMock()
    monkeypatch.setattr(
        "jiuwenswarm.gateway.cron.scheduler.logger.warning",
        warning,
    )

    await _safe_aclose(stream, request_id="cron-42")

    stream.aclose.assert_awaited_once_with()
    warning.assert_called_once()
    args, kwargs = warning.call_args
    assert "stream_gen.aclose failed" in args[0]
    assert args[1] == "cron-42"
    assert kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_safe_aclose_none_and_missing_aclose_are_noop() -> None:
    await _safe_aclose(None, request_id="x")
    await _safe_aclose(object(), request_id="y")


def test_run_report_imap_id_uses_specific_exceptions() -> None:
    """advanced-daily-report must not use a bare ``except:`` around IMAP ID."""
    from pathlib import Path

    src = Path(
        "jiuwenswarm/resources/agent/workspace/skills/"
        "advanced-daily-report/run_report.py"
    ).read_text(encoding="utf-8")
    # The ID-command guard must catch IMAP/OS errors, not everything.
    assert "mail._simple_command(\"ID\", args)" in src
    assert "except (imaplib.IMAP4.error, OSError)" in src
    # No bare except left in this skill entry script.
    for line in src.splitlines():
        assert line.strip() != "except:", line
