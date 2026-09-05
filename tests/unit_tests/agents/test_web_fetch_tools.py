# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import requests

from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import (
    _classify_fetch_error,
    _fetch_via_jina_reader_sync,
    _github_search_api_url,
    mcp_fetch_webpage,
)


def _run(coro) -> str:
    return asyncio.run(coro)


def _call(url: str, max_chars: int = 0, timeout_seconds: int = 30) -> str:
    """调用 @tool 包装后的底层 async 函数(mcp_fetch_webpage._func)。"""
    return _run(mcp_fetch_webpage._func(url, max_chars=max_chars, timeout_seconds=timeout_seconds))


def _fake_response(
    body: bytes,
    *,
    status_code: int = 200,
    content_type: str = "text/html",
    url: str = "https://example.com/",
) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        headers={"Content-Type": content_type},
        content=body,
        encoding="utf-8",
        apparent_encoding="utf-8",
        url=url,
        raise_for_status=lambda: None,
    )


# ── _github_search_api_url ─────────────────────────────────────────


class TestGithubSearchApiUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            (
                "https://github.com/search?q=deepseek+harness&type=repositories",
                "https://api.github.com/search/repositories?q=deepseek%20harness&per_page=10",
            ),
            (
                "https://github.com/search?q=deepseek+harness",
                "https://api.github.com/search/repositories?q=deepseek%20harness&per_page=10",
            ),
            (
                "https://www.github.com/search?q=foo&type=repositories&per_page=5",
                "https://api.github.com/search/repositories?q=foo&per_page=5",
            ),
            # 中文/特殊字符必须 URL 编码
            (
                "https://github.com/search?q=量化+因子&type=repositories",
                "https://api.github.com/search/repositories?q=%E9%87%8F%E5%8C%96%20%E5%9B%A0%E5%AD%90&per_page=10",
            ),
        ],
    )
    def test_rewrites_repository_search(self, url: str, expected: str) -> None:
        assert _github_search_api_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/search?q=foo&type=issues",
            "https://github.com/search?q=foo&type=pullrequests",
            "https://github.com/search",  # 缺 q
            "https://example.com/search?q=foo&type=repositories",
            "https://github.com/notsearch?q=foo",
            "https://api.github.com/search/repositories?q=foo",
        ],
    )
    def test_does_not_rewrite_other_urls(self, url: str) -> None:
        assert _github_search_api_url(url) is None

    def test_per_page_clamped_to_30(self) -> None:
        url = "https://github.com/search?q=foo&type=repositories&per_page=999"
        assert _github_search_api_url(url) == (
            "https://api.github.com/search/repositories?q=foo&per_page=30"
        )


# ── mcp_fetch_webpage: GitHub search 重写生效 ─────────────────────


class TestFetchWebpageGithubRewrite:
    def test_github_search_goes_to_api(self, monkeypatch) -> None:
        captured: list[str] = []

        def fake_get(url, **kwargs) -> SimpleNamespace:
            captured.append(url)
            return _fake_response(
                b'{"total_count": 1, "items": [{"full_name": "a/b"}]}',
                content_type="application/json",
            )

        monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get", fake_get)
        result = _call("https://github.com/search?q=deepseek+harness&type=repositories")
        assert captured == ["https://api.github.com/search/repositories?q=deepseek%20harness&per_page=10"]
        assert "a/b" in result


# ── mcp_fetch_webpage: 错误分类 ────────────────────────────────────


class TestFetchErrorClassification:
    def test_timeout_returns_fetch_error_category(self, monkeypatch) -> None:
        def fail_get(url, **kwargs):
            raise requests.exceptions.Timeout("timed out")

        def fail_jina(url, timeout_seconds):
            raise requests.exceptions.Timeout("jina also timed out")

        monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get", fail_get)
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_via_jina_reader_sync",
            fail_jina,
        )
        result = _call("https://example.com/page")
        assert result.startswith("[FETCH_ERROR: timeout]")

    def test_connection_error_category(self, monkeypatch) -> None:
        def fail_get(url, **kwargs):
            raise requests.exceptions.ConnectionError("connection refused")

        monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get", fail_get)
        result = _call("https://example.com/page")
        assert result.startswith("[FETCH_ERROR: connection]")

    def test_http_error_category(self, monkeypatch) -> None:
        def bad_response(url, **kwargs) -> SimpleNamespace:
            response = _fake_response(b"nope", status_code=500)

            def raise_for_status():
                raise requests.exceptions.HTTPError("500 Server Error")

            response.raise_for_status = raise_for_status
            return response

        monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get", bad_response)
        result = _call("https://example.com/page")
        assert result.startswith("[FETCH_ERROR: http_error]")

    def test_classify_fetch_error_unknown(self) -> None:
        assert _classify_fetch_error(ValueError("boom")) == "unknown"


# ── mcp_fetch_webpage: 降级链 ──────────────────────────────────────


class TestFetchFallbackChain:
    def test_timeout_falls_back_to_jina_reader(self, monkeypatch) -> None:
        def fail_get(url, **kwargs):
            raise requests.exceptions.Timeout("timed out")

        monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get", fail_get)
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_via_jina_reader_sync",
            lambda url, timeout_seconds: {
                "url": url,
                "status_code": 200,
                "title": "",
                "content": "fallback content from jina",
            },
        )
        result = _call("https://example.com/page")
        assert "fallback content from jina" in result

    def test_http_403_falls_back_to_jina_reader(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get",
            lambda url, **kwargs: _fake_response(b"forbidden", status_code=403),
        )
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.web_fetch_tools._fetch_via_jina_reader_sync",
            lambda url, timeout_seconds: {
                "url": url,
                "status_code": 200,
                "title": "",
                "content": "jina rendered it",
            },
        )
        result = _call("https://example.com/page")
        assert "jina rendered it" in result


# ── mcp_fetch_webpage: 内容解析 ────────────────────────────────────


class TestFetchContentParsing:
    def test_html_strips_scripts_and_styles(self, monkeypatch) -> None:
        html = (
            b"<html><head><title>Page Title</title>"
            b"<script>var x = 1;</script><style>.a { color: red; }</style></head>"
            b"<body><h1>Hello</h1><p>World</p></body></html>"
        )
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get",
            lambda url, **kwargs: _fake_response(html),
        )
        result = _call("https://example.com/page")
        assert "Page Title" in result
        assert "Hello" in result
        assert "World" in result
        assert "var x" not in result
        assert "color: red" not in result

    def test_empty_page_returns_empty_marker(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get",
            lambda url, **kwargs: _fake_response(b""),
        )
        result = _call("https://example.com/empty")
        assert "[empty]" in result

    def test_empty_url_rejected(self) -> None:
        result = _call("")
        assert "url cannot be empty" in result

    def test_jina_reader_sync_passes_through(self, monkeypatch) -> None:
        def fake_get(url, **kwargs) -> SimpleNamespace:
            assert url == "https://r.jina.ai/https://example.com/page"
            return _fake_response(b"reader output", content_type="text/plain")

        monkeypatch.setattr("jiuwenswarm.agents.harness.common.tools.web_fetch_tools._http_get", fake_get)
        data = _fetch_via_jina_reader_sync("https://example.com/page", timeout_seconds=5)
        assert isinstance(data["content"], str)
        assert "reader output" in data["content"]
