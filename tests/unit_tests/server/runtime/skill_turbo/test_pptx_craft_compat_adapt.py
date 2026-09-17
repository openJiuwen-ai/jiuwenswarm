# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo × 现行 pptx-craft 普通分支兼容适配回归（内嵌 fixture，无本机绝对路径）。"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    PageGenContext,
    PageWorkerNode,
    _build_page_prompt,
    _build_content_template_fill_prompt,
    _build_content_template_fill_system_prompt,
    _extract_chart_scaffold_region,
    _extract_designer_section,
    _filled_chart_scaffold_is_progressed,
    _is_chart_candidate_page,
    _merge_chart_scaffold_from_filled,
    _repair_content_template_chrome,
    _uses_content_template_fill,
    _uses_structural_template_fill,
    _validate_custom_content_template_fill_output,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import skill_turbo

# 模拟现行 designer.md 关键结构：预算为加粗正文；关键原则后接「禁止事项」（无质量控制清单）。
_DESIGNER_MD_FIXTURE = """
## 角色定位

无关前言。

**E. 页面内容预算契约（生成 HTML 前必须完成）**

预算片段 MARKER_BUDGET_CONTRACT

#### 3.2 版面计算

版面计算片段仍属预算段。

### 阶段 4：交付

交付片段不应进入预算抽取 MARKER_STAGE4_LEAK

## 弹性布局模式

弹性布局片段 MARKER_FLEX

## HTML 代码规范

代码规范。

## 页面布局规范

布局规范片段 MARKER_LAYOUT

## 视觉设计规范

视觉规范片段 MARKER_VISUAL

## 图表与数据可视化

图表规范。

### 激活 content-template 内的图表骨架（强制）

激活步骤 MARKER_ACTIVATION_STEPS

### custom 模式的图表骨架

custom scaffold。

## 图片使用规范

图片规范。

## 关键原则

### 防溢出核心策略

关键原则片段 MARKER_CRITICAL

### 防空白核心策略

防空白说明。

## 禁止事项

禁止事项片段 MARKER_FORBIDDEN_LEAK

## 输出要求

输出说明。
""".strip()


def _minimal_fill_prompt(*, style_id: str, outline_page: str = "- type: content\n  title: 测试页", **kwargs) -> str:
    return _build_content_template_fill_prompt(
        page_number=3,
        style_id=style_id,
        style_text="style stub",
        outline_page=outline_page,
        research_page="research stub",
        outline_full="OUTLINE_FULL_MARKER 不应出现在填槽 prompt",
        seed_html="<html><main>{{PAGE_CONTENT}}</main></html>",
        **kwargs,
    )


def test_extract_designer_section_matches_current_anchors():
    extracted = _extract_designer_section(_DESIGNER_MD_FIXTURE)

    assert "MARKER_CRITICAL" in extracted
    assert "MARKER_FLEX" in extracted
    assert "MARKER_LAYOUT" in extracted
    assert "MARKER_FORBIDDEN_LEAK" not in extracted
    assert "MARKER_STAGE4_LEAK" not in extracted
    assert "## 禁止事项" not in extracted


def test_extract_designer_section_legacy_budget_when_no_new_anchors():
    legacy_only = """
**E. 页面内容预算契约（生成 HTML 前必须完成）**

预算片段 MARKER_BUDGET_CONTRACT

### 阶段 4：交付

交付片段 MARKER_STAGE4_LEAK
""".strip()
    extracted = _extract_designer_section(legacy_only)

    assert "MARKER_BUDGET_CONTRACT" in extracted
    assert "MARKER_STAGE4_LEAK" not in extracted


def test_extract_designer_section_content_fill_uses_density_checklist_not_long_chapters():
    extracted = _extract_designer_section(
        _DESIGNER_MD_FIXTURE,
        include_charts=True,
        for_content_template_fill=True,
    )

    assert "PAGE_CONTENT 密度硬约束" in extracted
    assert "CHART_SCAFFOLD" in extracted
    assert "MARKER_BUDGET_CONTRACT" not in extracted
    assert "MARKER_FLEX" not in extracted
    assert "MARKER_LAYOUT" not in extracted
    assert "MARKER_VISUAL" not in extracted
    assert "MARKER_CRITICAL" not in extracted
    assert "## 图表与数据可视化" in extracted


