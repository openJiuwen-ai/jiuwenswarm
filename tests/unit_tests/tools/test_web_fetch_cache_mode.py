# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""fetch 双模式（use_cache）单元测试（per-agent cache + 多 URL 并行版）。

通过 mcp_fetch_webpage_impl 直接调用，cache 作为参数传入（绕过 @tool 的 pydantic 复制）。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import (
    CacheEntry,
    WebContentCache,
)


def _run(coro):
    return asyncio.run(coro)


def _make_cache_with_entry(url: str, content: str, update_time=None) -> WebContentCache:
    cache = WebContentCache()
    _run(cache.put(CacheEntry(url=url, content=content, update_time=update_time, title="T", source="paid:petal")))
    return cache


def _first(result):
    assert isinstance(result, list)
    assert result, "expected at least one result item"
    return result[0]


def _invoke(url, cache=None, use_cache=True):
    from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import mcp_fetch_webpage_impl
    return _run(mcp_fetch_webpage_impl(url=url, use_cache=use_cache, cache=cache))


class TestFetchCacheHit:
    def test_cache_hit_returns_content_and_metadata(self):
        cache = _make_cache_with_entry("https://example.com/cached", "cached content body", time.time() - 86400)
        result = _invoke("https://example.com/cached", cache=cache)
        item = _first(result)
        assert item["from_cache"] is True
        assert "cached content body" in item["content"]
        assert item.get("cache_age_days") is not None
        assert item.get("page_update_days") is not None

    def test_cache_hit_no_update_time_shows_none(self):
        cache = _make_cache_with_entry("https://example.com/nout", "content", update_time=None)
        result = _invoke("https://example.com/nout", cache=cache)
        item = _first(result)
        assert item["from_cache"] is True
        assert item.get("page_update_days") is None


class TestFetchCacheMiss:
    def test_cache_miss_falls_back_to_network(self):
        cache = WebContentCache()
        mock_data = {"url": "https://example.com/live", "status_code": 200, "title": "Live", "content": "live content"}
        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", return_value=mock_data):
            result = _invoke("https://example.com/live", cache=cache)
        item = _first(result)
        assert item["from_cache"] is False
        assert "live content" in item["content"]
        assert item["provider"] == "direct"
        stats = cache.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 1


class TestUseCacheFalse:
    def test_use_cache_false_bypasses_cache(self):
        cache = _make_cache_with_entry("https://example.com/bypass", "cached content", time.time() - 3600)
        mock_data = {"url": "https://example.com/bypass", "status_code": 200, "title": "Live", "content": "fresh content"}
        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", return_value=mock_data):
            result = _invoke("https://example.com/bypass", cache=cache, use_cache=False)
        item = _first(result)
        assert item["from_cache"] is False
        assert "fresh content" in item["content"]
        stats = cache.stats()
        assert stats["bypassed"] == 1
        assert stats["hits"] == 0


class TestFetchNoWriteback:
    def test_network_fetch_does_not_write_cache(self):
        cache = WebContentCache()
        mock_data = {"url": "https://example.com/nowrite", "status_code": 200, "title": "NW", "content": "should not be cached"}
        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", return_value=mock_data):
            _invoke("https://example.com/nowrite", cache=cache)
        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", return_value=mock_data):
            _invoke("https://example.com/nowrite", cache=cache)
        stats = cache.stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 2


class TestFetchNoCache:
    def test_no_cache_still_works(self):
        mock_data = {"url": "https://example.com/nocache", "status_code": 200, "title": "NC", "content": "no cache content"}
        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", return_value=mock_data):
            result = _invoke("https://example.com/nocache")
        item = _first(result)
        assert item["from_cache"] is False
        assert "no cache content" in item["content"]


class TestFetchMultipleUrls:
    def test_multiple_urls_returns_one_item_per_url_in_order(self):
        urls = ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
        contents = {"a": "content A", "b": "content B", "c": "content C"}

        def fake_fetch(url, timeout_seconds):
            key = url.rsplit("/", 1)[-1]
            return {"url": url, "status_code": 200, "title": "", "content": contents.get(key, "")}

        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", side_effect=fake_fetch):
            result = _invoke(urls)

        assert isinstance(result, list)
        assert len(result) == 3
        for item, url in zip(result, urls):
            assert item["url"] == url
            assert item["from_cache"] is False

    def test_multiple_urls_partial_failure_keeps_error_item(self):
        good = "https://example.com/ok"
        bad = "https://example.com/bad"

        def fake_fetch(url, timeout_seconds):
            if url == bad:
                raise RuntimeError("boom")
            return {"url": url, "status_code": 200, "title": "", "content": "ok content"}

        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", side_effect=fake_fetch):
            result = _invoke([good, bad])

        assert isinstance(result, list)
        assert len(result) == 2
        ok_item, bad_item = result
        assert ok_item["url"] == good
        assert "ok content" in ok_item["content"]
        assert "error" not in ok_item
        assert bad_item["url"] == bad
        assert bad_item.get("error")
        assert bad_item["content"] == ""

    def test_empty_url_list_returns_empty_results(self):
        result = _invoke([])
        assert result == []

    def test_single_url_string_still_works(self):
        mock_data = {"url": "https://example.com/single", "status_code": 200, "title": "S", "content": "single content"}
        with patch("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_webpage_sync", return_value=mock_data):
            result = _invoke("https://example.com/single")
        item = _first(result)
        assert item["url"] == "https://example.com/single"
        assert "single content" in item["content"]
