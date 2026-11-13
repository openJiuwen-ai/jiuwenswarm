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


def _strip_relay_duplicates(content: str) -> str | None:
    """剥除 relay 的 [文件输出约束] 块（220 已覆盖），保留 [记忆纠正规则] 及其后内容。

    记忆域已回退 deep 原样（stock 五件套 + relay [记忆纠正规则] 原块），[记忆纠正规则]
    不再剥除、原样保留。返回剥除后的内容；[文件输出约束] 锚点缺失、其后无 [记忆纠正规则]
    收尾、或两锚点之间出现其它 relay 分区标题（上游 TS 层布局变化）时返回 None（触发
    整体回退，不盲删到段尾）。
    """
    if not content:
        return None

    ws_idx = content.find(_ANCHOR_WORKSPACE_CONSTRAINT)
    if ws_idx < 0:
        return None  # 缺失 [文件输出约束] → 无重复块，回退

    mc_idx = content.find(_ANCHOR_MEMORY_CORRECTION, ws_idx + len(_ANCHOR_WORKSPACE_CONSTRAINT))
    if mc_idx < 0:
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
