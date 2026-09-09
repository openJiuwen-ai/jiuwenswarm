# -*- coding: utf-8 -*-
"""防止 skill_code 非法 import 导致 ppt 无法注册；并锁住 P2 模板早拒契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill_turbo.environment import SkillTurboEnvironment
from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    FallbackContractError as FHFallbackContractError,
)
from jiuwenswarm.server.runtime.skill_turbo.plan_node import (
    FallbackContractError,
    PlanNode,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import requirement_collect as rc
from jiuwenswarm.server.runtime.skill_turbo.validator import PlanCodeValidator

_PPT_SKILL_DIR = Path(rc.__file__).resolve().parent
_SKILL_CODES_PREFIX = "jiuwenswarm.server.runtime.skill_turbo.skill_codes"


def test_fallback_contract_error_reexported_from_plan_node() -> None:
    assert FallbackContractError is FHFallbackContractError


def test_all_ppt_skill_codes_pass_builtin_validation() -> None:
    """与 Environment._validate_skill_code_dir 同口径：任一文件失败则 ppt 无法注册。"""
    validator = PlanCodeValidator.for_builtin_skill_code([f"{_SKILL_CODES_PREFIX}."])
    failures: list[str] = []
    for code_file in sorted(_PPT_SKILL_DIR.rglob("*.py")):
        if any(part in {"__pycache__", ".git"} for part in code_file.parts):
            continue
        errors = validator.validate(code_file.read_text(encoding="utf-8"))
        if errors:
            failures.append(f"{code_file.relative_to(_PPT_SKILL_DIR)}: {errors}")
    assert failures == []


def test_requirement_collect_must_not_import_fallback_handler() -> None:
    source = Path(rc.__file__).read_text(encoding="utf-8")
    assert "fallback_handler" not in source
    validator = PlanCodeValidator.for_builtin_skill_code([f"{_SKILL_CODES_PREFIX}."])
    assert validator.validate(source) == []


def test_env_scan_registers_ppt_skill() -> None:
    SkillTurboEnvironment._scan_cache.clear()
    env = SkillTurboEnvironment({"tools": {}, "model": None})
    assert env.has_skill("ppt")


@pytest.mark.parametrize(
    "ctx",
    [
        {"pack_dir": r"D:\templates\pack"},
        {"style_mode": "template_canvas"},
        {"pack_dir": r"D:\templates\pack", "style_mode": "template_canvas"},
    ],
)
def test_p2_template_canvas_early_reject_raises_fallback_contract(ctx: dict) -> None:
    node = rc.RequirementCollectNode()
    with pytest.raises(FallbackContractError) as exc_info:
        node._set_style_mode(dict(ctx))
    assert "template_canvas" in exc_info.value.reason
    assert exc_info.value.node_name == "p2_requirement_collect"


@pytest.mark.asyncio
async def test_plan_node_run_rethrows_fallback_contract_without_llm_fallback() -> None:
    class _RejectNode(PlanNode):
        def __init__(self) -> None:
            super().__init__(plan_name="t", instruction="t", sub_plans=[])

        async def _execute(self, inputs: dict) -> dict:
            raise FallbackContractError(node_name="t", reason="early reject")

    called = {"n": 0}

    async def _fallback(_node, _inputs, _err):
        called["n"] += 1
        return {"status": "fallback"}

    node = _RejectNode()
    node.set_runtime_callbacks(fallback=_fallback)
    with pytest.raises(FallbackContractError):
        await node.run({})
    assert called["n"] == 0
