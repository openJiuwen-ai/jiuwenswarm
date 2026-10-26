# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""INIT 阶段处理器.

职责：
1. 创建工作区目录（resources/ skill/ evals/ output/）
2. 解析资源包（base64 → 文件 → 提取文本）
3. 解析已有 skill zip（修改/升级场景）
4. 判断任务模式（CREATE / CREATE_WITH_RESOURCES / MODIFY）
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from jiuwenswarm.server.runtime.skill.skilldev.context import SkillDevContext
from jiuwenswarm.server.runtime.skill.skilldev.schema import (
    SkillDevEventType,
    SkillDevStage,
    SkillDevTaskMode,
    determine_task_mode,
)
from jiuwenswarm.server.runtime.skill.skilldev.stages.base import StageHandler, StageResult

logger = logging.getLogger(__name__)


class InitStageHandler(StageHandler):
    """INIT 阶段：解析请求参数，准备工作区."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        await ctx.emit(SkillDevEventType.PROGRESS, {"message": "正在初始化工作区..."})

        # 判断任务模式
        ctx.state.mode = determine_task_mode(ctx.state.input)
        logger.info("[InitStage] task_id=%s mode=%s", ctx.task_id, ctx.state.mode.value)

        # 工作区已由 Pipeline 的 ensure_local 创建，此处直接使用
        resources_dir = ctx.workspace / "resources"
        skill_dir = ctx.workspace / "skill"

        # 解析上传的资源文件
        resources = ctx.state.input.get("resources", [])
        if resources:
            await ctx.emit(
                SkillDevEventType.PROGRESS,
                {"message": f"正在解析 {len(resources)} 个资源文件..."},
            )
            ctx.state.reference_texts = await self._extract_resources(
                resources, resources_dir
            )

        # 解析已有 skill 包（修改/升级场景）
        existing_skill = ctx.state.input.get("existing_skill")
        if existing_skill:
            await ctx.emit(
                SkillDevEventType.PROGRESS, {"message": "正在解析已有 Skill 包..."}
            )
            ctx.state.existing_skill_md = await self._extract_existing_skill(
                existing_skill, skill_dir
            )

        await ctx.emit(
            SkillDevEventType.PROGRESS, {"message": "初始化完成，准备生成开发计划"}
        )
        return StageResult(next_stage=SkillDevStage.PLAN)

    async def _extract_resources(
        self, resources: list[dict], dest_dir: Path
    ) -> list[str]:
        """解析资源文件列表，提取纯文本内容.

        支持格式：.zip（解压）/ .docx（python-docx）/ .pdf（pdfplumber）/ .txt / .md

        待实现: 实现各格式的文本提取逻辑
        """
        texts: list[str] = []
        for res in resources:
            name = res.get("name", "unknown")
            content_b64 = res.get("content_base64", "")
            try:
                raw = base64.b64decode(content_b64)
                file_path = dest_dir / name
                file_path.write_bytes(raw)
                # 待实现: 根据后缀分发到对应解析器（docx/pdf/txt/md/zip）
                text = self._parse_file_to_text(file_path)
                if text:
                    texts.append(text)
            except Exception as exc:
                logger.warning(
                    "[InitStage] 资源文件解析失败: name=%s error=%s", name, exc
                )
        return texts

    def _parse_file_to_text(self, file_path: Path) -> str:
        """将文件解析为纯文本.

        待实现: 实现各格式的解析逻辑：
            - .docx → python-docx
            - .pdf  → pdfplumber
            - .txt / .md → 直接读取
            - .zip  → 解压后递归处理
        """
        suffix = file_path.suffix.lower()
        if suffix in (".txt", ".md"):
            return file_path.read_text(encoding="utf-8", errors="ignore")
        # 待实现: 其他格式
        logger.warning("[InitStage] 暂不支持的文件格式: %s", suffix)
        return ""

    async def _extract_existing_skill(
        self, existing_skill: dict, dest_dir: Path
    ) -> str | None:
        """解压已有 skill.zip，提取 SKILL.md 内容.

        待实现: 实现 zip 解压逻辑
        """
        # 待实现:
        # import zipfile, io
        # raw = base64.b64decode(existing_skill["content_base64"])
        # with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        #     zf.extractall(dest_dir)
        # skill_md = dest_dir / "SKILL.md"
        # return skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
        logger.warning("[InitStage] _extract_existing_skill 尚未实现")
        return None
