# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent 驱动流程的 Skill 命名（仅新任务首次用户输入时生成）."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.llm.schema.message import SystemMessage, UserMessage

from jiuwenclaw.agentserver.skilldev.common_utils import strip_agent_output_noise
from jiuwenclaw.agentserver.skilldev.stages.validate_stage import parse_skill_frontmatter

logger = logging.getLogger(__name__)

_MAX_SKILL_NAME_ATTEMPTS = 2
_SKILL_NAME_MAX_PLAUSIBLE_LEN = 48
_SKILL_NAME_MAX_SEGMENTS = 8
_SKILL_NAME_MAX_RAW_LEN = 200
_SKILL_NAME_KEBAB_RE = re.compile(r"^(?:[a-z0-9]+(?:-[a-z0-9]+)*)$")

_SKILL_NAME_SYSTEM_PROMPT = """你是命名助手。请基于用户需求生成一个简短的 skill 标识名。

要求：
1) 只输出 skill 名称本身，不要思考，不要解释。
2) 使用 kebab-case，仅允许小写字母、数字、短横线。
3) 语义清晰，长度建议 2-4 个词，总长度不超过30字符。
4) skill 名称中不要出现`skill`字样，只输出技能名称本身。
5) 若用户已在需求中给出合法的 kebab-case 标识符（如「名字叫 xxx」等形式），必须原样采用该标识符，不得改写、翻译或按语义重新命名。
"""


def is_first_task_input(workspace: Path) -> bool:
    """是否为新任务首次输入（工作区尚未经 _init_workspace_dirs 初始化）."""
    if not workspace.exists():
        return True
    return not (workspace / "skill").is_dir()


def _skill_name_from_skill_md(workspace: Path) -> str | None:
    skill_md = workspace / "skill" / "SKILL.md"
    if not skill_md.is_file():
        return None
    name, _, _ = parse_skill_frontmatter(skill_md)
    name = (name or "").strip()
    return name or None


def _to_ascii_skill_kebab(s: str) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"^```[a-z]*\s*", "", t)
    t = re.sub(r"```\s*$", "", t).strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _SKILL_NAME_KEBAB_RE.match(ln) and len(ln) <= 64:
            return ln
    t = re.sub(r"[^a-z0-9]+", "-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t[:64].strip("-")


def normalize_skill_name(candidate: str, *, fallback_text: str = "") -> tuple[str, bool]:
    """清洗为 kebab-case；返回 (名称, 是否需要重试)."""
    cleaned = strip_agent_output_noise(candidate or "")
    needs_retry = False
    if len(cleaned) > _SKILL_NAME_MAX_RAW_LEN:
        needs_retry = True
    if re.search(
        r"<redacted_thinking|</?redacted_thinking>|<tool_call|</tool_call>",
        cleaned,
        re.I,
    ):
        needs_retry = True

    text = _to_ascii_skill_kebab(cleaned)
    if not text and fallback_text:
        text = _to_ascii_skill_kebab(fallback_text)
    if not text and fallback_text:
        text = "generated-skill"
    if not text:
        return ("", True)

    if len(text) > _SKILL_NAME_MAX_PLAUSIBLE_LEN:
        needs_retry = True
    if len(text.split("-")) > _SKILL_NAME_MAX_SEGMENTS:
        needs_retry = True
    if not _SKILL_NAME_KEBAB_RE.match(text):
        needs_retry = True
    return (text, needs_retry)


def _build_skill_name_user_query(*, user_query: str, attempt: int = 1) -> str:
    parts = [
        "请生成一个准确的 skill_name。",
        f"用户需求：{user_query}",
        "若用户已在需求中给出合法的 kebab-case 标识符，必须原样使用，不得改写。",
        "否则请根据用户需求语义生成 skill name。",
    ]
    if attempt > 1:
        parts.append(
            "本次只输出单独一行 kebab-case 标识，不要前缀、不要解释、不要换行列举多个方案。",
        )
    parts.append("只输出最终 skill_name，不要思考，不要解释，不要输出任何其他内容。")
    return "\n".join(parts)


def _extract_message_content(response: object) -> str:
    content = getattr(response, "content", None)
    if content is not None:
        return str(content)
    return str(response)


async def generate_skill_name_with_model(
    model: Model,
    *,
    user_query: str,
    task_id: str = "",
) -> str:
    """使用单次 LLM 调用生成 kebab-case skill 名称."""
    for attempt in range(1, _MAX_SKILL_NAME_ATTEMPTS + 1):
        user_prompt = _build_skill_name_user_query(user_query=user_query, attempt=attempt)
        try:
            chunks: list[str] = []
            async for chunk in model.stream(
                messages=[
                    SystemMessage(content=_SKILL_NAME_SYSTEM_PROMPT),
                    UserMessage(content=user_prompt),
                ],
            ):
                if chunk.content:
                    chunks.append(chunk.content)
            raw_name = "".join(chunks)
            logger.info(
                "[session=%s] [skill_naming] 生成 skill_name (尝试 %d/%d): %s",
                task_id,
                attempt,
                _MAX_SKILL_NAME_ATTEMPTS,
                raw_name,
            )
        except Exception as exc:
            logger.warning(
                "[session=%s] [skill_naming] 生成 skill_name 失败 (尝试 %d/%d): %s",
                task_id,
                attempt,
                _MAX_SKILL_NAME_ATTEMPTS,
                exc,
            )
            if attempt < _MAX_SKILL_NAME_ATTEMPTS:
                continue
            name, _ = normalize_skill_name("", fallback_text=user_query)
            return name

        name, need_retry = normalize_skill_name(raw_name, fallback_text="")
        if not need_retry and name:
            return name
        if attempt < _MAX_SKILL_NAME_ATTEMPTS:
            continue

    return "generated-skill"


async def resolve_skill_name_for_first_input(
    model: Model,
    *,
    workspace: Path,
    user_query: str,
    task_id: str = "",
) -> str | None:
    """仅在新任务首次输入时生成 skill_name；续聊返回 None（不写盘、不建目录）."""
    if not is_first_task_input(workspace):
        logger.info("[session=%s] [skill_naming] 非首次输入，跳过 skill_name 生成", task_id)
        return None

    from_md = _skill_name_from_skill_md(workspace)
    if from_md:
        normalized, need_retry = normalize_skill_name(from_md, fallback_text="")
        skill_name = from_md if not need_retry and normalized else normalized or from_md
        logger.info("[session=%s] [skill_naming] 从 SKILL.md 解析 skill_name: %s", task_id, skill_name)
        return skill_name

    query = (user_query or "").strip()
    if not query:
        return "generated-skill"

    skill_name = await generate_skill_name_with_model(
        model,
        user_query=query,
        task_id=task_id,
    )
    logger.info("[session=%s] [skill_naming] 首次输入生成 skill_name: %s", task_id, skill_name)
    return skill_name
