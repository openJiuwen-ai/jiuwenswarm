# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Runtime log formatters."""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any

_RUNTIME_LOG_SEVERITY_MAP = {
    "CRITICAL": "FATAL",
    "WARNING": "WARN",
}
_RUNTIME_LOG_FIELD_SEPARATOR = "|"
_RUNTIME_LOG_VALUE_PATTERN = r"(?P<value>[^,\s\]\)\}]+)"
_RUNTIME_LOG_SESSION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\[session=(?P<value>[^\]\s]+)\]"),
    re.compile(r"(?i)(?<![A-Za-z0-9_])session[_-]?id\s*[:=]\s*" + _RUNTIME_LOG_VALUE_PATTERN),
    re.compile(r"(?i)(?<![A-Za-z0-9_])session\s*[:=]\s*" + _RUNTIME_LOG_VALUE_PATTERN),
]
_RUNTIME_LOG_SANDBOX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(?<![A-Za-z0-9_])sandbox[_-]?id\s*[:=]\s*" + _RUNTIME_LOG_VALUE_PATTERN),
    re.compile(r"(?i)(?<![A-Za-z0-9_])sandbox\s*[:=]\s*" + _RUNTIME_LOG_VALUE_PATTERN),
]
_RUNTIME_LOG_LEGACY_PREFIX_PATTERN = re.compile(
    r"^\s*"
    r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3,6})?"
    r"(?:[+-]\d{2}:?\d{2})?(?:\s+DST)?\s+"
    r"(?:\[\d+\]\s+)?"
    r"(?:FATAL|CRITICAL|ERROR|WARN|WARNING|INFO|DEBUG)\s+"
    r"(?:(?:[A-Za-z_][\w.]*)(?:\s+|-+\s+))?"
    r"(?:(?:[\w.-]+\.py):\d+:\s*)?"
)
_RUNTIME_LOG_EMPTY_BRACKETS_PATTERN = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")


def _clean_runtime_log_field(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return (
        text.replace(_RUNTIME_LOG_FIELD_SEPARATOR, " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
    )


def _record_string_attr(record: logging.LogRecord, names: tuple[str, ...]) -> str:
    for name in names:
        value = getattr(record, name, None)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _extract_first_runtime_log_value(text: str, patterns: list[re.Pattern[str]]) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return str(match.group("value") or "").strip()
    return ""


def _remove_runtime_log_value_tokens(
    text: str,
    value: str,
    patterns: list[re.Pattern[str]],
) -> str:
    if not value:
        return text

    def _replace(match: re.Match[str]) -> str:
        matched_value = str(match.group("value") or "").strip()
        return "" if matched_value == value else match.group(0)

    for pattern in patterns:
        text = pattern.sub(_replace, text)
    return text


def _compact_runtime_log_message(text: str) -> str:
    text = _RUNTIME_LOG_LEGACY_PREFIX_PATTERN.sub("", text)
    text = _RUNTIME_LOG_EMPTY_BRACKETS_PATTERN.sub("", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*,+", ",", text)
    text = re.sub(r"(^|\s),\s*", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,")


class JsonOnlyFormatter(logging.Formatter):
    """Output message only, without timestamp/level/logger prefix."""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class RuntimeLogFormatter(logging.Formatter):
    """Format runtime logs as Time|Severity|SessionID|SandboxID|Position|msg."""

    def format(self, record: logging.LogRecord) -> str:
        raw_message = record.getMessage()
        session_id = _record_string_attr(record, ("session_id", "sessionID"))
        if not session_id:
            session_id = _extract_first_runtime_log_value(raw_message, _RUNTIME_LOG_SESSION_PATTERNS)

        sandbox_id = _record_string_attr(record, ("sandbox_id", "sandboxID"))
        if not sandbox_id:
            sandbox_id = _extract_first_runtime_log_value(raw_message, _RUNTIME_LOG_SANDBOX_PATTERNS)

        message = _remove_runtime_log_value_tokens(
            raw_message,
            session_id,
            _RUNTIME_LOG_SESSION_PATTERNS,
        )
        message = _remove_runtime_log_value_tokens(
            message,
            sandbox_id,
            _RUNTIME_LOG_SANDBOX_PATTERNS,
        )
        message = _compact_runtime_log_message(message)
        if record.exc_info:
            message = f"{message} {self.formatException(record.exc_info)}".strip()
        if record.stack_info:
            message = f"{message} {self.formatStack(record.stack_info)}".strip()

        timestamp = datetime.datetime.fromtimestamp(record.created).astimezone()
        timestamp_text = (
            f"{timestamp:%Y-%m-%d %H:%M:%S}."
            f"{timestamp.microsecond // 1000:03d}"
            f"{timestamp:%z}"
        )
        if len(timestamp_text) >= 5:
            timestamp_text = f"{timestamp_text[:-2]}:{timestamp_text[-2:]}"

        severity = _RUNTIME_LOG_SEVERITY_MAP.get(record.levelname, record.levelname)
        position = f"{record.name} {record.filename}:{record.lineno}:"
        row = [timestamp_text, severity, session_id, sandbox_id, position, message]
        return _RUNTIME_LOG_FIELD_SEPARATOR.join(_clean_runtime_log_field(item) for item in row)


def format_session_log(session_id: str | None, content: str) -> str:
    """Prefix log content with session_id when present."""
    sid = str(session_id or "").strip()
    if not sid:
        return content
    return f"[session={sid}] {content}"
