# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""runtime 门面包：契约符号同一性 + skill_turbo_runtime 中立名注册。"""

from __future__ import annotations

import sys


def test_facade_reexports_same_objects():
    from jiuwenswarm.server.runtime.skill_turbo import plan_node, runtime

    assert runtime.AbortError is plan_node.AbortError
    assert runtime.PlanNode is plan_node.PlanNode
    assert runtime.DisableThinkingMixin is plan_node.DisableThinkingMixin
    assert runtime.FallbackContractError is plan_node.FallbackContractError
    assert isinstance(runtime.CONTRACT_VERSION, int)


def test_fallback_contract_error_single_identity_across_engine_modules():
    """FallbackContractError 在 plan_node 与 fallback_handler 间为同一类对象。

    （自 test_ppt_skill_code_registration 迁入：except/re-raise 判定与
    fallback 契约拦截都依赖类型同一性，禁止两处独立定义。）
    """
    from jiuwenswarm.server.runtime.skill_turbo import plan_node
    from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
        FallbackContractError as FH_FallbackContractError,
    )

    assert plan_node.FallbackContractError is FH_FallbackContractError


def test_tool_utils_submodule_importable_and_complete():
    """runtime kit 单副本：tool_utils 公共符号经门面子模块完整可用。"""
    from jiuwenswarm.server.runtime.skill_turbo.runtime import tool_utils

    for name in (
        "run_bash",
        "BashResult",
        "BashExecError",
        "strip_code_fence",
        "parse_bash_payload",
        "combined_output",
        "quote_path",
        "normalize_tool_text",
        "call_tool_with_retry",
    ):
        assert hasattr(tool_utils, name), name


def test_ensure_runtime_facade_registers_neutral_name():
    from jiuwenswarm.server.runtime.skill_turbo.runtime import (
        ensure_runtime_facade,
    )

    ensure_runtime_facade()
    facade = sys.modules.get("skill_turbo_runtime")
    assert facade is not None
    from jiuwenswarm.server.runtime.skill_turbo import runtime as real

    assert facade is real
    # 幂等：重复注册不抛错、不换对象
    ensure_runtime_facade()
    assert sys.modules["skill_turbo_runtime"] is real
