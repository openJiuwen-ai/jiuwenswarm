# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured parser for pytest and unittest failure output.

Extracts failing test names, failure phases, exception types/messages,
source locations, and compacted tracebacks while stripping ANSI terminal
escape sequences and filtering noisy runner logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.foundation.tool import tool

logger = logging.getLogger(__name__)

SUPPORTED_FRAMEWORKS = frozenset({"auto", "pytest", "unittest"})
DEFAULT_MAX_FAILURES = 20
_MAX_FAILURES_MIN = 1
_MAX_FAILURES_CEILING = 100
DEFAULT_MAX_CHARS = 30_000
_MAX_CHARS_MIN = 1_000
_MAX_CHARS_CEILING = 100_000
_MAX_FRAMES_PER_FAILURE = 8
_MAX_EXC_MSG_LINES = 8
_MAX_EXC_MSG_CHARS = 1_500

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[PX^_][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-9;:<=>?]*[ -/]*[@-~]"
    r"|\x9b[0-9;:<=>?]*[ -/]*[@-~]"
    r"|\x1b[()*+][0-9A-Za-z]"
    r"|\x1b[@-_a-zA-Z0-9=>]"
)

_PYTEST_BANNER_RE = re.compile(r"^={3,}\s*(?P<title>.+?)\s*={3,}$")
_PYTEST_SUBSEC_RE = re.compile(r"^_{3,}\s+(?P<title>.+?)\s+_{3,}$")
_PYTEST_ERR_SETUP_RE = re.compile(
    r"^ERROR at (?P<phase>setup|teardown) of (?P<target>.+)$"
)
_PYTEST_ERR_COLLECT_RE = re.compile(r"^ERROR collecting (?P<target>.+)$")
_PYTEST_SUMMARY_BODY_RE = re.compile(
    r"^(?P<body>.+?)\s+in\s+(?P<duration>[\d.]+s(?:\s*\([^)]*\))?)$"
)
_PYTEST_COUNT_RE = re.compile(
    r"(\d+)\s+(failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?)",
    re.IGNORECASE,
)
_PYTEST_LOC_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:[\\/])?(?:[^\s:]*[./\\][^\s:]*|<[^>]+>))"
    r":(?P<line>\d+)(?::\s*(?P<rest>.*))?$"
)
_PYTEST_CODE_RE = re.compile(r"^>\s+(?P<code>.+)$")
_PYTEST_E_LINE_RE = re.compile(r"^E(?:\s{2,}|\t)(?P<eline>.*)$")
_PYTEST_E_BLANK_RE = re.compile(r"^E\s*$")
_PYTEST_SHORT_SUM_RE = re.compile(r"^(?P<kind>FAILED|ERROR)\s+(?P<rest>.+)$")
_PYTEST_NODEID_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+\S+::\S+")
_PYTEST_CAPTURED_RE = re.compile(
    r"^-{5,}\s*(?:Captured\s+(?:stdout|stderr|log)|live\s+log)\b.*-{5,}$",
    re.IGNORECASE,
)
_PYTEST_META_RE = re.compile(
    r"^(?:platform\s+\S+\s+--\s+Python\b|rootdir:|configfile:|plugins:|"
    r"cachedir:|collecting\s+\.\.\.|collected\s+\d+\s+items?\b|django:|Using existing\b)"
)
_PYTEST_PROGRESS_RE = re.compile(
    r"^(?:\S+\.(?:py|tsx?|jsx?)\s+[.FEsexXpP]+(?:\s+\[\s*\d+%\])?|"
    r"[.FEsexXpP\s]*[.FEsexXpP][.FEsexXpP\s]*\[\s*\d+%\]|"
    r"[.FEsexXpP]{2,})$"
)
_COVERAGE_HDR_RE = re.compile(
    r"^(?:-{5,}\s*coverage:|Name\s+Stmts\s+Miss|TOTAL\s+\d+\s+\d+)"
)

_UNITTEST_EQ_SEP_RE = re.compile(r"^={40,}\s*$")
_UNITTEST_DASH_SEP_RE = re.compile(r"^-{40,}\s*$")
_UNITTEST_HDR_RE = re.compile(r"^(?P<kind>FAIL|ERROR):\s+(?P<test_name>.+?)\s*$")
_UNITTEST_SIG_HDR_RE = re.compile(
    r"^(?:FAIL|ERROR):\s+\S+\s+\([^)]+\)(?:\s+\[.*\])?\s*$"
)
_UNITTEST_RAN_RE = re.compile(r"^Ran\s+(?P<ran>\d+)\s+tests?\s+in\s+(?P<dur>[\d.]+s)")
_UNITTEST_RESULT_RE = re.compile(
    r"^(?P<res>FAILED|OK)(?:\s*\((?P<details>[^)]+)\))?\s*$"
)
_UNITTEST_DETAIL_RE = re.compile(
    r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)",
    re.IGNORECASE,
)

_TB_START_RE = re.compile(r"^Traceback \(most recent call last\):\s*$")
_TB_FILE_RE = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+?)\s*$'
)
_PEP657_CARET_RE = re.compile(r"^[\s~^]+$")
_EXC_HDR_RE = re.compile(r"^(?P<exc>[A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(?P<msg>.*)$")
_EXC_BARE_RE = re.compile(r"^(?P<exc>[A-Za-z_][A-Za-z0-9_.]*)(?::\s*(?P<msg>.*))?$")

