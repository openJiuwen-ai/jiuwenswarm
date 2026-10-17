# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JiuWenSwarmDeepAdapter._resolve_model_by_name zen free-model resolution.

背景：Gateway 与 AgentServer 是独立进程、各持一份互不共享的 Zen 免费模型内存
缓存。创建端（Gateway）缓存就绪放行、执行端（AgentServer）缓存为空时，
``_resolve_model_by_name`` 直接查内存缓存，查不到就回退默认模型；对显式请求
的模型全部未命中时打 warning（含 requested 与 fallback 模型名）再回退，
不再静默——cron 无人值守执行时用错模型能及时从日志发现。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime import opencode_zen
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


@pytest.fixture(autouse=True)
def _restore_zen_cache():
    """直接操作模块级缓存的单测结束后恢复为空（与进程初始态一致）。"""
    yield
    with opencode_zen._zen_free_lock:
        opencode_zen._zen_free_entries = []


def _make_adapter(default_model_name: str = "my-default-model") -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model_cache: dict[str, Any] = {}
    adapter._model_name_to_keys: dict[str, list[str]] = {}
    adapter._model = SimpleNamespace(
        model_config=SimpleNamespace(model_name=default_model_name)
    )
    return adapter


def test_resolve_model_total_miss_falls_back_with_warning(monkeypatch):
    """全部未命中时回退默认模型，且打 warning（不再静默）。"""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module

    adapter = _make_adapter(default_model_name="my-default-model")
    monkeypatch.setattr(opencode_zen, "_zen_free_models_enabled", lambda: True)
    with opencode_zen._zen_free_lock:
        opencode_zen._zen_free_entries = []

    # 项目日志配置可能关闭 propagate，caplog 不稳；直接 patch logger.warning 收集
    warning_msgs: list[str] = []

    def _capture_warning(msg: str, *args: Any) -> None:
        warning_msgs.append(msg % args if args else msg)

    monkeypatch.setattr(interface_module.logger, "warning", _capture_warning)

    model = adapter._resolve_model_by_name("no-such-model")

    assert model is adapter._model
    assert warning_msgs, "total miss must log a warning instead of silent fallback"
    assert "no-such-model" in warning_msgs[0]
    assert "my-default-model" in warning_msgs[0]


def test_agentos_resolve_global_index_over_per_name_collision(monkeypatch):
    """The compatibility alias must use develop's global map, not name#counter."""
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PROFILE", "agentos")
    adapter = _make_adapter()
    default, backup, collision = object(), object(), object()
    adapter._model_cache = {"shared#0": default, "shared#1": backup, "shared#2": collision,
                            "backup-alias": backup}
    adapter._global_index_to_cache_key = {1: "shared#0", 2: "shared#1", 3: "shared#2"}
    assert adapter._resolve_model_by_name("shared#2") is backup
    assert adapter._resolve_model_by_name("backup-alias") is backup
    assert adapter._resolve_model_by_name("other#2") is adapter._model
    assert adapter._resolve_model_by_name("shared#99") is adapter._model
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_PROFILE")
    # Personal edition retains develop's existing indexed selection semantics.
    assert adapter._resolve_model_by_name("other#2") is backup
