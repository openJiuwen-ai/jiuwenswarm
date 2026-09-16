# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""B′ seed-slot-merge：破根标签 / 空 footer / 结构页 / 四预设+custom 内容页。"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    _postprocess_content_template_fill_html,
    _repair_content_template_chrome,
    _repair_structural_template_slots,
    _validate_content_template_fill_output,
    _validate_custom_content_template_fill_output,
)

# _is_valid_html / DOM 校验需要足够长度与 .ppt-slide 结构
_MINIMAL_SEED_HTML = """<!DOCTYPE html>
<html><head><title>{{PAGE_TITLE}}</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style type="text/tailwindcss">@layer utilities{.content-safe{width:1220px;height:620px}
.ppt-slide{@apply relative w-[1280px] h-[720px];}</style>
</head>
<body>
<div class="ppt-slide w-[1280px] h-[720px]">
  <div class="content-safe">
    <header class="flex-shrink-0 page-header"><h1 class="page-title">{{PAGE_TITLE}}</h1></header>
    <main class="flex-1 min-h-0 page-main">{{PAGE_CONTENT}}</main>
    <div class="flex-shrink-0 page-footer"><p class="page-footer-note">{{PAGE_FOOTER}}</p></div>
  </div>
</div>
</body></html>
"""

_STRUCTURAL_SEED_HTML = """<!DOCTYPE html>
<html><head><title>{{PAGE_TITLE}}</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style type="text/tailwindcss">@layer utilities{.ppt-slide{@apply relative w-[1280px] h-[720px];}
.content-safe{width:1220px}</style>
</head>
<body>
<div class="ppt-slide w-[1280px] h-[720px]">
  <div class="content-safe cover-stage">
    <h1 class="cover-title">{{PAGE_TITLE}}</h1>
    <div class="cover-body">{{PAGE_CONTENT}}</div>
  </div>
</div>
</body></html>
"""


class _Ctx:
    page_num = 7
    style_id = "custom"
    user_query = "生成PPT"
    style_constraints = ""
    total_pages = 10
    outline_page = "### P7: 类型*content | 标题*市场趋势 | 研究需求*✅"


def _fill_content(seed: str, *, title: str, content: str, footer: str) -> str:
    return (
        seed.replace("{{PAGE_TITLE}}", title)
        .replace("{{PAGE_CONTENT}}", content)
        .replace("{{PAGE_FOOTER}}", footer)
    )


@pytest.mark.parametrize(
    "style_id",
    [
        "business-classic",
        "elegant-narrative",
        "industrial-tech",
        "tech-minimal",
        "custom",
    ],
)
def test_seed_slot_merge_recovers_broken_ppt_slide_open_tag(style_id: str):
    """复现「ppt-slide ... ) @apply」脏开标签：merge 后根标签与 seed 一致、无 @apply 泄漏。"""
    seed = _MINIMAL_SEED_HTML
    filled = _fill_content(
        seed,
        title="市场趋势分析",
        content=(
            '<div class="w-full flex-1 min-h-0"><p>正文要点一与补充说明足够长</p></div>'
            '<div class="w-full flex-1 min-h-0"><p>正文要点二与补充说明足够长</p></div>'
        ),
        footer="来源：测试报告",
    )
    # 弄坏 .ppt-slide 开标签，模拟 CSS/@apply 泄漏进 body 文本
    filled = filled.replace(
        '<div class="ppt-slide w-[1280px] h-[720px]">',
        '<div class="ppt-slide flex flex-col) @apply relative w-[1280px] h-[720px] mx-auto">',
        1,
    )
    assert "@apply" in filled

    repaired = _repair_content_template_chrome(seed, filled)
    assert repaired is not None
    assert "flex flex-col) @apply" not in repaired
    assert 'class="ppt-slide w-[1280px] h-[720px]"' in repaired
    assert "市场趋势分析" in repaired
    assert "正文要点一" in repaired

    validate_fn = (
        _validate_custom_content_template_fill_output
        if style_id == "custom"
        else _validate_content_template_fill_output
    )
    ok, reason = validate_fn(seed, repaired)
    assert ok, reason

    ctx = _Ctx()
    ctx.style_id = style_id
    out, raw, fail_reason = _postprocess_content_template_fill_html(
        seed, filled, ctx, validate_fn
    )
    assert fail_reason == ""
    assert out
    assert "flex flex-col) @apply" not in out
    assert 'class="ppt-slide w-[1280px] h-[720px]"' in out
    assert raw == ""