_PYTEST_INTERNAL_MARKERS = ("_pytest/", "_pytest\\", "pluggy/", "pluggy\\")
_UNITTEST_INTERNAL_MARKERS = (
    "unittest/case.py",
    "unittest\\case.py",
    "unittest/suite.py",
    "unittest\\suite.py",
)


@dataclass(frozen=True)
class ParseTestFailureRequest:
    """Normalized inputs for ``parse_test_failure``."""

    raw_output: str
    framework: str = "auto"
    max_failures: int = DEFAULT_MAX_FAILURES
    max_chars: int = DEFAULT_MAX_CHARS


@dataclass(frozen=True)
class TraceFrame:
    """Single stack frame extracted from a failure traceback."""

    file_path: str
    line_no: int | None
    func_name: str = ""
    code_line: str = ""


@dataclass(frozen=True)
class ParsedFailure:
    """Structured representation of a single test failure or error."""

    test_name: str
    kind: str
    phase: str
    exception_type: str = ""
    exception_message: str = ""
    file_path: str = ""
    line_no: int | None = None
    func_name: str = ""
    code_line: str = ""
    frames: tuple[TraceFrame, ...] = ()


@dataclass(frozen=True)
class ParsedTestResult:
    """Aggregated parse result across a test runner output."""

    framework: str
    status: str
    summary_line: str
    counts: dict[str, int]
    failures: tuple[ParsedFailure, ...]
    filtered_noise_lines: int = 0


