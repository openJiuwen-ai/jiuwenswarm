# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the web image channel: discovery, download and local paths."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.web_search import paid
from jiuwenswarm.agents.harness.common.tools.web_search import image_search
from jiuwenswarm.agents.harness.common.tools.web_search.types import WebSearchRecord


def _png_bytes(width: int = 400, height: int = 300) -> bytes:
    pytest.importorskip("PIL.Image")
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, payload: bytes, content_type: str = "image/png") -> None:
        self._payload = payload
        self.headers = {"Content-Type": content_type}
        self.status_code = 200
        self.encoding = "utf-8"
        self.content = payload

    def iter_content(self, chunk_size: int = 65536):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start:start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _pin_workspace(monkeypatch, workspace: Path) -> None:
    """Pin the agent workspace so absolute test dirs stay inside the boundary."""
    import jiuwenswarm.common.utils as common_utils

    monkeypatch.setattr(common_utils, "get_agent_workspace_dir", lambda: workspace)


# ── provider payload ────────────────────────────────────────────────


def test_petal_normalize_extracts_image_urls() -> None:
    item = {
        "title": "t",
        "url": "https://example.com/news",
        "content": "body",
        "images": [
            "https://img.example.com/a.jpg",
            {"url": "https://img.example.com/b.png"},
            "ftp://ignored",
        ],
        "thumbnail": "https://img.example.com/thumb.jpg",
    }
    normalized = paid._petal_normalize_web_page_item(item)

    assert normalized["images"] == [
        "https://img.example.com/a.jpg",
        "https://img.example.com/b.png",
        "https://img.example.com/thumb.jpg",
    ]


def test_records_from_paid_payload_propagates_images() -> None:
    from jiuwenswarm.agents.harness.common.tools.web_search.providers import (
        records_from_paid_payload,
    )

    payload = {
        "answer": "answer " * 20,
        "urls": ["https://example.com/news"],
        "records": [
            {
                "url": "https://example.com/news",
                "summary": "summary",
                "content": "content",
                "images": ["https://img.example.com/a.jpg"],
            }
        ],
    }
    records, _answer = records_from_paid_payload("petal", payload, 5)

    assert len(records) == 1
    assert records[0].images == ["https://img.example.com/a.jpg"]


class _FakeJsonResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_bocha_search_sync_collects_images(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.tools.web_search.providers import (
        records_from_paid_payload,
    )

    payload = {
        "code": 200,
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "东南大学与华为合作",
                        "url": "https://www.seu.edu.cn/news/1.html",
                        "snippet": "snippet",
                        "summary": "summary",
                        "thumbnailUrl": "https://img.example.com/thumb.jpg",
                    },
                    {
                        "name": "无图页面",
                        "url": "https://example.com/plain",
                    },
                ]
            },
            "images": {
                "value": [
                    {
                        "name": "签约仪式",
                        "contentUrl": "https://img.example.com/hero.jpg",
                        "hostPageUrl": "https://www.seu.edu.cn/news/1.html",
                        "width": 1200,
                        "height": 800,
                    },
                    {
                        "contentUrl": "https://img.example.com/orphan.jpg",
                        "hostPageUrl": "https://unknown.example.com/page",
                    },
                    "ftp://ignored",
                ]
            },
        },
    }
    monkeypatch.setattr(
        paid,
        "read_env",
        lambda key, default="": {"BOCHA_API_KEY": "k"}.get(key, default),
    )
    monkeypatch.setattr(paid, "http_request", lambda *args, **kwargs: _FakeJsonResponse(payload))

    result = paid.bocha_search_sync("q", 5, 10)

    assert result["urls"] == [
        "https://www.seu.edu.cn/news/1.html",
        "https://example.com/plain",
    ]
    by_url = {record["url"]: record for record in result["records"]}
    assert by_url["https://www.seu.edu.cn/news/1.html"]["title"] == "东南大学与华为合作"
    assert by_url["https://www.seu.edu.cn/news/1.html"]["images"] == [
        "https://img.example.com/thumb.jpg",
        "https://img.example.com/hero.jpg",
    ]
    # 无法归属到结果页的图片不挂载，避免把无关图片放进配图候选
    assert by_url["https://example.com/plain"]["images"] == []

    records, _answer = records_from_paid_payload("bocha", result, 5)
    assert records[0].title == "东南大学与华为合作"
    assert records[0].images == [
        "https://img.example.com/thumb.jpg",
        "https://img.example.com/hero.jpg",
    ]


