# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for P2.4 reference URL search_mode adjustment."""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt import requirement_collect as rc


def test_extract_reference_urls_deduplicates():
    text = (
        "参考 https://zh.wikipedia.org/wiki/唐朝 和 "
        "https://zh.wikipedia.org/wiki/唐朝 以及 "
        "https://zh.wikipedia.org/wiki/长城"
    )
    urls = rc._extract_reference_urls(text)
    assert len(urls) == 2
    assert any("唐朝" in url for url in urls)
    assert any("长城" in url for url in urls)


def test_adjust_search_mode_promotes_auto_for_unparsed_reference_urls():
    user_text = "根据以下参考链接制作 PPT：https://zh.wikipedia.org/wiki/唐朝"
    derived = {"search_mode": "no_search", "source_type": "outline", "research_depth": "L1"}
    inputs = {"has_documents": False, "doc_parse_ok": False}

    adjusted = rc._adjust_search_mode_for_reference_urls(inputs, user_text, derived)

    assert adjusted["search_mode"] == "auto"


def test_adjust_search_mode_keeps_no_search_when_user_explicitly_disables_search():
    user_text = "不要搜索，参考 https://example.com/article 制作"
    derived = {"search_mode": "no_search", "source_type": "topic", "research_depth": "L1"}
    inputs = {"has_documents": False, "doc_parse_ok": False}

    adjusted = rc._adjust_search_mode_for_reference_urls(inputs, user_text, derived)

    assert adjusted["search_mode"] == "no_search"


def test_adjust_search_mode_keeps_no_search_when_local_docs_parsed():
    user_text = "根据参考链接和上传文档：https://example.com/article"
    derived = {"search_mode": "no_search", "source_type": "topic", "research_depth": "L1"}
    inputs = {"has_documents": True, "doc_parse_ok": True}

    adjusted = rc._adjust_search_mode_for_reference_urls(inputs, user_text, derived)

    assert adjusted["search_mode"] == "no_search"
