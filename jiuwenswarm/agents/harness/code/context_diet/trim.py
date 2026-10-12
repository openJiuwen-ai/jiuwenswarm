# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure trimming and summarizing helpers for tool results.

Everything here works on plain strings and dicts so it can be unit-tested
without agent-core and reused by offline tooling (the SWE-bench metrics
collector replays these functions over recorded tool results to estimate
savings). The rail in ``rails/context_diet_trim_rail.py`` is a thin adapter
that feeds ``ToolMessage.content`` through :func:`trim_tool_output` and stores
the full text via the ``spill`` callback.

Design rules
------------
* Never drop the *edges*: the first lines carry the command / header and the
  last lines carry the exit code or the final assertion, so both are kept.
* Never drop a *signal* line: anything matching :data:`SIGNAL_PATTERN`
  (errors, failures, tracebacks, warnings) in the omitted middle is kept with
  its original line number, capped by ``max_signal_lines``. Selection is
  two-pass: lines matching :data:`STRONG_SIGNAL_PATTERN` (a concrete failure)
  fill the cap first, weaker lines (:data:`WEAK_SIGNAL_PATTERN`: warnings,
  notes) only take what is left, so 300 warnings cannot crowd out 3 errors.
  ``passed`` / ``skipped`` lines are not signal at all.
* Always say what happened: the marker names the number of omitted lines and
  the spill path (or the ``read_file`` offset to resume from), so the model
  can recover any detail with one cheap call.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.agents.harness.code.context_diet.config import (
    GREP_TOOL_NAMES,
    READ_TOOL_NAMES,
    SHELL_TOOL_NAMES,
    TrimConfig,
)
from jiuwenswarm.agents.harness.code.context_diet.source_print import (
    source_print_reason,
)

MARKER = "[context-diet]"
"""Prefix of every line the trimmer inserts; grep for it in transcripts."""

_STRONG_RE = (
    r"\b(error|errors|fail|failed|failure|failures|traceback|exception|"
    r"assert|assertion|panic|fatal|denied|not found|no such file|"
    r"segmentation fault|killed|timed? ?out)\b"
    r"|(?-i:^\s*E\s{3,})"  # pytest assertion detail lines (case-sensitive)
    r"|(?-i:^\s*File \".*\", line \d+)"  # traceback frames
    r"|\bexit code\b\s*[:=]?\s*(?!0\b)-?\d+"  # non-zero exit codes
    r"|(?-i:\b\w+(?:Error|Exception)\b)"  # CamelCase exception names: ModuleNotFoundError, AssertionError
    r"|(?-i:^\S+\.py:\d+:(?!\d*:?\s*\w*[Ww]arning))"  # pytest file.py:line: locations (not warnings)
)
_WEAK_RE = r"\b(warning|warnings|warn|note|deprecated|deprecation|skipped)\b"

STRONG_SIGNAL_PATTERN = re.compile(_STRONG_RE, re.IGNORECASE)
"""Lines that describe a concrete failure. They fill ``max_signal_lines`` first."""

WEAK_SIGNAL_PATTERN = re.compile(_WEAK_RE, re.IGNORECASE)
"""Lines worth keeping when there is room left after the strong ones."""

SIGNAL_PATTERN = re.compile(f"(?:{_STRONG_RE})|(?:{_WEAK_RE})", re.IGNORECASE)
"""Union of the two: any line matching this is a 'signal' line."""

_FAILURE_LINE_PATTERN = re.compile(
    r"^\s*(FAILED|ERROR|Traceback|E\s{3,}|panic:|fatal:)|\b\w*(Error|Exception)\b\s*:"
)
"""Lines that name a concrete failure; preferred over generic signal words in one-line summaries."""

SOURCE_PRINT_LINE_CHARS = 2000
"""A source print with a line longer than this is clipped like other shell output (minified code, data)."""

_READ_LINE_RE = re.compile(r"^\s*(\d+)\t")
"""``cat -n`` style prefix produced by agent-core's ``read_file``."""

SpillFn = Callable[[str, str], str | None]
"""``spill(tool_name, full_text) -> path or None`` — persists the full output."""