def test_response_renders_image_urls_and_download_hint() -> None:
    from jiuwenswarm.agents.harness.common.tools.web_search.response import (
        format_web_search_response,
    )

    record = WebSearchRecord(
        title="新闻",
        url="https://example.com/news",
        snippet="s",
        source="paid:petal",
        images=["https://img.example.com/a.jpg", "https://img.example.com/b.jpg"],
    )
    text = format_web_search_response(
        "q",
        search_mode="default",
        selected_provider="paid:petal",
        quality_passed=True,
        primary_records=[record],
        supplementary_records=[],
        primary_answer="",
        providers_tried=["paid:petal(pass)"],
    )

    assert "Images: https://img.example.com/a.jpg | https://img.example.com/b.jpg" in text
    assert "web_image_search" in text


@pytest.fixture(autouse=True)
def _stub_public_dns(monkeypatch) -> None:
    """Default every hostname to a public IP; SSRF cases override this per test."""
    monkeypatch.setattr(
        image_search.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


# ── html extraction ─────────────────────────────────────────────────


def test_extract_image_candidates_filters_noise_and_resolves_relative() -> None:
    html = """
    <html><body>
      <img src="/static/logo.png" alt="logo" width="40" height="40">
      <img data-src="//cdn.example.com/photo.jpg" alt="会议现场" width="900" height="600">
      <img src="data:image/png;base64,AAAA">
      <img src="pixel.gif" alt="tracking pixel" width="1" height="1">
      <img src="chart.png" width="640" height="480">
    </body></html>
    """
    candidates = image_search.extract_image_candidates(
        html,
        "https://example.com/post/1",
    )
    urls = [item.url for item in candidates]

    assert "https://cdn.example.com/photo.jpg" in urls
    assert "https://example.com/post/chart.png" in urls
    assert all("logo" not in url and "pixel" not in url for url in urls)
    assert urls[0] == "https://cdn.example.com/photo.jpg"


def test_extract_image_candidates_keeps_document_order() -> None:
    html = """
    <html><body>
      <img src="/ad/banner.png" alt="扫码下载APP 广告" width="1200" height="300">
      <img src="/uploads/seu-campus.jpg" alt="东南大学九龙湖校区校园建筑" width="800" height="600">
    </body></html>
    """
    candidates = image_search.extract_image_candidates(
        html,
        "https://example.com/post/1",
    )

    # 相关性交给搜索引擎排序；客户端只保持文档顺序，不做关键词判断。
    assert [item.url for item in candidates] == [
        "https://example.com/ad/banner.png",
        "https://example.com/uploads/seu-campus.jpg",
    ]
    assert candidates[1].alt == "东南大学九龙湖校区校园建筑"
    assert not hasattr(candidates[0], "score")


def test_extract_image_candidates_honours_min_side() -> None:
    html = '<img src="/small.png" width="64" height="64"><img src="/big.png" width="800" height="600">'
    candidates = image_search.extract_image_candidates(
        html,
        "https://example.com/post/1",
        min_side=200,
    )

    assert [item.url for item in candidates] == ["https://example.com/big.png"]


def test_is_public_http_url_blocks_internal_targets() -> None:
    assert image_search.is_public_http_url("https://example.com/a.jpg") is True
    assert image_search.is_public_http_url("http://127.0.0.1/a.jpg") is False
    assert image_search.is_public_http_url("http://192.168.1.2/a.jpg") is False
    assert image_search.is_public_http_url("http://localhost/a.jpg") is False
    assert image_search.is_public_http_url("file:///etc/passwd") is False


def test_url_target_rejects_private_dns_resolution(monkeypatch) -> None:
    monkeypatch.setattr(
        image_search.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.0.0.8", 0))],
    )
    assert image_search._url_target_is_public("https://evil.example/a.jpg") is False


def test_url_target_allows_public_dns_resolution(monkeypatch) -> None:
    monkeypatch.setattr(
        image_search.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert image_search._url_target_is_public("https://cdn.example.org/a.jpg") is True


def test_url_target_rejects_unresolvable_host(monkeypatch) -> None:
    def _dns_failure(*_args, **_kwargs):
        raise OSError("dns unavailable")

    monkeypatch.setattr(image_search.socket, "getaddrinfo", _dns_failure)
    # 无法证明是公网地址时按内网拒绝，避免 DNS 抖动绕过预检。
    assert image_search._url_target_is_public("https://unknown.example/a.jpg") is False


def test_downloader_blocks_redirect_to_private_host_before_request(
    tmp_path: Path, monkeypatch
) -> None:
    requested: list[str] = []
    kwargs_seen: list[dict] = []

    class _RedirectResponse(_FakeResponse):
        def __init__(self) -> None:
            super().__init__(b"", "text/html")
            self.status_code = 302
            self.headers = {
                "Location": "http://127.0.0.1/secret.png",
                "Content-Type": "text/html",
            }

    def _fake_get(url, **kwargs):
        requested.append(url)
        kwargs_seen.append(kwargs)
        return _RedirectResponse()

    monkeypatch.setattr("jiuwenswarm.common.http_proxy_config.requests_get", _fake_get)
    candidate = image_search.ImageCandidate(url="https://img.example.com/a.png")

    assert image_search.ImageDownloader().download(
        candidate, tmp_path, max_bytes=1024 * 1024
    ) is None
    # 内网目标必须在发起请求前被拦下：只请求首跳，且不交给客户端自动跟随。
    assert requested == ["https://img.example.com/a.png"]
    assert kwargs_seen[0]["allow_redirects"] is False


def test_downloader_follows_public_redirect_hops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        image_search.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    requested: list[str] = []

    class _RedirectResponse(_FakeResponse):
        def __init__(self) -> None:
            super().__init__(b"", "text/html")
            self.status_code = 302
            self.headers = {
                "Location": "https://cdn.example.org/b.png",
                "Content-Type": "text/html",
            }

    def _fake_get(url, **_kwargs):
        requested.append(url)
        return _RedirectResponse() if len(requested) == 1 else _FakeResponse(_png_bytes())

    monkeypatch.setattr("jiuwenswarm.common.http_proxy_config.requests_get", _fake_get)
    candidate = image_search.ImageCandidate(url="https://img.example.com/a.png")

    image = image_search.ImageDownloader().download(
        candidate, tmp_path, max_bytes=1024 * 1024
    )

    assert image is not None
    assert requested == ["https://img.example.com/a.png", "https://cdn.example.org/b.png"]


# ── download + local paths ──────────────────────────────────────────


def test_downloader_writes_validated_image(tmp_path: Path, monkeypatch) -> None:
    payload = _png_bytes()
    monkeypatch.setattr(
        "jiuwenswarm.common.http_proxy_config.requests_get",
        lambda *_args, **_kwargs: _FakeResponse(payload),
    )
    candidate = image_search.ImageCandidate(
        url="https://img.example.com/a.png",
        source_page="https://example.com/news",
        alt="现场",
    )

    image = image_search.ImageDownloader().download(candidate, tmp_path, max_bytes=1024 * 1024)

    assert image is not None
    assert Path(image.local_path).is_file()
    assert image.mime_type == "image/png"
    assert image.width == 400 and image.height == 300


def test_downloader_rejects_non_image_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.http_proxy_config.requests_get",
        lambda *_args, **_kwargs: _FakeResponse(b"<html>not an image</html>", "text/html"),
    )
    candidate = image_search.ImageCandidate(url="https://example.com/page")

    assert image_search.ImageDownloader().download(candidate, tmp_path, max_bytes=1024) is None


@pytest.mark.asyncio
async def test_search_images_returns_paths(tmp_path: Path, monkeypatch) -> None:
    _pin_workspace(monkeypatch, tmp_path)
    payload = _png_bytes()
    monkeypatch.setattr(
        "jiuwenswarm.common.http_proxy_config.requests_get",
        lambda *_args, **_kwargs: _FakeResponse(payload),
    )

    async def _fake_records(*_args, **_kwargs):
        return (
            [
                WebSearchRecord(
                    title="新闻",
                    url="https://example.com/news",
                    snippet="s",
                    source="paid:petal",
                    images=["https://img.example.com/hero.png"],
                )
            ],
            ["paid:petal(pass)"],
        )

    monkeypatch.setattr(image_search, "_collect_records", _fake_records)
    monkeypatch.setattr(image_search, "_fetch_html", lambda *_args, **_kwargs: "")

    outcome = await image_search.search_images(
        "华为 东南大学",
        max_images=2,
        output_dir=tmp_path,
    )

    assert outcome.error == ""
    assert len(outcome.images) == 1
    content = outcome.to_content()
    assert str(tmp_path) in content
    assert "source: https://example.com/news" in content
    assert "| page: 新闻" in content
    assert outcome.as_dict()["images"][0]["source_title"] == "新闻"


@pytest.mark.asyncio
async def test_search_images_prefers_provider_results_without_page_scraping(
    tmp_path: Path, monkeypatch
) -> None:
    _pin_workspace(monkeypatch, tmp_path)
    payload = _png_bytes()
    monkeypatch.setattr(
        "jiuwenswarm.common.http_proxy_config.requests_get",
        lambda *_args, **_kwargs: _FakeResponse(payload),
    )

    async def _fake_records(*_args, **_kwargs):
        return (
            [
                WebSearchRecord(
                    title="BMW 1502",
                    url="https://example.com/page",
                    snippet="s",
                    source="paid:bocha",
                    images=["https://img.example.com/a.png"],
                )
            ],
            ["paid:bocha(pass)"],
        )

    scraped: list[str] = []

    def _fake_fetch(url, *_args, **_kwargs):
        scraped.append(url)
        return '<img src="/junk.png" width="1200" height="900">'

    monkeypatch.setattr(image_search, "_collect_records", _fake_records)
    monkeypatch.setattr(image_search, "_fetch_html", _fake_fetch)

    outcome = await image_search.search_images("BMW 1502", output_dir=tmp_path)

    assert len(outcome.images) == 1
    assert outcome.images[0].source_url == "https://img.example.com/a.png"
    # provider 已给出引擎排序的图，不再抓网页 <img> 掺入无关图。
    assert scraped == []
    assert outcome.stats["page_candidates"] == 0


@pytest.mark.asyncio
async def test_search_images_falls_back_to_pages_when_provider_has_no_images(
    tmp_path: Path, monkeypatch
) -> None:
    _pin_workspace(monkeypatch, tmp_path)
    payload = _png_bytes()
    monkeypatch.setattr(
        "jiuwenswarm.common.http_proxy_config.requests_get",
        lambda *_args, **_kwargs: _FakeResponse(payload),
    )

    async def _fake_records(*_args, **_kwargs):
        return (
            [
                WebSearchRecord(
                    title="BMW 1502 相册",
                    url="https://example.com/page",
                    snippet="s",
                    source="paid:bocha",
                )
            ],
            ["paid:bocha(pass)"],
        )

    monkeypatch.setattr(image_search, "_collect_records", _fake_records)
    monkeypatch.setattr(
        image_search,
        "_fetch_html",
        lambda *_args, **_kwargs: '<img src="/gallery.png" width="1024" height="768">',
    )

    outcome = await image_search.search_images("BMW 1502", output_dir=tmp_path)

    assert len(outcome.images) == 1
    assert outcome.images[0].source_url == "https://example.com/gallery.png"
    assert outcome.stats["page_candidates"] == 1


@pytest.mark.asyncio
async def test_search_images_reports_when_nothing_usable(tmp_path: Path, monkeypatch) -> None:
    async def _fake_records(*_args, **_kwargs):
        return ([], [])

    monkeypatch.setattr(image_search, "_collect_records", _fake_records)

    outcome = await image_search.search_images("q", output_dir=tmp_path)

    assert outcome.images == []
    assert outcome.error
    assert "No usable image" in outcome.to_content() or "Failed" in outcome.to_content()


@pytest.mark.asyncio
async def test_search_images_stops_on_total_time_budget(tmp_path: Path, monkeypatch) -> None:
    async def _fake_records(*_args, **_kwargs):
        return (
            [
                WebSearchRecord(
                    title="t",
                    url="https://example.com/news",
                    snippet="s",
                    source="paid:petal",
                    images=[
                        "https://img.example.com/a.jpg",
                        "https://img.example.com/b.jpg",
                    ],
                )
            ],
            ["paid:petal(pass)"],
        )

    class _Clock:
        def __init__(self) -> None:
            self.value = 1000.0

        def monotonic(self) -> float:
            self.value += 100.0
            return self.value

    monkeypatch.setattr(image_search, "_collect_records", _fake_records)
    monkeypatch.setattr(image_search, "time", _Clock())

    downloaded: list[str] = []

    def _fake_download(self, candidate, target_dir, **_kwargs):
        downloaded.append(candidate.url)
        return None

    monkeypatch.setattr(image_search.ImageDownloader, "download", _fake_download)

    outcome = await image_search.search_images("q", max_images=2, output_dir=tmp_path)

    assert downloaded == ["https://img.example.com/a.jpg"]
    assert any("total time budget" in hint for hint in outcome.hints)


def test_resolve_output_dir_ignores_unfilled_placeholder(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    import jiuwenswarm.common.utils as common_utils

    monkeypatch.setattr(common_utils, "get_agent_workspace_dir", lambda: tmp_path)

    resolved = image_search._resolve_output_dir("q", "{output_dir}/images")

    expected = tmp_path / "search_images" / hashlib.sha1(b"q").hexdigest()[:8]
    assert resolved == expected


def test_resolve_output_dir_rejects_path_outside_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _pin_workspace(monkeypatch, workspace)

    resolved = image_search._resolve_output_dir("q", tmp_path / "outside")

    expected = workspace / "search_images" / hashlib.sha1(b"q").hexdigest()[:8]
    assert resolved == expected


def test_resolve_output_dir_keeps_relative_dir_inside_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _pin_workspace(monkeypatch, workspace)

    resolved = image_search._resolve_output_dir("q", "slides/images")

    assert resolved == (workspace / "slides" / "images").resolve()


def test_bocha_reads_top_level_payload_alongside_inner_data(monkeypatch) -> None:
    payload = {
        "code": 200,
        "data": {},
        "webPages": {"value": [{"name": "t", "url": "https://example.com/a"}]},
        "summary": "顶层摘要",
    }
    monkeypatch.setattr(
        paid,
        "read_env",
        lambda key, default="": {"BOCHA_API_KEY": "k"}.get(key, default),
    )
    monkeypatch.setattr(paid, "http_request", lambda *args, **kwargs: _FakeJsonResponse(payload))

    result = paid.bocha_search_sync("q", 5, 10)

    assert result["answer"] == "顶层摘要"
    assert result["urls"] == ["https://example.com/a"]


@pytest.mark.asyncio
async def test_image_search_tool_reports_failure_when_outcome_has_error(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
        JiuwenHarnessImageSearchTool,
    )

    async def _fake_search_images(query: str, **_kwargs):
        return image_search.ImageSearchOutcome(query=query, error="no search results")

    monkeypatch.setattr(image_search, "search_images", _fake_search_images)
    tool = JiuwenHarnessImageSearchTool()

    output = await tool.invoke({"query": "q"})

    assert output.success is False
    assert output.error == "no search results"
    assert "Failed" in output.data["content"]


@pytest.mark.asyncio
async def test_image_search_tool_reports_success_without_error(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
        JiuwenHarnessImageSearchTool,
    )

    async def _fake_search_images(query: str, **_kwargs):
        return image_search.ImageSearchOutcome(query=query)

    monkeypatch.setattr(image_search, "search_images", _fake_search_images)
    tool = JiuwenHarnessImageSearchTool()

    output = await tool.invoke({"query": "q"})

    assert output.success is True
    assert output.error is None
