# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""content_cache 单元测试（per-agent 隔离版）。

覆盖：normalize_url、WebContentCache put/get、覆盖保护、stats、AgentCacheRegistry。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from jiuwenswarm.agents.harness.common.tools.web_search.content_cache import (
    AgentCacheRegistry,
    CacheEntry,
    WebContentCache,
    normalize_url,
    parse_update_time,
    reset_registry_for_tests,
    get_agent_cache_registry,
)


def _run(coro):
    return asyncio.run(coro)


class TestNormalizeUrl:
    def test_strips_fragment(self):
        assert normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_lowercases_host(self):
        assert normalize_url("https://Example.COM/Path") == "https://example.com/Path"

    def test_strips_trailing_slash(self):
        assert normalize_url("https://example.com/path/") == "https://example.com/path"

    def test_keeps_root_slash(self):
        assert normalize_url("https://example.com/") == "https://example.com/"

    def test_keeps_query(self):
        assert normalize_url("https://example.com/path?a=1&b=2") == "https://example.com/path?a=1&b=2"

    def test_empty_returns_empty(self):
        assert normalize_url("") == ""
        assert normalize_url("   ") == ""


class TestWebContentCachePutGet:
    def test_put_then_get_returns_entry(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/test", content="hello", update_time=1700000000.0, title="T", source="paid:petal")))
        entry = _run(cache.get("https://example.com/test"))
        assert entry is not None
        assert entry.content == "hello"
        assert entry.update_time == 1700000000.0
        assert entry.source == "paid:petal"

    def test_get_miss_returns_none(self):
        cache = WebContentCache()
        entry = _run(cache.get("https://example.com/nonexistent"))
        assert entry is None

    def test_put_empty_content_skipped(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/empty", content="", update_time=1700000000.0)))
        entry = _run(cache.get("https://example.com/empty"))
        assert entry is None

    def test_get_returns_without_update_time(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/nout", content="c", update_time=None)))
        entry = _run(cache.get("https://example.com/nout"))
        assert entry is not None
        assert entry.update_time is None


class TestOverwriteProtection:
    def test_overwrites_when_newer_content(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/ow", content="c1", update_time=1700000000.0, title="T1")))
        _run(cache.put(CacheEntry(url="https://example.com/ow", content="c2", update_time=1700000001.0, title="T2")))
        entry = _run(cache.get("https://example.com/ow"))
        assert entry.content == "c2"
        assert entry.update_time == 1700000001.0

    def test_preserves_old_update_time_when_new_is_none(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/preserve", content="c1", update_time=1700000000.0, title="T1")))
        _run(cache.put(CacheEntry(url="https://example.com/preserve", content="c2", update_time=None, title="T2")))
        entry = _run(cache.get("https://example.com/preserve"))
        assert entry.content == "c2"
        assert entry.update_time == 1700000000.0


class TestCacheStats:
    def test_hits_misses_counted(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/s1", content="c1", update_time=1700000000.0)))
        _run(cache.get("https://example.com/s1"))
        _run(cache.get("https://example.com/miss"))
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["bypassed"] == 0
        assert stats["total"] == 2

    def test_bypass_counted(self):
        cache = WebContentCache()
        cache.bypassed += 1
        cache.bypassed += 1
        stats = cache.stats()
        assert stats["bypassed"] == 2

    def test_hit_rate_pct(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/hr", content="c", update_time=1700000000.0)))
        _run(cache.get("https://example.com/hr"))
        _run(cache.get("https://example.com/miss1"))
        _run(cache.get("https://example.com/miss2"))
        cache.bypassed += 1
        stats = cache.stats()
        assert stats["hit_rate_pct"] == 25.0

    def test_clear_resets(self):
        cache = WebContentCache()
        _run(cache.put(CacheEntry(url="https://example.com/c", content="c", update_time=1700000000.0)))
        _run(cache.get("https://example.com/c"))
        _run(cache.clear())
        stats = cache.stats()
        assert stats["hits"] == 0
        assert stats["entries"] == 0


class TestAgentCacheRegistry:
    def setup_method(self):
        reset_registry_for_tests()

    def test_get_cache_returns_per_agent_isolated(self):
        registry = get_agent_cache_registry()
        cache_a = _run(registry.get_cache("agent_a"))
        cache_b = _run(registry.get_cache("agent_b"))
        assert cache_a is not cache_b

        _run(cache_a.put(CacheEntry(url="https://example.com/iso", content="from_a", update_time=1700000000.0)))
        entry_b = _run(cache_b.get("https://example.com/iso"))
        assert entry_b is None  # agent B 看不到 agent A 的缓存

    def test_same_agent_returns_same_cache(self):
        registry = get_agent_cache_registry()
        cache1 = _run(registry.get_cache("agent_x"))
        cache2 = _run(registry.get_cache("agent_x"))
        assert cache1 is cache2

    def test_default_agent_id(self):
        registry = get_agent_cache_registry()
        cache = _run(registry.get_cache(""))
        assert cache is not None

    def test_stats(self):
        registry = get_agent_cache_registry()
        _run(registry.get_cache("agent1"))
        _run(registry.get_cache("agent2"))
        stats = registry.stats()
        assert stats["agents"] == 2
        assert "agent1" in stats["agent_ids"]
        assert "agent2" in stats["agent_ids"]


class TestParseUpdateTime:
    def test_parse_int_epoch(self):
        assert parse_update_time(1700000000) == 1700000000.0

    def test_parse_iso8601(self):
        result = parse_update_time("2026-08-19T12:00:00")
        assert result is not None
        assert result > 0

    def test_parse_date_only(self):
        result = parse_update_time("2026-08-19")
        assert result is not None

    def test_parse_none_returns_none(self):
        assert parse_update_time(None) is None

    def test_parse_empty_string_returns_none(self):
        assert parse_update_time("") is None

    def test_parse_invalid_returns_none(self):
        assert parse_update_time("not a date") is None

    def test_parse_digit_string_as_epoch(self):
        assert parse_update_time("1700000000") == 1700000000.0
