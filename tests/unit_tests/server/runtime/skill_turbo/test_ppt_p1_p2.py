# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for P1/P2 research precheck, checklist convergence, role boundary."""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import deep_research as dr
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import (
    pipeline_role_boundary,
    research_evidence_limited_mentioned,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    _DENSITY_CHECKLIST_DIGEST,
)


def _config(**kwargs) -> dr._ResearchConfig:
    return dr._ResearchConfig(
        search_mode=kwargs.get("search_mode", "auto"),
        research_depth=kwargs.get("research_depth", "L2"),
        topic=kwargs.get("topic", "测试主题"),
    )


def test_measure_axis_table_counts_rows_and_cols():
    section = """
### P3: 测试
#### PPT 内容建议
- **主轴**（必填）：
  | 维度 | A | B | C | 来源 |
  | --- | --- | --- | --- | --- |
  | r1 | 1 | 2 | 3 | x |
  | r2 | 1 | 2 | 3 | x |
  | r3 | 1 | 2 | 3 | x |
  | r4 | 1 | 2 | 3 | x |
- **关键数据**：
  | 数据项 | 数值 | 单位/口径 | 归属对象 | 来源 | 时间 | 数据类型 |
  | --- | --- | --- | --- | --- | --- | --- |
"""
    rows, cols = dr._measure_axis_table(section)
    assert rows == 4
    assert cols == 3


def test_precheck_flags_axis_table_too_small():
    section = """
### P3: 测试
#### PPT 内容建议
- **主轴**：
  | 维度 | A | B | 来源 |
  | --- | --- | --- | --- |
  | r1 | 1 | 2 | x |
  | r2 | 1 | 2 | x |
  | r3 | 1 | 2 | x |
  | r4 | 1 | 2 | x |
  | r5 | 1 | 2 | x |
- **关键数据**：x
- **上屏要点**：1. a
"""
    issues = dr._precheck_research_section(section, _config())
    assert any(i.startswith("axis_table_too_small:5x2<4x3") for i in issues)


def test_precheck_uses_limited_quota_when_annotated():
    section = """
### P3: 测试
> 数据有限，基于用户素材整理。
#### PPT 内容建议
- **主轴**：
  | 维度 | A | B | 来源 |
  | --- | --- | --- | --- |
  | r1 | 1 | 2 | x |
  | r2 | 1 | 2 | x |
  | r3 | 1 | 2 | x |
- **关键数据**：x
- **上屏要点**：1. a
"""
    assert research_evidence_limited_mentioned(section)
    issues = dr._precheck_research_section(section, _config(search_mode="no_search"))
    assert not any(i.startswith("axis_table_too_small") for i in issues)


def test_validate_reason_rewrite_hints_axis_table():
    hints = dr._validate_reason_rewrite_hints(["axis_table_too_small:5x2<4x3"])
    assert "5×2" in hints
    assert "≥4" in hints


def test_density_checklist_no_turbo_private_rules():
    assert "17 项" not in _DENSITY_CHECKLIST_DIGEST
    assert "恰好 2 个" not in _DENSITY_CHECKLIST_DIGEST
    assert "禁止连续两页" not in _DENSITY_CHECKLIST_DIGEST
    assert "check-layout CLI" in _DENSITY_CHECKLIST_DIGEST


def test_pipeline_role_boundary_includes_stage():
    text = pipeline_role_boundary("P6")
    assert "P6" in text
    assert "user_dimensions" in text
    assert "信息不足不阻塞" in text
