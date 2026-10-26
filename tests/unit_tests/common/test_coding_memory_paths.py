# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import coding_memory_paths
from jiuwenswarm.common.coding_memory_paths import (
    CodingMemoryMigrationResult,
    prepare_project_coding_memory_dir,
)


def _memory(name: str, description: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "type: project\n"
        "---\n\n"
        f"{body}\n"
    )


def _agent_workspace(root: Path) -> Path:
    return root / "workspace_default" / "agent" / "workspace"


def _prepare_in_subprocess(
    paths: tuple[Path, Path],
) -> CodingMemoryMigrationResult:
    agent_workspace, project = paths
    return prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )


def test_project_migration_copies_only_authoritative_markdown_and_is_incremental(
    tmp_path: Path,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "projects" / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    first_content = _memory("First", "First description", "first body")
    (legacy / "first.md").write_text(first_content, encoding="utf-8")
    (legacy / "MEMORY.md").write_text(
        "- [First](first.md) — First description\n",
        encoding="utf-8",
    )
    (legacy / "memory.db").write_bytes(b"derived")
    (legacy / "nested").mkdir()
    (legacy / "nested" / "ignored.md").write_text("ignored", encoding="utf-8")

    first = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    target = Path(first.target_dir)
    assert first.failed is False
    assert first.sources_migrated == 1
    assert first.copied == 1
    assert (target / "first.md").read_text(encoding="utf-8") == first_content
    assert not (target / "memory.db").exists()
    assert not (target / "nested").exists()
    assert (legacy / "first.md").read_text(encoding="utf-8") == first_content
    report = json.loads(
        (target / ".coding-memory-migration-v1.json").read_text(encoding="utf-8")
    )
    assert report["version"] == 1

    unchanged = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )
    assert unchanged.sources_migrated == 0
    assert unchanged.copied == 0

    second_content = _memory("Second", "Second description", "second body")
    (legacy / "second.md").write_text(second_content, encoding="utf-8")
    incremental = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )
    assert incremental.sources_migrated == 1
    assert incremental.copied == 1
    assert incremental.duplicates == 1
    assert (target / "second.md").read_text(encoding="utf-8") == second_content


def test_migration_deduplicates_content_and_renames_filename_conflicts(
    tmp_path: Path,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    target = agent_workspace / "coding_memory" / "demo"
    legacy.mkdir(parents=True)
    target.mkdir(parents=True)

    legacy_conflict = _memory("Legacy", "Legacy description", "legacy body")
    duplicate = _memory("Duplicate", "Duplicate description", "same body")
    (legacy / "same.md").write_text(legacy_conflict, encoding="utf-8")
    (legacy / "duplicate-old.md").write_text(duplicate, encoding="utf-8")
    (legacy / "MEMORY.md").write_text(
        "- [Legacy](same.md) — Legacy description\n"
        "- [Duplicate](duplicate-old.md) — Duplicate description\n",
        encoding="utf-8",
    )
    (target / "same.md").write_text(
        _memory("New", "New description", "new body"),
        encoding="utf-8",
    )
    (target / "already-there.md").write_text(duplicate, encoding="utf-8")
    (target / "MEMORY.md").write_text(
        "- [New](same.md) — New description\n",
        encoding="utf-8",
    )

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    legacy_hash = hashlib.sha256((legacy / "same.md").read_bytes()).hexdigest()[:8]
    renamed = f"same__legacy_{legacy_hash}.md"
    assert result.renamed == 1
    assert result.duplicates == 1
    assert (target / renamed).read_text(encoding="utf-8") == legacy_conflict
    assert not (target / "duplicate-old.md").exists()
    index = (target / "MEMORY.md").read_text(encoding="utf-8")
    assert index.splitlines()[0] == "- [New](same.md) — New description"
    assert f"- [Legacy]({renamed}) — Legacy description" in index
    assert "(already-there.md)" in index


def test_default_workspace_migrates_only_known_default_legacy_location(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".jiuwenswarm"
    agent_workspace = _agent_workspace(data_root)
    legacy = (
        data_root
        / "service_default"
        / "agent_default"
        / "agent"
        / "jiuwenclaw_workspace"
        / "coding_memory"
    )
    legacy.mkdir(parents=True)
    (legacy / "default-note.md").write_text(
        _memory("Default", "Default description", "default body"),
        encoding="utf-8",
    )
    unrelated = (
        data_root
        / "service_other"
        / "agent_other"
        / "agent"
        / "jiuwenclaw_workspace"
        / "coding_memory"
    )
    unrelated.mkdir(parents=True)
    (unrelated / "must-not-copy.md").write_text("secret", encoding="utf-8")

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=None,
    )

    target = Path(result.target_dir)
    assert (target / "default-note.md").is_file()
    assert not (target / "must-not-copy.md").exists()
    assert result.sources_found == 1


def test_default_workspace_migrates_flat_and_previous_root_locations(
    tmp_path: Path,
) -> None:
    current_root = tmp_path / ".jiuwenswarm"
    previous_root = tmp_path / ".jiuwenclaw"
    agent_workspace = _agent_workspace(current_root)
    sources = [
        current_root / "agent" / "jiuwenclaw_workspace" / "coding_memory",
        previous_root
        / "service_default"
        / "agent_default"
        / "agent"
        / "jiuwenclaw_workspace"
        / "coding_memory",
    ]
    for index, source in enumerate(sources):
        source.mkdir(parents=True)
        (source / f"legacy-{index}.md").write_text(
            _memory(f"Legacy {index}", f"Description {index}", f"body {index}"),
            encoding="utf-8",
        )

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=None,
    )

    target = Path(result.target_dir)
    assert result.sources_found == 2
    assert result.sources_migrated == 2
    assert (target / "legacy-0.md").is_file()
    assert (target / "legacy-1.md").is_file()


