# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Zero-dependency cross-platform unified diff tool for files and text blocks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import difflib
import logging
from pathlib import Path
import re
from typing import Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.tools.command_runtime import (
    CommandRuntimePaths,
    _is_relative_to,
    current_command_runtime_paths,
)

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_LINES = 3
_MIN_CONTEXT_LINES = 0
_MAX_CONTEXT_LINES = 20

DEFAULT_MAX_CHARS = 30_000
_MIN_MAX_CHARS = 1_000
_MAX_MAX_CHARS = 100_000

_MAX_FILE_BYTES = 5 * 1024 * 1024
_BINARY_SNIFF_BYTES = 8192
_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)


@dataclass(frozen=True)
class _RawDiffInputs:
    """Unvalidated inputs passed to diff_files."""

    file_a: Any = None
    file_b: Any = None
    text_a: Any = None
    text_b: Any = None
    label_a: Any = None
    label_b: Any = None
    context_lines: Any = DEFAULT_CONTEXT_LINES
    ignore_trailing_whitespace: Any = False
    ignore_blank_lines: Any = False
    max_chars: Any = DEFAULT_MAX_CHARS


@dataclass(frozen=True)
class DiffRequest:
    """Normalized request parameters for a diff_files call."""

    mode: str
    file_a: str | None
    file_b: str | None
    text_a: str | None
    text_b: str | None
    label_a: str
    label_b: str
    context_lines: int
    ignore_trailing_whitespace: bool
    ignore_blank_lines: bool
    max_chars: int


