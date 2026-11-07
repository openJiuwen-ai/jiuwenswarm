# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent 失败归因诊断包。

openjiuwen 框架仅把整条轨迹丢给 LLM 自行归因，缺少步级规范化视图与
可对照的归因基线。本包将 Trajectory 转为有序 StepView，并提供 last_step /
random / LLM 整轨 / oracle / 程序化局部验证等多种归因器，以及强制
builder-first 落盘的 FailureAttributionRail。
"""

from jiuwenswarm.agents.harness.common.diagnosis.attribution import (
    AttributionResult,
    LastStepAttributor,
    LLMTrajectoryAttributor,
    OracleAttributor,
    RandomAttributor,
    StepAttributor,
)
from jiuwenswarm.agents.harness.common.diagnosis.attribution_rail import (
    FailureAttributionRail,
)
from jiuwenswarm.agents.harness.common.diagnosis.programmatic import (
    ProgrammaticAttributor,
    make_programmatic_attributors,
)
from jiuwenswarm.agents.harness.common.diagnosis.trajectory_view import (
    StepView,
    build_step_view,
    flatten_tool_calls,
    render_steps,
)

__all__ = [
    "StepView",
    "build_step_view",
    "flatten_tool_calls",
    "render_steps",
    "AttributionResult",
    "StepAttributor",
    "LastStepAttributor",
    "RandomAttributor",
    "LLMTrajectoryAttributor",
    "OracleAttributor",
    "ProgrammaticAttributor",
    "make_programmatic_attributors",
    "FailureAttributionRail",
]
