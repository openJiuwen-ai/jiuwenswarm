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

def test_plugin_root_override_never_reads_default_root(tmp_path, monkeypatch):
    """P3（atomgit 2026-11-18）：原 beta/gamma 恒不存在断言换强。

    原用例的 beta/gamma 任何路径都不会创建，无法捕获「忽略 plugin_root 回退
    默认根」回归；改为双根同名插件注入——指定 plugin_root 时只准读注入根，
    缺省时读默认根（monkeypatch 的 PLUGIN_ROOT），两根路径互串即红。"""
    from jiuwenswarm.agents.harness.common.memory import plugin_discovery as pd

    default_root = tmp_path / "fake-home-jiwuenswarm"
    injected_root = tmp_path / "injected"
    _make_plugin(default_root, "alpha", "return {'from': 'default-root'}")
    _make_plugin(injected_root, "alpha", "return {'from': 'injected-root'}")
    monkeypatch.setattr(pd, "PLUGIN_ROOT", default_root)
    assert pd.load_memory_plugin("alpha", plugin_root=injected_root) == {"from": "injected-root"}
    assert pd.load_memory_plugin("alpha") == {"from": "default-root"}


def test_scalar_allowed_plugins_is_normalized_not_substring(tmp_path, caplog):
    """P2（atomgit 2026-11-18 #201711173）：标量白名单不得退化为子串匹配。

    `allowed_plugins: hindsight-memory`（YAML 标量）下 `name in str` 会让
    `hindsight` 以子串穿过白名单；归一化后逗号切分逐元素精确匹配，
    非法类型 fail-closed。修复前：hindsight 穿门=红。"""
    _make_plugin(tmp_path, "hindsight", "return object()")
    with caplog.at_level(logging.WARNING):
        assert load_memory_plugin(
            "hindsight", allowed_plugins="hindsight-memory", plugin_root=tmp_path) is None
    assert "rejected by allowed_plugins" in caplog.text
    assert load_memory_plugin(
        "hindsight", allowed_plugins="alpha, hindsight", plugin_root=tmp_path) is not None
    assert load_memory_plugin(
        "hindsight", allowed_plugins={"hindsight": 1}, plugin_root=tmp_path) is None
