# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded workspace directory browsing for session files Ops."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session import workspace_files
from jiuwenswarm.server.runtime.session.workspace_files import SessionWorkspaceFiles


def _bind_project(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(
        workspace_files,
        "get_session_metadata",
        lambda session_id, **_: {"project_dir": str(root), "mode": "agent.work.normal"},
    )


@pytest.mark.asyncio
async def test_files_returns_sorted_root_with_unloaded_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    (root / "beta").mkdir(parents=True)
    (root / "beta" / "inner.txt").write_text("inner", encoding="utf-8")
    (root / "alpha.txt").write_text("alpha", encoding="utf-8")
    _bind_project(monkeypatch, root)

    result = await SessionWorkspaceFiles().files("s1", [])

    assert result["status"] == "ready"
    assert [(node["title"], node["kind"]) for node in result["treeData"]] == [
        ("beta", "directory"),
        ("alpha.txt", "file"),
    ]
    assert result["treeData"][0]["loaded"] is False
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_files_expands_only_requested_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    (root / "beta" / "nested").mkdir(parents=True)
    (root / "beta" / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    _bind_project(monkeypatch, root)

    result = await SessionWorkspaceFiles().files("s1", ["beta"])

    assert result["status"] == "ready"
    beta = result["treeData"][0]
    assert beta["key"] == "beta"
    assert beta["loaded"] is True
    assert beta["children"][0]["title"] == "nested"
    assert beta["children"][0]["loaded"] is False


@pytest.mark.asyncio
async def test_files_rejects_paths_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _bind_project(monkeypatch, root)

    for invalid in ("../outside", "/etc", "C:/Windows", "a\\b", "a//b"):
        result = await SessionWorkspaceFiles().files("s1", [invalid])
        assert result["status"] == "error", invalid
        assert result["treeData"] == []


@pytest.mark.asyncio
async def test_files_truncates_directories_beyond_the_entry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    for index in range(workspace_files.MAX_DIRECTORY_ENTRIES + 1):
        (root / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")
    _bind_project(monkeypatch, root)

    result = await SessionWorkspaceFiles().files("s1", [])

    assert result["status"] == "ready"
    assert result["truncated"] is True
    assert len(result["treeData"]) == workspace_files.MAX_DIRECTORY_ENTRIES


@pytest.mark.asyncio
async def test_files_reports_unavailable_without_a_workspace_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workspace_files, "get_session_metadata", lambda session_id, **_: {})
    monkeypatch.setattr(workspace_files, "get_registered_projectless_task_root", lambda session_id: None)

    result = await SessionWorkspaceFiles().files("s1", [])

    assert result["status"] == "unavailable"
    assert result["treeData"] == []


@pytest.mark.asyncio
async def test_read_file_returns_small_utf8_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_bytes(b"print('hi')\n")
    _bind_project(monkeypatch, root)

    result = await SessionWorkspaceFiles().read_file("s1", "src/main.py")

    assert result["status"] == "ready"
    assert result["file"]["content"] == "print('hi')\n"
    assert result["file"]["path"] == "src/main.py"


@pytest.mark.asyncio
async def test_read_file_rejects_oversized_and_binary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "big.txt").write_text("x" * (workspace_files.MAX_TEXT_BYTES + 1), encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\0\1\2")
    (root / "pic.png").write_bytes(b"\x89PNG\r\n")
    _bind_project(monkeypatch, root)
    service = SessionWorkspaceFiles()

    assert (await service.read_file("s1", "big.txt"))["status"] == "too_large"
    assert (await service.read_file("s1", "blob.bin"))["status"] == "unsupported"
    assert (await service.read_file("s1", "pic.png"))["status"] == "unsupported"


@pytest.mark.asyncio
async def test_read_file_reports_missing_and_forbidden_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _bind_project(monkeypatch, root)
    service = SessionWorkspaceFiles()

    assert (await service.read_file("s1", "gone.txt"))["status"] == "missing"
    assert (await service.read_file("s1", "../outside.txt"))["status"] == "forbidden"
