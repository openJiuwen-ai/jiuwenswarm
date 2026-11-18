# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline unit tests for ``fetch_docs`` (uses ``httpx.MockTransport`` and ``.invalid``)."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import time
from typing import Callable

import httpx
import pytest
from openjiuwen.core.common.exception.errors import ValidationError

from jiuwenswarm.agents.harness.common.tools import fetch_docs_tools as fdt
from jiuwenswarm.agents.harness.common.tools.fetch_docs_tools import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PAGES,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    MAX_BYTES_PER_PAGE,
    _normalize_request,
    fetch_docs,
)
from jiuwenswarm.common import utils as common_utils

ROOT_URL = "https://docs.invalid/guide/"


def _set_dns_records(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, list[str]],
) -> None:
    """Install a fake ``socket.getaddrinfo`` with default public IP plus overrides."""
    records: dict[str, list[str]] = {"*": ["93.184.216.34"]}
    records.update(overrides)

    # pylint: disable-next=redefined-builtin,too-many-arguments,huawei-too-many-arguments
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        _ = (family, type, proto, flags)
        ips = records.get(host) or records.get("*") or ["93.184.216.34"]
        results = []
        for ip_str in ips:
            family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
            sockaddr = (
                (ip_str, port or 443, 0, 0)
                if family == socket.AF_INET6
                else (ip_str, port or 443)
            )
            results.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return results

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect workspace cache storage into ``tmp_path`` for every test."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common_utils, "get_agent_workspace_dir", lambda: workspace)


@pytest.fixture(autouse=True)
def _stub_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``socket.getaddrinfo`` to avoid real DNS in every test."""
    _set_dns_records(monkeypatch, {})


def _install_mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[str]:
    """Wire ``httpx.MockTransport`` into ``_build_http_client`` and log requested URLs."""
    requested_urls: list[str] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return handler(request)

    transport = httpx.MockTransport(recording_handler)
    real_builder = fdt._build_http_client  # pylint: disable=protected-access

    def fake_builder(timeout_seconds: int, *, transport: httpx.BaseTransport | None = None):
        _ = transport
        return real_builder(timeout_seconds, transport=transport_ref)

    transport_ref = transport
    monkeypatch.setattr(fdt, "_build_http_client", fake_builder)
    return requested_urls


# ---------------------------------------------------------------------------
# 1. Card schema & request normalization
# ---------------------------------------------------------------------------


def test_fetch_docs_card_schema_exposes_top_level_params() -> None:
    props = fetch_docs.card.input_params["properties"]
    required = fetch_docs.card.input_params.get("required", [])

    assert "inputs" not in props
    assert set(props) == {
        "url",
        "max_depth",
        "max_pages",
        "timeout_seconds",
        "max_chars",
        "refresh",
        "query",
        "ttl_seconds",
        "path_prefix",
    }
    assert "url" in required
    assert props["query"].get("nullable") is True
    assert props["path_prefix"].get("nullable") is True


def test_normalize_request_defaults_and_upper_clamping() -> None:
    req = _normalize_request({"url": "https://docs.invalid:443/guide/#intro"})
    assert req.url == "https://docs.invalid/guide/"
    assert req.max_depth == DEFAULT_MAX_DEPTH
    assert req.max_pages == DEFAULT_MAX_PAGES
    assert req.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert req.max_chars == DEFAULT_MAX_CHARS
    assert req.ttl_seconds == DEFAULT_TTL_SECONDS
    assert req.refresh is False
    assert req.query is None
    assert req.path_prefix is None

    clamped = _normalize_request(
        {
            "url": "https://docs.invalid/guide/start.html",
            "max_depth": 99,
            "max_pages": 0,
            "timeout_seconds": 1,
            "max_chars": 999_999,
            "ttl_seconds": -10,
            "refresh": "true",
            "query": "  auth  ",
            "path_prefix": "guide",
        }
    )
    assert clamped.max_depth == 3
    assert clamped.max_pages == 1
    assert clamped.timeout_seconds == 5
    assert clamped.max_chars == 100_000
    assert clamped.ttl_seconds == 0
    assert clamped.refresh is True
    assert clamped.query == "auth"
    assert clamped.path_prefix == "/guide"


def test_normalize_request_lower_clamping_and_bool_guard() -> None:
    low = _normalize_request(
        {
            "url": "https://docs.invalid/guide/",
            "max_depth": -5,
            "max_pages": 500,
            "timeout_seconds": 500,
            "max_chars": 10,
            "ttl_seconds": 9_999_999,
            "max_depth_unused": None,
        }
    )
    assert low.max_depth == 0
    assert low.max_pages == 30
    assert low.timeout_seconds == 60
    assert low.max_chars == 1_000
    assert low.ttl_seconds == 604_800

    bool_guard = _normalize_request({"url": "https://docs.invalid/", "max_depth": True})
    assert bool_guard.max_depth == DEFAULT_MAX_DEPTH


def test_normalize_request_rejects_invalid_path_prefix() -> None:
    with pytest.raises(ValueError, match="not a full URL"):
        _normalize_request(
            {"url": "https://docs.invalid/guide/", "path_prefix": "https://docs.invalid/guide/"}
        )

    with pytest.raises(ValueError, match="does not match path_prefix"):
        _normalize_request({"url": "https://docs.invalid/guide/", "path_prefix": "/api/"})


@pytest.mark.asyncio
async def test_fetch_docs_requires_url_in_tool_invocation() -> None:
    with pytest.raises(ValidationError, match="url"):
        await fetch_docs.invoke({})


def test_tools_package_lazy_exports_fetch_docs() -> None:
    # pylint: disable=import-outside-toplevel
    from jiuwenswarm.agents.harness.common import tools as common_tools

    assert "fetch_docs" in common_tools.__all__
    assert common_tools.fetch_docs.card.name == "fetch_docs"


# ---------------------------------------------------------------------------
# 2. HTML-to-Markdown extraction
# ---------------------------------------------------------------------------

_CHROME_SAMPLE_HTML = """
<!doctype html>
<html>
  <head>
    <title>Guide Title</title>
    <style>body { color: red; }</style>
    <script>console.log("secret script noise");</script>
  </head>
  <body>
    <nav>
      <span>Nav Menu Noise</span>
      <a href="/guide/install.html">Install</a>
    </nav>
    <aside>Sidebar Noise <a href="config.html">Config</a></aside>
    <main>
      <h1>Welcome &amp; Intro</h1>
      <p>Use <code>pip install httpx</code> with <strong>TLS</strong> &lt;1.3&gt;.</p>
      <h2>Steps</h2>
      <ul>
        <li>First item
          <ol>
            <li>Sub step one</li>
            <li>Sub step two</li>
          </ol>
        </li>
        <li>Read <a href="api.html#auth">API Reference</a></li>
      </ul>
      <blockquote>Important note here.</blockquote>
      <pre><code class="language-python">def hello():
    return "world"
