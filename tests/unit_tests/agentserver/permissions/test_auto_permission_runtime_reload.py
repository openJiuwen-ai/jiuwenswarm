"""Same-rail Auto Permission runtime reload contracts."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import (
    lifecycle,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
    AutoPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    AutoReviewer,
    IsolatedModelReviewerClient,
)
from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import (
    PersistentAuditWriter,
)
from tests.unit_tests.agentserver.permissions.auto_permission_test_support import (
    FakeBaseRail,
)


def _auto_config(
    *,
    timeout_ms: int,
    output_format: str = "json_object",
    audit_enabled: bool,
    audit_root: str,
) -> dict[str, object]:
    return {
        "enabled": True,
        "mode": "auto",
        "auto": {
            "reviewer_timeout_ms": timeout_ms,
            "reviewer_output_format": output_format,
            "persistent_audit_enabled": audit_enabled,
            "persistent_audit_root": audit_root,
        },
    }


def test_same_rail_reload_refreshes_reviewer_and_audit_components(
    tmp_path,
) -> None:
    base = FakeBaseRail()
    reviewer = AutoReviewer(
        client=IsolatedModelReviewerClient(model=object()),
        timeout_ms=1000,
    )
    rail = AutoPermissionInterruptRail(
        base_rail=base,
        permission_config=_auto_config(
            timeout_ms=1000,
            audit_enabled=False,
            audit_root=str(tmp_path / "old"),
        ),
        workspace_root=tmp_path,
        auto_reviewer=reviewer,
    )
    updated = _auto_config(
        timeout_ms=4321,
        output_format="prompt",
        audit_enabled=True,
        audit_root=str(tmp_path / "new"),
    )

    rail.update_config(updated)

    assert rail.auto_reviewer is reviewer
    assert reviewer.timeout_seconds == pytest.approx(4.321)
    assert reviewer.client.output_format == "prompt"
    assert isinstance(rail.persistent_audit_writer, PersistentAuditWriter)
    assert rail.persistent_audit_writer.data_root == tmp_path / "new"
    first_writer = rail.persistent_audit_writer

    rail.update_config(updated)
    assert rail.persistent_audit_writer is first_writer

    disabled = _auto_config(
        timeout_ms=2000,
        audit_enabled=False,
        audit_root=str(tmp_path / "new"),
    )
    rail.update_config(disabled)

    assert reviewer.timeout_seconds == pytest.approx(2.0)
    assert reviewer.client.output_format == "json_object"
    assert "reviewer_min_confidence" not in rail.auto_options
    assert rail.persistent_audit_writer is None


def test_runtime_prepare_failure_preserves_installed_components(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = FakeBaseRail()
    reviewer = AutoReviewer(
        client=IsolatedModelReviewerClient(model=object()),
        timeout_ms=1000,
    )
    old_config = _auto_config(
        timeout_ms=1000,
        audit_enabled=False,
        audit_root=str(tmp_path / "old"),
    )
    rail = AutoPermissionInterruptRail(
        base_rail=base,
        permission_config=old_config,
        workspace_root=tmp_path,
        auto_reviewer=reviewer,
    )

    class FailingWriter:
        def __init__(self, *, data_root):
            del data_root
            raise RuntimeError("audit_writer_failed")

    monkeypatch.setattr(lifecycle, "PersistentAuditWriter", FailingWriter)

    with pytest.raises(RuntimeError, match="audit_writer_failed"):
        rail.update_config(
            _auto_config(
                timeout_ms=9000,
                output_format="prompt",
                audit_enabled=True,
                audit_root=str(tmp_path / "new"),
            )
        )

    assert reviewer.timeout_seconds == pytest.approx(1.0)
    assert not hasattr(reviewer, "min_confidence")
    assert reviewer.client.output_format == "json_object"
    assert rail.persistent_audit_writer is None
    assert rail.permission_config["auto"]["reviewer_timeout_ms"] == 1000
    assert base.config_updates == []
