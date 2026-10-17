"""Tests for compact auto-permission audit events."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.audit import (
    emit_permission_audit,
    logger as audit_logger,
)
from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import PersistentAuditWriter
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts


class RaisingAuditWriter:
    """Audit writer double that fails outside the built-in writer boundary."""

    def write(self, *_args, **_kwargs):
        raise KeyError("audit writer failed")


def _facts(tmp_path: Path):
    return build_tool_decision_facts(
        "read_file",
        {"path": str(tmp_path / "secret-token.txt")},
        workspace_root=tmp_path,
        original_args_were_valid_object=True,
    )


def _capture(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    caplog.set_level(logging.INFO, logger=audit_logger.name)
    monkeypatch.setattr(audit_logger, "handlers", [*audit_logger.handlers, caplog.handler])
    monkeypatch.setattr(
        logging.getLogger("jiuwenswarm.agents.harness.common.rails.permissions"),
        "propagate",
        True,
    )


def test_audit_has_no_raw_args_or_argument_digest(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(caplog, monkeypatch)
    emit_permission_audit(
        _facts(tmp_path),
        decision="deny",
        reason="policy_deny",
        degraded=False,
    )

    assert "secret-token.txt" not in caplog.text
    assert "args_digest" not in caplog.text
    assert "normalized_args_hash" not in caplog.text
    assert '"accesses_known":true' in caplog.text


def test_audit_persists_only_compact_record(tmp_path: Path) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path / "data")
    result = emit_permission_audit(
        _facts(tmp_path),
        decision="allow",
        reason="reviewer_allow_once",
        degraded=False,
        persistent_writer=writer,
    )

    assert result is not None and result.persisted is True
    content = result.path.read_text(encoding="utf-8")
    assert "secret-token.txt" not in content
    assert "args_digest" not in content


def test_audit_keeps_allowed_route_metadata(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(caplog, monkeypatch)
    emit_permission_audit(
        _facts(tmp_path),
        decision="ask",
        reason="browser_network_guard_unverified",
        degraded=True,
        extra={
            "decision_source": "host_route",
            "host_route_source": "manual_only",
        },
    )
    assert '"decision_source":"host_route"' in caplog.text
    assert '"host_route_source":"manual_only"' in caplog.text


def test_audit_keeps_safe_reviewer_prose_in_memory_and_persistent_sinks(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(caplog, monkeypatch)
    summary = "The cat food costs $100"
    writer = PersistentAuditWriter(data_root=tmp_path / "data")

    result = emit_permission_audit(
        _facts(tmp_path),
        decision="ask",
        reason="semantic_review_required",
        degraded=False,
        extra={"reviewer_reason_summary": summary},
        persistent_writer=writer,
    )

    assert f'"reviewer_reason_summary":"{summary}"' in caplog.text
    assert result is not None and result.persisted is True
    record = json.loads(result.path.read_text(encoding="utf-8"))
    assert record["reviewer_reason_summary"] == summary


def test_audit_redacts_sensitive_fields_and_filters_unknown_extra(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(caplog, monkeypatch)
    sensitive_path = str(tmp_path / "private" / "credentials.txt")
    synthetic_bearer = "Bearer " + ("A" * 24)

    emit_permission_audit(
        _facts(tmp_path),
        decision="ask",
        reason=f"file_guard requires approval: {sensitive_path}",
        degraded=False,
        grant_reason=synthetic_bearer,
        extra={
            "reviewer_reason_summary": synthetic_bearer,
            "unexpected_raw_value": sensitive_path,
        },
    )

    assert sensitive_path not in caplog.text
    assert synthetic_bearer not in caplog.text
    assert "unexpected_raw_value" not in caplog.text
    assert '"reason":"[redacted]"' in caplog.text
    assert '"reviewer_reason_summary":"[redacted]"' in caplog.text


def test_audit_sanitizes_non_json_extra_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(caplog, monkeypatch)

    emit_permission_audit(
        _facts(tmp_path),
        decision="ask",
        reason="semantic_review_required",
        degraded=False,
        extra={"reviewer_reason_summary": object()},
    )

    assert '"reviewer_reason_summary":"[redacted]"' in caplog.text
    assert "object at 0x" not in caplog.text


def test_audit_redacts_sensitive_mapping_keys(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(caplog, monkeypatch)
    sensitive_path = str(tmp_path / "private" / "credentials.txt")

    emit_permission_audit(
        _facts(tmp_path),
        decision="ask",
        reason="semantic_review_required",
        degraded=False,
        extra={"reviewer_reason_summary": {sensitive_path: "safe_value"}},
    )

    assert sensitive_path not in caplog.text
    assert '"reviewer_reason_summary":{"[redacted]":"safe_value"}' in caplog.text


def test_audit_writer_exception_does_not_escape(tmp_path: Path) -> None:
    result = emit_permission_audit(
        _facts(tmp_path),
        decision="ask",
        reason="semantic_review_required",
        degraded=True,
        persistent_writer=RaisingAuditWriter(),
    )

    assert result is None
