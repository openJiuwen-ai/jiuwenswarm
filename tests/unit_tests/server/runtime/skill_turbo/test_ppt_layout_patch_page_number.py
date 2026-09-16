# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P8.1 layout_patch must keep the visible page number anchor — issue: PPT pages 4/6 lost
the bottom-left page number after §3.5 in-place layout patch rewrote the full HTML.

Root cause: `_generate_layout_patch` postprocess chain lacks `_apply_visible_page_number_policy`
(which every other generation path applies), so when the patch LLM drops the
`data-skill-turbo-page-number` span there is no deterministic re-insert.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import (
    PageGenContext,
    PageWorkerNode,
)

# 生产链路注入的页码锚点格式（P8.1 `_insert_visible_page_marker` 产物）
_PAGE_NUMBER_SPAN = (
    '<span data-skill-turbo-page-number="true" data-position="bottom-left" '
    'style="position:absolute;z-index:30;min-width:48px;left:30px;bottom:16px;'
    'text-align:left;font-family:inherit;font-size:12px;line-height:1;'
    'font-weight:400;color:#898989;white-space:nowrap;background:transparent;'
    'border:0;padding:0;margin:0;">4 / 8</span>'
)

# 修补 LLM 可能意外残留的裸运行页码（无 data-skill-turbo 属性，应被 strip）
_BARE_PAGE_MARKER_SPAN = '<span class="footer-note">4 / 8</span>'


def _slide_html(inner_extra: str = "") -> str:
    """构造可通过 P8.1 结构页校验的单页 HTML（恰好 1 个 ppt-slide，main 在内）。"""
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
        "<title>测试页面标题内容</title></head><body>"
        '<div class="ppt-slide h-[720px]">'
        '<main class="flex-1 min-h-0"><section>核心内容区块测试数据展示</section></main>'
        f"{inner_extra}"
        "</div></body></html>"
    )


def _make_ctx(**overrides: Any) -> PageGenContext:
    defaults: dict[str, Any] = dict(
        page_num=4,
        style_id="business-classic",
        style_text="",
        outline_page="### P4:\n- **类型**：cover\n- **标题**：测试封面页",
        research_page="",
        outline_is_full=False,
        image_map_page="",
        designer_md_text="",
        style_constraints="页码位于每页左下角",
        user_query="制作测试 PPT，共 8 页",
        total_pages=8,
    )
    defaults.update(overrides)
    return PageGenContext(**defaults)


class _PatchLLM:
    """替身 stream_llm_collect：记录 system_prompt 并返回预设修补输出。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.system_prompts: list[str] = []
        self.prompts: list[str] = []

    async def __call__(self, *, prompt: str, system_prompt: str, **_kwargs: Any) -> str:
        self.prompts.append(prompt)
        self.system_prompts.append(system_prompt)
        return self.response


@pytest.mark.asyncio
async def test_layout_patch_missing_page_number_reinserted() -> None:
    """修补 LLM 丢失页码锚点时，成功出口必须重新插入统一页码（回归主用例）。"""
    gen = PageWorkerNode()
    llm = _PatchLLM(_slide_html())  # 修补输出丢了页码 span
    gen.stream_llm_collect = llm  # type: ignore[method-assign]

    html, _raw, reason = await gen._generate_layout_patch(
        _make_ctx(),
        current_html=_slide_html(_PAGE_NUMBER_SPAN),
        seed_html=_slide_html(),
        fix_hint="overflow: card bar",
    )

    assert reason == ""
    assert html.count('data-skill-turbo-page-number="true"') == 1
    assert ">4 / 8<" in html
    assert 'data-position="bottom-left"' in html


@pytest.mark.asyncio
async def test_layout_patch_kept_page_number_not_duplicated() -> None:
    """修补 LLM 保留页码锚点时，结果必须恰好 1 个页码（strip 后重插，不重复）。"""
    gen = PageWorkerNode()
    llm = _PatchLLM(_slide_html(_PAGE_NUMBER_SPAN))  # 修补输出保留了页码 span
    gen.stream_llm_collect = llm  # type: ignore[method-assign]

    html, _raw, reason = await gen._generate_layout_patch(
        _make_ctx(),
        current_html=_slide_html(_PAGE_NUMBER_SPAN),
        seed_html=_slide_html(),
        fix_hint="v-gap: card bar",
    )

    assert reason == ""
    assert html.count('data-skill-turbo-page-number="true"') == 1
    assert ">4 / 8<" in html


@pytest.mark.asyncio
async def test_layout_patch_without_page_number_policy_strips_marker() -> None:
    """用户未要求页码时不得插入；修补输出残留的裸运行页码文本应被移除。"""
    gen = PageWorkerNode()
    llm = _PatchLLM(_slide_html(_BARE_PAGE_MARKER_SPAN))
    gen.stream_llm_collect = llm  # type: ignore[method-assign]

    html, _raw, reason = await gen._generate_layout_patch(
        _make_ctx(style_constraints="", user_query="制作测试 PPT，共 8 页"),
        current_html=_slide_html(),
        seed_html=_slide_html(),
        fix_hint="overflow: card bar",
    )

    assert reason == ""
    assert "data-skill-turbo-page-number" not in html
    assert "4 / 8" not in html


@pytest.mark.asyncio
async def test_layout_patch_system_prompt_requires_page_number_anchor() -> None:
    """两个修补分支的 system_prompt 都必须显式要求保留页码锚点。"""
    gen = PageWorkerNode()
    llm = _PatchLLM(_slide_html())
    gen.stream_llm_collect = llm  # type: ignore[method-assign]

    # 分支一：无未填图表 option
    await gen._generate_layout_patch(
        _make_ctx(),
        current_html=_slide_html(_PAGE_NUMBER_SPAN),
        seed_html=_slide_html(),
        fix_hint="overflow: card bar",
    )
    # 分支二：存在可执行 const option = null
    await gen._generate_layout_patch(
        _make_ctx(),
        current_html=(
            "<!DOCTYPE html><html><body>"
            '<div class="ppt-slide h-[720px]">'
            '<main class="flex-1"><section>内容</section></main>'
            "<script>const option = null;</script>"
            "</div></body></html>"
        ),
        seed_html=_slide_html(),
        fix_hint="whitespace: chart empty",
    )

    assert len(llm.system_prompts) == 2
    for system_prompt in llm.system_prompts:
        assert "data-skill-turbo-page-number" in system_prompt
        assert "保留" in system_prompt
