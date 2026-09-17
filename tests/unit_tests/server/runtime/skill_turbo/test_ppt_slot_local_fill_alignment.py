# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""B′ seed-slot-merge：破根标签 / 空 footer / 结构页 / 四预设+custom 内容页。"""

from __future__ import annotations

import os as _os

import pytest as _pytest

if not _os.environ.get("JIUWENSWARM_TEST_TURBO_SKILLS_DIR"):
    _pytest.skip(
        "requires JIUWENSWARM_TEST_TURBO_SKILLS_DIR (external turbo layout)",
        allow_module_level=True,
    )

import pytest

from skill_turbo_codes_ppt.ppt.ppt_page_gen import (
    _postprocess_content_template_fill_html,
    _postprocess_structural_template_fill_html,
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
    assert 'src=""' in merged
    assert 'alt=""' in merged
    assert "src=\" data-pptx-role=" not in merged
    assert "正文区" in merged


def test_structural_seed_slot_merge_perfect_fill_keeps_clean_image_attrs():
    """Happy path：LLM 逐字保留骨架仅换 token 时，PATH/ALT 须干净，不得吞入相邻属性。"""
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
        seed.replace("{{PAGE_TITLE}}", "封面标题")
        .replace("{{PAGE_CONTENT}}", "<p>封面导语</p>")
        .replace("{{STRUCTURAL_IMAGE_PATH}}", "assets/cover.png")
        .replace("{{STRUCTURAL_IMAGE_ALT}}", "封面背景")
    )
    merged = _repair_structural_template_slots(seed, filled)
    assert merged is not None
    assert 'src="assets/cover.png"' in merged
    assert 'alt="封面背景"' in merged
    assert 'src=" data-pptx-role=' not in merged
    assert 'data-pptx-role="structural-background" src="assets/cover.png"' in merged
    assert merged.count("src=") == 1
    assert merged.count("alt=") == 1


def test_structural_seed_slot_merge_survives_img_attr_reorder():
    """属性槽：LLM 把 img 属性换序时仍须 merge 成功（DOM fallback，非整页写盘）。"""
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
    filled = """<!DOCTYPE html>
<html><head><title>封面标题</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style>@layer utilities{.ppt-slide{width:1280px}}</style>
</head>
<body>
<div class="ppt-slide w-[1280px] h-[720px]">
  <h1>封面标题</h1>
  <div class="body"><p>封面导语</p></div>
  <img alt="封面背景" data-pptx-role="structural-background" src="assets/cover-bg.png"/>
</div>
</body></html>
"""
    merged = _repair_structural_template_slots(seed, filled)
    assert merged is not None
    assert 'src="assets/cover-bg.png"' in merged
    assert 'alt="封面背景"' in merged
    assert 'src=" data-pptx-role=' not in merged
    assert merged.count("src=") == 1
    assert "封面标题" in merged
    assert "{{STRUCTURAL_IMAGE_PATH}}" not in merged
    assert "{{STRUCTURAL_IMAGE_ALT}}" not in merged


def test_structural_seed_slot_merge_survives_img_reindent():
    """属性槽：LLM 对 img 标签重缩进时邻接锚点失效，须仍能抽 PATH/ALT。"""
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
    filled = """<!DOCTYPE html>
<html><head><title>重缩进封面</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style>@layer utilities{.ppt-slide{width:1280px}}</style>
</head>
<body>
<div class="ppt-slide w-[1280px] h-[720px]">
  <h1>重缩进封面</h1>
  <div class="body"><p>导语</p></div>
  <img
    data-pptx-role="structural-background"
    src="assets/reindent.png"
    alt="重缩进背景"
  />
</div>
</body></html>
"""
    merged = _repair_structural_template_slots(seed, filled)
    assert merged is not None
    assert 'src="assets/reindent.png"' in merged
    assert 'alt="重缩进背景"' in merged
    assert 'src=" data-pptx-role=' not in merged
    assert merged.count("src=") == 1
    assert "{{STRUCTURAL_IMAGE" not in merged


def test_structural_postprocess_falls_back_to_old_gates_when_merge_fails():
    """整体重缩进致邻接切片失败、且无 <main> 时 merge 失败；旧门禁通过则仍落盘 LLM html。"""
    seed = _STRUCTURAL_SEED_HTML
    filled = """<!DOCTYPE html>
<html><head><title>整体重缩进封面</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style type="text/tailwindcss">@layer utilities{.ppt-slide{@apply relative w-[1280px] h-[720px];}
.content-safe{width:1220px}</style>
</head>
<body>
<div class="ppt-slide w-[1280px] h-[720px]">
<div class="content-safe cover-stage">
<h1 class="cover-title">
整体重缩进封面
</h1>
<div class="cover-body">
<p>副标题与简介足够长以通过校验</p>
</div>
</div>
</div>
</body></html>
"""
    assert _repair_structural_template_slots(seed, filled) is None
    ctx = _Ctx()
    ctx.page_num = 1
    out = _postprocess_structural_template_fill_html(
        filled, ctx, "cover", seed_html=seed
    )
    assert out
    assert "整体重缩进封面" in out
    assert "{{PAGE_TITLE}}" not in out
    assert "{{PAGE_CONTENT}}" not in out


def test_structural_postprocess_merge_fail_still_rejects_unfilled_placeholders():
    """merge 失败回退旧门禁时，残留 {{}} 仍须拒绝，不得假成功。"""
    seed = _STRUCTURAL_SEED_HTML
    filled = seed.replace("{{PAGE_TITLE}}", "只填了标题")
    # PAGE_CONTENT 仍为 {{PAGE_CONTENT}}；整体改缩进使邻接切片也失败
    filled = filled.replace(
        '  <div class="cover-body">{{PAGE_CONTENT}}</div>\n',
        '<div class="cover-body">{{PAGE_CONTENT}}</div>\n',
        1,
    )
    assert _repair_structural_template_slots(seed, filled) is None
    ctx = _Ctx()
    ctx.page_num = 1
    out = _postprocess_structural_template_fill_html(
        filled, ctx, "cover", seed_html=seed
    )
    assert out == ""
