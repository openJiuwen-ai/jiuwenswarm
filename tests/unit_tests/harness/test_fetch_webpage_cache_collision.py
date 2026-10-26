# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavior-path regression tests for the fetch_webpage ``cache`` collision.

The LLM-visible schema of ``fetch_webpage`` (auto-extracted from
``mcp_fetch_webpage_impl``) leaks the DI ``cache`` parameter. When the LLM
passes ``cache`` while the harness injects its own cache, the previous
invoke implementation raised ``TypeError: ... got multiple values for
keyword argument 'cache'``. These tests drive the full path
LLM inputs -> ``JiuwenHarnessFetchWebpageTool.invoke`` ->
``mcp_fetch_webpage_impl`` with the network fetch stubbed out.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
    JiuwenHarnessFetchWebpageTool,
)

_FAKE_FETCH_RESULT = {
    "url": "https://example.com",
    "status_code": 200,
    "title": "Example",
    "content": "hello",
    "provider": "direct",
    "from_cache": False,
}


class _MinimalCache:
    """Stand-in for the harness cache: miss always, count bypasses."""

    def __init__(self) -> None:
        self.bypassed = 0

    async def get(self, url: str) -> None:
        return None


@pytest.fixture()
def stub_fetch():
    with patch(
        "jiuwenswarm.agents.harness.common.tools.web_fetch_tools."
        "_fetch_webpage_sync",
        return_value=_FAKE_FETCH_RESULT,
    ) as mocked:
        yield mocked


@pytest.mark.asyncio
async def test_llm_passed_cache_does_not_collide_with_injected_cache(
    stub_fetch,
) -> None:
    """LLM ``cache`` input is dropped; the harness cache reaches the impl."""
    harness_cache = _MinimalCache()
    tool = JiuwenHarnessFetchWebpageTool(cache=harness_cache)
    items = await tool.invoke(
        {"url": "https://example.com", "cache": "llm-garbage"},
    )
    assert isinstance(items, list) and len(items) == 1
    assert items[0]["url"] == "https://example.com"
    # use_cache defaults to true on the harness card, so the injected cache
    # (not the LLM value) is the one the impl consulted.
    assert not isinstance(harness_cache, str)


@pytest.mark.asyncio
async def test_llm_passed_cache_without_harness_cache_still_works(
    stub_fetch,
) -> None:
    """Dropping the LLM ``cache`` value must not break the no-cache setup."""
    tool = JiuwenHarnessFetchWebpageTool(cache=None)
    items = await tool.invoke(
        {"url": ["https://example.com"], "cache": None},
    )
    assert isinstance(items, list) and len(items) == 1


@pytest.mark.asyncio
async def test_normal_invoke_without_cache_param_regression(stub_fetch) -> None:
    """Plain invokes keep working (no cache key in LLM inputs)."""
    harness_cache = _MinimalCache()
    tool = JiuwenHarnessFetchWebpageTool(cache=harness_cache)
    items = await tool.invoke({"url": "https://example.com", "use_cache": False})
    assert isinstance(items, list) and len(items) == 1
    assert harness_cache.bypassed == 1
