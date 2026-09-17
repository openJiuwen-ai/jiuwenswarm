# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project memory file discovery + merge.

Used by :class:`ProjectMemoryRail` to collect JiuwenSwarm project-memory files,
expand ``@include`` references, apply frontmatter ``paths:`` scoping against
the current workspace/cwd, and
merge the effective sources into a single prompt section.

Current scope
-------------
* multi-layer discovery: managed -> user -> project (root -> cwd) -> local
* fixed-filename + glob-based scanning (+ optional additional directories)
* nested git worktree handling
* symlink-safe de-duplication
* ``@include`` expansion on standalone text lines
* frontmatter stripping + workspace-scoped ``paths:`` conditional rules
* cache with explicit invalidation and filesystem snapshot fallback
* soft char-cap with truncation marker

Out of current scope (maybe defer to later extensions)
------------------------------------------------
* HTML block-comment stripping
* nested-memory attachment pipeline
* approval-gated external includes
"""
from __future__ import annotations

import functools
import glob as _glob
import os
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from threading import RLock
from typing import Iterable, Optional

from jiuwenswarm.common.utils import logger

# ---------------------------------------------------------------------------
# Discovery configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT_MARKERS: tuple[str, ...] = (
    ".git",
    ".jiuwen",
    ".claude",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
)

PROJECT_MEMORY_FILES: tuple[tuple[str, str], ...] = (
    ("JIUWENSWARM.md", "project"),
    (".jiuwen/JIUWENSWARM.md", "project"),
)

LOCAL_MEMORY_FILES: tuple[tuple[str, str], ...] = (
    ("JIUWENSWARM.local.md", "local"),
)

PROJECT_MEMORY_GLOBS: tuple[str, ...] = (
    ".jiuwen/rules/*.md",
)

USER_MEMORY_FILES: tuple[str, ...] = (
    "~/.jiuwen/JIUWENSWARM.md",
)

USER_MEMORY_GLOBS: tuple[str, ...] = (
    "~/.jiuwen/rules/*.md",
)

MANAGED_MEMORY_FILES: tuple[str, ...] = (
    "/etc/jiuwen/JIUWENSWARM.md",
)

MANAGED_MEMORY_GLOBS: tuple[str, ...] = (
    "/etc/jiuwen/rules/*.md",
)

ADDITIONAL_DIRECTORIES_ENV = "JIUWENSWARM_ADDITIONAL_DIRECTORIES"
DEFAULT_MAX_CHARS = 60_000
MAX_MEMORY_CHARACTER_COUNT = 40_000

# Priority: smaller = applied first (later = semantically override).
PRIORITY: dict[str, int] = {
    "managed": 10,
    "user": 20,
    "project": 30,
    "local": 40,
}

# Closing ``---`` may be followed by a newline OR be the end of file.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


# ---------------------------------------------------------------------------
# Data classes / cache entries
# ---------------------------------------------------------------------------


@dataclass
class LoadedMemoryFile:
    """A single memory file loaded from disk."""

    path: str
    kind: str
    content: str
    frontmatter: dict = field(default_factory=dict)
    priority: int = 30


@dataclass(frozen=True)
class GitWorktreeInfo:
    worktree_root: Path
    canonical_root: Path


@dataclass(frozen=True)
class _DiscoveryCacheEntry:
    files: tuple[LoadedMemoryFile, ...]
    watch_snapshot: tuple[tuple[str, bool, bool, int | None, int | None], ...]


_DISCOVERY_CACHE: dict[tuple[str, str, tuple[str, ...]], _DiscoveryCacheEntry] = {}
_CACHE_LOCK = RLock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def clear_project_memory_cache(workspace: str | None = None) -> None:
    """Clear cached discovery results.

    When ``workspace`` is provided, only cache entries rooted at that workspace
    are removed; otherwise the full cache is cleared.
    """
    with _CACHE_LOCK:
        if workspace is None:
            _DISCOVERY_CACHE.clear()
            return
        workspace_key = _safe_resolve(Path(workspace))
        stale_keys = [key for key in _DISCOVERY_CACHE if key[0] == workspace_key]
        for key in stale_keys:
            _DISCOVERY_CACHE.pop(key, None)


def find_project_root(cwd: str) -> Optional[Path]:
    """Walk up from ``cwd`` to find the first directory containing a root marker."""
    try:
        current = Path(cwd).resolve()
    except (OSError, ValueError, TypeError):
        return None
    for parent in [current, *current.parents]:
        for marker in PROJECT_ROOT_MARKERS:
            try:
                if (parent / marker).exists():
                    return parent
            except OSError:
                continue
    return None


def discover_and_load_memory_files(
    *,
    workspace: str,
    target_path: str | None = None,
    additional_directories: Iterable[str] | None = None,
) -> list[LoadedMemoryFile]:
    """Discover and load every applicable memory file.

    Order (low priority first):
        managed -> user -> project (root -> cwd, each directory contributes) -> local
    """
    workspace_path = Path(workspace)
    workspace_key = _safe_resolve(workspace_path)
    target_key = _safe_resolve(Path(target_path or workspace_key))
    normalized_additional_dirs = _normalize_additional_directories(
        additional_directories,
        workspace=Path(workspace_key),
    )
    cache_key = (workspace_key, target_key, normalized_additional_dirs)

    with _CACHE_LOCK:
        cached = _DISCOVERY_CACHE.get(cache_key)
        if cached is not None and cached.watch_snapshot == _build_watch_snapshot(
            path for path, *_ in cached.watch_snapshot
        ):
            return list(cached.files)

    files: list[LoadedMemoryFile] = []
    seen: set[str] = set()
    watch_paths: set[str] = set()

    for raw in MANAGED_MEMORY_FILES:
        _try_load_single(
            raw,
            "managed",
            files,
            seen,
            target_path=target_key,
            watch_paths=watch_paths,
        )
    _scan_absolute_globs(
        MANAGED_MEMORY_GLOBS,
        kind="managed",
        out=files,
        seen=seen,
        target_path=target_key,
        watch_paths=watch_paths,
    )

    for raw in USER_MEMORY_FILES:
        _try_load_single(
            raw,
            "user",
            files,
            seen,
            target_path=target_key,
            watch_paths=watch_paths,
        )
    _scan_absolute_globs(
        USER_MEMORY_GLOBS,
        kind="user",
        out=files,
        seen=seen,
        target_path=target_key,
        watch_paths=watch_paths,
    )

    project_root = find_project_root(workspace_key) or Path(workspace_key)
    if project_root is not None:
        try:
            cwd = Path(workspace_key)
        except (OSError, ValueError, TypeError):
            cwd = project_root

        worktree_info = _detect_git_worktree(cwd)
        scan_root = project_root
        if (
            worktree_info is not None
            and worktree_info.canonical_root != worktree_info.worktree_root
            and _is_relative_to(project_root, worktree_info.canonical_root)
        ):
            scan_root = worktree_info.canonical_root

        walk_dirs: list[Path] = []
        current = cwd
        while True:
            walk_dirs.append(current)
            if current == scan_root or current.parent == current:
                break
            current = current.parent
        walk_dirs.reverse()

        for d in walk_dirs:
            watch_paths.add(_safe_resolve(d))
            skip_project = _should_skip_project_dir(d, worktree_info)
            if not skip_project:
                _scan_relative_files(
                    base_dir=d,
                    entries=PROJECT_MEMORY_FILES,
                    out=files,
                    seen=seen,
                    target_path=target_key,
                    watch_paths=watch_paths,
                )
                _scan_relative_globs(
                    base_dir=d,
                    patterns=PROJECT_MEMORY_GLOBS,
                    kind="project",
                    out=files,
                    seen=seen,
                    target_path=target_key,
                    watch_paths=watch_paths,
                )
            _scan_relative_files(
                base_dir=d,
                entries=LOCAL_MEMORY_FILES,
                out=files,
                seen=seen,
                target_path=target_key,
                watch_paths=watch_paths,
            )

    for raw_dir in normalized_additional_dirs:
        extra_dir = Path(raw_dir)
        watch_paths.add(_safe_resolve(extra_dir))
        _scan_relative_files(
            base_dir=extra_dir,
            entries=PROJECT_MEMORY_FILES,
            out=files,
            seen=seen,
            target_path=target_key,
            watch_paths=watch_paths,
        )
        _scan_relative_globs(
            base_dir=extra_dir,
            patterns=PROJECT_MEMORY_GLOBS,
            kind="project",
            out=files,
            seen=seen,
            target_path=target_key,
            watch_paths=watch_paths,
        )
        _scan_relative_files(
            base_dir=extra_dir,
            entries=LOCAL_MEMORY_FILES,
            out=files,
            seen=seen,
            target_path=target_key,
            watch_paths=watch_paths,
        )

    # Stable sort by priority (preserves discovery order within same priority).
    files.sort(key=lambda f: f.priority)
    snapshot = _build_watch_snapshot(watch_paths)
    with _CACHE_LOCK:
        _DISCOVERY_CACHE[cache_key] = _DiscoveryCacheEntry(
            files=tuple(files),
            watch_snapshot=snapshot,
        )
    return list(files)


def merge_memory_content(
    files: Iterable[LoadedMemoryFile],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Merge files into a single text block with per-file headers.

    Applies a soft cap at ``max_chars`` and appends a truncation marker
    when exceeded.
    """
    parts: list[str] = []
    total = 0
    truncated_at: Optional[str] = None
    for f in files:
        chunk = f"### {f.kind} memory -- {_short(f.path)}\n{f.content}\n"
        if total + len(chunk) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                parts.append(chunk[:remaining])
            truncated_at = f.path
            break
        parts.append(chunk)
        total += len(chunk)
    merged = "\n".join(parts).strip()
    if truncated_at is not None:
        logger.warning(
            "[project_memory] merged content exceeded max_chars=%d; truncated at %s",
            max_chars,
            truncated_at,
        )
        merged += "\n\n<!-- project memory truncated (> max_chars) -->"
    return merged