@dataclass
class TrimResult:
    """Outcome of :func:`trim_tool_output`."""

    content: str
    """Model-facing text (unchanged when ``changed`` is False)."""
    changed: bool = False
    original_chars: int = 0
    trimmed_chars: int = 0
    omitted_lines: int = 0
    kept_signal_lines: int = 0
    spill_path: str | None = None
    kind: str = ""
    """``shell`` / ``grep`` / ``read_file`` / ``""`` (not a managed tool)."""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.trimmed_chars)

    def as_metadata(self) -> dict[str, Any]:
        """Compact dict stored on ``ToolMessage.metadata['context_diet']``."""
        return {
            "kind": self.kind,
            "original_chars": self.original_chars,
            "trimmed_chars": self.trimmed_chars,
            "omitted_lines": self.omitted_lines,
            "kept_signal_lines": self.kept_signal_lines,
            "spill_path": self.spill_path,
        }


def classify_tool(tool_name: str | None) -> str:
    """Map a tool name to ``shell`` / ``grep`` / ``read_file`` / ``""``.

    Names are matched by suffix so namespaced tools (``fs.read_file``) work.
    """
    name = (tool_name or "").strip().lower()
    if not name:
        return ""
    for candidate in SHELL_TOOL_NAMES:
        if name == candidate or name.endswith("." + candidate):
            return "shell"
    for candidate in GREP_TOOL_NAMES:
        if name == candidate or name.endswith("." + candidate):
            return "grep"
    for candidate in READ_TOOL_NAMES:
        if name == candidate or name.endswith("." + candidate):
            return "read_file"
    return ""


def trim_tool_output(
    tool_name: str | None,
    content: Any,
    config: TrimConfig,
    *,
    tool_args: Mapping[str, Any] | str | None = None,
    spill: SpillFn | None = None,
) -> TrimResult:
    """Return the trimmed model-facing text for one tool result.

    ``content`` that is not a string (structured results, images) is returned
    untouched; so is every tool that is not shell / grep / read_file.
    ``tool_args`` may be a mapping or the raw JSON string agent-core passes in
    ``ToolCallInputs.tool_args``.
    """
    if not isinstance(content, str):
        return TrimResult(content=content, kind="")
    kind = classify_tool(tool_name)
    if not kind or not config.enabled:
        return TrimResult(content=content, kind=kind, original_chars=len(content), trimmed_chars=len(content))
    spilled: list[str] = []

    def _recording_spill(name: str, text: str) -> str | None:
        path = spill(name, text) if spill is not None else None
        if path:
            spilled.append(path)
        return path

    spill_fn: SpillFn | None = _recording_spill if spill is not None else None
    source_reason = shell_source_print_reason(tool_args) if kind == "shell" and config.exempt_source_prints else None
    if source_reason:
        result = _trim_source_print(content, config, tool_name or "bash", spill_fn, source_reason)
    elif kind == "shell":
        result = _trim_shell(content, config, tool_name or "bash", spill_fn)
    elif kind == "grep":
        result = _trim_grep(content, config, tool_name or "grep", spill_fn)
    else:
        result = _trim_read_file(content, config, _as_dict(tool_args))
    final = _never_grow(content, kind, result)
    if final is not result:
        # The trim was discarded; its spill file is referenced nowhere, so remove it.
        for path in spilled:
            try:
                os.remove(path)
            except OSError:
                pass
    return final


def _never_grow(content: str, kind: str, result: TrimResult) -> TrimResult:
    """Keep the original when the trimmed text is not actually shorter.

    With low thresholds a result just over the line limit can come out longer
    than it went in, because the omission marker and spill path cost more than
    the few lines removed. Such a "trim" only loses information, so it is dropped.
    """
    if result.changed and len(result.content) >= len(content):
        return TrimResult(
            content=content,
            kind=kind,
            original_chars=len(content),
            trimmed_chars=len(content),
            details={**result.details, "skipped": "trim_not_shorter"},
        )
    return result


def shell_source_print_reason(tool_args: Mapping[str, Any] | str | None) -> str | None:
    """Viewer name when a shell call's command prints file contents or a diff, else ``None``."""
    args = _as_dict(tool_args)
    command = args.get("command") or args.get("cmd") or ""
    return source_print_reason(command) if isinstance(command, str) else None


