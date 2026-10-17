# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path, PureWindowsPath
from time import monotonic

# TEST ONLY: URL and credential-shaped strings are synthetic redaction inputs on
# RFC-reserved domains; this module performs no external network I/O.

import jiuwenswarm.agents.harness.common.workspace_paths as workspace_paths
from jiuwenswarm.agents.harness.common.workspace_paths import (
    STALE_SANDBOX_ARTIFACT_PATH,
    display_workspace_path,
    resolve_workspace_path,
    sanitize_review_ui_value,
    sanitize_visible_value,
)


def test_resolve_workspace_uri_stays_inside_current_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)

    resolved = resolve_workspace_path(
        "workspace://current/reports/out.pptx", workspace_root
    )

    assert resolved == workspace_root / "reports" / "out.pptx"


def test_resolve_workspace_uri_rejects_encoded_escape(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True)

    assert (
        resolve_workspace_path("workspace://current/%2e%2e/old.txt", workspace_root)
        is None
    )


def test_resolve_workspace_path_rejects_legacy_sandbox_artifact_path(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    legacy_path = tmp_path / ".sandbox-artifacts" / "sess_old" / "old.txt"
    workspace_root.mkdir(parents=True)

    assert resolve_workspace_path(legacy_path, workspace_root) is None


def test_display_current_workspace_path_as_canonical_path(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    path = workspace_root / "reports" / "out.pptx"

    assert display_workspace_path(path, workspace_root) == path.as_posix()


def test_sanitize_visible_value_preserves_current_paths_and_redacts_stale_paths(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    current_path = workspace_root / "reports" / "out.pptx"
    stale_path = tmp_path / ".sandbox-artifacts" / "sess_old" / "reports" / "old.pptx"
    value = {
        "stdout": f"created {current_path}",
        "stderr": f"stale {stale_path}",
        "items": [str(current_path)],
    }

    sanitized = sanitize_visible_value(value, workspace_root)

    assert sanitized["stdout"] == f"created {current_path}"
    assert sanitized["stderr"] == f"stale {STALE_SANDBOX_ARTIFACT_PATH}"
    assert sanitized["items"] == [str(current_path)]


def test_sanitize_visible_value_sanitizes_mapping_keys(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    current_path = workspace_root / "reports" / "out.pptx"
    stale_path = tmp_path / ".sandbox-artifacts" / "sess_old" / "old.pptx"

    sanitized = sanitize_visible_value(
        {
            str(current_path): "current",
            str(stale_path): "stale",
        },
        workspace_root,
    )

    assert sanitized == {
        str(current_path): "current",
        STALE_SANDBOX_ARTIFACT_PATH: "stale",
    }


def test_sanitize_visible_text_redacts_stale_sandbox_path_with_spaces(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    stale_path = tmp_path / ".sandbox-artifacts" / "sess_old" / "secret report.pdf"

    sanitized = sanitize_visible_value(
        {"stderr": f"stale={stale_path}"}, workspace_root
    )

    assert sanitized["stderr"] == f"stale={STALE_SANDBOX_ARTIFACT_PATH}"
    assert "secret report.pdf" not in sanitized["stderr"]


def test_sanitize_visible_text_preserves_current_path_when_stale_path_follows(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    current_path = workspace_root / "outputs" / "report.pptx"
    stale_path = tmp_path / ".sandbox-artifacts" / "sess_old" / "old.pptx"

    sanitized = sanitize_visible_value(
        {"command": f"--out {current_path} --old {stale_path}"},
        workspace_root,
    )

    assert str(current_path) in sanitized["command"]
    assert STALE_SANDBOX_ARTIFACT_PATH in sanitized["command"]


def test_sanitize_review_ui_value_relativizes_workspace_paths_and_preserves_other_paths(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace with spaces"
    current_path = workspace_root / "reports" / "out.pptx"
    stale_path = tmp_path / ".sandbox-artifacts" / "sess_old" / "old.pptx"
    similarly_named_external_path = Path(f"{workspace_root}-backup") / "old.pptx"
    value = {
        "path": str(current_path),
        "summary": (
            f"created {current_path} then checked "
            "/home/test-user/.jiuwenswarm/agent/workspace"
        ),
        "stale": str(stale_path),
        "similarly_named_external": str(similarly_named_external_path),
    }

    sanitized = sanitize_review_ui_value(value, workspace_root)

    assert sanitized["path"] == "reports/out.pptx"
    assert sanitized["summary"] == (
        "created reports/out.pptx then checked "
        "/home/test-user/.jiuwenswarm/agent/workspace"
    )
    assert sanitized["stale"] == str(stale_path)
    assert sanitized["similarly_named_external"] == str(similarly_named_external_path)


def test_sanitize_review_ui_value_preserves_secrets_redaction(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    current_path = workspace_root / "reports" / "out.pptx"
    synthetic_bearer = "Bearer " + ("A" * 16)
    synthetic_provider_token = "sk-" + ("T" * 26)
    value = {
        "cmd": (
            f"AUTHORIZATION={synthetic_bearer} save {current_path} "
            "https://example.invalid/?token=TEST_ONLY_TOKEN"
        ),
        "token": synthetic_provider_token,
    }

    sanitized = sanitize_review_ui_value(value, workspace_root)

    assert "AUTHORIZATION=[redacted]" in sanitized["cmd"]
    assert "token=[redacted]" in sanitized["cmd"]
    assert "reports/out.pptx" in sanitized["cmd"]
    assert synthetic_bearer not in sanitized["cmd"]
    assert synthetic_provider_token not in str(sanitized)
    assert sanitized["token"] == "[redacted]"


def test_sanitize_review_ui_value_preserves_traversal_and_file_uri_paths(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace with spaces"
    traversal_path = f"{workspace_root}/../outside.txt"
    nested_traversal_path = f"{workspace_root}/reports/../../outside.txt"
    file_uri = f"file://{workspace_root}/reports/out.pptx"

    sanitized = sanitize_review_ui_value(
        {
            "traversal": traversal_path,
            "nested_traversal": nested_traversal_path,
            "uri": file_uri,
        },
        workspace_root,
    )

    assert sanitized == {
        "traversal": traversal_path,
        "nested_traversal": nested_traversal_path,
        "uri": file_uri,
    }


def test_sanitize_review_ui_value_relativizes_windows_workspace_path_with_spaces(
    monkeypatch,
) -> None:
    workspace_root = PureWindowsPath(r"C:\workspace with spaces")
    monkeypatch.setattr(
        workspace_paths,
        "normalize_workspace_root",
        lambda _workspace_root: workspace_root,
    )

    sanitized = workspace_paths.sanitize_review_ui_value(
        {"path": r"C:\workspace with spaces\reports\out.pptx"},
        "unused",
    )

    assert sanitized["path"] == r"reports\out.pptx"


def test_sanitize_review_ui_value_handles_deep_non_traversal_path_promptly(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    nested_path = "/".join(f"segment_{index}" for index in range(300))

    started_at = monotonic()
    sanitized = sanitize_review_ui_value(
        {"path": f"{workspace_root}/{nested_path}"},
        workspace_root,
        max_length=10000,
    )

    assert sanitized["path"] == nested_path
    assert monotonic() - started_at < 1.0
