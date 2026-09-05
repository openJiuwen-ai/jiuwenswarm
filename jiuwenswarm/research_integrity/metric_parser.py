# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic metric extraction from experiment artifacts.

Metrics are *never* produced by an LLM: every number that can reach a paper
table must be parsed from a real artifact file with one of the locators below.
The parser is selected by the artifact's file extension; extensionless files
fall back to locator-prefix dispatch.

Supported locator forms:

- JSON  ``$.accuracy`` / ``$.results[0].score``      (dot path, ``[i]`` index)
- CSV   ``row=method_a,column=accuracy``             (row key x column header)
- JSONL ``jsonl[line=14].score``                     (0-based line index)

Any failure (missing file, unknown locator syntax, missing key, non-numeric
value) raises :class:`MetricParseError` — it never returns a guessed value.
"""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any


class MetricParseError(ValueError):
    """Raised when a metric cannot be deterministically extracted."""


def _to_float(value: Any, *, context: str) -> float:
    """Coerce an extracted value to float; reject bools and non-numbers."""
    if isinstance(value, bool):
        raise MetricParseError(f"{context}: boolean is not a numeric metric")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise MetricParseError(
                f"{context}: non-numeric string value {value!r}"
            ) from exc
    raise MetricParseError(f"{context}: non-numeric value of type {type(value).__name__}")


def _navigate_json(data: Any, segments: list[str], *, context: str) -> Any:
    """Walk ``data`` following dot/bracket path segments."""
    current = data
    for segment in segments:
        match = re.fullmatch(r"([^\[\]]+)(?:\[(\d+)\])?", segment)
        if not match:
            raise MetricParseError(f"{context}: malformed path segment {segment!r}")
        key, index = match.group(1), match.group(2)
        if key:
            if not isinstance(current, dict) or key not in current:
                raise MetricParseError(f"{context}: key {key!r} not found")
            current = current[key]
        if index is not None:
            idx = int(index)
            if not isinstance(current, list) or idx >= len(current):
                raise MetricParseError(f"{context}: index [{idx}] out of range")
            current = current[idx]
    return current


def _parse_json_locator(payload: Any, locator: str) -> float:
    """Extract a numeric value from parsed JSON via a ``$.a.b[0].c`` path."""
    path = locator[2:]  # strip leading "$."
    if not path:
        raise MetricParseError(f"{locator}: empty JSON path")
    segments = path.split(".")
    # A leading "$" remainder (e.g. "$.$") is malformed; keys may not be "$".
    value = _navigate_json(payload, segments, context=locator)
    return _to_float(value, context=locator)


def _parse_csv_locator(path: Path, locator: str) -> float:
    """Extract a numeric value from a CSV via ``row=<key>,column=<name>``."""
    match = re.fullmatch(r"row=(?P<row>[^,]+),column=(?P<col>.+)", locator)
    if not match:
        raise MetricParseError(
            f"{locator}: CSV locator must be 'row=<key>,column=<name>'"
        )
    row_key, column_name = match.group("row"), match.group("col")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise MetricParseError(f"{path.name}: empty CSV") from exc
        if column_name not in header:
            raise MetricParseError(
                f"{locator}: column {column_name!r} not in header {header}"
            )
        col_index = header.index(column_name)
        for row in reader:
            if not row:
                continue
            if row[0] == row_key:
                if col_index >= len(row):
                    raise MetricParseError(f"{locator}: short row {row!r}")
                return _to_float(row[col_index], context=locator)
    raise MetricParseError(f"{locator}: row key {row_key!r} not found")


def _parse_jsonl_locator(path: Path, locator: str) -> float:
    """Extract a numeric value from a JSONL file via ``jsonl[line=N].field``."""
    match = re.fullmatch(
        r"jsonl\[line=(?P<line>\d+)\]\.(?P<path>.+)", locator
    )
    if not match:
        raise MetricParseError(
            f"{locator}: JSONL locator must be 'jsonl[line=<n>].<path>'"
        )
    line_index = int(match.group("line"))
    field_path = match.group("path")
    with path.open("r", encoding="utf-8") as handle:
        for current_index, line in enumerate(handle):
            if current_index != line_index:
                continue
            stripped = line.strip()
            if not stripped:
                raise MetricParseError(f"{locator}: line {line_index} is blank")
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MetricParseError(
                    f"{locator}: line {line_index} is not valid JSON"
                ) from exc
            value = _navigate_json(
                payload, field_path.split("."), context=locator
            )
            return _to_float(value, context=locator)
    raise MetricParseError(f"{locator}: line {line_index} out of range")


def _parse_json_artifact(artifact: Path, locator: str) -> float:
    """Extract a metric from a JSON file via a ``$.a.b[0].c`` path."""
    if not locator.startswith("$."):
        raise MetricParseError(f"{locator}: unsupported locator syntax")
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MetricParseError(
            f"{artifact.name}: not valid JSON ({exc})"
        ) from exc
    return _parse_json_locator(payload, locator)


def parse_metric(path: str | Path, locator: str) -> float:
    """Deterministically extract one metric value from an artifact file.

    The parser is chosen by the artifact's file extension (``.json`` /
    ``.csv`` / ``.jsonl``); extensionless artifacts fall back to
    locator-prefix dispatch (``$.`` / ``row=`` / ``jsonl[``).

    Args:
        path: Artifact file path (JSON, CSV, or JSONL).
        locator: Locator string (see module docstring for the exact forms).

    Returns:
        The extracted float value. Non-finite values (NaN/inf) are returned
        as-is; callers decide whether to reject them (validator does by
        default) — parsing stays a pure extraction step.

    Raises:
        MetricParseError: The file is missing, the locator is malformed,
            the referenced position does not exist, or the value is not
            numeric.
    """
    artifact = Path(path)
    if not artifact.is_file():
        raise MetricParseError(f"{artifact}: artifact file does not exist")

    suffix = artifact.suffix.lower()
    if suffix == ".csv":
        return _parse_csv_locator(artifact, locator)
    if suffix == ".jsonl":
        return _parse_jsonl_locator(artifact, locator)
    if suffix == ".json":
        return _parse_json_artifact(artifact, locator)

    # Extensionless / unknown artifacts: dispatch on the locator prefix.
    if locator.startswith("$."):
        return _parse_json_artifact(artifact, locator)
    if locator.startswith("row="):
        return _parse_csv_locator(artifact, locator)
    if locator.startswith("jsonl["):
        return _parse_jsonl_locator(artifact, locator)

    raise MetricParseError(f"{locator}: unsupported locator syntax")


def is_finite_metric(value: float) -> bool:
    """Return whether *value* is a finite, paper-safe number."""
    return math.isfinite(value)


__all__ = ["MetricParseError", "parse_metric", "is_finite_metric"]