def _normalize_bounded_int(
    raw_value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be an integer, got bool.")
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _normalize_request(inputs: dict[str, Any]) -> ParseTestFailureRequest:
    raw_output = inputs.get("raw_output")
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise ValueError("raw_output cannot be empty.")

    raw_framework = inputs.get("framework")
    if raw_framework is None:
        framework = "auto"
    elif isinstance(raw_framework, str):
        framework = raw_framework.strip().lower() or "auto"
    else:
        raise ValueError(
            f"Unsupported framework: {raw_framework!r}. "
            "Expected 'auto', 'pytest', or 'unittest'."
        )
    if framework not in SUPPORTED_FRAMEWORKS:
        raise ValueError(
            f"Unsupported framework: {framework!r}. "
            "Expected 'auto', 'pytest', or 'unittest'."
        )

    max_failures = _normalize_bounded_int(
        inputs.get("max_failures"),
        field_name="max_failures",
        default=DEFAULT_MAX_FAILURES,
        minimum=_MAX_FAILURES_MIN,
        maximum=_MAX_FAILURES_CEILING,
    )
    max_chars = _normalize_bounded_int(
        inputs.get("max_chars"),
        field_name="max_chars",
        default=DEFAULT_MAX_CHARS,
        minimum=_MAX_CHARS_MIN,
        maximum=_MAX_CHARS_CEILING,
    )
    return ParseTestFailureRequest(
        raw_output=raw_output,
        framework=framework,
        max_failures=max_failures,
        max_chars=max_chars,
    )


def _resolve_carriage_returns(line: str) -> str:
    if "\r" not in line:
        return line
    parts = line.split("\r")
    for part in reversed(parts):
        if part.strip():
            return part
    return parts[-1]


_PYTEST_BANNER_SECTIONS = {
    "errors": "ERRORS",
    "failures": "FAILURES",
    "short test summary info": "SUMMARY",
    "warnings summary": "WARNINGS",
    "test session starts": "SESSION",
}
_PYTEST_COUNT_KEY_ALIASES = {"errors": "error", "warnings": "warning"}


@dataclass
class _PytestNativeState:
    """Mutable accumulator for pytest native-style failure subsections."""

    raw_frames: list[TraceFrame] = field(default_factory=list)
    e_lines: list[str] = field(default_factory=list)
    fallback_exc: str = ""
    fallback_msg: str = ""
    pending_code_line: str = ""


@dataclass
class _PytestScanState:
    """Mutable accumulator for line-by-line pytest output parsing."""

    failures: list[ParsedFailure] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    summary_line: str = ""
    filtered_noise: int = 0
    saw_summary_banner: bool = False
    section: str = "PREAMBLE"
    sub_title: str | None = None
    sub_lines: list[str] = field(default_factory=list)
    tb_line_candidates: list[ParsedFailure] = field(default_factory=list)
    short_summary_items: list[tuple[str, str, str]] = field(default_factory=list)
    in_captured_outside: bool = False


@dataclass
class _UnittestScanState:
    """Mutable accumulator for line-by-line unittest output parsing."""

    failures: list[ParsedFailure] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    ran_line: str = ""
    result_line: str = ""
    saw_ok: bool = False
    saw_failed: bool = False
    filtered_noise: int = 0


def _clean_terminal_output(raw_output: str) -> str:
    stripped = _ANSI_ESCAPE_RE.sub("", raw_output).replace("\x00", "")
    normalized = stripped.replace("\r\n", "\n")
    lines = [_resolve_carriage_returns(line).rstrip() for line in normalized.split("\n")]
    cleaned = "\n".join(lines)
    if not cleaned.strip():
        raise ValueError(
            "raw_output contains no readable text after stripping terminal codes."
        )
    return cleaned


def _is_pytest_line(line: str) -> bool:
    banner = _PYTEST_BANNER_RE.match(line)
    if banner:
        title = banner.group("title").strip()
        if title.lower() in _PYTEST_BANNER_SECTIONS:
            return True
        summary = _PYTEST_SUMMARY_BODY_RE.match(title)
        if summary and _PYTEST_COUNT_RE.search(summary.group("body")):
            return True
    return bool(
        _PYTEST_NODEID_LINE_RE.match(line)
        or _PYTEST_ERR_COLLECT_RE.match(line)
        or _PYTEST_SUBSEC_RE.match(line)
        or (line.startswith("platform ") and "pytest-" in line)
    )


def _is_unittest_line(lines: list[str], idx: int) -> bool:
    line = lines[idx]
    if (
        _UNITTEST_SIG_HDR_RE.match(line)
        or _UNITTEST_RAN_RE.match(line)
        or _UNITTEST_RESULT_RE.match(line)
    ):
        return True
    if _UNITTEST_EQ_SEP_RE.match(line) and idx + 1 < len(lines):
        return bool(_UNITTEST_HDR_RE.match(lines[idx + 1]))
    return False


def _detect_framework(lines: list[str]) -> str:
    has_unittest = False
    for idx, line in enumerate(lines):
        if _is_pytest_line(line):
            return "pytest"
        if _is_unittest_line(lines, idx):
            has_unittest = True
    return "unittest" if has_unittest else "unknown"


def _compact_frames(
    frames: list[TraceFrame],
    internal_markers: tuple[str, ...],
) -> tuple[TraceFrame, ...]:
    if len(frames) <= _MAX_FRAMES_PER_FAILURE:
        return tuple(frames)
    first = frames[0]
    last = frames[-1]
    middle: list[TraceFrame] = []
    for frame in frames[1:-1]:
        if not any(marker in frame.file_path for marker in internal_markers):
            middle.append(frame)
    kept = [first, *middle, last]
    if len(kept) > _MAX_FRAMES_PER_FAILURE:
        tail_count = _MAX_FRAMES_PER_FAILURE - 1
        kept = [first, *kept[-tail_count:]]
    return tuple(kept)


def _cap_exc_lines(lines: list[str]) -> str:
    kept: list[str] = []
    total_chars = 0
    for raw in lines[:_MAX_EXC_MSG_LINES]:
        piece = raw.rstrip()
        if not piece and not kept:
            continue
        added = len(piece) + (1 if kept else 0)
        if total_chars + added > _MAX_EXC_MSG_CHARS:
            remaining = _MAX_EXC_MSG_CHARS - total_chars - (1 if kept else 0)
            if remaining > 0:
                kept.append(piece[:remaining])
            break
        kept.append(piece)
        total_chars += added
    return "\n".join(kept).strip()


def _split_exc_text(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    if not cleaned:
        return "", ""
    match = _EXC_HDR_RE.match(cleaned)
    if match:
        exc_name = match.group("exc")
        if exc_name not in {"assert", "where", "and", "or", "not"}:
            return exc_name, match.group("msg").strip()
    if cleaned.startswith("assert "):
        return "AssertionError", cleaned
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", cleaned):
        return cleaned, ""
    return "", cleaned


def _is_tb_boundary(line: str) -> bool:
    return bool(
        _PYTEST_BANNER_RE.match(line)
        or _PYTEST_SUBSEC_RE.match(line)
        or _PYTEST_CAPTURED_RE.match(line)
        or _UNITTEST_EQ_SEP_RE.match(line)
        or _UNITTEST_DASH_SEP_RE.match(line)
        or _UNITTEST_RAN_RE.match(line)
    )


def _extract_tb_code_line(lines: list[str], start: int) -> tuple[str, int]:
    look = start + 1
    while look < len(lines):
        cand = lines[look]
        if not cand.strip() or _PEP657_CARET_RE.match(cand):
            look += 1
            continue
        if cand.startswith((" ", "\t")) and not _TB_FILE_RE.match(cand):
            return cand.strip(), look
        return "", start
    return "", start


def _try_consume_tb_exc(
    line: str,
    exc_type: str,
    exc_msg_lines: list[str],
) -> tuple[str, bool]:
    if exc_type:
        if line.startswith((" ", "\t")) or len(exc_msg_lines) < _MAX_EXC_MSG_LINES:
            exc_msg_lines.append(line.strip())
            return exc_type, False
        return exc_type, True
    if not line.startswith((" ", "\t")):
        bare = _EXC_BARE_RE.match(line.strip())
        if bare:
            msg = (bare.group("msg") or "").strip()
            if msg:
                exc_msg_lines.append(msg)
            return bare.group("exc"), False
    return "", False


def _parse_python_traceback_block(
    lines: list[str],
    start_idx: int,
) -> tuple[list[TraceFrame], str, str, int]:
    frames: list[TraceFrame] = []
    exc_type = ""
    exc_msg_lines: list[str] = []
    idx = start_idx + 1
    while idx < len(lines):
        line = lines[idx]
        if _is_tb_boundary(line):
            break
        frame_match = _TB_FILE_RE.match(line)
        if frame_match:
            code_line, idx = _extract_tb_code_line(lines, idx)
            frames.append(
                TraceFrame(
                    file_path=frame_match.group("file").strip(),
                    line_no=int(frame_match.group("line")),
                    func_name=frame_match.group("func").strip(),
                    code_line=code_line,
                )
            )
            idx += 1
            continue
        if not line.strip() and exc_type:
            break
        if not (_TB_START_RE.match(line) or not line.strip() or _PEP657_CARET_RE.match(line)):
            exc_type, should_break = _try_consume_tb_exc(line, exc_type, exc_msg_lines)
            if should_break:
                break
        idx += 1
    return frames, exc_type, _cap_exc_lines(exc_msg_lines), idx


def _build_parsed_failure(
    header: tuple[str, str, str],
    exc: tuple[str, str],
    frames: tuple[TraceFrame, ...],
) -> ParsedFailure:
    test_name, kind, phase = header
    exc_type, exc_msg = exc
    primary = frames[-1] if frames else None
    return ParsedFailure(
        test_name=test_name,
        kind=kind,
        phase=phase,
        exception_type=exc_type,
        exception_message=exc_msg,
        file_path=primary.file_path if primary else "",
        line_no=primary.line_no if primary else None,
        func_name=primary.func_name if primary else "",
        code_line=primary.code_line if primary else "",
        frames=frames,
    )


def _parse_subsection_header(title: str, default_kind: str) -> tuple[str, str, str]:
    test_name = title.strip()
    setup_match = _PYTEST_ERR_SETUP_RE.match(test_name)
    if setup_match:
        return setup_match.group("target").strip(), "ERROR", setup_match.group("phase")
    collect_match = _PYTEST_ERR_COLLECT_RE.match(test_name)
    if collect_match:
        return collect_match.group("target").strip(), "ERROR", "collection"
    return test_name, default_kind, "call"


def _strip_captured_tail(body_lines: list[str]) -> tuple[list[str], int]:
    clean_body: list[str] = []
    for idx, line in enumerate(body_lines):
        if _PYTEST_CAPTURED_RE.match(line):
            return clean_body, len(body_lines) - idx
        clean_body.append(line)
    return clean_body, 0


def _find_traceback_start(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if _TB_START_RE.match(line):
            return idx
    return None


def _collect_non_caret_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    for line in lines:
        if line.strip() and not _PEP657_CARET_RE.match(line):
            result.append(line.strip())
    return result


def _parse_pytest_tb_subsection(
    header: tuple[str, str, str],
    clean_body: list[str],
    tb_idx: int,
) -> ParsedFailure:
    raw_frames, exc_type, exc_msg, end_idx = _parse_python_traceback_block(clean_body, tb_idx)
    frames = _compact_frames(raw_frames, _PYTEST_INTERNAL_MARKERS)
    if not exc_type:
        e_lines = _collect_non_caret_lines(clean_body[end_idx:])
        if e_lines:
            exc_type, first_msg = _split_exc_text(e_lines[0])
            exc_msg = _cap_exc_lines([first_msg, *e_lines[1:]] if first_msg else e_lines[1:])
    return _build_parsed_failure(header, (exc_type, exc_msg), frames)


def _scan_pytest_loc_code(clean_body: list[str], start: int) -> str:
    code_line = ""
    look = start + 1
    while look < len(clean_body):
        cand = clean_body[look]
        if (
            _PYTEST_LOC_RE.match(cand)
            or _PYTEST_E_LINE_RE.match(cand)
            or _PYTEST_E_BLANK_RE.match(cand)
        ):
            break
        code_match = _PYTEST_CODE_RE.match(cand)
        if code_match:
            return code_match.group("code").strip()
        if not code_line and cand.startswith((" ", "\t")) and cand.strip():
            code_line = cand.strip()
        look += 1
    return code_line


def _extract_pytest_loc_frame(
    loc_match: re.Match[str],
    clean_body: list[str],
    idx: int,
    state: _PytestNativeState,
) -> None:
    rest = (loc_match.group("rest") or "").strip()
    func_name = rest[3:].strip() if rest.startswith("in ") else ""
    if rest and not rest.startswith("in "):
        fb_type, fb_msg = _split_exc_text(rest)
        if fb_type:
            state.fallback_exc = fb_type
            state.fallback_msg = fb_msg
    code_line = _scan_pytest_loc_code(clean_body, idx)
    if not code_line and state.pending_code_line:
        code_line = state.pending_code_line
        state.pending_code_line = ""
    state.raw_frames.append(
        TraceFrame(
            file_path=loc_match.group("file").strip(),
            line_no=int(loc_match.group("line")),
            func_name=func_name,
            code_line=code_line,
        )
    )


def _apply_pytest_code_line(extracted_code: str, state: _PytestNativeState) -> None:
    if state.raw_frames and not state.raw_frames[-1].code_line:
        last = state.raw_frames[-1]
        state.raw_frames[-1] = TraceFrame(
            file_path=last.file_path,
            line_no=last.line_no,
            func_name=last.func_name,
            code_line=extracted_code,
        )
    else:
        state.pending_code_line = extracted_code


def _resolve_pytest_e_lines(
    e_lines: list[str],
    fallback_exc: str,
    fallback_msg: str,
) -> tuple[str, str]:
    non_empty_e = [ln for ln in e_lines if ln.strip()]
    if not non_empty_e:
        return fallback_exc, fallback_msg
    first_e = non_empty_e[0].strip()
    parsed_exc, parsed_msg = _split_exc_text(first_e)
    if not parsed_exc:
        return fallback_exc, _cap_exc_lines(non_empty_e)
    if parsed_exc == "AssertionError" and first_e.startswith("assert "):
        return parsed_exc, _cap_exc_lines(non_empty_e)
    tail = [parsed_msg] if parsed_msg else []
    tail.extend(ln.strip() for ln in non_empty_e[1:])
    return parsed_exc, _cap_exc_lines(tail)


def _parse_pytest_native_subsection(
    header: tuple[str, str, str],
    clean_body: list[str],
) -> ParsedFailure:
    state = _PytestNativeState()
    for idx, line in enumerate(clean_body):
        loc_match = _PYTEST_LOC_RE.match(line)
        if loc_match:
            _extract_pytest_loc_frame(loc_match, clean_body, idx, state)
            continue
        code_match = _PYTEST_CODE_RE.match(line)
        if code_match:
            _apply_pytest_code_line(code_match.group("code").strip(), state)
            continue
        e_match = _PYTEST_E_LINE_RE.match(line)
        if e_match:
            state.e_lines.append(e_match.group("eline").rstrip())
        elif _PYTEST_E_BLANK_RE.match(line):
            state.e_lines.append("")
    exc = _resolve_pytest_e_lines(state.e_lines, state.fallback_exc, state.fallback_msg)
    frames = _compact_frames(state.raw_frames, _PYTEST_INTERNAL_MARKERS)
    return _build_parsed_failure(header, exc, frames)


def _parse_pytest_subsection(
    title: str,
    body_lines: list[str],
    default_kind: str,
) -> tuple[ParsedFailure, int]:
    header = _parse_subsection_header(title, default_kind)
    clean_body, filtered_lines = _strip_captured_tail(body_lines)
    tb_idx = _find_traceback_start(clean_body)
    if tb_idx is not None:
        return _parse_pytest_tb_subsection(header, clean_body, tb_idx), filtered_lines
    return _parse_pytest_native_subsection(header, clean_body), filtered_lines


def _nodeid_matches_title(nodeid: str, title: str) -> bool:
    if nodeid == title:
        return True
    if "::" not in nodeid:
        return False
    tail = nodeid.split("::", 1)[1]
    dotted = tail.replace("::", ".")
    leaf = nodeid.rsplit("::", 1)[-1]
    return title in {dotted, tail, leaf}


def _flush_pytest_subsection(state: _PytestScanState) -> None:
    if state.sub_title is None:
        return
    default_kind = "ERROR" if state.section == "ERRORS" else "FAILED"
    parsed, extra_noise = _parse_pytest_subsection(
        state.sub_title,
        state.sub_lines,
        default_kind,
    )
    state.failures.append(parsed)
    state.filtered_noise += extra_noise
    state.sub_title = None
    state.sub_lines = []


def _record_pytest_counts(body: str, counts: dict[str, int]) -> None:
    for cnt_match in _PYTEST_COUNT_RE.finditer(body):
        raw_key = cnt_match.group(2).lower()
        key = _PYTEST_COUNT_KEY_ALIASES.get(raw_key, raw_key)
        counts[key] = int(cnt_match.group(1))


def _handle_pytest_banner(title: str, state: _PytestScanState) -> bool:
    _flush_pytest_subsection(state)
    state.in_captured_outside = False
    mapped_section = _PYTEST_BANNER_SECTIONS.get(title.lower())
    if mapped_section is not None:
        state.section = mapped_section
        return True
    sum_match = _PYTEST_SUMMARY_BODY_RE.match(title)
    if sum_match and _PYTEST_COUNT_RE.search(sum_match.group("body")):
        state.saw_summary_banner = True
        state.summary_line = title
        _record_pytest_counts(sum_match.group("body"), state.counts)
        state.section = "DONE"
        return True
    return False


def _append_pytest_tb_line_candidate(
    state: _PytestScanState,
    loc_match: re.Match[str],
) -> None:
    err_text = (loc_match.group("rest") or "").strip()
    exc_type, exc_msg = _split_exc_text(err_text)
    file_path = loc_match.group("file").strip()
    line_no = int(loc_match.group("line"))
    state.tb_line_candidates.append(
        ParsedFailure(
            test_name=f"{file_path}:{line_no}",
            kind="ERROR" if state.section == "ERRORS" else "FAILED",
            phase="call",
            exception_type=exc_type,
            exception_message=exc_msg,
            file_path=file_path,
            line_no=line_no,
            frames=(TraceFrame(file_path=file_path, line_no=line_no),),
        )
    )


def _consume_pytest_failure_section_line(state: _PytestScanState, line: str) -> None:
    sub_match = _PYTEST_SUBSEC_RE.match(line)
    if sub_match:
        _flush_pytest_subsection(state)
        state.sub_title = sub_match.group("title").strip()
        state.sub_lines = []
        return
    if state.sub_title is not None:
        state.sub_lines.append(line)
        return
    loc_match = _PYTEST_LOC_RE.match(line)
    if loc_match and loc_match.group("rest"):
        _append_pytest_tb_line_candidate(state, loc_match)
        return
    if line.strip():
        state.filtered_noise += 1


def _consume_pytest_summary_line(state: _PytestScanState, line: str) -> None:
    sum_item = _PYTEST_SHORT_SUM_RE.match(line)
    if sum_item:
        kind = sum_item.group("kind")
        rest = sum_item.group("rest").strip()
        nodeid, _, err_text = rest.partition(" - ")
        state.short_summary_items.append((kind, nodeid.strip(), err_text.strip()))
    elif line.strip():
        state.filtered_noise += 1


def _consume_pytest_section_line(state: _PytestScanState, line: str) -> None:
    if state.section == "WARNINGS":
        state.filtered_noise += 1
        return
    if state.section in {"ERRORS", "FAILURES"}:
        _consume_pytest_failure_section_line(state, line)
        return
    if state.section == "SUMMARY":
        _consume_pytest_summary_line(state, line)
        return
    if state.in_captured_outside or _PYTEST_CAPTURED_RE.match(line):
        state.in_captured_outside = True
        state.filtered_noise += 1
        return
    if (
        _PYTEST_META_RE.match(line)
        or _COVERAGE_HDR_RE.match(line)
        or _PYTEST_PROGRESS_RE.match(line)
    ):
        state.filtered_noise += 1


def _find_matching_failure_idx(
    state: _PytestScanState,
    matched_indices: set[int],
    nodeid: str,
    exc_type: str,
) -> int | None:
    for idx, existing in enumerate(state.failures):
        if idx not in matched_indices and _nodeid_matches_title(nodeid, existing.test_name):
            return idx
    if not state.tb_line_candidates:
        return None
    file_part = nodeid.split("::", 1)[0]
    for idx, existing in enumerate(state.failures):
        if idx in matched_indices:
            continue
        if (
            existing.file_path.endswith(file_part)
            and (not exc_type or existing.exception_type == exc_type)
        ):
            return idx
    return None


def _merge_pytest_short_summary(state: _PytestScanState) -> None:
    matched_indices: set[int] = set()
    for kind, nodeid, err_text in state.short_summary_items:
        exc_type, exc_msg = _split_exc_text(err_text)
        matched_idx = _find_matching_failure_idx(state, matched_indices, nodeid, exc_type)
        if matched_idx is not None:
            matched_indices.add(matched_idx)
            old = state.failures[matched_idx]
            state.failures[matched_idx] = ParsedFailure(
                test_name=nodeid,
                kind=old.kind,
                phase=old.phase,
                exception_type=old.exception_type or exc_type,
                exception_message=old.exception_message or exc_msg,
                file_path=old.file_path,
                line_no=old.line_no,
                func_name=old.func_name,
                code_line=old.code_line,
                frames=old.frames,
            )
            continue
        phase = "collection" if kind == "ERROR" and "::" not in nodeid else "call"
        state.failures.append(
            ParsedFailure(
                test_name=nodeid,
                kind=kind,
                phase=phase,
                exception_type=exc_type,
                exception_message=exc_msg,
            )
        )


def _finalize_pytest_result(state: _PytestScanState) -> ParsedTestResult:
    summary_line = state.summary_line
    if state.saw_summary_banner:
        has_fail_count = bool(state.counts.get("failed", 0) or state.counts.get("error", 0))
        status = "failed" if (has_fail_count or state.failures) else "passed"
    else:
        status = "incomplete"
        if not summary_line:
            summary_line = (
                f"{len(state.failures)} failure(s) parsed (no pytest summary banner found)"
                if state.failures
                else "No pytest summary banner found"
            )
    return ParsedTestResult(
        framework="pytest",
        status=status,
        summary_line=summary_line,
        counts=state.counts,
        failures=tuple(state.failures),
        filtered_noise_lines=state.filtered_noise,
    )


def _parse_pytest(lines: list[str]) -> ParsedTestResult:
    state = _PytestScanState()
    for line in lines:
        banner = _PYTEST_BANNER_RE.match(line)
        if banner and _handle_pytest_banner(banner.group("title").strip(), state):
            continue
        _consume_pytest_section_line(state, line)
    _flush_pytest_subsection(state)
    if not state.failures and state.tb_line_candidates:
        state.failures.extend(state.tb_line_candidates)
    _merge_pytest_short_summary(state)
    return _finalize_pytest_result(state)


def _pick_unittest_primary(frames: tuple[TraceFrame, ...]) -> TraceFrame | None:
    if not frames:
        return None
    for frame in reversed(frames):
        if not any(marker in frame.file_path for marker in _UNITTEST_INTERNAL_MARKERS):
            return frame
    return frames[-1]


def _parse_unittest_exc_fallback(lines: list[str], idx: int) -> tuple[str, str, int]:
    exc_type = ""
    msg_lines: list[str] = []
    while idx < len(lines):
        cur = lines[idx]
        if (
            _UNITTEST_EQ_SEP_RE.match(cur)
            or _UNITTEST_DASH_SEP_RE.match(cur)
            or _UNITTEST_RAN_RE.match(cur)
        ):
            break
        stripped = cur.strip()
        if stripped and not exc_type:
            exc_type, first_msg = _split_exc_text(cur)
            if first_msg:
                msg_lines.append(first_msg)
        elif stripped:
            msg_lines.append(stripped)
        idx += 1
    return exc_type, _cap_exc_lines(msg_lines), idx


def _parse_unittest_failure_block(
    lines: list[str],
    idx: int,
    hdr_match: re.Match[str],
) -> tuple[ParsedFailure, int]:
    kind = "FAILED" if hdr_match.group("kind") == "FAIL" else "ERROR"
    test_name = hdr_match.group("test_name").strip()
    phase = "collection" if "_FailedTest" in test_name else "call"
    idx += 2
    while idx < len(lines) and not _UNITTEST_DASH_SEP_RE.match(lines[idx]):
        if _UNITTEST_EQ_SEP_RE.match(lines[idx]):
            break
        idx += 1
    if idx < len(lines) and _UNITTEST_DASH_SEP_RE.match(lines[idx]):
        idx += 1
    raw_frames: list[TraceFrame] = []
    if idx < len(lines) and _TB_START_RE.match(lines[idx]):
        raw_frames, exc_type, exc_msg, idx = _parse_python_traceback_block(lines, idx)
    else:
        exc_type, exc_msg, idx = _parse_unittest_exc_fallback(lines, idx)
    frames = _compact_frames(raw_frames, _UNITTEST_INTERNAL_MARKERS)
    primary = _pick_unittest_primary(frames)
    failure = ParsedFailure(
        test_name=test_name,
        kind=kind,
        phase=phase,
        exception_type=exc_type,
        exception_message=exc_msg,
        file_path=primary.file_path if primary else "",
        line_no=primary.line_no if primary else None,
        func_name=primary.func_name if primary else "",
        code_line=primary.code_line if primary else "",
        frames=frames,
    )
    return failure, idx


def _record_unittest_result_line(
    line: str,
    res_match: re.Match[str],
    state: _UnittestScanState,
) -> None:
    state.result_line = line.strip()
    if res_match.group("res") == "OK":
        state.saw_ok = True
    else:
        state.saw_failed = True
    details = res_match.group("details") or ""
    for detail in _UNITTEST_DETAIL_RE.finditer(details):
        key = detail.group(1).lower().replace(" ", "_")
        state.counts[key] = int(detail.group(2))


def _finalize_unittest_result(state: _UnittestScanState) -> ParsedTestResult:
    if state.saw_ok and not state.failures:
        status = "passed"
    elif state.saw_failed or (state.ran_line and state.failures):
        status = "failed"
    else:
        status = "incomplete"

    summary_parts = [part for part in (state.ran_line, state.result_line) if part]
    if summary_parts:
        summary_line = " | ".join(summary_parts)
    elif state.failures:
        summary_line = f"{len(state.failures)} failure(s) parsed (no unittest summary found)"
    else:
        summary_line = "No unittest summary found"

    return ParsedTestResult(
        framework="unittest",
        status=status,
        summary_line=summary_line,
        counts=state.counts,
        failures=tuple(state.failures),
        filtered_noise_lines=state.filtered_noise,
    )


def _parse_unittest(lines: list[str]) -> ParsedTestResult:
    state = _UnittestScanState()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        hdr_match = (
            _UNITTEST_HDR_RE.match(lines[idx + 1])
            if _UNITTEST_EQ_SEP_RE.match(line) and idx + 1 < len(lines)
            else None
        )
        if hdr_match:
            failure, idx = _parse_unittest_failure_block(lines, idx, hdr_match)
            state.failures.append(failure)
            continue
        ran_match = _UNITTEST_RAN_RE.match(line)
        if ran_match:
            state.counts["ran"] = int(ran_match.group("ran"))
            state.ran_line = line.strip()
            idx += 1
            continue
        res_match = _UNITTEST_RESULT_RE.match(line)
        if res_match:
            _record_unittest_result_line(line, res_match, state)
        elif _PYTEST_PROGRESS_RE.match(line) or _UNITTEST_DASH_SEP_RE.match(line):
            state.filtered_noise += 1
        idx += 1
    return _finalize_unittest_result(state)


def _parse_generic_tracebacks(lines: list[str]) -> ParsedTestResult:
    failures: list[ParsedFailure] = []
    idx = 0
    while idx < len(lines):
        if _TB_START_RE.match(lines[idx]):
            raw_frames, exc_type, exc_msg, next_idx = _parse_python_traceback_block(
                lines,
                idx,
            )
            frames = _compact_frames(raw_frames, ())
            primary = frames[-1] if frames else None
            label = (
                f"{primary.file_path}:{primary.line_no}"
                if primary and primary.line_no is not None
                else f"traceback_{len(failures) + 1}"
            )
            failures.append(
                ParsedFailure(
                    test_name=label,
                    kind="ERROR",
                    phase="unknown",
                    exception_type=exc_type,
                    exception_message=exc_msg,
                    file_path=primary.file_path if primary else "",
                    line_no=primary.line_no if primary else None,
                    func_name=primary.func_name if primary else "",
                    code_line=primary.code_line if primary else "",
                    frames=frames,
                )
            )
            idx = max(next_idx, idx + 1)
            continue
        idx += 1

    if failures:
        summary = f"{len(failures)} Python traceback block(s) extracted"
    else:
        summary = "No pytest/unittest summary or structured failure blocks detected."
    return ParsedTestResult(
        framework="unknown",
        status="incomplete",
        summary_line=summary,
        counts={},
        failures=tuple(failures),
        filtered_noise_lines=0,
    )


def _format_failure_block(index: int, failure: ParsedFailure) -> str:
    lines = [f"### [{index}] {failure.kind} ({failure.phase}): {failure.test_name}"]
    if failure.file_path:
        loc = failure.file_path
        if failure.line_no is not None:
            loc = f"{loc}:{failure.line_no}"
        if failure.func_name:
            loc = f"{loc} in {failure.func_name}"
        lines.append(f"Location: {loc}")
    if failure.code_line:
        lines.append(f"Code: {failure.code_line}")
    if failure.exception_type or failure.exception_message:
        if failure.exception_type and failure.exception_message:
            exc_text = f"{failure.exception_type}: {failure.exception_message}"
        else:
            exc_text = failure.exception_type or failure.exception_message
        lines.append(f"Exception: {exc_text}")
    if failure.frames:
        lines.append("Traceback:")
        for frame in failure.frames:
            loc = frame.file_path
            if frame.line_no is not None:
                loc = f"{loc}:{frame.line_no}"
            if frame.func_name:
                loc = f"{loc} in {frame.func_name}"
            if frame.code_line:
                lines.append(f"  - {loc} | {frame.code_line}")
            else:
                lines.append(f"  - {loc}")
    return "\n".join(lines)


def _render_result(result: ParsedTestResult, req: ParseTestFailureRequest) -> str:
    header = [
        f"Framework: {result.framework}",
        f"Status: {result.status}",
        f"Summary: {result.summary_line}",
    ]
    if result.filtered_noise_lines > 0:
        header.append(f"Noise filtered: {result.filtered_noise_lines} lines")
    sections = ["\n".join(header)]

    if not result.failures:
        if result.status == "passed":
            sections.append("No test failures or errors found.")
        else:
            sections.append("No pytest/unittest summary or structured failure blocks detected.")
    else:
        total = len(result.failures)
        shown_failures = result.failures[:req.max_failures]
        for idx, failure in enumerate(shown_failures, start=1):
            sections.append(_format_failure_block(idx, failure))
        if total > req.max_failures:
            sections.append(
                f"[Showing {len(shown_failures)} of {total} failures; "
                "raise max_failures to include more.]"
            )

    rendered = "\n\n".join(sections)
    if len(rendered) <= req.max_chars:
        return rendered

    marker = f"\n\n[Truncated at max_chars={req.max_chars}]"
    budget = max(0, req.max_chars - len(marker))
    prefix = rendered[:budget]
    last_newline = prefix.rfind("\n")
    if last_newline > 0:
        prefix = prefix[:last_newline]
    return prefix + marker


def _parse_test_failure_sync(req: ParseTestFailureRequest) -> str:
    cleaned = _clean_terminal_output(req.raw_output)
    lines = cleaned.split("\n")
    framework = _detect_framework(lines) if req.framework == "auto" else req.framework
    if framework == "pytest":
        parsed = _parse_pytest(lines)
    elif framework == "unittest":
        parsed = _parse_unittest(lines)
    else:
        parsed = _parse_generic_tracebacks(lines)
    return _render_result(parsed, req)


@tool(
    name="parse_test_failure",
    description=(
        "Parse raw pytest or unittest runner output into structured failure diagnostics. "
        "Strips terminal ANSI/OSC escape sequences and noisy runner logs, and extracts "
        "failing test names, failure phases (call/setup/teardown/collection), exception "
        "types and messages, file:line locations, and compacted tracebacks. Use this "
        "instead of re-running tests when you already have raw test output."
    ),
)
async def parse_test_failure(
    raw_output: str,
    framework: str = "auto",
    max_failures: int = DEFAULT_MAX_FAILURES,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Parse raw pytest or unittest output into structured failure summaries.

    Args:
        raw_output: Raw stdout/stderr text captured from a test runner.
        framework: Target test framework ("auto", "pytest", or "unittest").
        max_failures: Maximum number of failures to include (clamped to 1-100).
        max_chars: Maximum output character length (clamped to 1000-100000).

    Returns:
        Structured diagnostic summary or an error message prefixed with ``[ERROR]:``.
    """
    try:
        req = _normalize_request(
            {
                "raw_output": raw_output,
                "framework": framework,
                "max_failures": max_failures,
                "max_chars": max_chars,
            }
        )
        logger.info(
            "[parse_test_failure] framework=%s max_failures=%s max_chars=%s",
            req.framework,
            req.max_failures,
            req.max_chars,
        )
        return await asyncio.to_thread(_parse_test_failure_sync, req)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"[ERROR]: parse_test_failure failed: {exc}"
