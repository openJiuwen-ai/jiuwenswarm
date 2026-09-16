# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Research tool providers for swarm provider-based team assembly.

为科研论文自动生成流水线 (FARS 式四模块) 提供两个框架级工具:

* ``deepsearch_tools`` — openJiuwen-DeepSearch 文献检索工具 (Ideation/Writing
  模块的证据检索, 报告落盘可溯源)。
* ``memory_eval`` — LongMemEval 记忆策略评测执行工具 (Experiment 模块,
  逐题 token/延迟流水落盘, 支持断点续跑)。

两个 provider 均由 ``research_tools.enabled`` 门控 (默认 false, 不影响现有
部署), 参数从 config.yaml 的 ``research_tools`` 段烘焙。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ElementKind,
    harness_element,
)

from jiuwenswarm.agents.harness.common.tools.deepsearch_tools import DeepSearchToolkit
from jiuwenswarm.agents.harness.common.tools.memory_eval_tools import MemoryEvalToolkit
from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

# Provider name constants; namespaced under the shared "swarm." prefix.
DEEPSEARCH_TOOLS = "swarm.deepsearch_tools"
MEMORY_EVAL_TOOLS = "swarm.memory_eval_tools"


@harness_element(
    kind=ElementKind.TOOL,
    name=DEEPSEARCH_TOOLS,
    description="DeepSearch literature-research tool for the research pipeline "
    "(gated by research_tools.enabled + research_tools.deepsearch).",
)
def build_deepsearch_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the DeepSearch literature tool from the config source."""
    if not params.get("enabled", False):
        return []
    ds = params.get("deepsearch_config", {}) or {}
    if not ds.get("repo_dir"):
        logger.info("[swarm.deepsearch_tools] skipped: repo_dir not configured")
        return []
    try:
        tools = DeepSearchToolkit(
            repo_dir=str(ds.get("repo_dir", "")),
            python_exe=str(ds.get("python", "")),
            extra_args=list(ds.get("extra_args", []) or []),
            timeout_sec=int(ds.get("timeout_sec", 900)),
        ).get_tools()
        logger.info("[swarm.deepsearch_tools] built %d tools", len(tools))
        return list(tools)
    except Exception as exc:  # noqa: BLE001 - 工具构建失败不阻断团队装配
        logger.warning("[swarm.deepsearch_tools] construction failed: %s", exc)
        return []


@harness_element(
    kind=ElementKind.TOOL,
    name=MEMORY_EVAL_TOOLS,
    description="LongMemEval memory-policy benchmark tool for the research "
    "pipeline's Experiment phase (gated by research_tools.enabled + "
    "research_tools.memory_eval).",
)
def build_memory_eval_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the memory-eval execution tool from the config source."""
    if not params.get("enabled", False):
        return []
    me = params.get("memory_eval_config", {}) or {}
    if not me.get("harness_dir"):
        logger.info("[swarm.memory_eval_tools] skipped: harness_dir not configured")
        return []
    try:
        tools = MemoryEvalToolkit(
            harness_dir=str(me.get("harness_dir", "")),
            python_exe=str(me.get("python", "")),
            timeout_sec=int(me.get("timeout_sec", 7200)),
        ).get_tools()
        logger.info("[swarm.memory_eval_tools] built %d tools", len(tools))
        return list(tools)
    except Exception as exc:  # noqa: BLE001 - 工具构建失败不阻断团队装配
        logger.warning("[swarm.memory_eval_tools] construction failed: %s", exc)
        return []


__all__ = [
    "DEEPSEARCH_TOOLS",
    "MEMORY_EVAL_TOOLS",
    "build_deepsearch_tools",
    "build_memory_eval_tools",
]