def test_non_default_unbound_workspace_does_not_import_default_tenant(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".jiuwenswarm"
    agent_workspace = data_root / "workspace_customer" / "agent" / "workspace"
    legacy = (
        data_root
        / "service_default"
        / "agent_default"
        / "agent"
        / "jiuwenclaw_workspace"
        / "coding_memory"
    )
    legacy.mkdir(parents=True)
    (legacy / "default-only.md").write_text("default tenant", encoding="utf-8")

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=None,
    )

    assert result.sources_found == 0
    assert not (Path(result.target_dir) / "default-only.md").exists()


def test_unknown_unbound_workspace_layout_does_not_import_default_tenant(
    tmp_path: Path,
) -> None:
    tenant_root = tmp_path / "tenant-customer"
    agent_workspace = tenant_root / "agent" / "workspace"
    legacy = (
        tenant_root
        / "service_default"
        / "agent_default"
        / "agent"
        / "jiuwenclaw_workspace"
        / "coding_memory"
    )
    legacy.mkdir(parents=True)
    (legacy / "default-only.md").write_text("default tenant", encoding="utf-8")

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=None,
    )

    assert result.sources_found == 0
    assert not (Path(result.target_dir) / "default-only.md").exists()


def test_default_legacy_named_agent_workspace_is_recognized(tmp_path: Path) -> None:
    data_root = tmp_path / ".jiuwenswarm"
    agent_workspace = (
        data_root / "workspace_default" / "agent" / "jiuwenclaw_workspace"
    )
    legacy = (
        data_root
        / "service_default"
        / "agent_default"
        / "agent"
        / "jiuwenclaw_workspace"
        / "coding_memory"
    )
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text("legacy", encoding="utf-8")

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=None,
    )

    assert (Path(result.target_dir) / "legacy.md").is_file()


def test_index_limit_does_not_drop_imported_memory_files(tmp_path: Path) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    target = agent_workspace / "coding_memory" / "demo"
    legacy.mkdir(parents=True)
    target.mkdir(parents=True)
    (target / "MEMORY.md").write_text(
        "\n".join(f"# existing {index}" for index in range(199)),
        encoding="utf-8",
    )
    for index in range(2):
        (legacy / f"legacy-{index}.md").write_text(
            _memory(
                f"Legacy {index}",
                f"Legacy description {index}",
                f"body {index}",
            ),
            encoding="utf-8",
        )

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    assert result.index_truncated == 1
    assert len((target / "MEMORY.md").read_text(encoding="utf-8").splitlines()) == 200
    assert (target / "legacy-0.md").is_file()
    assert (target / "legacy-1.md").is_file()

    repeated = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )
    assert repeated.sources_migrated == 0
    assert repeated.index_truncated == 0


def test_completed_migration_fast_path_does_not_hash_file_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text("legacy body", encoding="utf-8")
    prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    def _unexpected_hash(_path: Path) -> str:
        raise AssertionError("completed migration should not hash file contents")

    class _UnexpectedLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("completed migration should not acquire the lock")

    monkeypatch.setattr(coding_memory_paths, "_hash_file", _unexpected_hash)
    monkeypatch.setattr(coding_memory_paths.portalocker, "Lock", _UnexpectedLock)
    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    assert result.sources_found == 1
    assert result.sources_migrated == 0


