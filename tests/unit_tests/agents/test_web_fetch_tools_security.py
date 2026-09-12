# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Smart-only URL constraints; all HTTP is stubbed, with no external access.

TEST ONLY: .invalid names and loopback/metadata literals are offline fixtures.
"""

import asyncio
from types import SimpleNamespace
from urllib.parse import quote
from unittest.mock import Mock

import pytest
import requests

from jiuwenswarm.agents.harness.common.tools import web_fetch_tools as fetch
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    PUBLIC_HTTPS_FETCH_CONTEXT_ATTR,
)

URL = "https://news.invalid/story"
SMART = SimpleNamespace(**{PUBLIC_HTTPS_FETCH_CONTEXT_ATTR: True})


@pytest.fixture
def http(monkeypatch):
    def reply(url, **kwargs):
        response = requests.Response()
        response.url = url
        response.status_code = 200
        response._content = b"page body"
        response.encoding = "utf-8"
        response.close = Mock()
        return response
    stub = Mock(side_effect=reply)
    monkeypatch.setattr(fetch, "_http_get", stub)
    return stub


async def invoke(url, *, smart=True, **inputs):
    return await fetch.mcp_fetch_webpage.invoke(
        {"url": url, **inputs}, _tool_callback_context=SMART if smart else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "http://news.invalid/a", "https://127.0.0.1/a", "https://169.254.169.254/a",
    "https://metadata.google.internal/a", "https://user@news.invalid/a",
    "https://news.invalid:99999/a", "https://news.invalid/a?token=test-only",
])
@pytest.mark.parametrize("stage", ["input", "decoded", "redirect"])
async def test_ineligible_targets_are_not_requested(http, bad, stage):
    url = bad
    if stage == "decoded":
        url = "https://search.invalid/l/?uddg=" + quote(bad, safe="")
    elif stage == "redirect":
        url = URL
        response = http.side_effect(url)
        response.status_code = 302
        response.headers["Location"] = bad
        http.side_effect = lambda *args, **kwargs: response
    assert "[ERROR]" in await invoke(url)
    assert http.call_count == (1 if stage == "redirect" else 0)
    if stage == "redirect":
        response.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("url", [URL, "news.invalid/story", "https://search.invalid/l/?uddg=" + quote(URL, safe="")])
async def test_public_normalization_and_mode_compatibility(http, smart, url):
    assert "page body" in await invoke(url, smart=smart)
    assert http.call_args.args == (URL,)
    assert http.call_args.kwargs.get("allow_redirects") is (False if smart else None)


@pytest.mark.asyncio
async def test_manual_private_ddg_target_keeps_develop_behavior(http):
    target = "http://127.0.0.1/probe"
    assert "page body" in await invoke("https://search.invalid/l/?uddg=" + quote(target, safe=""), smart=False)
    assert http.call_args.args == (target,)
    assert "allow_redirects" not in http.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_jina_fallback_keeps_existing_provider(http, smart, status):
    reply = http.side_effect
    def responses(url, **kwargs):
        response = reply(url)
        response.status_code = status if url == URL else 200
        return response
    http.side_effect = responses
    assert "page body" in await invoke(URL, smart=smart)
    assert [call.args[0] for call in http.call_args_list] == [URL, "https://r.jina.ai/" + URL]
    assert all(call.kwargs.get("allow_redirects") is (False if smart else None) for call in http.call_args_list)


@pytest.mark.asyncio
async def test_smart_cross_domain_redirect_and_requests_limit(http):
    response = http.side_effect(URL)
    response.status_code = 302
    response.headers["Location"] = "https://cdn.invalid/content"
    final = http.side_effect(response.headers["Location"])
    http.side_effect = [response, final]
    assert "page body" in await invoke(URL)
    response.close.assert_called_once()
    http.reset_mock(side_effect=True)
    http.side_effect = lambda *args, **kwargs: response
    assert "redirect limit" in await invoke(URL)
    assert http.call_count == requests.models.DEFAULT_REDIRECT_LIMIT + 1


@pytest.mark.asyncio
async def test_jina_redirect_is_checked_before_request(http):
    denied = http.side_effect(URL)
    denied.status_code = 403
    reader = http.side_effect("https://r.jina.ai/" + URL)
    reader.status_code = 302
    reader.headers["Location"] = "http://127.0.0.1/probe"
    http.side_effect = [denied, reader]
    assert "network_scheme_not_https" in await invoke(URL)
    assert http.call_count == 2
    reader.close.assert_called_once()


@pytest.mark.asyncio
async def test_concurrent_modes_and_no_llm_mode_parameter(http):
    results = await asyncio.gather(invoke(URL), invoke(URL, smart=False))
    assert all("page body" in result for result in results)
    assert sorted("allow_redirects" in call.kwargs for call in http.call_args_list) == [False, True]
    assert not fetch._PUBLIC_HTTPS_FETCH.get()
    assert set(fetch.mcp_fetch_webpage.card.input_params["properties"]) == {"url", "max_chars", "timeout_seconds"}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("offline failure"), asyncio.CancelledError()])
async def test_error_and_cancellation_restore_context(monkeypatch, http, error):
    async def fail(*args, **kwargs):
        assert fetch._PUBLIC_HTTPS_FETCH.get()
        raise error
    with monkeypatch.context() as patch:
        patch.setattr(fetch.asyncio, "to_thread", fail)
        if isinstance(error, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await invoke(URL)
        else:
            assert "offline failure" in await invoke(URL)
    assert not fetch._PUBLIC_HTTPS_FETCH.get()
    assert "page body" in await invoke("http://news.invalid/a", smart=False)
