# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for environment fingerprint capture."""

from __future__ import annotations

import sys
from pathlib import Path

from jiuwenswarm.research_integrity.fingerprint import (
    capture_environment_fingerprint,
)


def test_environment_fingerprint_basic(tmp_path: Path) -> None:
    """Fingerprint captures python/platform and is id-addressed."""
    fp = capture_environment_fingerprint(tmp_path, capture_git=False)

    assert fp.python_version == sys.version.split()[0]
    assert fp.platform
    assert len(fp.fingerprint_id) == 64
    assert fp.git_commit is None  # git capture disabled
    assert fp.git_dirty is None


def test_environment_fingerprint_deterministic(tmp_path: Path) -> None:
    """Same environment inputs -> same fingerprint id."""
    fp1 = capture_environment_fingerprint(tmp_path, capture_git=False)
    fp2 = capture_environment_fingerprint(tmp_path, capture_git=False)
    assert fp1.fingerprint_id == fp2.fingerprint_id


def test_environment_fingerprint_changes_with_content(tmp_path: Path) -> None:
    """Changing a hashed input changes the fingerprint id."""
    config = tmp_path / "config.yaml"
    config.write_text("a: 1\n", encoding="utf-8")

    fp1 = capture_environment_fingerprint(
        tmp_path, config_path=config, capture_git=False
    )
    config.write_text("a: 2\n", encoding="utf-8")
    fp2 = capture_environment_fingerprint(
        tmp_path, config_path=config, capture_git=False
    )

    assert fp1.fingerprint_id != fp2.fingerprint_id
    assert fp1.config_hash != fp2.config_hash


def test_environment_fingerprint_dataset_hashes(tmp_path: Path) -> None:
    """Dataset files are hashed individually into dataset_hashes."""
    dataset = tmp_path / "data.jsonl"
    dataset.write_text('{"q": 1}\n', encoding="utf-8")

    fp = capture_environment_fingerprint(
        tmp_path, dataset_paths=[dataset], capture_git=False
    )

    assert str(dataset) in fp.dataset_hashes
    assert len(fp.dataset_hashes[str(dataset)]) == 64


def test_environment_fingerprint_env_whitelist(tmp_path: Path, monkeypatch) -> None:
    """Only whitelisted env vars are recorded; others never leak."""
    monkeypatch.setenv("RESEARCH_TEST_ALLOWED", "visible")
    monkeypatch.setenv("RESEARCH_TEST_SECRET", "hidden")

    fp = capture_environment_fingerprint(
        tmp_path,
        capture_git=False,
        env_whitelist=["RESEARCH_TEST_ALLOWED"],
    )

    assert fp.environment_variables_whitelist == {"RESEARCH_TEST_ALLOWED": "visible"}
    dumped = fp.model_dump_json()
    assert "hidden" not in dumped


def test_environment_fingerprint_code_hash(tmp_path: Path) -> None:
    """The code hash covers .py files and changes when they change."""
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    fp1 = capture_environment_fingerprint(tmp_path, capture_git=False)
    assert fp1.code_hash is not None

    (tmp_path / "code.py").write_text("x = 2\n", encoding="utf-8")
    fp2 = capture_environment_fingerprint(tmp_path, capture_git=False)
    assert fp1.code_hash != fp2.code_hash


def test_environment_fingerprint_git_state(tmp_path: Path) -> None:
    """Inside a git repo the commit and dirty flag are captured."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    fp = capture_environment_fingerprint(tmp_path, capture_git=True)

    assert fp.git_commit and len(fp.git_commit) >= 7
    assert fp.git_dirty is False

    (tmp_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    fp_dirty = capture_environment_fingerprint(tmp_path, capture_git=True)
    assert fp_dirty.git_dirty is True
