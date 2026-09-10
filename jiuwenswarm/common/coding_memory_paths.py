# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve project-scoped Coding Memory paths and import legacy data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import ntpath
import os
from os import PathLike
from pathlib import Path
import re
import tempfile
from typing import Any

import portalocker


logger = logging.getLogger(__name__)

DEFAULT_CODING_MEMORY_PROJECT = "default"
_INDEX_FILENAME = "MEMORY.md"
_MIGRATION_REPORT_FILENAME = ".coding-memory-migration-v1.json"
_MIGRATION_REPORT_VERSION = 1
_MAX_INDEX_LINES = 200
_LOCK_TIMEOUT_SECONDS = 1
_INDEX_ENTRY_RE = re.compile(
    r"^\s*-\s+\[(?P<name>.+?)\]\((?P<path>[^)]+)\)\s+(?:—|-)\s+(?P<description>.*)$"
)


@dataclass(frozen=True)
class CodingMemoryMigrationResult:
    """Outcome of preparing one project-scoped Coding Memory directory."""

    target_dir: str
    source_paths: tuple[str, ...] = ()
    sources_found: int = 0
    sources_migrated: int = 0
    copied: int = 0
    duplicates: int = 0
    renamed: int = 0
    index_truncated: int = 0
    warnings: tuple[str, ...] = ()
    failed: bool = False


@dataclass(frozen=True)
class _LegacyMemoryFile:
    path: Path
    content_hash: str


def resolve_coding_memory_project_name(project_dir: str | PathLike[str] | None) -> str:
    """Return the project-scoped directory name used under coding_memory/."""
    if project_dir is None:
        return DEFAULT_CODING_MEMORY_PROJECT

    raw_project_dir = str(project_dir).strip()
    if not raw_project_dir:
        return DEFAULT_CODING_MEMORY_PROJECT

    project_name = ntpath.basename(raw_project_dir.rstrip("/\\"))
    project_name = project_name.replace("/", "_").replace("\\", "_").strip()
    if not project_name or project_name in {".", ".."}:
        return DEFAULT_CODING_MEMORY_PROJECT
    return project_name


def resolve_project_coding_memory_dir(
    *,
    agent_workspace_dir: str | PathLike[str],
    project_dir: str | PathLike[str] | None,
) -> str:
    """Resolve <agent_workspace>/coding_memory/<project_name>."""
    return os.path.join(
        os.path.abspath(str(agent_workspace_dir)),
        "coding_memory",
        resolve_coding_memory_project_name(project_dir),
    )


def resolve_project_coding_memory_workspace_path(
    *,
    project_dir: str | PathLike[str] | None,
) -> str:
    """Resolve the workspace-relative coding_memory/<project_name> path."""
    return os.path.join(
        "coding_memory",
        resolve_coding_memory_project_name(project_dir),
    )


def _canonical_path(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(str(path)))


def _data_root_from_agent_workspace(agent_workspace: Path) -> Path | None:
    """Return the root for <root>/<tenant>/agent/workspace layouts."""
    try:
        if (
            agent_workspace.name in {"workspace", "jiuwenclaw_workspace"}
            and agent_workspace.parent.name == "agent"
        ):
            tenant_root = agent_workspace.parents[1]
            if (
                tenant_root.name.startswith("agent_")
                and tenant_root.parent.name.startswith("service_")
            ):
                return tenant_root.parents[1]
            if tenant_root.name.startswith("workspace_"):
                return tenant_root.parent
            return tenant_root
    except IndexError:
        return None
    return None


def _is_default_agent_workspace(agent_workspace: Path) -> bool:
    """Return whether an unbound workspace has an unambiguous default identity."""
    try:
        if (
            agent_workspace.name not in {"workspace", "jiuwenclaw_workspace"}
            or agent_workspace.parent.name != "agent"
        ):
            return False
        tenant_root = agent_workspace.parents[1]
        tenant_name = tenant_root.name.casefold()
        if tenant_name == "workspace_default":
            return True
        if (
            tenant_name == "agent_default"
            and tenant_root.parent.name.casefold() == "service_default"
        ):
            return True
        return tenant_name in {".jiuwenswarm", ".jiuwenclaw"}
    except IndexError:
        return False


