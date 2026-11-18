# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cross-platform code search and pattern matching tool.

Provides structured, regex/literal search across files and directories in the
workspace with context lines, automatic binary file skipping, file size limits,
and ignore filters (skipping .git, node_modules, .venv, etc.).
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from openjiuwen.core.foundation.tool import tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_MATCHES = 50
_MAX_MATCHES_CEILING = 200
_MAX_CONTEXT_LINES = 10
_CHUNK_READ_SIZE = 1024
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

IGNORED_DIRECTORIES = frozenset({
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".coverage",
    "htmlcov",
    "dist",
    "build",
    ".idea",
    ".vscode",
})


@dataclass(frozen=True)
class GrepMatch:
    file_path: str
    line_number: int
    line_content: str
    context_before: list[tuple[int, str]]
    context_after: list[tuple[int, str]]


@dataclass(frozen=True)
class GrepRequest:
    query: str
    target_path: Path
    file_pattern: str
    is_regex: bool
    case_sensitive: bool
    context_lines: int
    max_matches: int


def _is_binary_file(file_path: Path) -> bool:
    """Detect if a file contains binary bytes."""
    try:
        with file_path.open("rb") as f:
            chunk = f.read(_CHUNK_READ_SIZE)
            return b"\x00" in chunk
    except OSError:
        return True


def _read_file_lines(file_path: Path) -> list[str] | None:
    """Read text lines from file using fallback encodings."""
    try:
        if file_path.stat().st_size > _MAX_FILE_SIZE_BYTES:
            logger.warning("[grep_code] Skipping oversized file (>10MB): %s", file_path)
            return None
    except OSError:
        return None

    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            with file_path.open("r", encoding=encoding) as f:
                return f.readlines()
        except (UnicodeDecodeError, OSError):
            continue
    return None


def _find_matching_files(root_dir: Path, file_pattern: str) -> Iterator[Path]:
    """Traverse directory yielding files matching pattern, skipping ignored dirs."""
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRECTORIES and not d.startswith(".")]
        for file_name in files:
            file_path = Path(root) / file_name
            rel_posix = file_path.relative_to(root_dir).as_posix()
            if fnmatch.fnmatchcase(file_name, file_pattern) or fnmatch.fnmatchcase(rel_posix, file_pattern):
                yield file_path


def _compile_pattern(query: str, is_regex: bool, case_sensitive: bool) -> re.Pattern[str]:
    """Compile regex or escaped literal into re.Pattern."""
    pattern_str = query if is_regex else re.escape(query)
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(pattern_str, flags)


def _slice_context(lines: list[str], start: int, end: int) -> list[tuple[int, str]]:
    return [(idx + 1, lines[idx].rstrip("\r\n")) for idx in range(start, end)]


def _search_in_lines(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    lines: list[str],
    pattern: re.Pattern[str],
    rel_path_str: str,
    context_lines: int,
    limit: int,
    current_count: int,
) -> list[GrepMatch]:
    """Search for matches in lines and extract context."""
    matches: list[GrepMatch] = []
    for i, line in enumerate(lines):
        if pattern.search(line):
            matches.append(
                GrepMatch(
                    file_path=rel_path_str,
                    line_number=i + 1,
                    line_content=line.rstrip("\r\n"),
                    context_before=_slice_context(lines, max(0, i - context_lines), i),
                    context_after=_slice_context(
                        lines, i + 1, min(len(lines), i + 1 + context_lines)
                    ),
                )
            )
            if current_count + len(matches) >= limit:
                break
    return matches


def _format_grep_results(
    matches: list[GrepMatch],
    query: str,
    total_files_scanned: int,
    truncated: bool,
    max_matches: int,
) -> str:
    """Format search matches into readable Markdown."""
    if not matches:
        return f"No matches found for {query!r} across {total_files_scanned} files."

    out: list[str] = [
        f"Found {len(matches)}{'+' if truncated else ''} match(es) "
        f"for {query!r} ({total_files_scanned} files scanned):\n"
    ]

    current_file = ""
    for m in matches:
        if m.file_path != current_file:
            current_file = m.file_path
            out.append(f"### `{current_file}`")

        out.append(f"**Line {m.line_number}**:")
        code_lines: list[str] = []
        for line_no, content in m.context_before:
            code_lines.append(f"{line_no:5d} | {content}")
        code_lines.append(f"{m.line_number:5d} > {m.line_content}")
        for line_no, content in m.context_after:
            code_lines.append(f"{line_no:5d} | {content}")

        out.append("```\n" + "\n".join(code_lines) + "\n```")

    if truncated:
        out.append(f"\n[Matches truncated at limit of {max_matches}. Refine search query.]")

    return "\n".join(out).strip()


