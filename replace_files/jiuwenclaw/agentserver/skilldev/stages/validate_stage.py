# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""VALIDATE 阶段处理器.

校验 GENERATE 产出的 SKILL.md 是否符合 Skill 规范：
- YAML frontmatter 存在且合法（name, description 必填）
- name 是 kebab-case，≤30 字符
- description ≤1024 加权字符且 ≤300 token（中文计2/0.6，英文计1/0.3）
- 只包含允许的 frontmatter key

校验失败 → 回退 GENERATE 重新生成。
校验成功 → 进入 SKIP_TESTS_CONFIRM（用户选择运行测试或跳过）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.context import SkillDevContext
from jiuwenclaw.agentserver.skilldev.schema import (
    ALLOWED_FRONTMATTER_KEYS,
    MAX_GENERATE_RETRIES,
    SKILL_NAME_MAX_LEN,
    SkillDevEventType,
    SkillDevStage,
)
from jiuwenclaw.agentserver.skilldev.stages.base import StageHandler, StageResult
from jiuwenclaw.agentserver.skilldev.utils.skill_description_fix import (
    normalize_skill_description,
    parse_frontmatter,
)
from jiuwenclaw.agentserver.skilldev.utils.skill_md_validation import (
    DESCRIPTION_MAX_TOKENS,
    DESCRIPTION_MAX_WEIGHTED,
    description_weighted_len,
    estimate_skill_tokens,
)

logger = logging.getLogger(__name__)


class ValidateStageHandler(StageHandler):
    """VALIDATE 阶段：校验 SKILL.md 格式合规性."""

    async def execute(self, ctx: SkillDevContext) -> StageResult:
        skill_md_path = ctx.workspace / "skill" / "SKILL.md"

        if not skill_md_path.exists():
            return await self._handle_failure(
                ctx, "SKILL.md 未生成"
            )

        name, description, _ = parse_skill_frontmatter(skill_md_path)
        if ctx.state.plan is None or not isinstance(ctx.state.plan, dict):
            ctx.state.plan = {}
        if name:
            ctx.state.plan["skill_name"] = name
        if description:
            ctx.state.plan["description"] = description
            
        valid, message = validate_skill_md(skill_md_path)
        await ctx.emit(
            SkillDevEventType.VALIDATE_RESULT, {"valid": valid, "message": message}
        )

        if not valid:
            return await self._handle_failure(ctx, message)

        ctx.state.generate_retries = 0
        ctx.state.last_validate_error = None
        return StageResult(next_stage=SkillDevStage.SKIP_TESTS_CONFIRM)

    async def _handle_failure(
        self, ctx: SkillDevContext, message: str
    ) -> StageResult:
        ctx.state.generate_retries += 1
        ctx.state.last_validate_error = message

        await ctx.emit(
            SkillDevEventType.VALIDATE_RESULT,
            {"valid": False, "message": message},
        )

        if ctx.state.generate_retries >= MAX_GENERATE_RETRIES:
            logger.error(
                "[session=%s] [ValidateStage] 校验失败且已达最大重试次数 (%d): %s",
                ctx.state.task_id,
                MAX_GENERATE_RETRIES,
                message,
            )
            return StageResult(next_stage=SkillDevStage.ERROR)

        logger.warning(
            "[session=%s] [ValidateStage] 校验失败 (第 %d 次): %s，回退到 GENERATE",
            ctx.state.task_id,
            ctx.state.generate_retries,
            message,
        )
        return StageResult(next_stage=SkillDevStage.GENERATE)


# ---------------------------------------------------------------------------
# 校验逻辑（内化自官方 quick_validate.py）
# ---------------------------------------------------------------------------


def validate_skill_md(skill_md_path: Path) -> tuple[bool, str]:
    """校验 SKILL.md 的 YAML frontmatter 格式.

    返回 (is_valid, message)。
    """
    content = skill_md_path.read_text(encoding="utf-8")

    if not content.startswith("---"):
        return False, "SKILL.md 缺少 YAML frontmatter（应以 --- 开头）"

    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "YAML frontmatter 格式无效"

    frontmatter = parse_frontmatter(match.group(1))

    if "name" not in frontmatter:
        return False, "frontmatter 缺少必填字段 'name'"
    if "description" not in frontmatter:
        return False, "frontmatter 缺少必填字段 'description'"

    unexpected = set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS
    if unexpected:
        return False, f"frontmatter 包含未允许的字段: {', '.join(sorted(unexpected))}"

    # name: kebab-case
    name = frontmatter["name"].strip()
    if name and not re.match(r"^[a-z0-9-]+$", name):
        return False, f"name '{name}' 必须是 kebab-case（小写字母、数字、连字符）"
    if name and _has_invalid_hyphen_usage(name):
        return False, f"name '{name}' 不能以连字符开头/结尾或包含连续连字符"
    if name and len(name) > SKILL_NAME_MAX_LEN:
        return False, f"name 过长（{len(name)} 字符，最大 {SKILL_NAME_MAX_LEN}）"

    # description
    desc = normalize_skill_description(frontmatter["description"])
    if not desc:
        return False, "description 不能为空"
    weighted_len = description_weighted_len(desc)
    if weighted_len > DESCRIPTION_MAX_WEIGHTED:
        return False, (
            f"description 过长（加权长度 {weighted_len}，"
            f"最大 {DESCRIPTION_MAX_WEIGHTED}；中文计2、英文计1）"
        )
    desc_tokens = estimate_skill_tokens(desc)
    if desc_tokens > DESCRIPTION_MAX_TOKENS:
        return False, (
            f"description token 超限（约 {desc_tokens}，"
            f"最大 {DESCRIPTION_MAX_TOKENS}；中文 0.6 / 英文 0.3 token/字符）"
        )
    if desc != frontmatter["description"].strip():
        return False, (
            "description 不能以 Markdown 标记（如 >、-、*、#）开头；"
            "请使用纯文本单行描述"
        )

    return True, "SKILL.md 校验通过"


def parse_skill_frontmatter(skill_md_path: Path) -> tuple[str, str, str]:
    """从 SKILL.md 解析出 (name, description, body_content).

    轻量解析器，无 PyYAML 依赖。
    """
    content = skill_md_path.read_text(encoding="utf-8-sig")
    match = re.match(r"^---\n(.*?)\n---\n?(.*)", content, re.DOTALL)
    if not match:
        return "", "", content

    fm = parse_frontmatter(match.group(1))
    return (
        fm.get("name", ""),
        normalize_skill_description(fm.get("description", "")),
        match.group(2),
    )


def _has_invalid_hyphen_usage(name: str) -> bool:
    """校验 name 中连字符使用是否非法."""
    return name.startswith("-") or name.endswith("-") or "--" in name
