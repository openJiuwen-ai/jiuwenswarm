# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""workspace path normalize + delete nofollow regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.common.workspace.service import WorkspaceService
from jiuwenswarm.common.workspace.zones import normalize_relative_path

_DELETABLE_PREFIX = "agent/jiuwenclaw_workspace/projects"


def test_normalize_relative_path_accepts_relative() -> None:
    assert normalize_relative_path("a/b/c") == "a/b/c"
    assert normalize_relative_path("./a/./b/") == "a/b"
    assert normalize_relative_path("") == ""
    assert normalize_relative_path(".") == ""
    assert normalize_relative_path("./") == ""


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("../escape", "path_traversal"),
        ("a/../../b", "path_traversal"),
        ("a/\x00/b", "path_traversal"),
        ("/abs/path", "absolute_path"),
        ("//unc/share", "absolute_path"),
        ("C:/Windows", "absolute_path"),
        ("c:\\Windows\\System32", "absolute_path"),
    ],
)
def test_normalize_relative_path_rejects_unsafe(raw: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        normalize_relative_path(raw)


def _make_deletable_tree(root: Path) -> Path:
    target = root / "agent" / "jiuwenclaw_workspace" / "projects" / "demo"
    target.mkdir(parents=True)
    (target / "keep-me.txt").write_text("payload", encoding="utf-8")
    return target


def test_delete_removes_symlink_without_following_target(tmp_path: Path) -> None:
    tenant = tmp_path / "tenant"
    tenant.mkdir()
    projects = _make_deletable_tree(tenant)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "do-not-delete.txt"
    victim.write_text("safe", encoding="utf-8")

    link = projects / "escape-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform")

    svc = WorkspaceService(tenant_root=tenant)
    rel = f"{_DELETABLE_PREFIX}/demo/escape-link"
    result = svc.delete_entries([rel])

    assert result["results"] == [
        {"relative_path": rel, "ok": True, "error": None, "freed_bytes": 0}
    ]
    assert not link.exists()
    assert not link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "safe"


def test_delete_directory_does_not_follow_nested_symlink(tmp_path: Path) -> None:
    tenant = tmp_path / "tenant"
    tenant.mkdir()
    projects = _make_deletable_tree(tenant)
    nested = projects / "nested"
    nested.mkdir()
    (nested / "inner.txt").write_text("inner", encoding="utf-8")

    outside = tmp_path / "outside-nested"
    outside.mkdir()
    victim = outside / "keep.txt"
    victim.write_text("keep", encoding="utf-8")
    nested_link = nested / "link-out"
    try:
        nested_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires privilege on this platform")

    svc = WorkspaceService(tenant_root=tenant)
    rel = f"{_DELETABLE_PREFIX}/demo/nested"
    result = svc.delete_entries([rel])

    assert result["results"] == [
        {"relative_path": rel, "ok": True, "error": None, "freed_bytes": 5}
    ]
    assert not nested.exists()
    assert victim.read_text(encoding="utf-8") == "keep"


def test_delete_rejects_absolute_path(tmp_path: Path) -> None:
    tenant = tmp_path / "tenant"
    tenant.mkdir()
    svc = WorkspaceService(tenant_root=tenant)
    result = svc.delete_entries(["/tmp/evil"])
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error"] == "bad_path"


def test_delete_rejects_path_traversal(tmp_path: Path) -> None:
    tenant = tmp_path / "tenant"
    tenant.mkdir()
    svc = WorkspaceService(tenant_root=tenant)
    result = svc.delete_entries(["../outside"])
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error"] == "bad_path"