def _clamp_int(
    value: Any,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def _validate_bool(value: Any, *, name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a boolean")


def _normalize_optional_str(value: Any, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if "\x00" in value:
        raise ValueError(f"{name} contains NUL byte")
    return value


def _normalize_file_arg(value: Any, *, name: str) -> str | None:
    text = _normalize_optional_str(value, name=name)
    if text is None:
        return None
    return text.strip() or None


def _resolve_diff_mode(
    file_a: str | None,
    file_b: str | None,
    text_a: str | None,
    text_b: str | None,
) -> str:
    has_file = file_a is not None or file_b is not None
    has_text = text_a is not None or text_b is not None
    if has_file and has_text:
        raise ValueError(
            "Provide either (file_a, file_b) or (text_a, text_b), not both"
        )
    if not has_file and not has_text:
        raise ValueError(
            "Must provide either (file_a, file_b) or (text_a, text_b)"
        )
    if has_file:
        if not file_a or not file_b:
            raise ValueError("Both file_a and file_b are required in file mode")
        return "file"
    if text_a is None or text_b is None:
        raise ValueError("Both text_a and text_b are required in text mode")
    return "text"


def _resolve_labels(
    mode: str,
    inputs: tuple[str | None, str | None],
    labels: tuple[str | None, str | None],
) -> tuple[str, str]:
    raw_a, raw_b = inputs
    lbl_a, lbl_b = labels
    default_a = (raw_a or "") if mode == "file" else "text_a"
    default_b = (raw_b or "") if mode == "file" else "text_b"
    return (lbl_a or "").strip() or default_a, (lbl_b or "").strip() or default_b


def _normalize_request(raw: _RawDiffInputs) -> DiffRequest:
    file_a = _normalize_file_arg(raw.file_a, name="file_a")
    file_b = _normalize_file_arg(raw.file_b, name="file_b")
    text_a = _normalize_optional_str(raw.text_a, name="text_a")
    text_b = _normalize_optional_str(raw.text_b, name="text_b")
    mode = _resolve_diff_mode(file_a, file_b, text_a, text_b)
    label_a, label_b = _resolve_labels(
        mode,
        (file_a, file_b),
        (
            _normalize_optional_str(raw.label_a, name="label_a"),
            _normalize_optional_str(raw.label_b, name="label_b"),
        ),
    )
    return DiffRequest(
        mode=mode,
        file_a=file_a,
        file_b=file_b,
        text_a=text_a,
        text_b=text_b,
        label_a=label_a,
        label_b=label_b,
        context_lines=_clamp_int(
            raw.context_lines,
            name="context_lines",
            default=DEFAULT_CONTEXT_LINES,
            minimum=_MIN_CONTEXT_LINES,
            maximum=_MAX_CONTEXT_LINES,
        ),
        ignore_trailing_whitespace=_validate_bool(
            raw.ignore_trailing_whitespace,
            name="ignore_trailing_whitespace",
        ),
        ignore_blank_lines=_validate_bool(
            raw.ignore_blank_lines,
            name="ignore_blank_lines",
        ),
        max_chars=_clamp_int(
            raw.max_chars,
            name="max_chars",
            default=DEFAULT_MAX_CHARS,
            minimum=_MIN_MAX_CHARS,
            maximum=_MAX_MAX_CHARS,
        ),
    )


def _allowed_roots(runtime_paths: CommandRuntimePaths) -> list[Path]:
    roots: list[Path] = [runtime_paths.project_root]
    if runtime_paths.workspace_root is not None:
        roots.append(runtime_paths.workspace_root)
    roots.append(runtime_paths.agent_workspace_root)
    try:
        from openjiuwen.core.sys_operation.cwd import (  # pylint: disable=import-outside-toplevel
            get_team_workspace,
        )

        team_workspace = get_team_workspace()
        if team_workspace:
            roots.append(Path(team_workspace).resolve())
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    deduped: list[Path] = []
    for root in roots:
        if root not in deduped:
            deduped.append(root)
    return deduped


def _resolve_workspace_file(
    raw_path: str,
    runtime_paths: CommandRuntimePaths,
    roots: list[Path],
) -> Path:
    if "\x00" in raw_path:
        raise ValueError("file path contains NUL byte")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = runtime_paths.current_cwd / candidate
    resolved = candidate.resolve()
    if not any(_is_relative_to(resolved, root) for root in roots):
        raise ValueError(f"path is outside workspace: {raw_path}")
    if not resolved.exists():
        raise FileNotFoundError(f"file does not exist: {raw_path}")
    if resolved.is_dir():
        raise ValueError(f"path is a directory, expected a file: {raw_path}")
    if not resolved.is_file():
        raise ValueError(f"unsupported file type: {raw_path}")
    return resolved


def _read_text_file(path: Path, raw_label: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat file {raw_label}: {exc}") from exc
    if size > _MAX_FILE_BYTES:
        raise ValueError(
            f"file {raw_label} exceeds maximum size of {_MAX_FILE_BYTES} bytes"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read file {raw_label}: {exc}") from exc
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        raise ValueError(f"binary file is not supported: {raw_label}")
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"unable to decode text file: {raw_label}")


def _display_label(path: Path, base: Path, explicit: str, raw: str) -> str:
    if explicit != raw:
        return explicit
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _prepare_lines(
    text: str,
    *,
    ignore_trailing_whitespace: bool,
    ignore_blank_lines: bool,
) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized.split("\n")
    if normalized.endswith("\n"):
        raw_lines = raw_lines[:-1]
    elif normalized == "":
        raw_lines = []

    processed: list[str] = []
    for line in raw_lines:
        item = line.rstrip() if ignore_trailing_whitespace else line
        if ignore_blank_lines and not item.strip():
            continue
        processed.append(item)
    return processed


def _format_numbered_diff(
    diff_lines: list[str],
) -> tuple[list[str], int, int, int]:
    rendered: list[str] = []
    insertions = 0
    deletions = 0
    hunks = 0
    old_lineno = 0
    new_lineno = 0

    in_header = True
    for line in diff_lines:
        if in_header and (line.startswith("--- ") or line.startswith("+++ ")):
            rendered.append(line)
            continue
        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            in_header = False
            hunks += 1
            old_lineno = int(hunk_match.group(1))
            new_lineno = int(hunk_match.group(3))
            rendered.append(line)
            continue
        if line.startswith("-"):
            deletions += 1
            rendered.append(f"-{old_lineno:>4d} {'':>4s} | {line[1:]}")
            old_lineno += 1
        elif line.startswith("+"):
            insertions += 1
            rendered.append(f"+{'':>4s} {new_lineno:>4d} | {line[1:]}")
            new_lineno += 1
        elif line.startswith(" "):
            rendered.append(
                f" {old_lineno:>4d} {new_lineno:>4d} | {line[1:]}"
            )
            old_lineno += 1
            new_lineno += 1
        else:
            rendered.append(line)

    return rendered, insertions, deletions, hunks


def _load_diff_inputs(req: DiffRequest) -> tuple[str, str, str, str]:
    if req.mode == "file":
        if req.file_a is None or req.file_b is None:
            raise ValueError("Both file_a and file_b are required in file mode")
        runtime_paths = current_command_runtime_paths(require_runtime_cwd=False)
        roots = _allowed_roots(runtime_paths)
        path_a = _resolve_workspace_file(req.file_a, runtime_paths, roots)
        path_b = _resolve_workspace_file(req.file_b, runtime_paths, roots)
        return (
            _read_text_file(path_a, req.file_a),
            _read_text_file(path_b, req.file_b),
            _display_label(
                path_a, runtime_paths.current_cwd, req.label_a, req.file_a
            ),
            _display_label(
                path_b, runtime_paths.current_cwd, req.label_b, req.file_b
            ),
        )
    if req.text_a is None or req.text_b is None:
        raise ValueError("Both text_a and text_b are required in text mode")
    return req.text_a, req.text_b, req.label_a, req.label_b


def _truncate_diff_output(
    full_output: str,
    header_len: int,
    max_chars: int,
) -> str:
    if len(full_output) <= max_chars:
        return full_output
    closing_marker = f"\n... [Truncated at max_chars={max_chars}]\n```"
    budget = max(120, max_chars - len(closing_marker))
    prefix = full_output[:budget]
    last_nl = prefix.rfind("\n")
    if last_nl > header_len:
        prefix = prefix[:last_nl]
    return (prefix + closing_marker)[:max_chars]


def _render_diff_output(
    req: DiffRequest,
    labels: tuple[str, str],
    line_counts: tuple[int, int],
    raw_diff: list[str],
) -> str:
    label_a, label_b = labels
    count_a, count_b = line_counts
    if not raw_diff:
        return (
            f"Diff Summary: a/{label_a} -> b/{label_b} | mode: {req.mode}\n"
            f"Lines: {count_a} vs {count_b} | "
            "Stats: +0 insertions, -0 deletions, 0 hunks\n\n"
            "No differences found (+0, -0)."
        )
    numbered_lines, insertions, deletions, hunks = _format_numbered_diff(
        raw_diff
    )
    header = (
        f"Diff Summary: a/{label_a} -> b/{label_b} | mode: {req.mode}\n"
        f"Lines: {count_a} vs {count_b} | "
        f"Stats: +{insertions} insertions, -{deletions} deletions, "
        f"{hunks} hunk(s)"
    )
    full_output = f"{header}\n\n```diff\n" + "\n".join(numbered_lines) + "\n```"
    return _truncate_diff_output(full_output, len(header), req.max_chars)


def _diff_files_sync(req: DiffRequest) -> str:
    content_a, content_b, label_a, label_b = _load_diff_inputs(req)
    lines_a = _prepare_lines(
        content_a,
        ignore_trailing_whitespace=req.ignore_trailing_whitespace,
        ignore_blank_lines=req.ignore_blank_lines,
    )
    lines_b = _prepare_lines(
        content_b,
        ignore_trailing_whitespace=req.ignore_trailing_whitespace,
        ignore_blank_lines=req.ignore_blank_lines,
    )
    raw_diff = list(
        difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=f"a/{label_a}",
            tofile=f"b/{label_b}",
            n=req.context_lines,
            lineterm="",
        )
    )
    return _render_diff_output(
        req,
        (label_a, label_b),
        (len(lines_a), len(lines_b)),
        raw_diff,
    )


@tool(
    name="diff_files",
    description=(
        "Generate a compact cross-platform unified diff with line numbers and "
        "change statistics (+insertions/-deletions) between two workspace files "
        "(file_a, file_b) or two text blocks (text_a, text_b). "
        f"Defaults: context_lines={DEFAULT_CONTEXT_LINES} "
        f"({_MIN_CONTEXT_LINES}-{_MAX_CONTEXT_LINES}), "
        "ignore_trailing_whitespace=False, ignore_blank_lines=False, "
        f"max_chars={DEFAULT_MAX_CHARS} ({_MIN_MAX_CHARS}-{_MAX_MAX_CHARS})."
    ),
)
async def diff_files(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    file_a: str | None = None,
    file_b: str | None = None,
    text_a: str | None = None,
    text_b: str | None = None,
    label_a: str | None = None,
    label_b: str | None = None,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    ignore_trailing_whitespace: bool = False,
    ignore_blank_lines: bool = False,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Compute a unified diff between two workspace files or two text strings.

    Args:
        file_a: First workspace file path (when comparing files).
        file_b: Second workspace file path (when comparing files).
        text_a: First text content (when comparing in-memory strings).
        text_b: Second text content (when comparing in-memory strings).
        label_a: Optional display label for the first input.
        label_b: Optional display label for the second input.
        context_lines: Unchanged context lines around each hunk (0-20).
        ignore_trailing_whitespace: Strip trailing whitespace before comparing.
        ignore_blank_lines: Ignore blank lines when comparing.
        max_chars: Maximum output character budget (1000-100000, default 30000).
    """
    try:
        raw = _RawDiffInputs(
            file_a, file_b, text_a, text_b, label_a, label_b,
            context_lines, ignore_trailing_whitespace, ignore_blank_lines,
            max_chars,
        )
        req = _normalize_request(raw)
        logger.info(
            "[diff_files] mode=%s label_a=%s label_b=%s",
            req.mode, req.label_a, req.label_b,
        )
        return await asyncio.to_thread(_diff_files_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: diff_files failed: {exc}"


BUILTIN_TOOL_SPECS = (
    {
        "name": "diff_files",
        "module": ".diff_tools",
        "symbol": "diff_files",
        "display_en": (
            "Compute unified diff with line numbers between two files or text blocks."
        ),
        "aliases_zh": "文件对比 文本对比 差异比较 代码差异 diff 补丁",
        "category_path": "file-diff",
        "category_description": (
            "对比两个工作区文件或两段文本的差异，输出带行号和统计的统一补丁。"
        ),
        "category_description_en": (
            "Compare two workspace files or text blocks and output a numbered "
            "unified diff with change statistics."
        ),
        "permission_scope": "path_read",
    },
)
