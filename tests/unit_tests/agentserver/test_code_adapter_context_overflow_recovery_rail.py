# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ContextOverflowRecoveryRail registration in JiuwenSwarmCodeAdapter.

链路 B (code 模式) 补齐溢出兜底 rail 的桩测。验证：
- TC-001: _FIXED_RAIL_NAMES 含 ContextOverflowRecoveryRail（防动态重复注册 + 标记为固定）
- TC-002: _RAIL_BUILD_NAMES 不含 ContextOverflowRecoveryRail（固定 rail 不走动态加载）
- TC-003: _build_agent_rails 返回的固定列表含 _context_overflow_recovery_rail 条目（属性名正确）
- TC-004: _build_context_overflow_recovery_rail 复用父类方法，可调用且返回 rail 实例或 None（不抛）

NOTE: 复用 lsp_rail 测试的 mock 注入模式 —— InMemoryTrajectoryRegistry 在已发布的
openjiuwen 包里尚不存在，需在 import 链解析前注入 mock，否则 collection 失败。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import openjiuwen.agent_evolving.trajectory as _traj_mod
if not hasattr(_traj_mod, "InMemoryTrajectoryRegistry"):
    _traj_mod.InMemoryTrajectoryRegistry = MagicMock

import jiuwenswarm.server.runtime.agent_adapter.interface_code as _ic_mod
from jiuwenswarm.agents.harness.common.rails.context_overflow_recovery_rail import (
    ContextOverflowRecoveryRail,
)

_RAIL_BUILD_NAMES = getattr(_ic_mod, "_RAIL_BUILD_NAMES")
_RailBuildInfo = getattr(_ic_mod, "_RailBuildInfo")
JiuwenSwarmCodeAdapter = _ic_mod.JiuwenSwarmCodeAdapter
_FIXED_RAIL_NAMES = getattr(JiuwenSwarmCodeAdapter, "_FIXED_RAIL_NAMES")


# ─── TC-001: _FIXED_RAIL_NAMES 含 ContextOverflowRecoveryRail ─────────────


def test_fixed_rail_names_contains_context_overflow_recovery_rail():
    """Verify ContextOverflowRecoveryRail is registered as a fixed rail for code mode.

    链路 B 原本缺溢出恢复 rail；补齐后该名字必须在 _FIXED_RAIL_NAMES 里，
    既标记为固定 rail，又防止 modes.code.rails 动态配置时重复注册。
    """
    assert "ContextOverflowRecoveryRail" in _FIXED_RAIL_NAMES, (
        "ContextOverflowRecoveryRail should be in _FIXED_RAIL_NAMES "
        "so code mode has overflow recovery parity with deep mode (link A)"
    )


# ─── TC-002: _RAIL_BUILD_NAMES 不含 ContextOverflowRecoveryRail ───────────


def test_rail_build_names_does_not_contain_context_overflow_recovery_rail():
    """Verify recovery rail is NOT in the dynamic rail map (it's a fixed rail)."""
    assert "ContextOverflowRecoveryRail" not in _RAIL_BUILD_NAMES, (
        "ContextOverflowRecoveryRail is a fixed rail and should not appear "
        "in _RAIL_BUILD_NAMES (dynamic-config map); fixed rails are added "
        "directly to _build_agent_rails"
    )


# ─── TC-003: _build_agent_rails 固定列表含 recovery rail 条目 ──────────────


def test_build_agent_rails_includes_context_overflow_recovery_rail_entry():
    """Verify the fixed rail list built by _build_agent_rails contains a
    _context_overflow_recovery_rail RailBuildInfo with the correct attr_name.

    _build_agent_rails 末尾会调 _instantiate_rails 把 RailBuildInfo 列表实例化成
    rail 列表，因此桩掉 _instantiate_rails 让它原样返回 rail_infos，避免触发真实
    rail 构造的重依赖，只校验 RailBuildInfo 的 attr_name 与 build_func。
    """
    adapter = JiuwenSwarmCodeAdapter()

    # 桩 _instantiate_rails：直接返回传入的 rail_infos（不实例化），便于检查条目
    captured: list = []
    real_instantiate = adapter._instantiate_rails

    def _passthrough_instantiate(rail_infos, config_base, *args, **kwargs):
        captured.extend(rail_infos)
        return rail_infos

    object.__setattr__(adapter, "_instantiate_rails", _passthrough_instantiate)
    try:
        adapter._build_agent_rails(config={}, config_base={}, mode="code")
    finally:
        object.__setattr__(adapter, "_instantiate_rails", real_instantiate)

    rail_infos = captured
    attr_names = [ri.attr_name for ri in rail_infos]
    assert "_context_overflow_recovery_rail" in attr_names, (
        "fixed rail list should contain _context_overflow_recovery_rail entry"
    )
    # 与 _context_processor_rail 相邻，体现“上下文处理 + 溢出兜底”的成对设计
    proc_idx = attr_names.index("_context_processor_rail")
    recovery_idx = attr_names.index("_context_overflow_recovery_rail")
    assert recovery_idx == proc_idx + 1, (
        "_context_overflow_recovery_rail should immediately follow "
        f"_context_processor_rail (got proc_idx={proc_idx}, recovery_idx={recovery_idx})"
    )
    # build_func 应复用父类 _build_context_overflow_recovery_rail（静态方法）
    recovery_info = rail_infos[recovery_idx]
    assert recovery_info.build_func == adapter._build_context_overflow_recovery_rail, (
        "recovery rail build_func should be the inherited parent static method "
        "_build_context_overflow_recovery_rail"
    )


# ─── TC-004: _build_context_overflow_recovery_rail 可调用且返回 rail/None ──


def test_build_context_overflow_recovery_rail_callable_returns_rail_or_none():
    """Verify the inherited parent build method is callable and returns either
    a ContextOverflowRecoveryRail instance or None (never raises).

    父类 JiuWenSwarmDeepAdapter._build_context_overflow_recovery_rail 是 @staticmethod，
    内部 try/except 包裹实例化；CodeAdapter 继承后应同样可调用，返回值类型正确。
    """
    adapter = JiuwenSwarmCodeAdapter()
    # 静态方法，可通过实例或类调用
    result = adapter._build_context_overflow_recovery_rail()
    assert result is None or isinstance(result, ContextOverflowRecoveryRail), (
        f"expected None or ContextOverflowRecoveryRail, got {type(result).__name__}"
    )
    # 成功路径：max_recovery_attempts 应为 3（父类硬编码）
    if result is not None:
        assert result._max_recovery_attempts == 3, (
            "ContextOverflowRecoveryRail should be built with max_recovery_attempts=3"
        )