def test_content_fill_prompt_omits_outline_full_and_long_designer_chapters():
    prompt = _minimal_fill_prompt(
        style_id="business-classic",
        outline_page="**类型**：trend\n**标题**：趋势页",
        designer_md_text=_DESIGNER_MD_FIXTURE,
    )

    assert "OUTLINE_FULL_MARKER" not in prompt
    assert "大纲全文" not in prompt
    assert "PAGE_CONTENT 密度硬约束" in prompt
    assert "MARKER_FLEX" not in prompt
    assert "MARKER_LAYOUT" not in prompt
    assert "MARKER_VISUAL" not in prompt
    assert "MARKER_CRITICAL" not in prompt
    assert "## 图表与数据可视化" in prompt  # trend 为图表候选页


def test_content_fill_prompt_non_chart_page_skips_chart_section():
    prompt = _minimal_fill_prompt(
        style_id="business-classic",
        outline_page="**类型**：case\n**标题**：案例页",
        designer_md_text=_DESIGNER_MD_FIXTURE,
    )
    assert "PAGE_CONTENT 密度硬约束" in prompt
    assert "## 图表与数据可视化" not in prompt
    assert "OUTLINE_FULL_MARKER" not in prompt


def test_content_fill_prompt_retry_uses_original_html_as_edit_base():
    prompt = _minimal_fill_prompt(
        style_id="business-classic",
        outline_page="**类型**：data\n**标题**：数据页",
        rewrite_hint="修复 chart 容器高度链",
        original_html="<html><body><main><section>旧版本</section></main></body></html>",
    )

    assert "上次产物（原始 HTML，作为本轮定点修复基底）" in prompt
    assert "<section>旧版本</section>" in prompt
    assert "本轮必须基于上次产物做针对性修复，不要回退为从 seed 重新整页填充" in prompt
    assert "`seed_html` 仅用于约束骨架/Chrome/占位符边界；`original_html` 才是当前页面已形成状态的来源" in prompt


def test_content_fill_prompt_custom_vs_preset_page_content_rules():
    custom_prompt = _minimal_fill_prompt(style_id="custom")
    preset_prompt = _minimal_fill_prompt(style_id="business-classic")

    assert "一个且仅一个首层根容器" not in custom_prompt
    assert "至少两个直接子块" in custom_prompt
    assert "THEME_CSS_VARIABLES" in custom_prompt
    assert "只允许替换 3 类占位符" not in custom_prompt
    assert "只替换三处占位符" not in custom_prompt
    assert "一个且仅一个首层根容器" in preset_prompt
    assert "至少两个直接子块" not in preset_prompt
    assert "只允许替换 3 类占位符" in preset_prompt


def test_is_chart_candidate_default_types():
    assert _is_chart_candidate_page("data")
    assert _is_chart_candidate_page("trend")
    assert not _is_chart_candidate_page("case")


def test_is_chart_candidate_semantic_elevation_content():
    assert _is_chart_candidate_page(
        "content",
        outline_page="**类型**：content\n**数据需求**：营收对比",
    )
    assert not _is_chart_candidate_page(
        "content",
        outline_page="**类型**：content\n**标题**：观点页",
        research_page="纯叙事，无数据可视化",
    )


def test_is_chart_candidate_semantic_elevation_case_via_research():
    assert _is_chart_candidate_page(
        "case",
        outline_page="**类型**：case\n**标题**：案例",
        research_page="结果含关键指标同比增长 12%",
    )


def test_is_chart_candidate_structural_never_elevates():
    assert not _is_chart_candidate_page(
        "agenda",
        outline_page="**类型**：agenda\n**数据需求**：目录",
        research_page="图表趋势对比",
    )


def test_content_fill_prompt_content_type_elevated_by_data_needs():
    prompt = _minimal_fill_prompt(
        style_id="business-classic",
        outline_page="**类型**：content\n**标题**：观点页\n**数据需求**：市场份额",
        designer_md_text=_DESIGNER_MD_FIXTURE,
    )
    assert "`CHART_SCAFFOLD` 不在 Chrome 锁内" in prompt
    assert "MARKER_ACTIVATION_STEPS" in prompt
    assert "非图表候选页" not in prompt