# ─── source prints (shell commands that print files / diffs) ──────────────


def _trim_source_print(
    content: str, config: TrimConfig, tool_name: str, spill: SpillFn | None, reason: str
) -> TrimResult:
    """Keep a source print whole, like a file read; cut only past ``source_print_max_lines``."""
    lines = content.splitlines()
    total = len(lines)
    if any(len(line) > SOURCE_PRINT_LINE_CHARS for line in lines):
        # Minified bundles and one-line JSON are not code anyone edits by line;
        # clip them like any other shell output.
        result = _cut_long_lines(content, lines, config, tool_name, spill)
        result.details["source_print"] = reason
        return result
    cap = config.source_print_max_lines
    if cap <= 0 or total <= cap:
        return TrimResult(
            content=content,
            kind="shell",
            original_chars=len(content),
            trimmed_chars=len(content),
            details={"skipped": "source_print", "source_print": reason, "total_lines": total},
        )
    spill_path = spill(tool_name, content) if spill is not None else None
    out = list(lines[:cap])
    out.append(
        f"{MARKER} output of `{reason}` is longer than {cap} lines: lines {cap + 1}–{total} omitted; "
        + (f"full output saved to {spill_path} (read_file with offset/limit). " if spill_path else "")
        + "Print a narrower range instead."
    )
    trimmed = "\n".join(out)
    return TrimResult(
        content=trimmed,
        changed=True,
        original_chars=len(content),
        trimmed_chars=len(trimmed),
        omitted_lines=total - cap,
        spill_path=spill_path,
        kind="shell",
        details={"total_lines": total, "source_print": reason},
    )


# ─── shell ────────────────────────────────────────────────────────────────


def _trim_shell(content: str, config: TrimConfig, tool_name: str, spill: SpillFn | None) -> TrimResult:
    lines = content.splitlines()
    total = len(lines)
    if total <= config.bash_max_lines and len(content) <= config.bash_max_chars:
        return TrimResult(content=content, kind="shell", original_chars=len(content), trimmed_chars=len(content))

    head_n = min(config.bash_head_lines, total)
    tail_n = min(config.bash_tail_lines, max(0, total - head_n))
    if head_n + tail_n >= total:
        # Long by characters but short by lines: keep the line structure and
        # only cut very long individual lines.
        return _cut_long_lines(content, lines, config, tool_name, spill)

    middle_start, middle_end = head_n, total - tail_n
    signal: list[tuple[int, str]] = []
    dropped_strong = 0
    if config.keep_signal_lines and config.max_signal_lines > 0:
        signal, dropped_strong = _select_signal_lines(lines, middle_start, middle_end, config.max_signal_lines)

    spill_path = spill(tool_name, content) if spill is not None else None
    omitted = (middle_end - middle_start) - len(signal)

    out: list[str] = list(lines[:head_n])
    out.append(_omission_marker(omitted, len(content), spill_path, len(signal)))
    for idx, text in signal:
        out.append(f"{MARKER} L{idx + 1}: {_clip(text, 400)}")
    if dropped_strong:
        out.append(
            f"{MARKER} {dropped_strong} more error/failure line(s) exceeded max_signal_lines"
            + (f"; see {spill_path}" if spill_path else "")
        )
    if signal:
        out.append(f"{MARKER} (end of kept signal lines; tail follows)")
    out.extend(lines[middle_end:])
    trimmed = "\n".join(out)
    return TrimResult(
        content=trimmed,
        changed=True,
        original_chars=len(content),
        trimmed_chars=len(trimmed),
        omitted_lines=omitted,
        kept_signal_lines=len(signal),
        spill_path=spill_path,
        kind="shell",
        details={"total_lines": total, "head": head_n, "tail": tail_n, "dropped_strong_signal_lines": dropped_strong},
    )


