# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""builtin_skill_code 策略：相对导入放行 + 门面白名单（外部 turbo code 适配）。"""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidator


def _v() -> PlanCodeValidator:
    return PlanCodeValidator.for_builtin_skill_code(["skill_turbo_codes_ppt."])


def test_relative_import_allowed():
    """外部 turbo code 包内互引（相对导入）放行。"""
    errors = _v().validate("from .ppt_common import PptCommon\n")
    assert errors == []


def test_runtime_facade_whitelisted():
    assert _v().validate("from skill_turbo_runtime import AbortError, PlanNode\n") == []
    assert _v().validate("from skill_turbo_runtime.tool_utils import run_bash\n") == []


def test_engine_internal_still_denied():
    # 引擎内部路径仍禁止（skill code 不得绕过门面）
    errors = _v().validate(
        "from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor\n"
    )
    assert errors
    assert "禁止 import" in errors[0]


def test_dangerous_stdlib_still_denied():
    assert _v().validate("import os\n")
    assert _v().validate("import subprocess\n")


def test_abort_reraise_still_enforced():
    code = "try:\n    pass\nexcept Exception as e:\n    pass\n"
    errors = _v().validate(code)
    assert any("AbortError" in e for e in errors)


def test_plan_code_policy_still_denies_relative():
    """plan_code 策略不受影响（plan_code 无包上下文，仍禁相对导入）。"""
    from jiuwenswarm.server.runtime.skill_turbo.validator import (
        PlanCodeValidator as V,
    )

    plan_v = V(allowed_import_prefixes=["skill_turbo_codes_ppt."])
    errors = plan_v.validate("from .ppt import root\n")
    assert errors