def test_content_fill_prompt_chart_candidate_preset_scaffold_exemption():
    prompt = _minimal_fill_prompt(
        style_id="tech-minimal",
        outline_page="**类型**：data\n**标题**：数据页",
        designer_md_text=_DESIGNER_MD_FIXTURE,
    )
    assert "`CHART_SCAFFOLD` 不在 Chrome 锁内" in prompt
    assert "允许替换的可编辑区" in prompt
    assert "非图表候选页" not in prompt


def test_content_fill_prompt_non_chart_preset_keeps_three_slots():
    prompt = _minimal_fill_prompt(
        style_id="business-classic",
        outline_page="**类型**：case\n**标题**：案例页",
    )
    assert "只允许替换 3 类占位符" in prompt
    assert "CHART_SCAFFOLD 不在 Chrome 锁内" not in prompt
    assert "非图表候选页" in prompt


def test_content_fill_system_prompt_chart_candidate():
    preset = _build_content_template_fill_system_prompt(
        style_id="tech-minimal",
        page_type="trend",
    )
    assert "CHART_SCAFFOLD 不在 Chrome 锁内" in preset
    custom = _build_content_template_fill_system_prompt(
        style_id="custom",
        page_type="data",
    )
    assert "CHART_SCAFFOLD 不在 Page Chrome 锁内" in custom


def test_repair_content_template_chrome_preserves_filled_scaffold():
    seed = (
        "<html><head><title>{{PAGE_TITLE}}</title></head><body>"
        '<div class="content-safe"><header><h1>{{PAGE_TITLE}}</h1></header>'
        '<main class="page-main">{{PAGE_CONTENT}}</main>'
        '<div class="flex-shrink-0"><p>{{PAGE_FOOTER}}</p></div>'
        "<!-- CHART_SCAFFOLD_BEGIN "
        '<script>const option = null; document.getElementById("chart-1");</script> '
        "CHART_SCAFFOLD_END --></body></html>"
    )
    filled = seed.replace("{{PAGE_TITLE}}", "标题")
    filled = filled.replace("{{PAGE_CONTENT}}", '<div id="chart-1">chart</div>')
    filled = filled.replace("{{PAGE_FOOTER}}", "来源")
    filled = filled.replace(
        "const option = null;",
        'const option = {"series": [{"type": "bar", "data": [1]}]};',
    )
    filled = filled.replace(
        "<head><title>标题</title></head>",
        "<head><title>改坏</title></head>",
    )

    repaired = _repair_content_template_chrome(seed, filled)
    assert repaired is not None
    assert "改坏" not in repaired
    assert 'const option = {"series"' in repaired


def test_custom_chart_prompt_mentions_chart_font_family():
    prompt = _minimal_fill_prompt(
        style_id="custom",
        outline_page="**类型**：trend\n**标题**：趋势页",
    )
    assert "CHART_FONT_FAMILY" in prompt
    assert "须按" in prompt and "style-custom.md" in prompt


def test_extract_chart_scaffold_region_ignores_scripts_after_body_with_prior_comment():
    """</body> 前有 HTML 注释时，须在去注释坐标系内截断，避免误取 body 后的 script。"""
    scaffold_script = (
        '<script>const option = {"series":[{"data":[1]}]}; '
        'echarts.init(document.getElementById("chart-1"));</script>'
    )
    decoy_script = (
        '<script>const option = {"series":[{"data":[999]}]}; '
        'echarts.init(document.getElementById("decoy"));</script>'
    )
    filled = (
        "<html><body>"
        '<main><div id="chart-1"></div></main>'
        "<!-- layout note: keep chart area visible -->"
        f"{scaffold_script}"
        "</body>"
        f"{decoy_script}"
        "</html>"
    )
    region = _extract_chart_scaffold_region(filled)
    assert region is not None
    assert 'getElementById("chart-1")' in region
    assert 'getElementById("decoy")' not in region
    assert "[1]" in region
    assert "[999]" not in region


