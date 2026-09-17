# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for project-isolated coding-memory storage paths."""

from pathlib import Path

import pytest

from jiuwenswarm.common import coding_memory_paths
from jiuwenswarm.common.coding_memory_paths import (
    CODING_MEMORY_PROJECT_HASH_LENGTH,
    CODING_MEMORY_PROJECT_NAME_MAX_BYTES,
    DEFAULT_CODING_MEMORY_PROJECT,
    resolve_coding_memory_project_name,
    resolve_project_coding_memory_dir,
)


def test_same_project_path_resolves_to_stable_memory_key(tmp_path: Path) -> None:
    project_dir = tmp_path / "frontend"
    project_dir.mkdir()

    direct = resolve_coding_memory_project_name(project_dir)
    equivalent = resolve_coding_memory_project_name(project_dir / ".")

    assert direct == equivalent
    prefix, digest = direct.rsplit("-", 1)
    assert prefix == "frontend"
    assert len(digest) == CODING_MEMORY_PROJECT_HASH_LENGTH
    assert all(character in "0123456789abcdef" for character in digest)


def test_same_named_projects_at_different_paths_are_isolated(tmp_path: Path) -> None:
    first_project = tmp_path / "first-workspace" / "frontend"
    second_project = tmp_path / "second-workspace" / "frontend"
    first_project.mkdir(parents=True)
    second_project.mkdir(parents=True)

    first_key = resolve_coding_memory_project_name(first_project)
    second_key = resolve_coding_memory_project_name(second_project)

    assert first_key.startswith("frontend-")
    assert second_key.startswith("frontend-")
    assert first_key != second_key


def test_project_memory_directory_uses_isolated_key(tmp_path: Path) -> None:
    agent_workspace = tmp_path / "agent-workspace"
    project_dir = tmp_path / "workspace" / "frontend"

    resolved = Path(
        resolve_project_coding_memory_dir(
            agent_workspace_dir=agent_workspace,
            project_dir=project_dir,
        )
    )

    assert resolved.parent == agent_workspace.absolute() / "coding_memory"
    assert resolved.name == resolve_coding_memory_project_name(project_dir)


def test_missing_project_path_uses_default_key() -> None:
    assert resolve_coding_memory_project_name(None) == DEFAULT_CODING_MEMORY_PROJECT
    assert resolve_coding_memory_project_name("") == DEFAULT_CODING_MEMORY_PROJECT


def test_non_empty_root_path_keeps_hashed_project_identity(tmp_path: Path) -> None:
    key = resolve_coding_memory_project_name(tmp_path.anchor)

    assert key.startswith("project-")
    assert key != DEFAULT_CODING_MEMORY_PROJECT


def test_sanitized_empty_project_names_remain_isolated(tmp_path: Path) -> None:
    first_key = resolve_coding_memory_project_name(tmp_path / "first" / "...")
    second_key = resolve_coding_memory_project_name(tmp_path / "second" / "...")

    assert first_key != DEFAULT_CODING_MEMORY_PROJECT
    assert second_key != DEFAULT_CODING_MEMORY_PROJECT
    assert first_key != second_key


def test_project_prefix_is_limited_by_utf8_bytes(tmp_path: Path) -> None:
    key = resolve_coding_memory_project_name(tmp_path / (chr(0x1F600) * 80))
    prefix, _digest = key.rsplit("-", 1)

    assert len(prefix.encode("utf-8")) <= CODING_MEMORY_PROJECT_NAME_MAX_BYTES


def test_symlink_alias_uses_same_project_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "actual-project"
    alias_dir = tmp_path / "alias-project"
    project_dir.mkdir()
    canonical_path = coding_memory_paths._normalize_coding_memory_project_path(
        project_dir
    )
    original_realpath = coding_memory_paths.os.path.realpath

    def _resolve_alias(path: str) -> str:
        if coding_memory_paths.os.path.abspath(path) == str(alias_dir.absolute()):
            return canonical_path
        return original_realpath(path)

    monkeypatch.setattr(coding_memory_paths.os.path, "realpath", _resolve_alias)

    assert resolve_coding_memory_project_name(alias_dir) == (
        resolve_coding_memory_project_name(project_dir)
    )
