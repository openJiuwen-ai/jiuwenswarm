# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline unit tests for trafilatura article extraction inside mcp_fetch_webpage."""

from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.common.tools import web_fetch_tools as fetch

_ARTICLE_URL = "https://docs.invalid/guides/architecture"
_RICH_ARTICLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Swarm Design Guide</title></head>
<body>
  <header>
    <nav>Home | Docs | Pricing | Sign In | Nav Noise Menu Item</nav>
  </header>
  <aside>Sponsored Sidebar Banner Noise</aside>
  <script>var tracking = "noise_script_payload";</script>
  <main>
    <article>
      <h1>Swarm Architecture Overview</h1>
      <p>The runtime coordinates agents through deterministic capability routing and
      isolated sandbox workers for reliable multi-step execution.</p>
      <p>Structured Markdown extraction preserves headings and tables while dropping
      repetitive site navigation menus and footer boilerplate.</p>
      <table>
        <thead><tr><th>Layer</th><th>Responsibility</th></tr></thead>
        <tbody>
          <tr><td>Harness</td><td>Tool registration and permission checks</td></tr>
          <tr><td>Gateway</td><td>WebSocket transport and session routing</td></tr>
        </tbody>
      </table>
    </article>
  </main>
  <footer>Legal Notice: All rights reserved boilerplate footer text.</footer>
</body>
</html>
"""


def _mock_response(
    url: str,
    body: str,
    *,
    content_type: str = "text/html; charset=utf-8",
    status_code: int = 200,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.url = url
    resp.headers = {"Content-Type": content_type}
    resp.content = body.encode("utf-8")
    resp.encoding = "utf-8"
    resp.apparent_encoding = "utf-8"
    resp.is_redirect = False
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_trafilatura_extracts_clean_markdown_and_strips_noise(monkeypatch) -> None:
    pytest.importorskip("trafilatura")
    resp = _mock_response(_ARTICLE_URL, _RICH_ARTICLE_HTML)
    monkeypatch.setattr(fetch, "fetch_response", lambda *a, **kw: resp)

    result = await fetch.mcp_fetch_webpage.invoke({"url": _ARTICLE_URL})

    assert "Status: 200" in result
    assert "Title: Swarm Design Guide" in result
    assert "Swarm Architecture Overview" in result
    assert "Harness" in result
    assert "Gateway" in result
    assert "Nav Noise Menu Item" not in result
    assert "Sponsored Sidebar Banner Noise" not in result
    assert "noise_script_payload" not in result
    assert "boilerplate footer text" not in result.lower()


@pytest.mark.asyncio
async def test_trafilatura_none_falls_back_to_strip_tags(monkeypatch) -> None:
    spa_html = (
        "<html><head><title>App Shell</title></head>"
        "<body><script>window.__INIT__=1;</script>"
        "<div id='root'>Loading workspace status</div></body></html>"
    )
    resp = _mock_response(_ARTICLE_URL, spa_html)
    monkeypatch.setattr(fetch, "fetch_response", lambda *a, **kw: resp)
    monkeypatch.setattr(
        fetch, "_extract_html_with_trafilatura", lambda raw, url: ("", "")
    )

    result = await fetch.mcp_fetch_webpage.invoke({"url": _ARTICLE_URL})

    assert "Title: App Shell" in result
    assert "Loading workspace status" in result
    assert "window.__INIT__" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["import_error", "runtime_error"])
async def test_trafilatura_errors_fall_back_gracefully(monkeypatch, mode: str) -> None:
    html = (
        "<html><head><title>Fallback Page</title></head>"
        "<body><style>.x{color:red}</style><p>Readable fallback text</p></body></html>"
    )
    resp = _mock_response(_ARTICLE_URL, html)
    monkeypatch.setattr(fetch, "fetch_response", lambda *a, **kw: resp)

    if mode == "import_error":
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "trafilatura":
                raise ImportError("trafilatura missing")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.delitem(sys.modules, "trafilatura", raising=False)
        monkeypatch.setattr(builtins, "__import__", fake_import)
    else:
        bad_mod = SimpleNamespace(
            extract=MagicMock(side_effect=RuntimeError("boom")),
            extract_metadata=MagicMock(),
        )
        monkeypatch.setitem(sys.modules, "trafilatura", bad_mod)

    result = await fetch.mcp_fetch_webpage.invoke({"url": _ARTICLE_URL})

    assert "Title: Fallback Page" in result
    assert "Readable fallback text" in result
    assert "color:red" not in result


@pytest.mark.asyncio
async def test_trafilatura_backfills_missing_title_from_metadata(monkeypatch) -> None:
    no_title_html = (
        "<html><body><article><h1>Metadata Article Title</h1>"
        "<p>Main body paragraph kept intact.</p></article></body></html>"
    )
    resp = _mock_response(_ARTICLE_URL, no_title_html)
    monkeypatch.setattr(fetch, "fetch_response", lambda *a, **kw: resp)
    monkeypatch.setattr(
        fetch,
        "_extract_html_with_trafilatura",
        lambda raw, url: ("# Metadata Article Title\n\nMain body paragraph kept intact.",
                          "Resolved Meta Title"),
    )

    result = await fetch.mcp_fetch_webpage.invoke({"url": _ARTICLE_URL})

    assert "Title: Resolved Meta Title" in result
    assert "# Metadata Article Title" in result


@pytest.mark.asyncio
async def test_non_html_content_bypasses_trafilatura(monkeypatch) -> None:
    resp = _mock_response(
        "https://docs.invalid/data.json",
        '{\n  "ok": true,\n  "items": [1, 2]\n}',
        content_type="application/json",
    )
    monkeypatch.setattr(fetch, "fetch_response", lambda *a, **kw: resp)
    monkeypatch.setattr(
        fetch,
        "_extract_html_with_trafilatura",
        MagicMock(side_effect=AssertionError("must not call trafilatura on json")),
    )

    result = await fetch.mcp_fetch_webpage.invoke({"url": "https://docs.invalid/data.json"})

    assert "Status: 200" in result
    assert '"ok": true' in result


@pytest.mark.asyncio
async def test_max_chars_clips_markdown_and_schema_unchanged(monkeypatch) -> None:
    resp = _mock_response(_ARTICLE_URL, _RICH_ARTICLE_HTML)
    monkeypatch.setattr(fetch, "fetch_response", lambda *a, **kw: resp)

    props = set(fetch.mcp_fetch_webpage.card.input_params["properties"])
    assert props == {"url", "max_chars", "timeout_seconds"}

    result = await fetch.mcp_fetch_webpage.invoke({"url": _ARTICLE_URL, "max_chars": 60})

    assert "...[truncated]" in result


@pytest.mark.asyncio
async def test_trafilatura_metadata_error_keeps_extracted_body(monkeypatch) -> None:
    no_title_html = (
        "<html><body><article><h1>Body Title</h1>"
        "<p>Main text.</p></article></body></html>"
    )
    resp = _mock_response(_ARTICLE_URL, no_title_html)
    monkeypatch.setattr(fetch, "fetch_response", lambda *a, **kw: resp)
    stub_mod = SimpleNamespace(
        extract=MagicMock(return_value="# Body Title\n\nMain text."),
        extract_metadata=MagicMock(side_effect=RuntimeError("meta fail")),
    )
    monkeypatch.setitem(sys.modules, "trafilatura", stub_mod)

    result = await fetch.mcp_fetch_webpage.invoke({"url": _ARTICLE_URL})

    assert "# Body Title" in result
    assert "Main text." in result