def test_merge_chart_scaffold_active_script_without_comment():
    seed = (
        "<html><body>"
        '<main>{{PAGE_CONTENT}}</main>'
        '<div class="flex-shrink-0"><p>f</p></div>'
        "<!-- CHART_SCAFFOLD_BEGIN "
        '<script>const option = null;</script> '
        "CHART_SCAFFOLD_END --></body></html>"
    )
    active_script = (
        '<script>const option = {"series":[]}; '
        'document.getElementById("chart-1"); echarts.init(document.getElementById("chart-1"));'
        "</script>"
    )
    filled = seed.replace(
        "<!-- CHART_SCAFFOLD_BEGIN "
        '<script>const option = null;</script> '
        "CHART_SCAFFOLD_END -->",
        active_script,
    )
    merged = _merge_chart_scaffold_from_filled(seed, filled)
    assert "CHART_SCAFFOLD_BEGIN" not in merged
    assert 'const option = {"series"' in merged


def test_chart_scaffold_path_b_ignores_main_inline_echarts_init():
    """PAGE_CONTENT 内违规 echarts.init 不得当作 scaffold 进度写回 seed。"""
    dormant_scaffold = (
        "<!-- CHART_SCAFFOLD_BEGIN "
        '<script>const option = null;</script> '
        "CHART_SCAFFOLD_END -->"
    )
    main_inline = (
        '<script>const option = {"series":[{"data":[1]}]}; '
        'echarts.init(document.getElementById("chart-1"));</script>'
    )
    seed = (
        "<html><body>"
        '<main>{{PAGE_CONTENT}}</main>'
        '<div class="flex-shrink-0"><p>f</p></div>'
        f"{dormant_scaffold}"
        "</body></html>"
    )
    filled = (
        "<html><body>"
        f'<main><div id="chart-1"></div>{main_inline}</main>'
        '<div class="flex-shrink-0"><p>f</p></div>'
        f"{dormant_scaffold}"
        "</body></html>"
    )
    assert _filled_chart_scaffold_is_progressed(filled) is False
    assert _extract_chart_scaffold_region(filled) is None
    merged = _merge_chart_scaffold_from_filled(seed, filled)
    assert "CHART_SCAFFOLD_BEGIN" in merged
    assert "const option = null" in merged


_RESEARCH_OUTLINE = """**类型**：content
**标题**：测试内容页
**研究需求**：✅
**页研究查询**：foo
"""


def test_uses_content_template_fill_allows_custom():
    assert _uses_content_template_fill("custom", "content", _RESEARCH_OUTLINE) is True
    assert _uses_content_template_fill("business-classic", "content", _RESEARCH_OUTLINE) is True
    assert _uses_structural_template_fill("custom", "cover") is True
    assert _uses_content_template_fill("custom", "cover", _RESEARCH_OUTLINE) is False


def test_validate_custom_content_fill_theme_and_placeholders():
    # _is_valid_html 要求长度 >= 200；_validate_slide_dom 要求 class="...ppt-slide"
    pad = "<!-- pad -->" * 20
    seed = (
        '<!DOCTYPE html><html><head><style id="theme-contract">'
        "{{THEME_CSS_VARIABLES}}</style></head><body>"
        f"{pad}"
        '<div class="ppt-slide"><div class="content-safe">'
        "<header><h1>{{PAGE_TITLE}}</h1></header>"
        '<main class="page-main flex-1">{{PAGE_CONTENT}}</main>'
        "<footer><p>{{PAGE_FOOTER}}</p></footer>"
        "</div></div></body></html>"
    )
    filled_ok = seed.replace("{{THEME_CSS_VARIABLES}}", "--color-text:#111;")
    filled_ok = filled_ok.replace("{{PAGE_TITLE}}", "标题")
    filled_ok = filled_ok.replace(
        "{{PAGE_CONTENT}}",
        '<section class="flex-shrink-0">a</section>'
        '<div class="flex-1 min-h-0">b</div>',
    )
    filled_ok = filled_ok.replace("{{PAGE_FOOTER}}", "来源")
    ok, reason = _validate_custom_content_template_fill_output(seed, filled_ok)
    assert ok, reason

    filled_theme_left = filled_ok.replace("--color-text:#111;", "{{THEME_CSS_VARIABLES}}")
    ok2, reason2 = _validate_custom_content_template_fill_output(seed, filled_theme_left)
    assert not ok2
    assert reason2 == "unfilled_placeholders"


