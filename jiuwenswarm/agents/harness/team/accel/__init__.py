# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team delivery acceleration: cost discipline and fast file tools."""

from jiuwenswarm.agents.harness.team.accel.accel_prompt_rail import AccelPromptRail
from jiuwenswarm.agents.harness.team.accel.accel_sys_operation_rail import (
    AccelSysOperationRail,
)
from jiuwenswarm.agents.harness.team.accel.worktree_sys_rail_patch import (
    apply_team_accel_sys_rail_patch,
)

# Widen AgentConfigurator's worktree guard so swarm.team_accel_sys is not
# followed by a duplicate core.sys_operation that overwrites FlashEdit.
apply_team_accel_sys_rail_patch()

__all__ = ["AccelPromptRail", "AccelSysOperationRail", "apply_team_accel_sys_rail_patch"]
