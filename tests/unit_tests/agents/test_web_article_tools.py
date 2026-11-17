# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the web article reader tool (read_web_article)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from jiuwenswarm.agents.harness.common.tools.web_article_tools import read_web_article

SAMPLE_ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Deep Dive into Autonomous Agents</title>
    <meta name="author" content="Dr. Jane Doe">
    <meta name="date" content="2026-05-15">
</head>
<body>
    <nav><ul><li>Home</li><li>Blog</li><li>About</li></ul></nav>
    <article>
        <h1>Deep Dive into Autonomous Agents</h1>
        <p class="byline">By Dr. Jane Doe | May 15, 2026</p>
        <p>Autonomous multi-agent systems represent a major paradigm shift in AI development.</p>
        <p>Agents collaborate through specialized toolkits, workspaces, and shared channels.</p>
        <table>
            <tr><th>Component</th><th>Purpose</th></tr>
            <tr><td>Harness</td><td>Tool execution sandbox</td></tr>
            <tr><td>Swarm</td><td>Multi-agent orchestration</td></tr>
        </table>
    </article>
    <div class="sidebar">Advertisements and promotional text</div>
    <footer>Copyright 2026 Tech Weekly</footer>
</body>
</html>
"""


def _make_mock_response(html: str) -> MagicMock:
    """Helper to create a well-formed mock requests.Response."""
    mock_resp = MagicMock()
    mock_resp.content = html.encode("utf-8")
    mock_resp.text = html
    mock_resp.encoding = "utf-8"
    mock_resp.apparent_encoding = "utf-8"
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


@pytest.mark.asyncio
async def test_read_web_article_success() -> None:
    """Verify clean Markdown extraction from an HTML article."""
    mock_resp = _make_mock_response(SAMPLE_ARTICLE_HTML)

    with patch(
        "jiuwenswarm.agents.harness.common.tools.web_article_tools.fetch_response",
        return_value=mock_resp,
    ) as mock_fetch:
        result = await read_web_article.invoke({"url": "https://example.com/blog/deep-dive"})
        assert mock_fetch.call_args[1].get("public_https") is True

    assert "Source URL: https://example.com/blog/deep-dive" in result
    assert "Autonomous multi-agent systems" in result
    assert "Advertisements and promotional text" not in result


@pytest.mark.asyncio
async def test_read_web_article_empty_body() -> None:
    """Verify handling when webpage returns empty body."""
    mock_resp = _make_mock_response("   ")

    with patch(
        "jiuwenswarm.agents.harness.common.tools.web_article_tools.fetch_response",
        return_value=mock_resp,
    ):
        result = await read_web_article.invoke({"url": "https://example.com/empty"})

    assert "returned empty body" in result


@pytest.mark.asyncio
async def test_read_web_article_no_article_extracted() -> None:
    """Verify fallback message when trafilatura finds no main article content."""
    mock_resp = _make_mock_response("<html><body><script>var x = 1;</script></body></html>")

    with patch(
        "jiuwenswarm.agents.harness.common.tools.web_article_tools.fetch_response",
        return_value=mock_resp,
    ):
        result = await read_web_article.invoke({"url": "https://example.com/spa-app"})

    assert "Trafilatura could not extract article text" in result
    assert "mcp_fetch_webpage" in result


@pytest.mark.asyncio
async def test_read_web_article_truncation() -> None:
    """Verify max_chars truncation behavior."""
    long_html = (
        "<html><body><article><p>"
        + ("Detailed content paragraph. " * 200)
        + "</p></article></body></html>"
    )
    mock_resp = _make_mock_response(long_html)

    with patch(
        "jiuwenswarm.agents.harness.common.tools.web_article_tools.fetch_response",
        return_value=mock_resp,
    ):
        result = await read_web_article.invoke({
            "url": "https://example.com/long-post",
            "max_chars": 1000,
        })

    assert "[Article truncated at 1000 characters" in result


@pytest.mark.asyncio
async def test_read_web_article_invalid_url_and_errors() -> None:
    """Verify error reporting for invalid schemes and fetch failures."""
    # Invalid scheme
    res_bad_url = await read_web_article.invoke({"url": "ftp://files.example.com/doc"})
    assert "[ERROR]: read_web_article failed:" in res_bad_url
    assert "URL must start with http:// or https://" in res_bad_url

    # Empty URL
    res_empty = await read_web_article.invoke({"url": ""})
    assert "[ERROR]: read_web_article failed:" in res_empty
    assert "URL cannot be empty" in res_empty

    # Network failure
    with patch(
        "jiuwenswarm.agents.harness.common.tools.web_article_tools.fetch_response",
        side_effect=ConnectionError("Host unreachable"),
    ):
        res_fail = await read_web_article.invoke({"url": "https://broken.domain.com"})
        assert "[ERROR]: read_web_article failed:" in res_fail
        assert "Host unreachable" in res_fail


@pytest.mark.asyncio
async def test_read_web_article_ssrf_protection() -> None:
    """Verify strict rejection of internal IPs and cloud metadata across schemes."""
    # Cloud metadata on HTTP
    res_meta = await read_web_article.invoke({"url": "http://169.254.169.254/latest/meta-data/"})
    assert "[ERROR]: read_web_article failed:" in res_meta
    assert "URL failed safety check" in res_meta

    # Loopback on HTTP
    res_loopback = await read_web_article.invoke({"url": "http://127.0.0.1:8080/secret"})
    assert "[ERROR]: read_web_article failed:" in res_loopback
    assert "URL failed safety check" in res_loopback

    # Private IP on HTTPS
    res_private = await read_web_article.invoke({"url": "https://10.0.0.1/admin"})
    assert "[ERROR]: read_web_article failed:" in res_private
    assert "URL failed safety check" in res_private


@pytest.mark.asyncio
async def test_read_web_article_timeout_and_options() -> None:
    """Verify custom timeout and comment options are respected."""
    mock_resp = _make_mock_response(
        "<html><body><article><p>Fast response.</p></article></body></html>"
    )

    with patch(
        "jiuwenswarm.agents.harness.common.tools.web_article_tools.fetch_response",
        return_value=mock_resp,
    ) as mock_fetch:
        result = await read_web_article.invoke({
            "url": "https://example.com/custom-timeout",
            "timeout_seconds": 25,
            "include_comments": True,
        })
        assert mock_fetch.call_args[1].get("timeout_seconds") == 25
        assert mock_fetch.call_args[1].get("public_https") is True
        assert "Fast response." in result
