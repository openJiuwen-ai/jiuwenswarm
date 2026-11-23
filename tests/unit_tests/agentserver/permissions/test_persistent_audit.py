"""Tests for compact persistent permission audit records."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import ReviewerRecommendation
from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import (
    AUDIT_TEXT_REDACTED,
    MAX_MODEL_RATIONALE_LENGTH,
    LocalAuditDetails,
    PersistentAuditWriter,
    resolve_persistent_audit_root,
    sanitize_audit_field,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts


class ExplodingString:
    """Value double whose string conversion fails inside audit sanitization."""

    def __str__(self) -> str:
        raise RuntimeError("cannot render audit value")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owner-only audit file check")
def test_local_audit_redacts_complete_model_output_before_bounding(tmp_path: Path) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path)
    private_path = "/home/example-user/PRIVATE_PATH_ONLY.txt"
    synthetic_secret = "sk-synthetic-value-only"
    rationale = f"路径 {private_path} 凭据 {synthetic_secret}。" + "检查" * MAX_MODEL_RATIONALE_LENGTH
    result = writer.write(
        _facts(tmp_path),
        decision="deny",
        reason="reviewer_deny",
        degraded=False,
        extra={"record_kind": "permission_reviewer_assessment"},
        local_details=LocalAuditDetails(
            "tiv_synthetic",
            ReviewerRecommendation("deny", synthetic_secret, rationale),
        ),
    )

    assert result.persisted is True
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    text = result.path.read_text(encoding="utf-8")
    assert "PRIVATE_PATH_ONLY" not in text and synthetic_secret not in text
    model = json.loads(text)["model_recommendation"]
    assert model["outcome"] == "deny"
    assert model["reason_code"] == AUDIT_TEXT_REDACTED
    assert model["rationale"].startswith("路径 [path] 凭据 [redacted]。")
    assert model["rationale"].endswith("...")
    assert len(model["rationale"]) == MAX_MODEL_RATIONALE_LENGTH


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owner-only audit file check")
@pytest.mark.parametrize("rationale,private_fragment", [
    ("拒绝读取/etc/PRIVATE_AUDIT_SENTINEL", "PRIVATE_AUDIT_SENTINEL"),
    ("检查" * 508 + " sk-synthetic-value-near-boundary", "sk-synt"),
])
def test_local_audit_masks_adjacent_paths_and_secrets_at_the_limit(tmp_path, rationale, private_fragment):
    writer = PersistentAuditWriter(data_root=tmp_path)
    result = writer.write(
        _facts(tmp_path),
        decision="deny",
        reason="reviewer_deny",
        degraded=False,
        extra={"record_kind": "permission_reviewer_assessment"},
        local_details=LocalAuditDetails("tiv_synthetic", ReviewerRecommendation("deny", "test", rationale)),
    )

    assert result.persisted is True
    text = json.loads(result.path.read_text(encoding="utf-8"))["model_recommendation"]["rationale"]
    assert private_fragment not in text


@pytest.mark.skipif(sys.platform != "linux", reason="Linux owner-only audit file check")
@pytest.mark.parametrize("symlink", [False, True])
def test_existing_shared_or_symlink_audit_does_not_get_model_details(tmp_path: Path, symlink: bool) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path)
    writer.audit_path.parent.mkdir()
    target = tmp_path / "outside-audit.jsonl" if symlink else writer.audit_path
    target.touch()
    target.chmod(0o600 if symlink else 0o664)
    if symlink:
        writer.audit_path.symlink_to(target)

    result = writer.write(
        _facts(tmp_path),
        decision="allow",
        reason="reviewer_allow_once",
        degraded=False,
        extra={"record_kind": "permission_reviewer_assessment"},
        local_details=LocalAuditDetails(
            "tiv_synthetic",
            ReviewerRecommendation("allow_once", "aligned", "批准当前操作。"),
        ),
    )

    content = target.read_text(encoding="utf-8")
    if symlink:
        assert result.persisted is False
        assert content == ""
    else:
        assert result.persisted is True
        assert stat.S_IMODE(target.stat().st_mode) == 0o664
        record = json.loads(content)
        assert record["decision"] == "allow"
        assert "model_recommendation" not in record and "invocation_id" not in record


def _facts(tmp_path: Path):
    return build_tool_decision_facts(
        "read_file",
        {"path": str(tmp_path / "secret-token.txt")},
        workspace_root=tmp_path,
        original_args_were_valid_object=True,
    )


def test_persistent_audit_has_no_argument_digest_or_secret_state(tmp_path: Path) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path / "data")
    result = writer.write(
        _facts(tmp_path),
        decision="allow",
        reason="reviewer_allow_once",
        degraded=False,
        extra={
            "reviewer_lifecycle": "approved",
            "reviewer_override_token": "must-not-persist",
        },
    )

    content = result.path.read_text(encoding="utf-8")
    record = json.loads(content)
    assert result.persisted is True
    assert "args_digest" not in record
    assert "normalized_args_hash" not in record
    assert "secret-token.txt" not in content
    assert "must-not-persist" not in content
    assert not hasattr(writer, "secret_key")


def test_persistent_audit_keeps_compact_route_provenance(tmp_path: Path) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path / "data")
    result = writer.write(
        _facts(tmp_path),
        decision="ask",
        reason="semantic_review_required",
        degraded=False,
        extra={
            "host_route_reason": "semantic_review_required",
            "host_route_source": "semantic_reviewer",
            "decision_source": "auto_reviewer",
        },
    )
    record = json.loads(result.path.read_text(encoding="utf-8"))
    assert record["host_route_source"] == "semantic_reviewer"
    assert record["decision_source"] == "auto_reviewer"


def test_persistent_audit_redacts_free_text(tmp_path: Path) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path / "data")
    secret_path = tmp_path / ".env"
    result = writer.write(
        _facts(tmp_path),
        decision="ask",
        reason=f"cat {secret_path} && echo sk-secret-token",
        degraded=False,
        grant_reason=f"grant matched {secret_path}",
    )
    record = json.loads(result.path.read_text(encoding="utf-8"))
    assert record["reason"] == "[redacted]"
    assert record["grant_reason"] == "[redacted]"


def test_missing_or_unwritable_root_degrades(tmp_path: Path) -> None:
    missing = PersistentAuditWriter(data_root=None).write(
        _facts(tmp_path),
        decision="manual",
        reason="audit_missing_root",
        degraded=False,
    )
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file", encoding="utf-8")
    failed = PersistentAuditWriter(data_root=blocked).write(
        _facts(tmp_path),
        decision="allow",
        reason="writer_failure",
        degraded=False,
    )
    assert missing.reason == "audit_root_unavailable"
    assert failed.reason == "audit_write_failed"


def test_persistent_audit_root_resolves_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "env-data"))
    assert resolve_persistent_audit_root({}) == tmp_path / "env-data"


def test_persistent_audit_sanitization_failure_degrades(tmp_path: Path) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path / "data")

    result = writer.write(
        _facts(tmp_path),
        decision="ask",
        reason="semantic_review_required",
        degraded=False,
        extra={"reviewer_reason_summary": ExplodingString()},
    )

    assert result.persisted is False
    assert result.degraded is True
    assert result.reason == "audit_write_failed"


def test_persistent_audit_redacts_sensitive_mapping_keys(tmp_path: Path) -> None:
    writer = PersistentAuditWriter(data_root=tmp_path / "data")
    sensitive_path = str(tmp_path / "private" / "credentials.txt")

    result = writer.write(
        _facts(tmp_path),
        decision="ask",
        reason="semantic_review_required",
        degraded=False,
        extra={"reviewer_reason_summary": {sensitive_path: "safe_value"}},
    )

    content = result.path.read_text(encoding="utf-8")
    record = json.loads(content)
    assert sensitive_path not in content
    assert record["reviewer_reason_summary"] == {"[redacted]": "safe_value"}


@pytest.mark.parametrize(
    "summary",
    [
        "The cat food costs $100",
        "Please find me another option",
    ],
)
def test_reviewer_summary_preserves_unambiguous_natural_language(summary: str) -> None:
    assert sanitize_audit_field("reviewer_reason_summary", summary) == summary


@pytest.mark.parametrize(
    "summary",
    [
        "$100",
        "$HOME",
        "${HOME}",
        "$(whoami)",
        "$?",
        "$$",
        "$!",
        "$#",
        "$@",
        "$*",
        "$-",
        "$0",
        "$1",
        "$'secret'",
        '$"text"',
        "$((1+2))",
        "cat secret.txt",
        "cat ./secret",
        "find /tmp",
        "git status",
        "curl --header value",
        "wget https://example.invalid/file",
        "cat file > output",
        "cat file && echo done",
        "The cat food costs $100 and $HOME is set",
        "The cat food costs $100 but use cat secret.txt",
        "The command is cat secret.txt",
        "The operation is find credentials",
        "The reviewer approves cat private-key.pem",
    ],
)
def test_reviewer_summary_redacts_ambiguous_or_command_like_text(summary: str) -> None:
    assert sanitize_audit_field("reviewer_reason_summary", summary) == AUDIT_TEXT_REDACTED


def test_natural_language_exception_does_not_relax_other_or_nested_fields() -> None:
    summary = "The cat food costs $100"

    assert sanitize_audit_field("reason", summary) == AUDIT_TEXT_REDACTED
    assert sanitize_audit_field("unknown", summary) == AUDIT_TEXT_REDACTED
    assert sanitize_audit_field("reviewer_reason_summary", {"note": summary}) == {
        "note": AUDIT_TEXT_REDACTED
    }