def _legacy_source_candidates(
    *,
    agent_workspace_dir: Path,
    project_dir: str | PathLike[str] | None,
) -> list[Path]:
    raw_project_dir = str(project_dir).strip() if project_dir is not None else ""
    if raw_project_dir:
        return [Path(raw_project_dir).expanduser() / "coding_memory"]

    if not _is_default_agent_workspace(agent_workspace_dir):
        return []

    data_root = _data_root_from_agent_workspace(agent_workspace_dir)
    roots: list[Path] = []
    if data_root is not None:
        roots.append(data_root)
        if data_root.name == ".jiuwenswarm":
            roots.append(data_root.parent / ".jiuwenclaw")

    candidates: list[Path] = []
    for root in roots:
        candidates.extend(
            [
                root
                / "service_default"
                / "agent_default"
                / "agent"
                / "jiuwenclaw_workspace"
                / "coding_memory",
                root / "agent" / "jiuwenclaw_workspace" / "coding_memory",
            ]
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _canonical_path(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_markdown_files(source: Path) -> tuple[list[_LegacyMemoryFile], list[str]]:
    files: list[_LegacyMemoryFile] = []
    warnings: list[str] = []
    for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if path.name.casefold() == _INDEX_FILENAME.casefold():
            continue
        if path.suffix.casefold() != ".md":
            continue
        if path.is_symlink():
            warnings.append(f"skipped symbolic link: {path.name}")
            continue
        if not path.is_file():
            continue
        files.append(_LegacyMemoryFile(path=path, content_hash=_hash_file(path)))
    return files, warnings


def _source_fingerprint(source: Path, files: list[_LegacyMemoryFile]) -> str:
    digest = hashlib.sha256()
    index_path = source / _INDEX_FILENAME
    if index_path.is_file() and not index_path.is_symlink():
        digest.update(_INDEX_FILENAME.encode("utf-8"))
        digest.update(_hash_file(index_path).encode("ascii"))
    for memory_file in files:
        digest.update(memory_file.path.name.encode("utf-8", errors="surrogatepass"))
        digest.update(memory_file.content_hash.encode("ascii"))
    return digest.hexdigest()


def _source_state_token(source: Path) -> str:
    """Hash cheap file metadata for the completed-migration fast path."""
    digest = hashlib.sha256()
    for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_symlink() or not path.is_file():
            continue
        if (
            path.name.casefold() != _INDEX_FILENAME.casefold()
            and path.suffix.casefold() != ".md"
        ):
            continue
        stat = path.stat()
        digest.update(path.name.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_ctime_ns).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "wb") as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _parse_index_entries(path: Path) -> tuple[list[str], dict[str, tuple[str, str]]]:
    if path.is_symlink():
        return [], {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return [], {}
    entries: dict[str, tuple[str, str]] = {}
    for line in lines:
        match = _INDEX_ENTRY_RE.match(line)
        if match:
            entries[os.path.normcase(match.group("path"))] = (
                match.group("name"),
                match.group("description"),
            )
    return lines, entries


def _frontmatter_title_and_description(path: Path) -> tuple[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    end = stripped.find("---", 3)
    if end < 0:
        return None
    fields: dict[str, str] = {}
    for line in stripped[3:end].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    name = fields.get("name")
    description = fields.get("description")
    if not name or not description:
        return None
    return name, description


def _conflict_filename(target: Path, source_file: _LegacyMemoryFile) -> str:
    stem = source_file.path.stem
    suffix = source_file.path.suffix
    for length in (8, 12, 16, 64):
        candidate = f"{stem}__legacy_{source_file.content_hash[:length]}{suffix}"
        candidate_path = target / candidate
        if candidate_path.is_symlink():
            continue
        if (
            not candidate_path.exists()
            or _hash_file(candidate_path) == source_file.content_hash
        ):
            return candidate
    raise FileExistsError(
        f"unable to allocate conflict filename for {source_file.path.name}"
    )


def _append_index_entries(
    *,
    target: Path,
    source: Path,
    final_names: dict[str, str],
) -> int:
    target_index = target / _INDEX_FILENAME
    target_lines, target_entries = _parse_index_entries(target_index)
    source_lines, source_entries = _parse_index_entries(source / _INDEX_FILENAME)
    appended: list[str] = []

    final_names_by_key = {
        os.path.normcase(name): value for name, value in final_names.items()
    }
    ordered_source_names: list[str] = []
    seen_source_names: set[str] = set()
    for line in source_lines:
        match = _INDEX_ENTRY_RE.match(line)
        if match:
            source_name = match.group("path")
            source_key = os.path.normcase(source_name)
            if source_key in final_names_by_key:
                ordered_source_names.append(source_name)
                seen_source_names.add(source_key)
    ordered_source_names.extend(
        name for name in final_names if os.path.normcase(name) not in seen_source_names
    )

    for source_name in ordered_source_names:
        final_name = final_names_by_key[os.path.normcase(source_name)]
        if os.path.normcase(final_name) in target_entries:
            continue
        metadata = _frontmatter_title_and_description(target / final_name)
        if metadata is None:
            metadata = source_entries.get(os.path.normcase(source_name))
        if metadata is None:
            metadata = (Path(final_name).stem, "Imported legacy coding memory")
        name, description = metadata
        appended.append(f"- [{name}]({final_name}) — {description}")
        target_entries[os.path.normcase(final_name)] = metadata

    if not appended:
        return 0
    merged_lines = target_lines + appended
    truncated = max(0, len(merged_lines) - _MAX_INDEX_LINES)
    _atomic_write_text(
        target_index,
        "\n".join(merged_lines[:_MAX_INDEX_LINES]),
    )
    return truncated


def _is_regular_memory_markdown(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if path.suffix.casefold() != ".md":
        return False
    return path.name.casefold() != _INDEX_FILENAME.casefold()


def _is_regular_legacy_source(source: Path, target_key: str) -> bool:
    if _canonical_path(source) == target_key:
        return False
    if not source.is_dir():
        return False
    return not source.is_symlink()


def _load_report(path: Path) -> dict[str, Any]:
    report = _read_json_object(path)
    if report.get("version") != _MIGRATION_REPORT_VERSION:
        return {"version": _MIGRATION_REPORT_VERSION, "sources": {}}
    if not isinstance(report.get("sources"), dict):
        report["sources"] = {}
    return report


def _report_entry_is_complete(entry: dict[str, Any]) -> bool:
    complete = entry.get("complete")
    if isinstance(complete, bool):
        return complete
    return entry.get("index_truncated", 0) == 0


def prepare_project_coding_memory_dir(
    *,
    agent_workspace_dir: str | PathLike[str],
    project_dir: str | PathLike[str] | None,
) -> CodingMemoryMigrationResult:
    """Prepare the new Coding Memory directory and import legacy Markdown.

    Migration is best-effort and fail-open. Legacy sources are never modified,
    and derived databases and caches are intentionally not copied.
    """
    target = Path(
        resolve_project_coding_memory_dir(
            agent_workspace_dir=agent_workspace_dir,
            project_dir=project_dir,
        )
    )
    warnings: list[str] = []
    source_paths: list[str] = []
    totals = {
        "sources_found": 0,
        "sources_migrated": 0,
        "copied": 0,
        "duplicates": 0,
        "renamed": 0,
        "index_truncated": 0,
    }

    try:
        candidates = _legacy_source_candidates(
            agent_workspace_dir=Path(agent_workspace_dir).absolute(),
            project_dir=project_dir,
        )
        target_key = _canonical_path(target)
        regular_sources: list[Path] = []
        for source in candidates:
            if _is_regular_legacy_source(source, target_key):
                regular_sources.append(source)

        if not any(
            _canonical_path(source) != target_key and source.is_dir()
            for source in candidates
        ):
            target.mkdir(parents=True, exist_ok=True)
            return CodingMemoryMigrationResult(target_dir=str(target))

        if regular_sources and target.is_dir():
            report = _load_report(target / _MIGRATION_REPORT_FILENAME)
            report_sources = report["sources"]
            fast_path_matches = True
            for source in regular_sources:
                source_key = _canonical_path(source)
                previous = report_sources.get(source_key)
                if (
                    not isinstance(previous, dict)
                    or not _report_entry_is_complete(previous)
                    or previous.get("state") != _source_state_token(source)
                ):
                    fast_path_matches = False
                    break
            if fast_path_matches:
                return CodingMemoryMigrationResult(
                    target_dir=str(target),
                    source_paths=tuple(_canonical_path(item) for item in regular_sources),
                    sources_found=len(regular_sources),
                )

        target.parent.mkdir(parents=True, exist_ok=True)
        project_name = resolve_coding_memory_project_name(project_dir)
        lock_path = target.parent / f".{project_name}.coding-memory-migration.lock"
        with portalocker.Lock(
            str(lock_path),
            mode="a",
            timeout=_LOCK_TIMEOUT_SECONDS,
        ):
            target.mkdir(parents=True, exist_ok=True)
            report_path = target / _MIGRATION_REPORT_FILENAME
            report = _load_report(report_path)
            report_sources = report["sources"]

            existing_hashes: dict[str, str] = {}
            for path in target.iterdir():
                if _is_regular_memory_markdown(path):
                    existing_hashes[_hash_file(path)] = path.name

            report_changed = False
            for source in candidates:
                if _canonical_path(source) == target_key or not source.is_dir():
                    continue
                if source.is_symlink():
                    warnings.append(f"{source}: skipped symbolic-link source")
                    continue
                totals["sources_found"] += 1
                source_key = _canonical_path(source)
                source_paths.append(source_key)
                try:
                    source_files, source_warnings = _scan_markdown_files(source)
                    warnings.extend(f"{source}: {item}" for item in source_warnings)
                    fingerprint = _source_fingerprint(source, source_files)
                    state = _source_state_token(source)
                    previous = report_sources.get(source_key)
                    if (
                        isinstance(previous, dict)
                        and previous.get("fingerprint") == fingerprint
                        and _report_entry_is_complete(previous)
                    ):
                        if previous.get("state") != state:
                            previous["state"] = state
                            report_changed = True
                        continue

                    final_names: dict[str, str] = {}
                    copied = duplicates = renamed = 0
                    for source_file in source_files:
                        duplicate_name = existing_hashes.get(source_file.content_hash)
                        if duplicate_name is not None:
                            final_names[source_file.path.name] = duplicate_name
                            duplicates += 1
                            continue

                        destination_name = source_file.path.name
                        destination = target / destination_name
                        if destination.exists():
                            destination_name = _conflict_filename(target, source_file)
                            destination = target / destination_name
                            renamed += 1

                        if (
                            destination.exists()
                            and _hash_file(destination) == source_file.content_hash
                        ):
                            duplicates += 1
                        else:
                            _atomic_write_bytes(
                                destination,
                                source_file.path.read_bytes(),
                            )
                            copied += 1
                        existing_hashes[source_file.content_hash] = destination_name
                        final_names[source_file.path.name] = destination_name

                    truncated = _append_index_entries(
                        target=target,
                        source=source,
                        final_names=final_names,
                    )
                    report_sources[source_key] = {
                        "source": source_key,
                        "fingerprint": fingerprint,
                        "state": state,
                        "complete": True,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "copied": copied,
                        "duplicates": duplicates,
                        "renamed": renamed,
                        "index_truncated": truncated,
                        "warning_count": len(source_warnings),
                        "warnings": source_warnings,
                    }
                    totals["sources_migrated"] += 1
                    totals["copied"] += copied
                    totals["duplicates"] += duplicates
                    totals["renamed"] += renamed
                    totals["index_truncated"] += truncated
                    report_changed = True
                except (OSError, ValueError) as exc:
                    warnings.append(f"failed to migrate {source}: {exc}")

            if report_changed:
                _atomic_write_text(
                    report_path,
                    json.dumps(
                        report,
                        ensure_ascii=True,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
    except Exception as exc:
        warnings.append(f"failed to prepare {target}: {exc}")

    failed = any(item.startswith("failed to ") for item in warnings)
    result = CodingMemoryMigrationResult(
        target_dir=str(target),
        source_paths=tuple(source_paths),
        warnings=tuple(warnings),
        failed=failed,
        **totals,
    )
    log_extra = {
        "coding_memory_migration": {
            "target": result.target_dir,
            "sources": result.source_paths,
            "sources_found": result.sources_found,
            "sources_migrated": result.sources_migrated,
            "copied": result.copied,
            "duplicates": result.duplicates,
            "renamed": result.renamed,
            "index_truncated": result.index_truncated,
            "failed": result.failed,
        }
    }
    if result.failed:
        logger.warning("Coding Memory legacy migration incomplete", extra=log_extra)
    elif result.sources_migrated:
        logger.info("Coding Memory legacy migration completed", extra=log_extra)
    return result


__all__ = [
    "CodingMemoryMigrationResult",
    "DEFAULT_CODING_MEMORY_PROJECT",
    "prepare_project_coding_memory_dir",
    "resolve_coding_memory_project_name",
    "resolve_project_coding_memory_dir",
    "resolve_project_coding_memory_workspace_path",
]
