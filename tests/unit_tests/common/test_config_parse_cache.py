# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the config.yaml parse cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.common import config as config_module
from jiuwenswarm.common.config import _read_with_retry


@pytest.fixture(name="yaml_file")
def _yaml_file(tmp_path: Path) -> Path:
    """Write a small YAML file and clear the cache around the test."""
    path = tmp_path / "config.yaml"
    path.write_text("alpha: 1\nnested:\n  key: value\n", encoding="utf-8")
    config_module._YAML_PARSE_CACHE.clear()
    yield path
    config_module._YAML_PARSE_CACHE.clear()


def _parse_count(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count how often the YAML parser actually runs."""
    calls = {"n": 0}
    real_load = config_module.yaml.safe_load

    def _counting_load(stream):
        calls["n"] += 1
        return real_load(stream)

    monkeypatch.setattr(config_module.yaml, "safe_load", _counting_load)
    return calls


def test_repeat_reads_parse_once(yaml_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The parse is the expensive part, so it must not repeat."""
    calls = _parse_count(monkeypatch)

    for _ in range(5):
        assert _read_with_retry(yaml_file) == {"alpha": 1, "nested": {"key": "value"}}

    assert calls["n"] == 1


def test_edit_invalidates_the_cache(yaml_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rewritten file must be re-parsed, not served from cache."""
    calls = _parse_count(monkeypatch)
    _read_with_retry(yaml_file)

    yaml_file.write_text("alpha: 2\n", encoding="utf-8")
    # Size differs here; the stamp also carries mtime for same-size edits.
    assert _read_with_retry(yaml_file) == {"alpha": 2}
    assert calls["n"] == 2


def test_same_size_edit_still_invalidates(
    yaml_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte-identical length must not defeat invalidation."""
    yaml_file.write_text("alpha: 1\n", encoding="utf-8")
    calls = _parse_count(monkeypatch)
    _read_with_retry(yaml_file)

    stat_result = yaml_file.stat()
    yaml_file.write_text("alpha: 9\n", encoding="utf-8")
    os.utime(yaml_file, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000))

    assert _read_with_retry(yaml_file) == {"alpha": 9}
    assert calls["n"] == 2


def test_callers_get_independent_copies(yaml_file: Path) -> None:
    """Downstream mutates the result in place, so copies must not be shared.

    ``_normalize_config`` edits the dict it is handed; sharing one object across
    callers would let it accumulate normalizations into the cache.
    """
    first = _read_with_retry(yaml_file)
    first["injected"] = True
    first["nested"]["key"] = "mutated"

    second = _read_with_retry(yaml_file)

    assert "injected" not in second
    assert second["nested"]["key"] == "value"


def test_missing_file_is_not_cached(tmp_path: Path) -> None:
    """An unstattable path must fall through rather than poison the cache."""
    missing = tmp_path / "absent.yaml"

    with pytest.raises(OSError):
        _read_with_retry(missing)

    assert str(missing) not in config_module._YAML_PARSE_CACHE


def test_distinct_paths_do_not_collide(tmp_path: Path) -> None:
    """Two config files must not serve each other's contents."""
    config_module._YAML_PARSE_CACHE.clear()
    first = tmp_path / "a.yaml"
    second = tmp_path / "b.yaml"
    first.write_text("which: a\n", encoding="utf-8")
    second.write_text("which: b\n", encoding="utf-8")

    assert _read_with_retry(first) == {"which": "a"}
    assert _read_with_retry(second) == {"which": "b"}
    assert _read_with_retry(first) == {"which": "a"}
