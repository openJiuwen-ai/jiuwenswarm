# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Batch B: rule-based research page finalize + citation enrichment."""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.deep_research import (
    PageWorkerNode,
    _ResearchConfig,
    count_named_citations,
    enrich_section_citations,
    source_label_from_url,
)


def _ok_section(page_num: int = 1) -> str:
    return f"""### P{page_num}: 市场规模
> 页面类型：content | 研究级别：L2
**核心论点**：全球市场规模达 100 亿 [Gartner]

#### PPT 内容建议
- **推荐主标题**：市场规模概览
- **主轴**（必填）：
  | 指标 | 2022 | 2023 | 2024 | 来源 |
  | --- | --- | --- | --- | --- |
  | 规模 | 80 | 90 | 100 | Gartner |
  | 增速 | 10% | 12% | 11% | IDC |
  | 份额 | 20% | 22% | 25% | 官方 |
  | 渗透 | 30% | 35% | 40% | 研报 |
- **关键数据**：
  | 数据项 | 数值 | 单位/口径 | 归属对象 | 来源 | 时间 | 数据类型 |
  | --- | --- | --- | --- | --- | --- | --- |
  | 规模 | 100 | 亿 | 全球 | Gartner | 2024 | 规模 |
- **上屏要点**：
  1. 2024 年规模 100 亿 [Gartner]
- **案例**：
  - Acme — 营收增长 30% [官方]

#### 来源留痕
| 名称 | URL | 类别 | 评分 |
| --- | --- | --- | --- |
| Gartner | https://www.gartner.com/a | 行业研报 | A |
"""


def test_count_named_citations_ignores_numeric_and_dedupes():
    text = "结论 [Gartner] 再引 [Gartner] 伪引 [1] 另一源 [IDC]"
    assert count_named_citations(text) == 2


def test_source_label_from_url_strips_www():
    assert source_label_from_url("https://www.Example.com/path") == "example.com"
    assert source_label_from_url("not-a-url") == ""


def test_enrich_section_citations_appends_host_labels():
    section = "### P1: t\n要点不足 [OnlyOne]\n"
    extractions = [
        {"url": "https://www.gartner.com/x", "content": "a"},
        {"url": "https://idc.com/y", "content": "b"},
        {"url": "https://www.gartner.com/z", "content": "dup host"},
        {"url": "", "content": "[数据有限]", "data_limited": True},
    ]
    out = enrich_section_citations(section, extractions, min_citations=3)
    assert count_named_citations(out) >= 3
    assert "gartner.com" in out
    assert "idc.com" in out
    assert "**来源标注**" in out


def test_finalize_page_section_passes_and_enriches():
    worker = PageWorkerNode()
    config = _ResearchConfig(
        search_mode="force_search",
        research_depth="L2",
        topic="AI",
        writer_profile="deep",
        min_citations=3,
    )
    # 故意只有 1 个命名引用，应由 enrich 补足
    section = _ok_section(2).replace("[Gartner]", "", 1).replace("[IDC]", "").replace("[官方]", "").replace("[研报]", "")
    # 保留核心论点里的一个引用后仍可能不足；再清掉上屏/案例引用
    section = section.replace("[Gartner]", "[OnlyOne]", 1)
    for label in ("[Gartner]", "[IDC]", "[官方]", "[研报]"):
        section = section.replace(label, "")
    assert count_named_citations(section) < 3

    page = {"page_number": 2, "page_type": "content"}
    extractions = [
        {"url": "https://www.gartner.com/a", "content": "x"},
        {"url": "https://idc.com/b", "content": "y"},
    ]
    out, ok = worker._finalize_page_section(page, section, extractions, config, 200)
    assert ok
    assert count_named_citations(out) >= 3


def test_finalize_page_section_fails_on_bad_header():
    worker = PageWorkerNode()
    config = _ResearchConfig(
        search_mode="auto",
        research_depth="L2",
        topic="AI",
    )
    section = _ok_section(1)
    page = {"page_number": 9}
    out, ok = worker._finalize_page_section(page, section, [], config, 200)
    assert not ok
    assert out == section
