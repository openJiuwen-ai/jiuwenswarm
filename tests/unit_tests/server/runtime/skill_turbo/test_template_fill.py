# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for template_fill helpers."""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.template_fill import (
    FillMode,
    PageGenPolicy,
    apply_template_slots,
    build_page_template_map,
    count_page_main_direct_children,
    detect_page_type,
    extract_theme_blocks,
    resolve_fill_mode,
    resolve_skill_root,
    scan_placeholders,
    should_seed_pages,
)


def test_detect_page_type_from_outline():
    outline = "### P3: 市场分析\n页类型: data\n"
    assert detect_page_type(outline) == "data"


def test_detect_page_type_markdown_and_blockquote_formats():
    assert detect_page_type("- **类型**：cover") == "cover"
    assert detect_page_type("> 页面类型：data") == "data"


def test_build_page_template_map():
    outline_pages = {
        1: "页类型: cover",
        2: "页类型: agenda",
        3: "正文",
    }
    assert build_page_template_map(outline_pages, 3) == (
        "1=cover-template.html,2=agenda-template.html,3=content-template.html"
    )


def test_build_page_template_map_markdown_type():
    outline_pages = {
        1: "- **类型**：cover",
        2: "> 页面类型：data",
        3: "- **类型**：ending",
    }
    assert build_page_template_map(outline_pages, 3) == (
        "1=cover-template.html,2=content-template.html,3=ending-template.html"
    )


def test_scan_and_apply_placeholders():
    seed = "<title>{{PAGE_TITLE}}</title><main>{{PAGE_CONTENT}}</main>"
    assert scan_placeholders(seed) == ["PAGE_TITLE", "PAGE_CONTENT"]
    filled = apply_template_slots(
        seed,
        {"PAGE_TITLE": "Hello", "PAGE_CONTENT": "<div>body</div>"},
    )
    assert "{{" not in filled
    assert "Hello" in filled


def test_extract_theme_blocks():
    style = """
## 主题
```css
:root { --primary: #000; }
```
```css
.page-main { padding: 1rem; }
```
"""
    variables, rules = extract_theme_blocks(style)
    assert ":root" in variables
    assert ".page-main" in rules


def test_count_page_main_direct_children():
    content = "<section>A</section><div>B</div>"
    assert count_page_main_direct_children(content) == 2
    assert count_page_main_direct_children("") == 0


def test_resolve_fill_mode_preset_seeded():
    policy = PageGenPolicy(allow_free_gen_fallback=False)
    mode = resolve_fill_mode(
        style_mode="preset",
        style_id="business-classic",
        pages_seeded=True,
        page_type="data",
        policy=policy,
    )
    assert mode == FillMode.PRESET_TEMPLATE


def test_resolve_fill_mode_custom_allows_fallback():
    policy = PageGenPolicy.from_inputs({"style_mode": "custom"}, style_id="custom")
    assert policy.allow_free_gen_fallback is True
    mode = resolve_fill_mode(
        style_mode="custom",
        style_id="custom",
        pages_seeded=False,
        page_type="data",
        policy=policy,
    )
    assert mode == FillMode.CUSTOM_TEMPLATE


def test_resolve_fill_mode_preset_without_seed_stays_template():
    policy = PageGenPolicy(allow_free_gen_fallback=False)
    mode = resolve_fill_mode(
        style_mode="preset",
        style_id="business-classic",
        pages_seeded=False,
        page_type="data",
        policy=policy,
    )
    assert mode == FillMode.PRESET_TEMPLATE


def test_should_seed_pages():
    assert should_seed_pages("preset") is True
    assert should_seed_pages("custom") is True
    assert should_seed_pages("template_canvas") is False


def test_resolve_skill_root():
    from pathlib import Path, PureWindowsPath

    assert Path(resolve_skill_root(r"D:\skills\pptx-craft")) == Path(r"D:\skills")
    assert PureWindowsPath(resolve_skill_root(r"D:\skills\pptx-craft")).name == "skills"


def test_resolve_skill_root_posix_path():
    from pathlib import Path

    assert Path(resolve_skill_root("/skills/pptx-craft")) == Path("/skills")
    assert Path(resolve_skill_root("/skills/pptx-craft")).name == "skills"
