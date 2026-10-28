# -*- coding: utf-8 -*-
"""_merge_env_config_to_inputs 的 python_cmd 注入守卫（F_04 / S_01 步 1 / S_04 inputs 约定）。

skill_code 子进程解释器契约：executor 注入 sys.executable（缺省 / 空白），
显式非空值保留（resume 持久化值不覆盖）；skill 侧禁止 import sys / 探测 PATH。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor


def _make_executor(
    skill_root: str = "/skills", skill_name: str = "pptx-craft"
) -> SkillTurboExecutor:
    """绕过 __init__ 重装配：_merge_env_config_to_inputs 只消费 _env 两字段。"""
    executor = SkillTurboExecutor.__new__(SkillTurboExecutor)
    executor._env = SimpleNamespace(skill_root=skill_root, skill_name=skill_name)
    return executor


def test_injects_sys_executable_when_missing() -> None:
    """缺省：注入引擎当前解释器（venv / 打包后 embeddable python）。"""
    merged = _make_executor()._merge_env_config_to_inputs({})
    assert merged["python_cmd"] == sys.executable


def test_blank_treated_as_unset() -> None:
    """空白串视为未设置：显式传 "" / 空白按"要默认值"处理。"""
    for blank in ("", "   "):
        merged = _make_executor()._merge_env_config_to_inputs({"python_cmd": blank})
        assert merged["python_cmd"] == sys.executable


def test_preserves_explicit_non_empty() -> None:
    """显式非空值保留：resume 持久化的 python_cmd 不被覆盖。"""
    merged = _make_executor()._merge_env_config_to_inputs(
        {"python_cmd": "/opt/custom/python.exe"}
    )
    assert merged["python_cmd"] == "/opt/custom/python.exe"


def test_coexists_with_skill_root_and_skill_name() -> None:
    """三注入键共存且互不影响（回归：python_cmd 不破坏既有注入语义）。"""
    merged = _make_executor()._merge_env_config_to_inputs({})
    assert merged["skill_root"] == "/skills"
    assert merged["skill_name"] == "pptx-craft"
    assert merged["python_cmd"] == sys.executable