def test_index_metadata_falls_back_to_legacy_index_then_filename(
    tmp_path: Path,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    (legacy / "indexed.md").write_text("no frontmatter", encoding="utf-8")
    (legacy / "orphan.md").write_text("also no frontmatter", encoding="utf-8")
    (legacy / "MEMORY.md").write_text(
        "- [Legacy title](indexed.md) — Legacy description\n",
        encoding="utf-8",
    )

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    index = (Path(result.target_dir) / "MEMORY.md").read_text(encoding="utf-8")
    assert index.splitlines() == [
        "- [Legacy title](indexed.md) — Legacy description",
        "- [orphan](orphan.md) — Imported legacy coding memory",
    ]


def test_failed_copy_is_fail_open_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text(
        _memory("Legacy", "Legacy description", "body"),
        encoding="utf-8",
    )
    original_atomic_write = coding_memory_paths._atomic_write_bytes

    def _fail_memory_copy(path: Path, content: bytes) -> None:
        if path.name == "legacy.md":
            raise OSError("simulated read-only target")
        original_atomic_write(path, content)

    monkeypatch.setattr(coding_memory_paths, "_atomic_write_bytes", _fail_memory_copy)
    failed = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )
    assert failed.failed is True
    assert not (
        Path(failed.target_dir) / ".coding-memory-migration-v1.json"
    ).exists()
    assert (legacy / "legacy.md").is_file()

    monkeypatch.setattr(coding_memory_paths, "_atomic_write_bytes", original_atomic_write)
    retried = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )
    assert retried.failed is False
    assert (Path(retried.target_dir) / "legacy.md").is_file()


def test_report_serialization_handles_surrogate_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text("legacy", encoding="utf-8")
    original_canonical_path = coding_memory_paths._canonical_path

    def _canonical_path_with_surrogate(path: Path) -> str:
        value = original_canonical_path(path)
        return value + "\udcff" if path == legacy else value

    monkeypatch.setattr(
        coding_memory_paths,
        "_canonical_path",
        _canonical_path_with_surrogate,
    )
    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    assert result.failed is False
    report = (
        Path(result.target_dir) / ".coding-memory-migration-v1.json"
    ).read_text(encoding="utf-8")
    assert "\\udcff" in report


def test_unexpected_report_write_error_is_fail_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text("legacy", encoding="utf-8")
    original_atomic_write = coding_memory_paths._atomic_write_text

    def _fail_report(path: Path, content: str) -> None:
        if path.name == ".coding-memory-migration-v1.json":
            raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogate")
        original_atomic_write(path, content)

    monkeypatch.setattr(coding_memory_paths, "_atomic_write_text", _fail_report)
    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    assert result.failed is True
    assert (legacy / "legacy.md").is_file()


def test_lock_failure_is_fail_open_and_leaves_source_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    source_file = legacy / "legacy.md"
    source_file.write_text("legacy body", encoding="utf-8")

    class _UnavailableLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> None:
            raise coding_memory_paths.portalocker.exceptions.LockException(
                "simulated timeout"
            )

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(coding_memory_paths.portalocker, "Lock", _UnavailableLock)
    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    assert result.failed is True
    assert source_file.read_text(encoding="utf-8") == "legacy body"
    assert not (
        Path(result.target_dir) / ".coding-memory-migration-v1.json"
    ).exists()


def test_symbolic_link_markdown_is_not_migrated(tmp_path: Path) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = legacy / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    result = prepare_project_coding_memory_dir(
        agent_workspace_dir=agent_workspace,
        project_dir=project,
    )

    assert not (Path(result.target_dir) / "linked.md").exists()
    assert result.warnings


def test_concurrent_prepare_is_idempotent(tmp_path: Path) -> None:
    agent_workspace = _agent_workspace(tmp_path / "data")
    project = tmp_path / "demo"
    legacy = project / "coding_memory"
    legacy.mkdir(parents=True)
    (legacy / "legacy.md").write_text(
        _memory("Legacy", "Legacy description", "body"),
        encoding="utf-8",
    )

    with ProcessPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                _prepare_in_subprocess,
                [(agent_workspace, project)] * 2,
            )
        )

    target = Path(results[0].target_dir)
    assert sum(result.sources_migrated for result in results) == 1
    assert sorted(path.name for path in target.glob("*.md")) == ["MEMORY.md", "legacy.md"]


@pytest.mark.asyncio
async def test_auto_extraction_prepares_migration_before_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.auto_memory import extraction_runner
    from jiuwenswarm.agents.harness.common.auto_memory import extract_memories
    from jiuwenswarm.common import utils

    project = tmp_path / "demo"
    project.mkdir()
    agent_workspace = _agent_workspace(tmp_path / "data")
    legacy = project / "coding_memory"
    legacy.mkdir()
    (legacy / "legacy.md").write_text(
        _memory("Legacy", "Legacy description", "body"),
        encoding="utf-8",
    )
    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: agent_workspace)
    monkeypatch.setattr(
        extract_memories,
        "_check_coding_memory_write_in_history",
        lambda _history: False,
    )
    run_extraction = AsyncMock()
    monkeypatch.setattr(
        extraction_runner,
        "_run_memory_extraction_with_cache_sharing",
        run_extraction,
    )

    await extraction_runner._execute_auto_memory_extraction(
        project_dir=str(project),
        session_id="session",
        messages=[{"role": "user", "content": "remember this"}],
    )

    target = agent_workspace / "coding_memory" / "demo"
    assert (target / "legacy.md").is_file()
    assert run_extraction.await_count == 1
    assert run_extraction.await_args.kwargs["memory_dir"] == target