def get_large_memory_files(
    files: Iterable[LoadedMemoryFile],
    *,
    threshold: int = MAX_MEMORY_CHARACTER_COUNT,
) -> list[dict[str, Any]]:
    """Return warning dicts for memory files exceeding per-file character threshold."""
    warnings: list[dict[str, Any]] = []
    for f in files:
        char_count = len(f.content)
        if char_count > threshold:
            warnings.append({
                "path": f.path,
                "kind": f.kind,
                "char_count": char_count,
                "threshold": threshold,
                "message": f"Large {_short(f.path)} ({char_count} chars > {threshold}) will impact performance",
            })
    return warnings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_additional_directories(
    additional_directories: Iterable[str] | None,
    *,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    if additional_directories is None:
        env_value = os.getenv(ADDITIONAL_DIRECTORIES_ENV, "")
        additional_directories = [
            item.strip()
            for item in env_value.split(os.pathsep)
            if item.strip()
        ]
    result: list[str] = []
    seen: set[str] = set()
    for raw in additional_directories:
        candidate = Path(os.path.expanduser(str(raw)))
        if not candidate.is_absolute() and workspace is not None:
            candidate = workspace / candidate
        normalized = _safe_resolve(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _scan_relative_files(
    *,
    base_dir: Path,
    entries: tuple[tuple[str, str], ...],
    out: list[LoadedMemoryFile],
    seen: set[str],
    target_path: str,
    watch_paths: set[str],
) -> None:
    for rel, kind in entries:
        watch_paths.add(_safe_resolve(base_dir))
        abs_path = base_dir / rel
        if abs_path.is_file():
            _load_path(
                abs_path,
                kind,
                out,
                seen,
                target_path=target_path,
                watch_paths=watch_paths,
            )


def _scan_relative_globs(
    *,
    base_dir: Path,
    patterns: tuple[str, ...],
    kind: str,
    out: list[LoadedMemoryFile],
    seen: set[str],
    target_path: str,
    watch_paths: set[str],
) -> None:
    for pattern in patterns:
        watch_paths.add(_safe_resolve(base_dir / Path(pattern).parent))
        for matched in sorted(_glob.glob(str(base_dir / pattern))):
            path = Path(matched)
            if path.is_file():
                _load_path(
                    path,
                    kind,
                    out,
                    seen,
                    target_path=target_path,
                    watch_paths=watch_paths,
                )


def _scan_absolute_globs(
    patterns: tuple[str, ...],
    *,
    kind: str,
    out: list[LoadedMemoryFile],
    seen: set[str],
    target_path: str,
    watch_paths: set[str],
) -> None:
    for raw_pattern in patterns:
        expanded = os.path.expanduser(raw_pattern)
        watch_paths.add(_safe_resolve(Path(expanded).parent))
        for matched in sorted(_glob.glob(expanded)):
            path = Path(matched)
            if path.is_file():
                _load_path(
                    path,
                    kind,
                    out,
                    seen,
                    target_path=target_path,
                    watch_paths=watch_paths,
                )


def _try_load_single(
    raw: str,
    kind: str,
    out: list[LoadedMemoryFile],
    seen: set[str],
    *,
    target_path: str,
    watch_paths: set[str],
) -> None:
    expanded = os.path.expanduser(raw)
    path = Path(expanded)
    watch_paths.add(_safe_resolve(path.parent))
    if path.is_file():
        _load_path(
            path,
            kind,
            out,
            seen,
            target_path=target_path,
            watch_paths=watch_paths,
        )


def _load_path(
    path: Path,
    kind: str,
    out: list[LoadedMemoryFile],
    seen: set[str],
    *,
    target_path: str,
    watch_paths: set[str],
) -> None:
    try:
        resolved = str(path.resolve())
    except (OSError, ValueError, RuntimeError):
        return
    watch_paths.add(resolved)
    if resolved in seen:
        return
    seen.add(resolved)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError) as exc:
        logger.warning("[project_memory] failed to read %s: %s", resolved, exc)
        return

    frontmatter, body = _parse_frontmatter(raw)
    if not _frontmatter_paths_match(frontmatter, path=path, target_path=target_path):
        return

    body_without_includes = _expand_includes(
        body,
        current_path=path,
        kind=kind,
        out=out,
        seen=seen,
        target_path=target_path,
        watch_paths=watch_paths,
    )
    body_stripped = body_without_includes.strip()
    if not body_stripped:
        return
    out.append(
        LoadedMemoryFile(
            path=resolved,
            kind=kind,
            content=body_stripped,
            frontmatter=frontmatter,
            priority=PRIORITY.get(kind, 30),
        )
    )


def _expand_includes(
    body: str,
    *,
    current_path: Path,
    kind: str,
    out: list[LoadedMemoryFile],
    seen: set[str],
    target_path: str,
    watch_paths: set[str],
) -> str:
    kept_lines: list[str] = []
    in_fence = False

    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            kept_lines.append(line)
            continue

        if in_fence:
            kept_lines.append(line)
            continue

        include_spec = _extract_include_spec(line)
        if include_spec is None:
            kept_lines.append(line)
            continue

        include_path = _resolve_include_path(include_spec, current_path=current_path)
        if include_path is None:
            continue
        watch_paths.add(_safe_resolve(include_path.parent))
        watch_paths.add(_safe_resolve(include_path))
        if not include_path.is_file():
            continue

        _load_path(
            include_path,
            kind,
            out,
            seen,
            target_path=target_path,
            watch_paths=watch_paths,
        )

    return "\n".join(kept_lines)


def _extract_include_spec(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("@include "):
        return stripped[len("@include "):].strip()
    if stripped.startswith("@") and not stripped.startswith("@@"):
        return stripped[1:].strip()
    return None


def _resolve_include_path(spec: str, *, current_path: Path) -> Path | None:
    cleaned = spec.strip().strip('"').strip("'")
    if not cleaned:
        return None
    if cleaned.startswith("~/"):
        return Path(os.path.expanduser(cleaned))
    if cleaned.startswith("/"):
        return Path(cleaned)
    if cleaned.startswith("./") or cleaned.startswith("../"):
        return (current_path.parent / cleaned).resolve()
    return (current_path.parent / cleaned).resolve()


def _frontmatter_paths_match(
    frontmatter: dict,
    *,
    path: Path,
    target_path: str,
) -> bool:
    globs = _extract_paths_globs(frontmatter)
    if not globs:
        return True
    return _paths_match_target(globs, rule_path=path, target_path=target_path)


def _extract_paths_globs(frontmatter: dict) -> tuple[str, ...]:
    raw = frontmatter.get("paths")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        result = tuple(str(item).strip() for item in raw if str(item).strip())
        return result
    return ()


def _paths_match_target(
    globs: tuple[str, ...],
    *,
    rule_path: Path,
    target_path: str,
) -> bool:
    """Match ``paths:`` globs against the current workspace/cwd path.

    This rail runs before each model call and does not have a stable notion of
    "current target file" in the surrounding business flow, so scoped rules are
    evaluated against the active workspace/cwd for the turn.
    """
    try:
        target = Path(target_path).resolve()
        project_root = find_project_root(str(target))
        if project_root is not None and not _is_relative_to(rule_path, project_root):
            rule_base = project_root
        else:
            rule_base = rule_path.parent
            for parent in rule_path.parents:
                if parent.name in {".jiuwen", ".claude"}:
                    rule_base = parent.parent
                    break
        relative = target.relative_to(rule_base.resolve())
    except (OSError, RuntimeError, ValueError):
        return False

    relative_posix = relative.as_posix().strip("/")
    if relative_posix:
        trail = posixpath.normpath(posixpath.join(relative_posix, ".")) + "/"
        with_dir_magic = posixpath.join(relative_posix, "__dir__")
        candidates = {relative_posix, trail, with_dir_magic}
    else:
        candidates = {"", "/", "/__dir__", ".", "__dir__"}

    for glob_pattern in globs:
        normalized = glob_pattern.strip().lstrip("./").replace("\\", "/")
        if not normalized:
            continue
        for candidate in candidates:
            if fnmatchcase(candidate, normalized):
                return True
    return False


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Return ``(frontmatter_dict, body)`` using lightweight YAML parsing."""
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    lines = match.group(1).splitlines()
    result: dict[str, object] = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            idx += 1
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            result[key] = _parse_frontmatter_value(value)
            idx += 1
            continue

        block: list[str] = []
        idx += 1
        while idx < len(lines):
            nxt = lines[idx]
            nxt_stripped = nxt.strip()
            if not nxt_stripped:
                idx += 1
                continue
            if not nxt.startswith((" ", "\t")) and ":" in nxt and not nxt_stripped.startswith("-"):
                break
            block.append(nxt_stripped)
            idx += 1
        result[key] = _parse_frontmatter_block(block)

    return result, raw[match.end():]


def _parse_frontmatter_value(value: str):
    value = value.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        parsed_list = _parse_inline_frontmatter_list(value)
        if parsed_list is not None:
            return parsed_list
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    return value.strip('"').strip("'")


def _parse_inline_frontmatter_list(value: str) -> list[str] | None:
    inner = value[1:-1].strip()
    if not inner:
        return []

    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False

    for ch in inner:
        if quote is not None:
            if escaped:
                current.append(ch)
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == quote:
                quote = None
                continue
            current.append(ch)
            continue

        if ch in {'"', "'"}:
            quote = ch
            continue
        if ch == ",":
            item = "".join(current).strip()
            if item:
                items.append(item.strip('"').strip("'"))
            current = []
            continue
        current.append(ch)

    if quote is not None:
        return None

    tail = "".join(current).strip()
    if tail:
        items.append(tail.strip('"').strip("'"))
    return items


def _parse_frontmatter_block(lines: list[str]):
    if not lines:
        return []
    if all(line.startswith("-") for line in lines):
        return [
            line[1:].strip().strip('"').strip("'")
            for line in lines
            if line[1:].strip()
        ]
    return [line.strip().strip('"').strip("'") for line in lines if line.strip()]


def _detect_git_worktree(cwd: Path) -> GitWorktreeInfo | None:
    worktree_root = _git_path(cwd, "rev-parse", "--show-toplevel")
    common_dir = _git_path(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if worktree_root is None or common_dir is None:
        return None

    canonical_root = _canonical_root_from_common_dir(common_dir)
    if canonical_root is None:
        return None
    return GitWorktreeInfo(
        worktree_root=worktree_root,
        canonical_root=canonical_root,
    )


@functools.lru_cache(maxsize=1)
def _git_executable() -> str | None:
    candidate = shutil.which("git")
    if not candidate:
        return None
    try:
        return str(Path(candidate).resolve(strict=False))
    except OSError:
        return candidate


def _git_path(cwd: Path, *args: str) -> Path | None:
    git_exe = _git_executable()
    if git_exe is None:
        return None
    try:
        completed = subprocess.run(
            [git_exe, "-C", str(cwd), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    output = completed.stdout.strip()
    if not output:
        return None
    try:
        return Path(output).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _canonical_root_from_common_dir(common_dir: Path) -> Path | None:
    if common_dir.name == ".git":
        return common_dir.parent
    parent = common_dir.parent
    if parent.name == "worktrees" and parent.parent.name == ".git":
        return parent.parent.parent
    return None


def _should_skip_project_dir(
    directory: Path,
    worktree_info: GitWorktreeInfo | None,
) -> bool:
    if worktree_info is None:
        return False
    if worktree_info.canonical_root == worktree_info.worktree_root:
        return False
    return (
        _is_relative_to(directory, worktree_info.canonical_root)
        and not _is_relative_to(directory, worktree_info.worktree_root)
    )


def _build_watch_snapshot(
    paths: Iterable[str],
) -> tuple[tuple[str, bool, bool, int | None, int | None], ...]:
    snapshot: list[tuple[str, bool, bool, int | None, int | None]] = []
    for raw in sorted({str(path) for path in paths if str(path).strip()}):
        path = Path(raw)
        try:
            stat_result = path.stat()
        except OSError:
            snapshot.append((raw, False, False, None, None))
            continue
        snapshot.append(
            (
                raw,
                True,
                path.is_dir(),
                getattr(stat_result, "st_mtime_ns", None),
                None if path.is_dir() else stat_result.st_size,
            )
        )
    return tuple(snapshot)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_resolve(path: Path) -> str:
    try:
        return str(path.resolve())
    except (OSError, RuntimeError, ValueError):
        return str(path)


def _short(path: str) -> str:
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        return path
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


__all__ = [
    "ADDITIONAL_DIRECTORIES_ENV",
    "DEFAULT_MAX_CHARS",
    "GitWorktreeInfo",
    "LoadedMemoryFile",
    "LOCAL_MEMORY_FILES",
    "MANAGED_MEMORY_FILES",
    "MANAGED_MEMORY_GLOBS",
    "PROJECT_MEMORY_FILES",
    "PROJECT_MEMORY_GLOBS",
    "PROJECT_ROOT_MARKERS",
    "USER_MEMORY_FILES",
    "USER_MEMORY_GLOBS",
    "clear_project_memory_cache",
    "discover_and_load_memory_files",
    "find_project_root",
    "get_large_memory_files",
    "MAX_MEMORY_CHARACTER_COUNT",
    "merge_memory_content",
]
