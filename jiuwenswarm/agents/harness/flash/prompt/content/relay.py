# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash relay 归并域段正文 + 去重纯函数（自 flash_relay_merge_rail 抽离）。

持有 ``request_system_prompt`` 源段名/段号、剥除锚点与 ``_strip_relay_duplicates``
纯函数；``FlashRelayMergeRail`` 从本模块 re-import，零行为变化。relay TS 层内容
一律在 flash 侧处理，不编辑 relay 源码。
"""

from __future__ import annotations

# 源段名（RuntimePromptRail 每轮注入 priority 110；FlashRelayMergeRail 无条件重加到 _PRIO_REQUEST）。
_SECTION_REQUEST = "request_system_prompt"
_PRIO_REQUEST = 999  # 静态区末尾号（静态 7xx 之后、动态 1000+ 之前；见 sections.PENDING_ADJUDICATION）

# ── 剥除锚点（relay TS 层硬编码中文，与 agent 语言无关）。─────────────────
_ANCHOR_WORKSPACE_CONSTRAINT = "[文件输出约束]"
_ANCHOR_MEMORY_CORRECTION = "[记忆纠正规则]"


def _strip_relay_duplicates(content: str) -> str | None:
    """剥除 relay 末尾的 [文件输出约束] 块（220 已覆盖），保留 [记忆纠正规则]。

    记忆域已回退 deep 原样（stock 五件套 + relay [记忆纠正规则] 原块），640 归并撤销，
    故 [记忆纠正规则] 不再剥除、原样保留。返回剥除后的内容；[文件输出约束] 锚点缺失
    返回 None（触发整体回退）。[文件输出约束] 恒位于 [记忆纠正规则] 之前，若其后还有
    [记忆纠正规则]，只剥中间的 [文件输出约束] 块、保留后者及其后内容。
    """
    if not content:
        return None

    ws_idx = content.find(_ANCHOR_WORKSPACE_CONSTRAINT)
    if ws_idx < 0:
        return None  # 缺失 [文件输出约束] → 无重复块，回退

    mc_idx = content.find(_ANCHOR_MEMORY_CORRECTION, ws_idx)
    if mc_idx < 0:
        # 无 [记忆纠正规则] → [文件输出约束] 直到段尾，整块剥除。
        return content[:ws_idx].rstrip()

    # 保留 [记忆纠正规则]（及其后内容），只剥中间的 [文件输出约束] 块。
    return (content[:ws_idx] + content[mc_idx:]).rstrip()