def _select_signal_lines(lines: list[str], start: int, end: int, cap: int) -> tuple[list[tuple[int, str]], int]:
    """Pick up to ``cap`` signal lines from ``lines[start:end]``, strongest first.

    Pass one takes every :data:`STRONG_SIGNAL_PATTERN` match in file order;
    pass two fills the remaining budget with :data:`WEAK_SIGNAL_PATTERN`
    matches. The result is sorted by line number. The second return value is
    the number of *strong* lines the cap could not accommodate, so the caller
    can tell the model that more failures exist in the spill file.
    """
    strong: list[tuple[int, str]] = []
    weak: list[tuple[int, str]] = []
    for idx in range(start, end):
        text = lines[idx]
        if STRONG_SIGNAL_PATTERN.search(text):
            strong.append((idx, text))
        elif WEAK_SIGNAL_PATTERN.search(text):
            weak.append((idx, text))
    kept = strong[:cap]
    dropped_strong = len(strong) - len(kept)
    remaining = cap - len(kept)
    if remaining > 0 and weak:
        kept.extend(weak[:remaining])
    kept.sort(key=lambda item: item[0])
    return kept, dropped_strong


def _cut_long_lines(
    content: str,
    lines: list[str],
    config: TrimConfig,
    tool_name: str,
    spill: SpillFn | None,
) -> TrimResult:
    """Few but very long lines (minified output, one-line JSON): clip each line."""
    budget = max(200, config.bash_max_chars // max(1, len(lines)))
    spill_path = spill(tool_name, content) if spill is not None else None
    out = []
    clipped = 0
    for line in lines:
        if len(line) > budget:
            out.append(line[:budget] + f" …{MARKER} {len(line) - budget} chars clipped")
            clipped += 1
        else:
            out.append(line)
    if not clipped:
        return TrimResult(content=content, kind="shell", original_chars=len(content), trimmed_chars=len(content))
    out.append(_omission_marker(0, len(content), spill_path, 0, clipped_lines=clipped))
    trimmed = "\n".join(out)
    return TrimResult(
        content=trimmed,
        changed=True,
        original_chars=len(content),
        trimmed_chars=len(trimmed),
        spill_path=spill_path,
        kind="shell",
        details={"clipped_lines": clipped},
    )


# ─── grep / glob ──────────────────────────────────────────────────────────


def _trim_grep(content: str, config: TrimConfig, tool_name: str, spill: SpillFn | None) -> TrimResult:
    lines = content.splitlines()
    total = len(lines)
    if total <= config.grep_max_lines:
        return TrimResult(content=content, kind="grep", original_chars=len(content), trimmed_chars=len(content))
    head_n = min(config.grep_head_lines, total)
    spill_path = spill(tool_name, content) if spill is not None else None
    omitted = total - head_n
    out = list(lines[:head_n])
    out.append(
        f"{MARKER} {omitted} more result lines omitted ({total} total). "
        + (f"Full list: {spill_path}. " if spill_path else "")
        + "Narrow the pattern or path instead of paging through all matches."
    )
    trimmed = "\n".join(out)
    return TrimResult(
        content=trimmed,
        changed=True,
        original_chars=len(content),
        trimmed_chars=len(trimmed),
        omitted_lines=omitted,
        spill_path=spill_path,
        kind="grep",
        details={"total_lines": total},
    )


# ─── read_file ────────────────────────────────────────────────────────────


def _trim_read_file(content: str, config: TrimConfig, tool_args: Mapping[str, Any]) -> TrimResult:
    limit = config.read_file_max_lines
    if limit <= 0:
        return TrimResult(content=content, kind="read_file", original_chars=len(content), trimmed_chars=len(content))
    if tool_args.get("limit") not in (None, "", 0):
        # The model asked for an explicit window; respect it.
        return TrimResult(content=content, kind="read_file", original_chars=len(content), trimmed_chars=len(content))
    lines = content.splitlines()
    total = len(lines)
    if total <= limit:
        return TrimResult(content=content, kind="read_file", original_chars=len(content), trimmed_chars=len(content))

    kept = lines[:limit]
    # Work out the 1-based number of the first omitted line from the cat -n prefix.
    first_omitted = None
    match = _READ_LINE_RE.match(lines[limit]) if limit < total else None
    if match:
        first_omitted = int(match.group(1))
    last_match = _READ_LINE_RE.match(lines[-1])
    last_line = int(last_match.group(1)) if last_match else None
    resume_offset = (first_omitted - 1) if first_omitted else limit
    path = tool_args.get("file_path") or tool_args.get("path") or ""
    span = f"lines {first_omitted}–{last_line}" if first_omitted and last_line else f"{total - limit} lines"
    kept.append(
        f"{MARKER} {span} not shown ({total} lines returned). "
        f"Re-read the rest with read_file(offset={resume_offset}, limit=…)"
        + (f" on {path}" if path else "")
        + " or use grep for a specific symbol."
    )
    trimmed = "\n".join(kept)
    return TrimResult(
        content=trimmed,
        changed=True,
        original_chars=len(content),
        trimmed_chars=len(trimmed),
        omitted_lines=total - limit,
        kind="read_file",
        details={"total_lines": total, "resume_offset": resume_offset},
    )


# ─── summaries for compaction placeholders ────────────────────────────────


def summarize_tool_result(
    tool_name: str | None,
    tool_args: Mapping[str, Any] | Any,
    content: str,
    *,
    max_chars: int = 240,
) -> str:
    """One-line summary of a tool result used as a compaction placeholder.

    The line names the tool, its most informative argument and one key
    conclusion pulled from the result (exit code, first error, match count or
    the line span of a file read).
    """
    args = _as_dict(tool_args)
    kind = classify_tool(tool_name)
    name = (tool_name or "tool").split(".")[-1]
    lines = content.splitlines() if isinstance(content, str) else []
    if kind == "shell":
        cmd = _clip(str(args.get("command") or args.get("cmd") or "").strip().replace("\n", " "), 90)
        exit_code = _find_exit_code(lines)
        signal = next((ln.strip() for ln in lines if _FAILURE_LINE_PATTERN.search(ln)), "") or next(
            (ln.strip() for ln in lines if SIGNAL_PATTERN.search(ln) and "Exit Code" not in ln), ""
        )
        first = signal or next((ln.strip() for ln in lines if ln.strip() and not ln.startswith("Command:")), "")
        summary = f"{name} `{cmd}` → exit {exit_code if exit_code is not None else '?'}; {_clip(first, 120)}"
    elif kind == "grep":
        pattern = _clip(str(args.get("pattern") or args.get("query") or ""), 60)
        where = _clip(str(args.get("path") or args.get("include") or ""), 50)
        summary = f"{name} /{pattern}/ {where} → {len(lines)} lines".rstrip()
    elif kind == "read_file":
        path = _clip(str(args.get("file_path") or args.get("path") or ""), 90)
        first_no = _first_line_number(lines[0]) if lines else None
        last_no = _first_line_number(lines[-1]) if lines else None
        span = f"L{first_no}–L{last_no}" if first_no and last_no else f"{len(lines)} lines"
        summary = f"{name} {path} ({span})"
    else:
        first = next((ln.strip() for ln in lines if ln.strip()), "")
        summary = f"{name}: {_clip(first, 160)}"
    return _clip(summary, max_chars)


# ─── helpers ──────────────────────────────────────────────────────────────


def _omission_marker(
    omitted: int,
    total_chars: int,
    spill_path: str | None,
    kept_signal: int,
    *,
    clipped_lines: int = 0,
) -> str:
    parts = [MARKER]
    if clipped_lines:
        parts.append(f"{clipped_lines} long line(s) clipped;")
    else:
        parts.append(f"{omitted} line(s) omitted here;")
    parts.append(f"full output {total_chars} chars")
    if spill_path:
        parts.append(f"saved to {spill_path} (read_file with offset/limit or grep it)")
    else:
        parts.append("not persisted")
    text = " ".join(parts)
    if kept_signal:
        text += f"; {kept_signal} error/warning line(s) kept below"
    return text + "."


def _find_exit_code(lines: list[str]) -> int | None:
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.lower().startswith("exit code"):
            digits = re.findall(r"-?\d+", stripped)
            if digits:
                return int(digits[0])
    return None


def _first_line_number(line: str) -> int | None:
    match = _READ_LINE_RE.match(line)
    return int(match.group(1)) if match else None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


__all__ = [
    "MARKER",
    "SIGNAL_PATTERN",
    "STRONG_SIGNAL_PATTERN",
    "WEAK_SIGNAL_PATTERN",
    "TrimResult",
    "classify_tool",
    "shell_source_print_reason",
    "summarize_tool_result",
    "trim_tool_output",
]
