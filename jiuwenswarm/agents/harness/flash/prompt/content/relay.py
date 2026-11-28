# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash relay 归并域段正文 + 去重纯函数（自 flash_relay_merge_rail 抽离）。

持有 ``request_system_prompt`` 源段名/段号、剥除锚点与 ``_strip_relay_duplicates``
纯函数，供声明式注册表（registry）与 FlashRelayMergeRail 消费。relay TS 层内容
一律在 flash 侧处理，不编辑 relay 源码。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 源段名（RuntimePromptRail 每轮注入 priority 110；FlashRelayMergeRail 无条件重加到 _PRIO_REQUEST）。
_SECTION_REQUEST = "request_system_prompt"
_PRIO_REQUEST = 999  # 静态区末尾号（静态 7xx 之后、动态 1000+ 之前；见 sections.PENDING_ADJUDICATION）

# ── 剥除锚点（relay TS 层硬编码中文，与 agent 语言无关）。─────────────────
_ANCHOR_WORKSPACE_CONSTRAINT = "[文件输出约束]"
_ANCHOR_MEMORY_CORRECTION = "[记忆纠正规则]"

# relay 分区标题形如「## [文件输出约束]」或独立成行的「[文件输出约束]」。
_RELAY_SECTION_HEADER = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+)?(\[[^\]\n]+\])[ \t]*$")


def _find_section_header(text: str, title: str, start: int = 0) -> int | None:
    """返回独占一行的分区标题 ``title`` 的起始偏移；找不到返回 None。

    ``request_system_prompt`` 除 relay 分区外还携带调用方人格、交互提示与 Add-in 上下文，
    正文里可能出现同名标签提及（如人格段写「如需解释 [记忆纠正规则]」）。裸子串 ``find``
    会命中这些提及、抢走块边界，故分区定位一律只认独占一行的标题（可带 ``#`` 前缀）。
    """
    for match in _RELAY_SECTION_HEADER.finditer(text, start):
        if match.group(1) == title:
            return match.start()
    return None


def _strip_relay_duplicates(content: str) -> str | None:
    """剥除 relay 的 [文件输出约束] 块（220 已覆盖），保留 [记忆纠正规则] 及其后内容。

    记忆域已回退 deep 原样（stock 五件套 + relay [记忆纠正规则] 原块），[记忆纠正规则]
    不再剥除、原样保留。返回剥除后的内容；[文件输出约束] 锚点缺失、其后无 [记忆纠正规则]
    收尾、或两锚点之间出现其它 relay 分区标题（上游 TS 层布局变化）时返回 None（触发
    整体回退，不盲删到段尾）。
    """
    if not content:
        return None

    ws_idx = _find_section_header(content, _ANCHOR_WORKSPACE_CONSTRAINT)
    if ws_idx is None:
        return None  # 缺失 [文件输出约束] 分区标题 → 无重复块，回退

    mc_idx = _find_section_header(
        content, _ANCHOR_MEMORY_CORRECTION, ws_idx + len(_ANCHOR_WORKSPACE_CONSTRAINT)
    )
    if mc_idx is None:
        # 无 [记忆纠正规则] 收尾 → 无法确定 [文件输出约束] 块的结束边界，回退（不盲删到段尾）。
        logger.warning(
            "[FlashRelayMerge] %s 后无 %s 收尾，无法确定块边界，回退保留原段",
            _ANCHOR_WORKSPACE_CONSTRAINT,
            _ANCHOR_MEMORY_CORRECTION,
        )
        return None

    # 精确删除：[文件输出约束] 与 [记忆纠正规则] 之间只允许 [文件输出约束] 这一个分区标题；
    # 若出现其它 relay 分区标题，说明 TS 层布局已变化，回退避免静默删除无关内容。
    span = content[ws_idx:mc_idx]
    for match in _RELAY_SECTION_HEADER.finditer(span):
        if match.group(1) != _ANCHOR_WORKSPACE_CONSTRAINT:
            logger.warning(
                "[FlashRelayMerge] %s 与 %s 之间出现其它分区标题 %s，回退保留原段",
                _ANCHOR_WORKSPACE_CONSTRAINT,
                _ANCHOR_MEMORY_CORRECTION,
                match.group(1),
            )
            return None

    return (content[:ws_idx] + content[mc_idx:]).rstrip()


# ── relay [记忆纠正规则] 块：第 6 条删除 + 旧工具名改写 ──────────────────────
# 两处改写都限定在该块内：request_system_prompt 还含 [Personality Override]（调用方人格）、
# 交互提示与 Add-in 上下文，对整段做字符串替换会误伤其中的同名文本（如 read_memory.md）。
# 第 6 条按「行首编号 + 关键短语」定位，编号漂移也能命中。
_MEMORY_RULE6_MARKER = "移入回收站即为合规的软删除"
_MEMORY_RULE6_LINE = re.compile(r"^[ \t]*\d+[.、][ \t]*")


def _memory_block_span(text: str) -> tuple[int, int] | None:
    """返回 ``[记忆纠正规则]`` 块的 ``(start, end)``；锚点缺失返回 None。

    ``start`` 只认 ``[记忆纠正规则]`` 独占一行的标题，正文里的同名提及不抢边界；
    ``end`` 取块后第一个 relay 分区标题（``[xxx]`` 独占一行）或段尾。
    """
    start = _find_section_header(text, _ANCHOR_MEMORY_CORRECTION)
    if start is None:
        return None
    end = len(text)
    for match in _RELAY_SECTION_HEADER.finditer(
        text, start + len(_ANCHOR_MEMORY_CORRECTION)
    ):
        end = match.start()
        break
    return start, end


def _strip_memory_rule6(text: str) -> tuple[str, bool]:
    """删 ``[记忆纠正规则]`` 的删除=回收站一条，返回 ``(new_text, hit)``。"""
    span = _memory_block_span(text)
    if span is None:
        return text, False
    start, end = span
    lines = text[start:end].split("\n")
    for index, line in enumerate(lines):
        if _MEMORY_RULE6_MARKER in line and _MEMORY_RULE6_LINE.match(line):
            del lines[index]
            return text[:start] + "\n".join(lines) + text[end:], True
    return text, False


def _rewrite_memory_tool_names(text: str, replacements) -> tuple[str, bool]:
    """改写 ``[记忆纠正规则]`` 块内的旧记忆工具名，返回 ``(new_text, hit)``。

    映射表由调用方注入（取 ``memory_names.MEMORY_NAME_REPLACEMENTS``，与
    FlashMemoryRail 共用同一份）；best-effort：锚点缺失或没有旧名即原样返回。
    """
    span = _memory_block_span(text)
    if span is None:
        return text, False
    start, end = span
    block = text[start:end]
    hit = False
    for old, new in replacements or ():
        if old in block:
            block = block.replace(old, new)
            hit = True
    if not hit:
        return text, False
    return text[:start] + block + text[end:], True
