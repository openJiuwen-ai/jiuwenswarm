# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for deterministic metric parsing (JSON / CSV / JSONL)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.research_integrity.metric_parser import (
    MetricParseError,
    parse_metric,
)


def test_json_metric_parser(tmp_path: Path) -> None:
    """$.path locators navigate dicts, nested dicts, and list indices."""
    artifact = tmp_path / "results.json"
    artifact.write_text(
        json.dumps(
            {
                "accuracy": 0.75,
                "results": [{"score": 0.5}, {"score": 0.9}],
                "metrics": {"f1": 0.66},
            }
        ),
        encoding="utf-8",
    )

    assert parse_metric(artifact, "$.accuracy") == pytest.approx(0.75)
    assert parse_metric(artifact, "$.results[1].score") == pytest.approx(0.9)
    assert parse_metric(artifact, "$.metrics.f1") == pytest.approx(0.66)


def test_json_metric_parser_string_numbers(tmp_path: Path) -> None:
    """Numeric strings are coerced; booleans and objects are rejected."""
    artifact = tmp_path / "results.json"
    artifact.write_text(
        json.dumps({"a": "0.5", "b": True, "c": {"d": 1}}), encoding="utf-8"
    )

    assert parse_metric(artifact, "$.a") == pytest.approx(0.5)
    with pytest.raises(MetricParseError, match="boolean"):
        parse_metric(artifact, "$.b")
    with pytest.raises(MetricParseError, match="non-numeric"):
        parse_metric(artifact, "$.c")


def test_json_metric_parser_errors(tmp_path: Path) -> None:
    """Missing keys, bad paths, and missing files raise MetricParseError."""
    artifact = tmp_path / "results.json"
    artifact.write_text(json.dumps({"accuracy": 0.75}), encoding="utf-8")

    with pytest.raises(MetricParseError, match="not found"):
        parse_metric(artifact, "$.missing")
    with pytest.raises(MetricParseError, match="out of range"):
        parse_metric(artifact, "$.accuracy[3]")
    with pytest.raises(MetricParseError, match="does not exist"):
        parse_metric(tmp_path / "nope.json", "$.accuracy")
    with pytest.raises(MetricParseError, match="unsupported locator"):
        parse_metric(artifact, "accuracy")


def test_csv_metric_parser(tmp_path: Path) -> None:
    """row=<key>,column=<name> locators index by first-column key."""
    artifact = tmp_path / "results.csv"
    artifact.write_text(
        "method,accuracy,token_usage\n"
        "method_a,0.75,1024\n"
        "recentk,0.61,1024\n",
        encoding="utf-8",
    )

    assert parse_metric(artifact, "row=method_a,column=accuracy") == pytest.approx(0.75)
    assert parse_metric(artifact, "row=recentk,column=token_usage") == pytest.approx(1024)


def test_csv_metric_parser_errors(tmp_path: Path) -> None:
    """Unknown column, unknown row key, and malformed locators are errors."""
    artifact = tmp_path / "results.csv"
    artifact.write_text("method,accuracy\nmethod_a,0.75\n", encoding="utf-8")

    with pytest.raises(MetricParseError, match="column"):
        parse_metric(artifact, "row=method_a,column=f1")
    with pytest.raises(MetricParseError, match="not found"):
        parse_metric(artifact, "row=fullctx,column=accuracy")
    with pytest.raises(MetricParseError, match="must be"):
        parse_metric(artifact, "method_a")


def test_jsonl_metric_parser(tmp_path: Path) -> None:
    """jsonl[line=N].field locators parse one line of a JSONL file."""
    artifact = tmp_path / "trace.jsonl"
    artifact.write_text(
        "\n".join(
            json.dumps({"step": i, "score": i / 10}) for i in range(3)
        )
        + "\n",
        encoding="utf-8",
    )

    assert parse_metric(artifact, "jsonl[line=0].score") == pytest.approx(0.0)
    assert parse_metric(artifact, "jsonl[line=2].score") == pytest.approx(0.2)


def test_jsonl_metric_parser_errors(tmp_path: Path) -> None:
    """Out-of-range lines and invalid JSON lines are errors."""
    artifact = tmp_path / "trace.jsonl"
    artifact.write_text('{"score": 1.0}\nnot json\n', encoding="utf-8")

    with pytest.raises(MetricParseError, match="out of range"):
        parse_metric(artifact, "jsonl[line=5].score")
    with pytest.raises(MetricParseError, match="not valid JSON"):
        parse_metric(artifact, "jsonl[line=1].score")
