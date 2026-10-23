# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""math_reasoning 专家团包的加载与桥接工具校验（全部离线，不依赖平台部署）。"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from openjiuwen.agent_teams.experts import load_agent_group_package_bundle

REPO_ROOT = Path(__file__).resolve().parents[4]
PKG = (
    REPO_ROOT
    / "jiuwenswarm"
    / "resources"
    / "agent"
    / "workspace"
    / "plugins"
    / "agent_groups"
    / "math_reasoning"
)
MEMBERS = ("nl_solver", "formal_prover", "opt_solver", "paper_gen")


@pytest.fixture(scope="module")
def bundle():
    return load_agent_group_package_bundle(PKG)


def test_manifest_declares_leader_and_four_members(bundle):
    assert set(bundle.templates) == {"leader", *MEMBERS}


def test_every_member_carries_math_platform_tool(bundle):
    for name in MEMBERS:
        template = bundle.templates[name]
        tool_classes = [tool.params["class_name"] for tool in template.tools]
        assert "MathPlatformTool" in tool_classes, name
        for tool in template.tools:
            assert Path(tool.params["file_path"]).is_file()


def test_leader_has_no_execution_tool(bundle):
    leader = bundle.templates["leader"]
    tool_classes = [tool.params["class_name"] for tool in leader.tools]
    assert "MathPlatformTool" not in tool_classes


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location(
        "math_platform_bridge", PKG / "tools" / "math_platform.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_tool_rejects_invalid_backend():
    module = _load_bridge_module()
    tool = module.MathPlatformTool()
    result = asyncio.run(tool.invoke({"backend": "nope", "question": "1+1"}))
    assert result["success"] is False


def test_bridge_tool_requires_question():
    module = _load_bridge_module()
    tool = module.MathPlatformTool()
    result = asyncio.run(tool.invoke({"backend": "nl", "question": ""}))
    assert result["success"] is False
