# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SKILL.md frontmatter 轻量解析与 description 规范化、自动修正."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_frontmatter(text: str) -> dict[str, str]:
    """极简 YAML frontmatter 解析（key: value 单行 + block scalar）.

    生产环境可替换为 yaml.safe_load（需添加 PyYAML 依赖）。
    """
    result: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in text.split("\n"):
        key_match = re.match(r"^([a-zA-Z_-]+):\s*(.*)", line)
        if key_match:
            if current_key:
                result[current_key] = "\n".join(current_lines).strip()
            current_key = key_match.group(1)
            value = key_match.group(2)
            current_lines = [] if value in ("|", ">") else [value]
        elif current_key:
            current_lines.append(line)

    if current_key:
        result[current_key] = "\n".join(current_lines).strip()

    return result


def normalize_skill_description(text: str) -> str:
    """清洗 description：去除 Markdown/YAML 残留前缀，压缩为单行纯文本。"""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    # 去除误入的 YAML 块标量指示符（如 >-、|-）
    cleaned = re.sub(r"^[>|]\-?\s*", "", cleaned)
    # 循环剥离行首 Markdown 标记（引用 >、列表 -/*、标题 #）
    while True:
        prev = cleaned
        cleaned = re.sub(r"^[>#*•]\s*", "", cleaned)
        cleaned = re.sub(r"^-\s+", "", cleaned)
        cleaned = cleaned.lstrip()
        if cleaned == prev:
            break
    return cleaned


def fix_skill_md_description(skill_md_path: Path) -> bool:
    """修正 SKILL.md frontmatter 中的 description 并写回。返回是否发生修改。"""
    if not skill_md_path.exists():
        return False

    content = skill_md_path.read_text(encoding="utf-8")
    match = re.match(r"^(---\n)(.*?)(\n---\n?)(.*)", content, re.DOTALL)
    if not match:
        return False

    fm_text = match.group(2)
    fm = parse_frontmatter(fm_text)
    if "description" not in fm:
        return False

    normalized = normalize_skill_description(fm["description"])
    if not normalized:
        return False

    needs_content_fix = normalized != fm["description"].strip()
    needs_format_fix = _description_uses_block_scalar(fm_text)
    if not needs_content_fix and not needs_format_fix:
        return False

    new_fm = _replace_description_in_frontmatter(fm_text, normalized)
    new_content = match.group(1) + new_fm + match.group(3) + match.group(4)
    _ = skill_md_path.write_text(new_content, encoding="utf-8")
    logger.info(
        "[skilldev.utils.skill_description_fix] 已修正 SKILL.md description: %r -> %r",
        fm["description"][:80],
        normalized[:80],
    )
    return True


def _description_uses_block_scalar(fm_text: str) -> bool:
    """判断 frontmatter 中 description 是否使用了 YAML 块标量或多行写法。"""
    lines = fm_text.split("\n")
    for i, line in enumerate(lines):
        desc_match = re.match(r"^description:\s*(.*)", line)
        if not desc_match:
            continue
        value = desc_match.group(1).strip()
        if value in ("|", ">", "|-", "|+", ">-", ">+"):
            return True
        if re.match(r"^[>|]\-?", value):
            return True
        if i + 1 < len(lines) and lines[i + 1] and lines[i + 1][0].isspace():
            return True
    return False


def _replace_description_in_frontmatter(fm_text: str, new_desc: str) -> str:
    """将 frontmatter 中的 description 字段替换为单行规范化值。"""
    lines = fm_text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        desc_match = re.match(r"^description:\s*(.*)", line)
        if desc_match:
            result.append(f"description: {new_desc}")
            value = desc_match.group(1).strip()
            i += 1
            if value in ("|", ">", "|-", "|+", ">-", ">+"):
                while i < len(lines):
                    cont = lines[i]
                    if cont and not cont[0].isspace():
                        break
                    i += 1
            continue
        result.append(line)
        i += 1
    return "\n".join(result)
