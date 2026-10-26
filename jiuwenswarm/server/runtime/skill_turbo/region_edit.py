# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""选区/编辑已有 PPT 请求的共享关键词（指南层与运行时守卫的唯一词源）。

指南层（SkillTurboPromptRail 注入的排除提示词）为主防线，运行时守卫
（skill_turbo_tools 的 [REGION-EDIT-BYPASS] 块）作兜底——两层引用同一份
关键词，避免多套口径漏拦。待 pptx-craft 流水线支持"编辑已有 PPT"短路
分支后，随 [REGION-EDIT-BYPASS] 一并删除。
"""

from __future__ import annotations

REGION_EDIT_KEYWORDS: tuple[str, ...] = (
    "PPT选区", "选区原文", "选区类型", "选区位置", "选区容器",
    "选区 class", "选区class", "修改要求", "选区字段", "布局优化",
    "选区优化", "内容优化",
)


def region_edit_keyword_summary() -> str:
    """指南用关键词列表（"/" 连接），与运行时守卫同源。"""
    return "/".join(REGION_EDIT_KEYWORDS)


__all__ = ["REGION_EDIT_KEYWORDS", "region_edit_keyword_summary"]