@pytest.mark.asyncio
async def test_skill_turbo_template_path_bypass_hard_fails():
    raw = await skill_turbo.invoke(
        {"query": r"用模板目录：C:\templates\demo-pack 生成一份产品发布会 PPT"}
    )
    result = json.loads(raw) if isinstance(raw, str) else raw

    assert result.get("success") is False
    error = str(result.get("error") or "")
    assert "自定义模板" in error or "模板包" in error
    assert "skill_tool" in error


@pytest.mark.asyncio
async def test_generate_one_passes_original_html_to_content_template_fill(monkeypatch):
    node = object.__new__(PageWorkerNode)
    captured: dict[str, str] = {}

    async def fake_generate_content_template_fill(
        self, ctx, *, rewrite_hint="", original_html="", seed_html_override=""
    ):
        captured["rewrite_hint"] = rewrite_hint
        captured["original_html"] = original_html
        return "<html></html>", "", ""

    monkeypatch.setattr(
        PageWorkerNode,
        "_generate_content_template_fill",
        fake_generate_content_template_fill,
    )

    ctx = PageGenContext(
        page_num=3,
        style_id="business-classic",
        style_text="style stub",
        outline_page=_RESEARCH_OUTLINE,
        research_page="research stub",
        outline_is_full=False,
        image_map_page="",
        designer_md_text="",
        user_query="",
        total_pages=8,
        pptx_root="D:/pptx-craft",
        outline_full=_RESEARCH_OUTLINE,
    )

    html, raw_html, reason = await PageWorkerNode._generate_one(
        node,
        ctx,
        rewrite_hint="仅修复 overflow",
        original_html="<html><body><main>旧页</main></body></html>",
    )

    assert html == "<html></html>"
    assert raw_html == ""
    assert reason == ""
    assert captured["rewrite_hint"] == "仅修复 overflow"
    assert captured["original_html"] == "<html><body><main>旧页</main></body></html>"


def test_layout_fix_hint_chart_axis_range():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
        _layout_fix_hint_from_cli_output,
    )

    hit = _layout_fix_hint_from_cli_output(
        '[page-3][chart-axis-range] series "A" 数据极值 120 高于 yAxis[0].max 100'
    )
    assert "min/max 覆盖数据极值" in hit
    miss = _layout_fix_hint_from_cli_output("[page-1][overflow] main")
    assert "chart-axis-range" not in miss


def test_designer_injection_includes_axis_range_rule():
    charts_text = """
## 图表与数据可视化

- 数值轴值域：先确定 series 数据再设置数值轴；显式 min/max 必须完整覆盖该轴承载的数据，
  不需要固定范围时由 ECharts 自动计算。

### 激活 content-template 内的图表骨架（强制）

按骨架激活。

### custom 模式的图表骨架

保持 custom 说明。
""".strip()

    fill_extracted = _extract_designer_section(
        "",
        include_charts=True,
        for_content_template_fill=True,
        charts_text=charts_text,
    )
    free_extracted = _extract_designer_section(
        "",
        include_charts=True,
        charts_text=charts_text,
    )

    assert "数值轴值域" in fill_extracted
    assert "数值轴值域" in free_extracted
    assert "显式 min/max 必须完整覆盖" in fill_extracted
    assert "显式 min/max 必须完整覆盖" in free_extracted


def test_strip_page_gen_excluded_text_basic_and_guards():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import (
        PptCommon,
    )

    notes_frag = "NOTES_VERBATIM_FRAGMENT."
    size_frag = "SIZE_LIMIT_20MB."
    src = f"Make a sleep health PPT. {notes_frag} {size_frag} Keep the rest."
    out = PptCommon.strip_page_gen_excluded_text(
        src,
        {
            "notes_request_verbatim": [notes_frag],
            "file_size_constraint": size_frag,
        },
    )
    assert notes_frag not in out
    assert size_frag not in out
    assert "sleep health" in out

    # fail-open：片段等于全文
    assert (
        PptCommon.strip_page_gen_excluded_text(
            "整段", {"notes_request_verbatim": ["整段"]}
        )
        == "整段"
    )
    # 未命中原样
    assert (
        PptCommon.strip_page_gen_excluded_text(
            src, {"notes_request_verbatim": ["missing-fragment"]}
        )
        == src
    )
    assert PptCommon.strip_page_gen_excluded_text(src, {}) == src


