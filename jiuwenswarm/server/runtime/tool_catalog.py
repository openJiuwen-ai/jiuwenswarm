# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent 工具目录（内部）：简短描述（面向人/UI）与 ToolCard.description（面向模型）分离。

供 AgentServer、权限编排等模块直接调用，不对外暴露独立 RPC。
"""

from __future__ import annotations

import logging
import re
from importlib import import_module
from typing import Any, Iterable

from openjiuwen.core.foundation.tool import ToolCard

logger = logging.getLogger(__name__)

_SHORT_DESCRIPTION_MAX_LEN = 100
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_FALLBACK_UNKNOWN_TEMPLATE = "工具「{name}」（暂无简短说明）"

__all__ = [
    "collect_tools_catalog_from_claws",
    "collect_tools_catalog_from_swarms",
    "get_registered_tools_catalog",
    "get_stable_tools_catalog",
    "is_placeholder_short_description",
    "merge_tools_catalog_entries",
    "resolve_short_description",
    "short_description_from_description",
    "tool_catalog_entry_from_card",
    "ui_list_short_description",
]


def _metadata_entries_to_catalog(entries: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue
        description = str(item.get("description", "") or "")
        catalog.append(
            {
                "name": name,
                "description": description,
                "short_description": resolve_short_description(name, description),
            }
        )
    return catalog


def _list_upstream_tool_metadata(module_name: str, language: str) -> list[dict[str, str]]:
    module = import_module(module_name)
    list_metadata = getattr(module, "list_tool_metadata", None)
    if callable(list_metadata):
        return list_metadata(language)
    registry = getattr(module, "_REGISTRY", None)
    if not isinstance(registry, dict):
        raise RuntimeError(f"{module_name} does not expose tool metadata")
    return [
        {"name": name, "description": registry[name].get_description(language)}
        for name in sorted(registry)
    ]


def get_stable_tools_catalog(language: str = "cn") -> dict[str, dict[str, str]]:
    """Return built-in and Agent Team metadata without creating a runtime."""
    normalized_language = "en" if str(language).strip().lower().startswith("en") else "cn"
    catalogs: list[list[dict[str, str]]] = []
    try:
        entries = _list_upstream_tool_metadata(
            "openjiuwen.harness.prompts.tools",
            normalized_language,
        )
        catalogs.append(_metadata_entries_to_catalog(entries))
    except Exception:
        logger.exception("[tool_catalog] failed to load built-in tool metadata")

    try:
        from openjiuwen.agent_teams.tools.locales import make_translator
        from openjiuwen.agent_teams.tools.tool_permissions import (
            HUMAN_AGENT_TOOLS,
            LEADER_TOOLS,
            MEMBER_TOOLS_BY_DISPATCH,
        )

        team_names = set(LEADER_TOOLS) | set(HUMAN_AGENT_TOOLS)
        for member_tools in MEMBER_TOOLS_BY_DISPATCH.values():
            team_names.update(member_tools)
        translator = make_translator(normalized_language)
        catalogs.append(
            _metadata_entries_to_catalog(
                [
                    {"name": name, "description": str(translator(name))}
                    for name in sorted(team_names)
                ]
            )
        )
    except Exception:
        logger.exception("[tool_catalog] failed to load Agent Team tool metadata")

    return merge_tools_catalog_entries(catalogs)


def _is_sentence_terminal(index: int, char: str, text: str) -> bool:
    """判断 index 处字符是否为句末标点（排除 schema 可选标记如 description?、id?）。"""
    if char in "。！？":
        return True
    if char in "!?":
        if index > 0 and text[index - 1].isalnum():
            if index + 1 < len(text) and text[index + 1] in ",;)]}":
                return False
        return True
    if char != ".":
        return False
    if index + 1 >= len(text):
        return True
    nxt = text[index + 1]
    if nxt in " \t":
        return True
    # window.Provide / offloaded.The — 句号后紧跟大写新句（无空格）也视为句末；chat.delta 等仍不切分
    if nxt.isupper():
        return True
    return False


def _is_meaningful_english_sentence(text: str) -> bool:
    """过滤 JSON/schema 碎片（如 ", id?"），仅保留像自然语言的英文句。"""
    normalized = str(text or "").strip()
    if not normalized or _has_cjk(normalized):
        return False
    if not re.search(r"[A-Za-z]{2,}", normalized):
        return False
    if len(normalized) < 8:
        return False
    return bool(re.search(r"[A-Za-z]{2,}\s+[A-Za-z]", normalized))


def _split_sentences(text: str) -> list[str]:
    """按句末标点切分（支持中英文）。"""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    sentences: list[str] = []
    start = 0
    for index, char in enumerate(normalized):
        if not _is_sentence_terminal(index, char, normalized):
            continue
        part = normalized[start:index + 1].strip()
        if part:
            sentences.append(part)
        start = index + 1
        while start < len(normalized) and normalized[start] in " \t":
            start += 1
    tail = normalized[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def short_description_from_description(description: str) -> str:
    """从 ToolCard.description 提取 short_description：中英文各取第一句，再截断至 100 字。"""
    desc = str(description or "").strip()
    if not desc:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n+", desc) if part.strip()]
    if not paragraphs:
        return ""

    zh_sentence: str | None = None
    en_sentence: str | None = None
    for paragraph in paragraphs:
        for sentence in _split_sentences(paragraph):
            if _has_cjk(sentence):
                if zh_sentence is None:
                    zh_sentence = sentence
            elif en_sentence is None and _is_meaningful_english_sentence(sentence):
                en_sentence = sentence
        if zh_sentence and en_sentence:
            break

    sentences = [part for part in (zh_sentence, en_sentence) if part]
    if not sentences:
        first = _split_sentences(paragraphs[0])
        sentences = [first[0]] if first else [paragraphs[0]]

    joined = " ".join(sentences)
    return _truncate_short_description(joined)


def _truncate_short_description(text: str, max_len: int = _SHORT_DESCRIPTION_MAX_LEN) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_len:
        return normalized
    return f"{normalized[: max_len - 3].rstrip()}..."


def resolve_short_description(tool_name: str, model_description: str = "") -> str:
    name = str(tool_name or "").strip()
    extracted = short_description_from_description(model_description)
    if extracted:
        return extracted
    if name:
        return _truncate_short_description(_FALLBACK_UNKNOWN_TEMPLATE.format(name=name))
    return _truncate_short_description("未知工具。")


def tool_catalog_entry_from_card(card: ToolCard) -> dict[str, str]:
    name = str(getattr(card, "name", "") or "").strip()
    description = str(getattr(card, "description", "") or "")
    short = resolve_short_description(name, description)
    entry: dict[str, str] = {
        "name": name,
        "description": description,
        "short_description": short,
    }
    tool_id = str(getattr(card, "id", "") or "").strip()
    if tool_id:
        entry["id"] = tool_id
    return entry


def get_registered_tools_catalog(ability_manager: Any) -> list[dict[str, str]]:
    """枚举 ability_manager 中已注册工具（name / description / short_description）。"""
    if ability_manager is None:
        return []
    list_fn = getattr(ability_manager, "list", None)
    if not callable(list_fn):
        return []
    by_name: dict[str, dict[str, str]] = {}
    for item in list_fn() or []:
        if not isinstance(item, ToolCard):
            continue
        entry = tool_catalog_entry_from_card(item)
        name = entry.get("name", "")
        if name:
            by_name[name] = entry
    return [by_name[k] for k in sorted(by_name)]


def is_placeholder_short_description(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return True
    if normalized in {"未知工具。", "未知工具"}:
        return True
    return "暂无简短说明" in normalized


def ui_list_short_description(
    tool_name: str,
    *,
    description: str = "",
    short_description: str = "",
) -> str:
    short = str(short_description or "").strip()
    if short and not is_placeholder_short_description(short):
        return short
    return short_description_from_description(description)


def _catalog_entry_richness(entry: dict[str, str]) -> int:
    description = str(entry.get("description", "") or "")
    short = str(entry.get("short_description", "") or "")
    score = len(description)
    if short and not is_placeholder_short_description(short):
        score += 1000 + len(short)
    return score


def merge_tools_catalog_entries(
    catalogs: Iterable[Iterable[dict[str, str]]],
) -> dict[str, dict[str, str]]:
    """Merge tool catalogs by name, keeping the richest description."""
    by_name: dict[str, dict[str, str]] = {}
    for catalog in catalogs:
        for entry in catalog or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "") or "").strip()
            if not name:
                continue
            normalized = {
                str(key): str(value)
                for key, value in entry.items()
                if value is not None
            }
            normalized["name"] = name
            existing = by_name.get(name)
            if (
                existing is None
                or _catalog_entry_richness(normalized)
                > _catalog_entry_richness(existing)
            ):
                by_name[name] = normalized
    return by_name


def collect_tools_catalog_from_claws(claws: Iterable[Any]) -> dict[str, dict[str, str]]:
    """Union registered tools from initialized agent runtime wrappers."""
    catalogs: list[list[dict[str, str]]] = []
    for claw in claws or []:
        if claw is None:
            continue
        list_fn = getattr(claw, "get_registered_tools_catalog", None)
        if not callable(list_fn):
            continue
        try:
            entries = list_fn()
        except Exception:
            logger.exception("[tool_catalog] get_registered_tools_catalog failed")
            continue
        if entries:
            catalogs.append(entries)
    return merge_tools_catalog_entries(catalogs)


def collect_tools_catalog_from_swarms(
    swarms: Iterable[Any],
) -> dict[str, dict[str, str]]:
    """Backward-compatible Team runtime catalog collector."""
    return collect_tools_catalog_from_claws(swarms)