def test_seed_slot_merge_allows_empty_page_footer_custom():
    seed = _MINIMAL_SEED_HTML
    filled = _fill_content(
        seed,
        title="空页脚页",
        content=(
            '<div class="w-full flex-1 min-h-0"><p>正文块一足够长用于校验</p></div>'
            '<div class="w-full flex-1 min-h-0"><p>正文块二足够长用于校验</p></div>'
        ),
        footer="",
    )
    repaired = _repair_content_template_chrome(seed, filled)
    assert repaired is not None
    assert "{{PAGE_FOOTER}}" not in repaired
    ok, reason = _validate_custom_content_template_fill_output(seed, repaired)
    assert ok, reason


def test_structural_seed_slot_merge_keeps_skeleton():
    seed = _STRUCTURAL_SEED_HTML
    filled = (
        seed.replace("{{PAGE_TITLE}}", "封面主标题")
        .replace(
            "{{PAGE_CONTENT}}",
            '<p class="subtitle">副标题与简介足够长</p>',
        )
        .replace(
            '<div class="ppt-slide w-[1280px] h-[720px]">',
            '<div class="ppt-slide flex flex-col) @apply overflow-hidden">',
            1,
        )
    )
    merged = _repair_structural_template_slots(seed, filled)
    assert merged is not None
    assert "flex flex-col) @apply" not in merged
    assert 'class="ppt-slide w-[1280px] h-[720px]"' in merged
    assert "封面主标题" in merged
    assert "副标题与简介" in merged
    assert "@layer utilities" in merged


def test_structural_seed_slot_merge_preserves_background_img():
    seed = _STRUCTURAL_SEED_HTML
    filled = seed.replace("{{PAGE_TITLE}}", "有背景封面").replace(
        "{{PAGE_CONTENT}}",
        "<p>简介正文</p>",
    )
    filled = filled.replace(
        '<div class="ppt-slide w-[1280px] h-[720px]">',
        (
            '<div class="ppt-slide w-[1280px] h-[720px]">'
            '<img class="absolute inset-0 w-full h-full object-cover" src="assets/cover.png" alt=""/>'
            '<div class="absolute inset-0 bg-white/75"></div>'
        ),
        1,
    )
    merged = _repair_structural_template_slots(seed, filled)
    assert merged is not None
    assert 'src="assets/cover.png"' in merged
    assert "absolute inset-0" in merged
    assert "有背景封面" in merged


def test_structural_seed_slot_merge_duplicate_title_and_empty_optional_slot():
    """同名 PAGE_TITLE 只抽一次；可选空槽不因自创 slop 闸失败。"""
    seed = """<!DOCTYPE html>
<html><head><title>{{PAGE_TITLE}}</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style>@layer utilities{.ppt-slide{width:1280px}}</style>
</head>
<body>
<div class="ppt-slide w-[1280px] h-[720px]">
  <h1>{{PAGE_TITLE}}</h1>
  <div class="body">{{PAGE_CONTENT}}</div>
  <img data-pptx-role="structural-background" src="{{STRUCTURAL_IMAGE_PATH}}" alt="{{STRUCTURAL_IMAGE_ALT}}"/>
</div>
</body></html>
"""
    filled = (
        seed.replace("{{PAGE_TITLE}}", "双标题页")
        .replace("{{PAGE_CONTENT}}", "<p>正文区</p>")
        .replace("{{STRUCTURAL_IMAGE_PATH}}", "")
        .replace("{{STRUCTURAL_IMAGE_ALT}}", "")
    )
    merged = _repair_structural_template_slots(seed, filled)
    assert merged is not None
    assert merged.count("双标题页") >= 2
    assert "{{PAGE_TITLE}}" not in merged
    assert "{{STRUCTURAL_IMAGE_PATH}}" not in merged
    assert "正文区" in merged