</code></pre>
      <table>
        <tr><th>Option</th><th>Default</th></tr>
        <tr><td>timeout</td><td>15</td></tr>
      </table>
    </main>
    <footer>Footer Copyright Noise <a href="/guide/footer-only.html">Footer</a></footer>
  </body>
</html>
"""


def _make_chrome_sample_handler() -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == ROOT_URL:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                text=_CHROME_SAMPLE_HTML,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=f"<html><title>{url}</title><body><h1>Page</h1><p>Body {url}</p></body></html>",
        )

    return handler


@pytest.mark.asyncio
async def test_html_to_markdown_strips_chrome_and_preserves_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = _install_mock_http(monkeypatch, _make_chrome_sample_handler())
    result = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 1})

    for noise in ("Nav Menu Noise", "Sidebar Noise", "Footer Copyright Noise", "secret script"):
        assert noise not in result

    assert "https://docs.invalid/guide/install.html" in requested
    assert "https://docs.invalid/guide/config.html" in requested
    assert "https://docs.invalid/guide/api.html" in requested
    assert "https://docs.invalid/guide/footer-only.html" not in requested

    assert "# Welcome & Intro" in result
    assert "## Steps" in result
    assert "`pip install httpx`" in result
    assert "**TLS**" in result
    assert "<1.3>" in result
    assert "- First item" in result
    assert "  1. Sub step one" in result
    assert "[API Reference](https://docs.invalid/guide/api.html)" in result
    assert "> Important note here." in result
    assert '```python\ndef hello():\n    return "world"\n```' in result
    assert "| Option | Default |" in result
    assert "| timeout | 15 |" in result


@pytest.mark.asyncio
async def test_html_meta_charset_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    gbk_html = (
        '<html><head><meta charset="gb18030"><title>中文文档</title></head>'
        "<body><p>你好文档系统</p></body></html>"
    ).encode("gb18030")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=gbk_html)

    _install_mock_http(monkeypatch, handler)
    result = await fetch_docs.invoke({"url": "https://docs.invalid/cn.html", "max_depth": 0})
    assert "中文文档" in result
    assert "你好文档系统" in result


# ---------------------------------------------------------------------------
# 3. Markdown & plain-text passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markdown_and_text_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    md_body = (
        "# API Overview\n\n"
        "See [Auth Guide](./auth.md) for tokens.\n\n"
        "```bash\n# Not a heading\ncurl https://docs.invalid/ping\n```\n"
    )
    auth_md = "# Auth Guide\n\nPass bearer credentials via headers.\n"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("README.md"):
            return httpx.Response(200, headers={"Content-Type": "text/markdown"}, text=md_body)
        if url.endswith("auth.md"):
            return httpx.Response(200, headers={"Content-Type": "text/plain"}, text=auth_md)
        return httpx.Response(404)

    _install_mock_http(monkeypatch, handler)
    result = await fetch_docs.invoke({"url": "https://docs.invalid/docs/README.md", "max_depth": 1})

    assert "# API Overview" in result
    assert "# Auth Guide" in result
    assert "Pass bearer credentials via headers." in result
    assert "```bash\n# Not a heading\ncurl https://docs.invalid/ping\n```" in result
    assert "# Not a heading" not in result.split("## Index")[1].split("---")[0]


# ---------------------------------------------------------------------------
# 4. BFS layered crawling & boundary enforcement
# ---------------------------------------------------------------------------


_BFS_SAMPLE_PAGES = {
    "https://docs.invalid/guide/": (
        '<a href="/guide/d1.html">D1</a>'
        '<a href="/guide/d1.html#frag">D1 Frag</a>'
        '<a href="https://other.invalid/guide/x.html">Other Host</a>'
        '<a href="https://docs.invalid:8443/guide/port.html">Other Port</a>'
        '<a href="https://docs.invalid/blog/post.html">Sibling Prefix</a>'
        '<a href="https://docs.invalid/guide-v2/intro.html">Non-boundary</a>'
        '<a href="/guide/logo.png">PNG</a>'
        '<a href="mailto:hi@docs.invalid">Mail</a>'
        '<a href="javascript:void(0)">JS</a>'
    ),
    "https://docs.invalid/guide/d1.html": (
        '<a href="/guide/d2.html">D2</a>'
        '<a href="/guide/missing.html">Broken</a>'
    ),
    "https://docs.invalid/guide/d2.html": '<a href="/guide/d3.html">D3</a>',
    "https://docs.invalid/guide/d3.html": '<a href="/guide/d4.html">D4</a>',
    "https://docs.invalid/guide/d4.html": "<p>Depth 4 should never fetch</p>",
}


def _make_bfs_handler() -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in _BFS_SAMPLE_PAGES:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text=f"<html><title>{url}</title><body>{_BFS_SAMPLE_PAGES[url]}</body></html>",
            )
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_bfs_depth_zero_and_one_scope_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = _install_mock_http(monkeypatch, _make_bfs_handler())

    r0 = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0, "refresh": True})
    assert requested == [ROOT_URL]
    assert "pages=1/10" in r0
    assert "URL: https://docs.invalid/guide/d1.html" not in r0

    requested.clear()
    r1 = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 1, "refresh": True})
    assert ROOT_URL in requested
    assert "https://docs.invalid/guide/d1.html" in requested
    assert "https://docs.invalid/guide/d2.html" not in requested
    assert "https://other.invalid/guide/x.html" not in requested
    assert "https://docs.invalid:8443/guide/port.html" not in requested
    assert "https://docs.invalid/blog/post.html" not in requested
    assert "https://docs.invalid/guide-v2/intro.html" not in requested
    assert "https://docs.invalid/guide/logo.png" not in requested
    assert "Errors (1)" not in r1


@pytest.mark.asyncio
async def test_bfs_max_depth_clamping_and_max_pages_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = _install_mock_http(monkeypatch, _make_bfs_handler())

    r_max = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 99, "refresh": True})
    assert "https://docs.invalid/guide/d2.html" in requested
    assert "https://docs.invalid/guide/d3.html" in requested
    assert "https://docs.invalid/guide/d4.html" not in requested
    assert "Depth 4 should never fetch" not in r_max
    assert "Errors (1)" in r_max

    requested.clear()
    r_cap = await fetch_docs.invoke(
        {"url": ROOT_URL, "max_depth": 3, "max_pages": 2, "refresh": True}
    )
    assert len(requested) == 2
    assert "pages=2/2" in r_cap


# ---------------------------------------------------------------------------
# 5. Local cache, TTL, refresh=True, and query search
# ---------------------------------------------------------------------------


def _make_versioned_docs_handler() -> Callable[[httpx.Request], httpx.Response]:
    version = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != ROOT_URL:
            return httpx.Response(404)
        version["count"] += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=(
                "<html><title>Docs Home</title><body>"
                f"<h1>Authentication</h1><p>Use OAuth2 bearer v{version['count']}.</p>"
                "<h2>Rate Limits</h2><p>Default 100 requests per minute.</p>"
                "</body></html>"
            ),
        )

    return handler


@pytest.mark.asyncio
async def test_local_cache_hit_and_query(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _install_mock_http(monkeypatch, _make_versioned_docs_handler())

    first = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0})
    assert "Cache: miss" in first
    assert "Use OAuth2 bearer v1." in first
    assert len(requested) == 1

    index_files = list(fdt.get_fetch_docs_cache_dir().glob("*/index.json"))
    assert len(index_files) == 1
    index_data = json.loads(index_files[0].read_text(encoding="utf-8"))
    assert len(index_data["pages"]) == 1
    assert (index_files[0].parent / index_data["pages"][0]["file"]).is_file()

    requested.clear()
    hit = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0})
    assert "Cache: hit" in hit
    assert "Use OAuth2 bearer v1." in hit
    assert requested == []

    queried = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0, "query": "OAuth2"})
    assert "Cache: hit" in queried
    assert "Query Matches" in queried
    assert "Authentication" in queried
    assert requested == []

    no_match = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0, "query": "nonexistent"})
    assert "No sections matched query: nonexistent" in no_match
    assert requested == []


@pytest.mark.asyncio
async def test_local_cache_ttl_expiry_and_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _install_mock_http(monkeypatch, _make_versioned_docs_handler())

    first = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0})
    assert "Use OAuth2 bearer v1." in first
    assert len(requested) == 1

    index_files = list(fdt.get_fetch_docs_cache_dir().glob("*/index.json"))
    index_data = json.loads(index_files[0].read_text(encoding="utf-8"))
    index_data["fetched_at"] = time.time() - 7_200
    index_files[0].write_text(json.dumps(index_data), encoding="utf-8")

    expired = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0, "ttl_seconds": 60})
    assert "Cache: miss" in expired
    assert "Use OAuth2 bearer v2." in expired
    assert len(requested) == 2

    requested.clear()
    refreshed = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0, "refresh": True})
    assert "Cache: miss" in refreshed
    assert "Use OAuth2 bearer v3." in refreshed
    assert len(requested) == 1


@pytest.mark.asyncio
async def test_entry_redirect_cache_key_hits_on_repeat_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://docs.invalid/guide":
            return httpx.Response(301, headers={"Location": "/guide/"})
        if url == ROOT_URL:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="<html><title>Redirected</title><body><p>Arrived</p></body></html>",
            )
        return httpx.Response(404)

    requested = _install_mock_http(monkeypatch, handler)
    res1 = await fetch_docs.invoke({"url": "https://docs.invalid/guide", "max_depth": 0})
    assert "Cache: miss" in res1
    assert "Source: https://docs.invalid/guide/" in res1
    assert len(requested) == 2

    requested.clear()
    res2 = await fetch_docs.invoke({"url": "https://docs.invalid/guide", "max_depth": 0})
    assert "Cache: hit" in res2
    assert requested == []


# ---------------------------------------------------------------------------
# 6. SSRF, DNS rebinding, redirect & SSL security
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        "http://docs.invalid/a",
        "file:///etc/passwd",
        "ftp://docs.invalid/a",
        "https://localhost/a",
        "https://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://172.16.0.1/a",
        "https://192.168.1.1/a",
        "https://169.254.169.254/latest/meta-data",
        "https://100.64.0.1/a",
        "https://metadata.google.internal/",
        "https://internal.local/a",
        "https://svc.internal/a",
        "https://[::1]/a",
        "https://[fc00::1]/a",
        "https://[::ffff:127.0.0.1]/a",
        "https://224.0.0.1/a",
        "https://[ff02::1]/a",
        "https://[64:ff9b::127.0.0.1]/a",
        "https://user@docs.invalid/a",
        "https://docs.invalid:99999/a",
        "https://docs.invalid/a?token=test-only",
    ],
)
async def test_ssrf_urls_rejected_without_network_call(
    monkeypatch: pytest.MonkeyPatch,
    bad_url: str,
) -> None:
    requested = _install_mock_http(monkeypatch, lambda _req: httpx.Response(200, text="nope"))
    result = await fetch_docs.invoke({"url": bad_url})

    assert result.startswith("[ERROR]: fetch_docs failed:")
    assert requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolved_ips",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["169.254.169.254"],
        ["::1"],
        ["224.0.0.1"],
        ["64:ff9b::127.0.0.1"],
        ["93.184.216.34", "192.168.1.50"],
    ],
)
async def test_dns_rebinding_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch,
    resolved_ips: list[str],
) -> None:
    _set_dns_records(monkeypatch, {"rebind.invalid": resolved_ips})
    requested = _install_mock_http(monkeypatch, lambda _req: httpx.Response(200, text="nope"))
    result = await fetch_docs.invoke({"url": "https://rebind.invalid/guide/"})

    assert result.startswith("[ERROR]: fetch_docs failed:")
    assert "network_host_not_public" in result
    assert requested == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_target",
    [
        "https://127.0.0.1/admin",
        "http://docs.invalid/downgrade",
        "https://169.254.169.254/latest/meta-data",
        "https://private.invalid/secret",
    ],
)
async def test_redirect_to_unsafe_target_blocked(
    monkeypatch: pytest.MonkeyPatch,
    redirect_target: str,
) -> None:
    _set_dns_records(monkeypatch, {"private.invalid": ["192.168.1.10"]})

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ROOT_URL:
            return httpx.Response(302, headers={"Location": redirect_target})
        return httpx.Response(200, text="must not reach")

    requested = _install_mock_http(monkeypatch, handler)
    result = await fetch_docs.invoke({"url": ROOT_URL})

    assert result.startswith("[ERROR]: fetch_docs failed:")
    assert requested == [ROOT_URL]


@pytest.mark.asyncio
async def test_redirect_hop_limit_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(302, headers={"Location": f"/guide/hop-{counter['n']}"})

    requested = _install_mock_http(monkeypatch, handler)
    result = await fetch_docs.invoke({"url": ROOT_URL})

    assert result.startswith("[ERROR]: fetch_docs failed:")
    assert "Exceeded max redirect hops" in result
    assert len(requested) == 6


@pytest.mark.asyncio
async def test_ssl_verify_env_propagates_to_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_SSL_VERIFY", "false")
    captured: dict[str, object] = {}
    real_client_cls = httpx.Client

    def spy_client(
        timeout=None,
        follow_redirects=False,
        verify=True,
        headers=None,
        transport=None,
    ):
        _ = transport
        captured["verify"] = verify
        return real_client_cls(
            timeout=timeout,
            follow_redirects=follow_redirects,
            verify=verify,
            headers=headers,
            transport=httpx.MockTransport(
                lambda _req: httpx.Response(
                    200,
                    headers={"Content-Type": "text/html"},
                    text="<title>SSL</title><p>ok</p>",
                )
            ),
        )

    monkeypatch.setattr(fdt.httpx, "Client", spy_client)
    result = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0})
    assert "ok" in result
    assert captured["verify"] is False


# ---------------------------------------------------------------------------
# 7. Streaming byte cap, max_chars truncation & error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_byte_cap_truncates_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Build >1 MiB UTF-8 stream with multi-byte characters straddling 1 MiB.
    chunk_unit = ("a" * 32_765 + "中").encode("utf-8")  # 32_768 bytes per chunk
    total_chunks = 40  # 1_310_720 bytes > 1 MiB
    chunks_read = {"count": 0}

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(total_chunks):
                chunks_read["count"] += 1
                yield chunk_unit

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            stream=OversizedStream(),
        )

    _install_mock_http(monkeypatch, handler)
    result = await fetch_docs.invoke(
        {"url": "https://docs.invalid/big.txt", "max_depth": 0, "max_chars": 100_000}
    )

    assert not result.startswith("[ERROR]")
    assert f"response exceeded {MAX_BYTES_PER_PAGE} bytes limit" in result
    assert chunks_read["count"] < total_chunks
    index_files = list(fdt.get_fetch_docs_cache_dir().glob("*/index.json"))
    assert len(index_files) == 1
    index_data = json.loads(index_files[0].read_text(encoding="utf-8"))
    assert index_data["pages"][0]["truncated_by_bytes"] is True


@pytest.mark.asyncio
async def test_max_chars_truncation_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "word " * 800
    _install_mock_http(
        monkeypatch,
        lambda _req: httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=f"<title>Long</title><body><p>{body}</p></body>",
        ),
    )

    result = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0, "max_chars": 1_000})
    assert len(result) <= 1_000
    assert "[Truncated at max_chars=1000]" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 500])
async def test_root_http_errors_return_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    _install_mock_http(monkeypatch, lambda _req: httpx.Response(status_code))
    result = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0})
    assert result.startswith("[ERROR]: fetch_docs failed:")
    assert f"HTTP {status_code}" in result


@pytest.mark.asyncio
async def test_timeout_returns_error_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _install_mock_http(monkeypatch, raise_timeout)
    result = await fetch_docs.invoke({"url": ROOT_URL, "max_depth": 0})
    assert result.startswith("[ERROR]: fetch_docs failed:")
    assert "timed out" in result