def _resolve_search_path(raw_path: str) -> Path:
    """Resolve target path against workspace enforcing strict boundaries."""
    cleaned = raw_path.strip().strip("'\"")
    path = Path(cleaned).expanduser()

    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_workspace

        ws = get_workspace()
    except Exception:  # pylint: disable=broad-exception-caught
        ws = None

    if not ws:
        raise ValueError("runtime workspace is unavailable")

    ws_path = Path(ws).resolve()
    if not path.is_absolute():
        path = ws_path / path

    resolved = path.resolve()
    if not resolved.is_relative_to(ws_path):
        raise ValueError(f"Search path must be inside workspace: {raw_path}")

    if not resolved.exists():
        raise FileNotFoundError(f"Path does not exist: {raw_path}")
    return resolved


def _resolve_workspace_root(target: Path) -> Path:
    try:
        # pylint: disable-next=import-outside-toplevel
        from openjiuwen.core.sys_operation.cwd import get_workspace

        ws = get_workspace()
        return Path(ws).resolve()
    except Exception:  # pylint: disable=broad-exception-caught
        return target if target.is_dir() else target.parent


def _grep_code_sync(req: GrepRequest) -> str:
    """Execute code search synchronously."""
    try:
        pattern = _compile_pattern(req.query, req.is_regex, req.case_sensitive)
    except re.error as exc:
        return f"[ERROR]: Invalid regular expression {req.query!r}: {exc}"

    target = req.target_path
    file_iter = (
        iter([target]) if target.is_file() else _find_matching_files(target, req.file_pattern)
    )
    workspace_path = _resolve_workspace_root(target)
    all_matches: list[GrepMatch] = []
    files_scanned_count = 0
    is_truncated = False
    fetch_limit = req.max_matches + 1

    for file_path in file_iter:
        if _is_binary_file(file_path):
            continue
        lines = _read_file_lines(file_path)
        if lines is None:
            continue
        files_scanned_count += 1
        try:
            rel_str = str(file_path.resolve().relative_to(workspace_path))
        except (ValueError, OSError):
            rel_str = file_path.name
        matches = _search_in_lines(
            lines=lines,
            pattern=pattern,
            rel_path_str=rel_str,
            context_lines=req.context_lines,
            limit=fetch_limit,
            current_count=len(all_matches),
        )
        all_matches.extend(matches)
        if len(all_matches) >= fetch_limit:
            is_truncated = True
            all_matches = all_matches[:req.max_matches]
            break

    return _format_grep_results(
        matches=all_matches,
        query=req.query,
        total_files_scanned=files_scanned_count,
        truncated=is_truncated,
        max_matches=req.max_matches,
    )


@tool(
    name="grep_code",
    description=(
        "Fast, cross-platform code and text search across files in the workspace. "
        "Supports regex or literal matching, context lines, file glob patterns, "
        "and automatically skips binary files and dependency folders (.git, node_modules, etc.)."
    ),
)
async def grep_code(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    query: str,
    path: str = ".",
    file_pattern: str = "*",
    is_regex: bool = False,
    case_sensitive: bool = False,
    context_lines: int = 1,
    max_matches: int = DEFAULT_MAX_MATCHES,
) -> str:
    """Async entrypoint for cross-platform code search."""
    if not query:
        return "[ERROR]: Query cannot be empty."

    try:
        resolved_path = _resolve_search_path(path)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: Failed to resolve search path: {exc}"

    capped_max_matches = min(max(1, max_matches), _MAX_MATCHES_CEILING)
    capped_context_lines = min(max(0, context_lines), _MAX_CONTEXT_LINES)

    req = GrepRequest(
        query=query,
        target_path=resolved_path,
        file_pattern=file_pattern,
        is_regex=is_regex,
        case_sensitive=case_sensitive,
        context_lines=capped_context_lines,
        max_matches=capped_max_matches,
    )
    logger.info(
        "[grep_code] query=%s path=%s pattern=%s",
        req.query,
        req.target_path,
        req.file_pattern,
    )
    return await asyncio.to_thread(_grep_code_sync, req)
