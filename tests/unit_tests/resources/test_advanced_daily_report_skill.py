"""Contract guards for the bundled advanced-daily-report skill script.

``test_run_report_imap_id_uses_specific_exceptions`` moved here from
``tests/unit_tests/gateway/test_cron_safe_aclose.py``: the upstream
agent-gateway copy of that mirror test is skipped with the reason
"skill resource lives in the jiuwenswarm repo; move with resource fixtures",
so the assertion belongs to this repo and must not be dropped with the
gateway mirror layer.
"""
from __future__ import annotations

from pathlib import Path

_RUN_REPORT = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/advanced-daily-report/run_report.py"
)


def test_run_report_imap_id_uses_specific_exceptions() -> None:
    src = _RUN_REPORT.read_text(encoding="utf-8")
    # The ID-command guard must catch IMAP/OS errors, not everything.
    assert 'mail._simple_command("ID", args)' in src
    assert "except (imaplib.IMAP4.error, OSError)" in src
    # No bare except left in this skill entry script.
    for line in src.splitlines():
        assert line.strip() != "except:", line