def test_p4_prompt_strips_notes_and_file_size_from_user_query():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.content_plan import (
        _build_p43_prompt,
    )

    notes_frag = "每页下方附上完整口述台词。"
    size_frag = "文件控制在 20MB。"
    inputs = {
        "topic": "睡眠健康",
        "page_count": 6,
        "audience": "管理层",
        "source_type": "topic",
        "search_mode": "auto",
        "presentation_purpose": "工作汇报",
        "focus_areas": "行业趋势",
        "content_branch": "research",
        "query": (
            "做一份睡眠健康 PPT，面向管理层，重点讲行业趋势、用户痛点、市场规模、"
            f"核心结论。{notes_frag}{size_frag} 请保持商务表达。"
        ),
        "notes_request_verbatim": [notes_frag],
        "file_size_constraint": size_frag,
        "user_dimensions": [],
        "user_structure": "",
    }

    prompt = _build_p43_prompt(inputs, "", "")

    user_line = next(
        line for line in prompt.splitlines() if line.startswith("- 用户原文：")
    )

    assert "做一份睡眠健康 PPT" in user_line
    assert "市场规模" in user_line
    assert "请保持商务表达。" in user_line
    assert notes_frag not in user_line
    assert size_frag not in user_line


def test_content_fill_prompt_includes_footer_rule_and_speaker_notes_ban():
    prompt = _minimal_fill_prompt(
        style_id="business-classic",
        outline_page="**类型**：trend\n**标题**：趋势页",
    )

    assert "口径注释" in prompt
    assert "不是演讲备注/口播台词/QA" in prompt
    assert "演讲稿/演讲备注/讲者备注/speaker notes 禁止写入页面" in prompt


def test_free_gen_page_prompt_includes_speaker_notes_ban():
    prompt = _build_page_prompt(
        3,
        "business-classic",
        "style stub",
        "**类型**：trend\n**标题**：趋势页\n**研究需求**：✅\n**数据需求**：市场份额",
        "research stub",
        user_query="每页下方附上完整口述台词。",
    )

    assert "演讲稿/演讲备注/讲者备注/speaker notes 禁止写入页面" in prompt
    assert "notesSlide" in prompt


def test_delivery_summary_records_unsupported_file_size():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.delivery import (
        DeliveryNode,
    )

    summary = DeliveryNode._build_summary(
        "sent",
        "a.pptx",
        9,
        "/tmp/pages",
        "sent",
        file_size_constraint="不超过 20MB",
    )
    assert "指定文件大小" in summary
    summary2 = DeliveryNode._build_summary(
        "sent",
        "a.pptx",
        9,
        "/tmp/pages",
        "sent",
        file_size_constraint="",
    )
    assert "指定文件大小" not in summary2


def test_p2_slot_prompt_mentions_file_size_and_notes_verbatim():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.requirement_collect import (
        _P21_SLOT_SYSTEM_PROMPT,
    )

    assert "file_size_constraint" in _P21_SLOT_SYSTEM_PROMPT
    assert "notes_request_verbatim" in _P21_SLOT_SYSTEM_PROMPT
    assert "文件体积要求不属于版式" in _P21_SLOT_SYSTEM_PROMPT


def test_merge_slot_payload_applies_notes_verbatim_and_file_size():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.requirement_collect import (
        _merge_slot_payload,
    )

    inputs: dict[str, object] = {}
    payload = {
        "topic": "睡眠健康",
        "notes_request_verbatim": ["每页下方附上完整口述台词"],
        "file_size_constraint": "不超过 20MB",
    }

    _merge_slot_payload(inputs, payload)

    assert inputs["topic"] == "睡眠健康"
    assert inputs["notes_request_verbatim"] == ["每页下方附上完整口述台词"]
    assert inputs["file_size_constraint"] == "不超过 20MB"


def test_merge_slot_payload_defaults_notes_verbatim_and_file_size():
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.requirement_collect import (
        _merge_slot_payload,
    )

    inputs: dict[str, object] = {}
    payload = {"topic": "睡眠健康"}

    _merge_slot_payload(inputs, payload)

    assert inputs["notes_request_verbatim"] == []
    assert inputs["file_size_constraint"] == ""
