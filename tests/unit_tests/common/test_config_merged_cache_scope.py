# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""合并配置缓存只随 YAML 文件变化失效，ns 级清理不再连带重解析。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.common import config as config_module


@pytest.fixture(name="load_count")
def _load_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    user_yaml = tmp_path / "config.yaml"
    user_yaml.write_text("alpha: 1\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_current_config_yaml_path", lambda: user_yaml)
    calls = {"n": 0, "path": user_yaml}
    real_load = config_module.load_yaml_dict

    def _counting(path):
        calls["n"] += 1
        return real_load(path)

    monkeypatch.setattr(config_module, "load_yaml_dict", _counting)
    config_module.invalidate_merged_config_cache()
    yield calls
    config_module.clear_config_cache()


def test_namespace_clear_keeps_merged_yaml_cache(load_count: dict[str, object]) -> None:
    first = config_module.get_merged_config_dict()
    config_module.clear_config_cache(service_id="default", agent_id="office")
    config_module.clear_config_cache(service_id="default", agent_id="default")
    assert config_module.get_merged_config_dict() is first
    assert load_count["n"] == 2  # 模板 + 用户 override 各解析一次


def test_full_clear_still_reparses(load_count: dict[str, object]) -> None:
    config_module.get_merged_config_dict()
    config_module.clear_config_cache()
    config_module.get_merged_config_dict()
    assert load_count["n"] == 4


def test_same_mtime_rewrite_with_new_size_reparses(load_count: dict[str, object]) -> None:
    first = config_module.get_merged_config_dict()
    path = Path(load_count["path"])
    stat = path.stat()
    path.write_text("alpha: 22\n", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert config_module.get_merged_config_dict() is not first
    assert load_count["n"] == 4


def test_missing_user_yaml_is_still_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, load_count) -> None:
    monkeypatch.setattr(config_module, "_current_config_yaml_path", lambda: tmp_path / "absent.yaml")
    first = config_module.get_merged_config_dict()
    assert config_module.get_merged_config_dict() is first
    assert load_count["n"] == 2


def test_stat_failure_skips_merged_cache(monkeypatch: pytest.MonkeyPatch, load_count) -> None:
    user_yaml = Path(load_count["path"])
    real_stat = Path.stat

    def _stat(path, *args, **kwargs):
        if path == user_yaml:
            raise PermissionError("stat denied")
        return real_stat(path, *args, **kwargs)

    loads = {"n": 0}

    def _load(_path):
        loads["n"] += 1
        return {}

    monkeypatch.setattr(Path, "stat", _stat)
    monkeypatch.setattr(config_module, "load_yaml_dict", _load)
    first = config_module.get_merged_config_dict()
    assert config_module.get_merged_config_dict() is not first
    assert loads["n"] == 4


def test_yaml_edit_still_reparses_after_namespace_clear(load_count: dict[str, object]) -> None:
    first = config_module.get_merged_config_dict()
    config_module.clear_config_cache(service_id="default", agent_id="office")
    path = Path(load_count["path"])
    path.write_text("alpha: 2\n", encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert config_module.get_merged_config_dict() is not first
    assert load_count["n"] == 4
