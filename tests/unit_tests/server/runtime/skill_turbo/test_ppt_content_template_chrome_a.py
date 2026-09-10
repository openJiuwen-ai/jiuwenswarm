# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Batch A: footer regex + content-template chrome reason split / repair."""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    _REPAIRABLE_CONTENT_TEMPLATE_REASONS,
    _extract_footer_block,
    _repair_content_template_chrome,
    _validate_content_template_fill_output,
)

# _is_valid_html requires len >= 200
_MINIMAL_SEED_HTML = """<!DOCTYPE html>
<html><head><title>{{PAGE_TITLE}}</title>
<script>tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}</script>
<style>@layer utilities{.content-safe{width:1220px}}</style>
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


def test_extract_footer_ignores_flex_shrink_cards_inside_main():
    """main 内 flex-shrink-0 卡片不得被当成 footer。"""
    html = """<!DOCTYPE html>
<html><head><title>T</title></head>
<body>
<div class="ppt-slide">
  <div class="content-safe">
    <header class="flex-shrink-0"><h1>标题</h1></header>
    <main class="flex-1 min-h-0">
      <div class="flex-shrink-0"><p>内容区卡片，不是页脚</p></div>
      <div class="flex-1 min-h-0"><p>正文</p></div>
    </main>
    <div class="flex-shrink-0"><p>来源：官方报告</p></div>
  </div>
</div>
</body></html>"""
    footer = _extract_footer_block(html)
    assert footer
    assert "来源：官方报告" in footer
    assert "内容区卡片" not in footer


def test_extract_footer_missing_when_no_main():
    html = '<div class="flex-shrink-0"><p>孤岛卡片</p></div>'
    assert _extract_footer_block(html) == ""


def test_validate_splits_head_chrome_reason_and_repair_recovers():
    seed = _MINIMAL_SEED_HTML
    filled = (
        seed.replace("{{PAGE_TITLE}}", "真实标题")
        .replace(
            "{{PAGE_CONTENT}}",
            '<div class="w-full flex-1 min-h-0"><p>正文要点一与补充说明</p></div>',
        )
        .replace("{{PAGE_FOOTER}}", "来源：测试")
        .replace(
            "tailwind.config={theme:{extend:{colors:{brand:'#c00'}}}}",
            "tailwind.config={theme:{extend:{colors:{brand:'#0f0'}}}}",
        )
    )
    ok, reason = _validate_content_template_fill_output(seed, filled)
    assert not ok
    assert reason == "head_chrome_changed"
    assert reason in _REPAIRABLE_CONTENT_TEMPLATE_REASONS

    repaired = _repair_content_template_chrome(seed, filled)
    assert repaired is not None
    ok2, reason2 = _validate_content_template_fill_output(seed, repaired)
    assert ok2, reason2
    assert "真实标题" in repaired
    assert "正文要点一" in repaired
    assert "来源：测试" in repaired
    assert "#c00" in repaired


def test_validate_footer_chrome_changed_when_footer_structure_rewritten():
    seed = _MINIMAL_SEED_HTML
    filled = (
        seed.replace("{{PAGE_TITLE}}", "真实标题")
        .replace(
            "{{PAGE_CONTENT}}",
            '<div class="w-full flex-1 min-h-0"><p>正文要点内容</p></div>',
        )
        .replace(
            '<div class="flex-shrink-0 page-footer"><p class="page-footer-note">{{PAGE_FOOTER}}</p></div>',
            '<footer class="mt-auto"><span>来源：改结构</span></footer>',
        )
    )
    ok, reason = _validate_content_template_fill_output(seed, filled)
    assert not ok
    assert reason == "footer_chrome_changed"
    assert reason in _REPAIRABLE_CONTENT_TEMPLATE_REASONS
