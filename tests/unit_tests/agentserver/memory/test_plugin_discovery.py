# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the generic memory plugin loader (tmp-dir injection only)."""
import logging
from jiuwenswarm.agents.harness.common.memory.plugin_discovery import load_memory_plugin

ENTRY = "def build_provider(config):\n    {body}\n"

def _make_plugin(root, name, body):
    (root / name).mkdir(parents=True)
    (root / name / "plugin.py").write_text(ENTRY.format(body=body))

def test_load_returns_provider_and_whitelist(tmp_path):
    _make_plugin(tmp_path, "alpha", "return {'config': config}")
    assert load_memory_plugin("alpha", {"k": 1}, plugin_root=tmp_path) == {"config": {"k": 1}}
    assert load_memory_plugin("alpha", plugin_root=tmp_path) is not None  # no whitelist -> pass

def test_missing_entry_mapped_to_none_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        got = load_memory_plugin("ghost", plugin_root=tmp_path)
    assert got is None and "plugin 'ghost' entry not found" in caplog.text

def test_outside_whitelist_rejected(tmp_path, caplog):
    _make_plugin(tmp_path, "alpha", "return object()")
    with caplog.at_level(logging.WARNING):
        got = load_memory_plugin("alpha", allowed_plugins=["beta"], plugin_root=tmp_path)
    assert got is None and "rejected by allowed_plugins" in caplog.text

def test_factory_exception_mapped_to_none(tmp_path, caplog):
    _make_plugin(tmp_path, "boom", "raise RuntimeError('boom')")
    with caplog.at_level(logging.WARNING):
        assert load_memory_plugin("boom", plugin_root=tmp_path) is None
    assert "load failed" in caplog.text

def test_unsafe_names_rejected(tmp_path):
    for bad in ("../etc", "a.b", "中文", "a;b", "/abs", ""):
        assert load_memory_plugin(bad, plugin_root=tmp_path) is None

def test_tmp_injection_never_touches_real_user_dir(tmp_path):
    _make_plugin(tmp_path, "alpha", "return object()")
    load_memory_plugin("alpha", plugin_root=tmp_path)
    assert not (tmp_path / "beta").exists() and not (tmp_path / "gamma").exists()
