# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for artifact hashing, discovery, and tamper verification."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.research_integrity.artifact_tracker import (
    discover_artifacts,
    record_artifact,
    sha256_file,
    verify_artifact,
)


def test_artifact_hash(tmp_path: Path) -> None:
    """record_artifact hashes content, size, and kind correctly."""
    artifact_path = tmp_path / "results.json"
    artifact_path.write_text('{"accuracy": 0.75}', encoding="utf-8")

    record = record_artifact(artifact_path, run_id="run_t_1")

    assert record.sha256 == sha256_file(artifact_path)
    assert len(record.sha256) == 64
    assert record.size_bytes == artifact_path.stat().st_size
    assert record.kind == "json"
    assert record.run_id == "run_t_1"
    assert record.artifact_id.startswith("art_run_t_1_")


def test_artifact_hash_missing_file(tmp_path: Path) -> None:
    """Recording a non-existent artifact raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        record_artifact(tmp_path / "missing.json", run_id="run_t_1")


def test_artifact_kind_detection(tmp_path: Path) -> None:
    """Kind is derived from suffix (json/csv/jsonl/txt/other)."""
    cases = {"a.json": "json", "b.csv": "csv", "c.jsonl": "jsonl",
             "d.txt": "txt", "e.bin": "other"}
    for name, kind in cases.items():
        path = tmp_path / name
        path.write_bytes(b"x")
        assert record_artifact(path, run_id="run_t_1").kind == kind


def test_discover_artifacts_relative_to_base(tmp_path: Path) -> None:
    """Relative artifact paths resolve against base_dir."""
    out_dir = tmp_path / "results"
    out_dir.mkdir()
    (out_dir / "r.json").write_text("{}", encoding="utf-8")

    records = discover_artifacts(
        ["results/r.json"], run_id="run_t_1", base_dir=tmp_path
    )

    assert len(records) == 1
    assert records[0].path.endswith("r.json")


def test_verify_artifact_detects_tampering(tmp_path: Path) -> None:
    """A post-hash modification invalidates the artifact record."""
    artifact_path = tmp_path / "results.json"
    artifact_path.write_text('{"accuracy": 0.75}', encoding="utf-8")
    record = record_artifact(artifact_path, run_id="run_t_1")

    assert verify_artifact(record) is True

    artifact_path.write_text('{"accuracy": 0.99}', encoding="utf-8")
    assert verify_artifact(record) is False


def test_verify_artifact_detects_deletion(tmp_path: Path) -> None:
    """A deleted artifact no longer verifies."""
    artifact_path = tmp_path / "results.json"
    artifact_path.write_text("{}", encoding="utf-8")
    record = record_artifact(artifact_path, run_id="run_t_1")

    artifact_path.unlink()
    assert verify_artifact(record) is False
