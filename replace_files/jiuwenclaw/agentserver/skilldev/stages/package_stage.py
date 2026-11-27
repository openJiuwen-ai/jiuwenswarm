# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PACKAGE 阶段处理器.

- 将 skill/ 目录打包为 {skill_name}.skill（zip 格式，与官方 .skill 格式一致）
- 排除 evals/（根目录级）、__pycache__、node_modules、.DS_Store、*.pyc 等
- 推送 ARTIFACT_READY 事件 → 进入 COMPLETED（描述优化已在打包前完成）
"""

from __future__ import annotations

import logging
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.common_utils import repack_skill_dir
from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import SkillDevEventType, SkillDevStage
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)


class PackageStageHandler(StageHandler):
    """PACKAGE 阶段：打包 skill/ 为 .skill (zip) 文件."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        skill_dir = ctx.workspace / "skill"
        output_dir = ctx.workspace / "output"

        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "正在打包..."}
        )

        skill_path, _ = repack_skill_dir(skill_dir, output_dir, ctx.state.task_id)

        ctx.state.zip_path = str(skill_path)
        ctx.state.zip_size = skill_path.stat().st_size

        await ctx.emit(
            SkillDevEventType.ARTIFACT_READY,
            {
                "artifact": {
                    "id": "skill_package",
                    "name": skill_path.name,
                    "type": "skill_package",
                    "size_bytes": ctx.state.zip_size,
                    "browsable": True,
                    "downloadable": True,
                },
            },
        )
        return StageResult(next_stage=SkillDevStage.COMPLETED)
