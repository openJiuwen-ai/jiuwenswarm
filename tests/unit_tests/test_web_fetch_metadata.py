import pytest
import requests

from jiuwenswarm.agents.harness.common.recommendation import topic_page_fetch
from jiuwenswarm.agents.harness.common.recommendation.topic_url_safety import (
    public_fetch_url_rejection_reason_sync,
)
from jiuwenswarm.agents.harness.common.tools import web_fetch_tools


def _response(body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = "https://example.org/article"
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response._content = body.encode("utf-8")
    return response


def test_extract_published_at_prefers_structured_page_metadata() -> None:
    html = """
    <html><head>
      <meta property="article:published_time" content="2026-08-29T08:30:00+08:00">
      <script type="application/ld+json">
        {"datePublished": "2026-08-28T01:00:00Z"}
      </script>
    </head><body>正文</body></html>
    """

    assert topic_page_fetch.extract_published_at(html) == (
        "2026-08-29T08:30:00+08:00",
        "meta.article:published_time",
    )


def test_extract_published_at_supports_json_ld_and_jina_reader_header() -> None:
    assert topic_page_fetch.extract_published_at(
        '<script type="application/ld+json">{"datePublished":"2026-08-28"}</script>'
    ) == ("2026-08-28", "json_ld.datePublished")
    assert topic_page_fetch.extract_published_at(
        "Title: Example\nPublished Time: 2026-08-27T12:00:00Z\nMarkdown Content:\ntext"
    ) == ("2026-08-27T12:00:00Z", "jina_reader.published_time")


def test_extract_published_at_supports_real_arxiv_citation_date_shape() -> None:
    html = """
    <html><head>
      <meta name="citation_online_date" content="2026/08/28">
      <meta name="citation_date" content="2026/08/19">
      <script type="application/ld+json">
        {"datePublished": "2026-08-18T12:00:00Z"}
      </script>
    </head><body>arXiv abstract page</body></html>
    """

    assert topic_page_fetch.extract_published_at(html) == (
        "2026-08-19T00:00:00+00:00",
        "meta.citation_date",
    )


def test_extract_published_at_falls_back_to_citation_online_date() -> None:
    html = """
    <html><head>
      <meta name="citation_online_date" content="2026/08/28">
    </head><body>academic abstract page</body></html>
    """

    assert topic_page_fetch.extract_published_at(html) == (
        "2026-08-28T00:00:00+00:00",
        "meta.citation_online_date",
    )


def test_extract_published_at_does_not_guess_from_body_dates() -> None:
    assert topic_page_fetch.extract_published_at(
        "正文提到了 2026-08-29，但页面没有发布时间元数据。"
    ) == (None, None)


def test_fetch_html_prioritizes_main_content_over_title_and_navigation(
    monkeypatch,
) -> None:
    navigation = "导航链接 " * 2_000
    response = _response(
        "<html><head><title>仅标题中的候选支持</title></head>"
        f"<body><nav>{navigation}</nav>"
        "<main><p>正文中的可核验证据。</p></main></body></html>"
    )
    monkeypatch.setattr(web_fetch_tools, "_http_get", lambda *_args, **_kwargs: response)

    page = topic_page_fetch.fetch_topic_page_sync(
        "https://example.org/article",
        timeout_seconds=30,
    )

    assert page["title"] == "仅标题中的候选支持"
    assert page["content"] == "正文中的可核验证据。"


def test_fetch_html_excludes_repeated_page_title_but_preserves_body(
    monkeypatch,
) -> None:
    response = _response(
        "<html><head><title>Release 1.2.3</title></head>"
        "<body><main><h1>Release 1.2.3</h1>"
        "<p>Version 1.2.3 fixes the documented retry bug.</p>"
        "<h2>Upgrade notes</h2>"
        "<p>Existing configurations remain compatible.</p>"
        "</main></body></html>"
    )
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_sync(
        "https://example.org/releases/1.2.3",
        timeout_seconds=30,
    )

    assert page["content"] == (
        "Version 1.2.3 fixes the documented retry bug. "
        "Upgrade notes "
        "Existing configurations remain compatible."
    )


def test_fetch_html_does_not_treat_a_heading_as_page_content(monkeypatch) -> None:
    response = _response(
        "<html><head><title>Release 1.2.3</title></head>"
        "<body><main><h1>Release 1.2.3</h1></main></body></html>"
    )
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_sync(
        "https://example.org/releases/1.2.3",
        timeout_seconds=30,
    )

    assert page["content"] == ""


def test_fetch_html_preserves_non_title_heading_content(monkeypatch) -> None:
    response = _response(
        "<html><head><title>Release 1.2.3</title></head>"
        "<body><main><h2>Python 3.11 is now required</h2></main></body></html>"
    )
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_sync(
        "https://example.org/releases/1.2.3",
        timeout_seconds=30,
    )

    assert page["content"] == "Python 3.11 is now required"


def test_fetch_html_prefers_article_when_main_is_absent(monkeypatch) -> None:
    response = _response(
        "<html><body><aside>页面辅助内容</aside>"
        "<article><p>文章中的可核验证据。</p></article></body></html>"
    )
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_sync(
        "https://example.org/article",
        timeout_seconds=30,
    )

    assert page["content"] == "文章中的可核验证据。"


def test_fetch_html_falls_back_to_body_without_main_or_article(monkeypatch) -> None:
    response = _response(
        "<html><head><title>页面标题</title></head>"
        "<body><nav>导航链接</nav><p>普通页面正文。</p></body></html>"
    )
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_sync(
        "https://example.org/article",
        timeout_seconds=30,
    )

    assert page["content"] == "普通页面正文。"


def test_fetch_html_excludes_head_when_xhtml_declares_encoding(monkeypatch) -> None:
    response = _response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<html><head><title>仅标题中的候选支持</title>"
        "<script>bad()</script></head>"
        "<body><p>正文中的可核验证据。</p></body></html>"
    )
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_sync(
        "https://example.org/article",
        timeout_seconds=30,
    )

    assert page["content"] == "正文中的可核验证据。"


def test_jina_reader_excludes_response_header_from_evidence_content(
    monkeypatch,
) -> None:
    response = _response(
        "Title: 仅标题中的候选支持\n"
        "URL Source: https://example.org/article\n"
        "Published Time: 2026-09-01T08:30:00Z\n\n"
        "Markdown Content:\n"
        "正文中的可核验证据。"
    )
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_via_jina_sync(
        "https://example.org/article",
        timeout_seconds=30,
    )

    assert page["content"] == "正文中的可核验证据。"
    assert page["title"] == "仅标题中的候选支持"
    assert page["published_at"] == "2026-09-01T08:30:00Z"
    assert page["published_at_source"] == "jina_reader.published_time"


def test_jina_reader_excludes_repeated_title_but_preserves_other_headings(
    monkeypatch,
) -> None:
    response = _response(
        "Title: Release 1.2.3\n"
        "URL Source: https://example.org/releases/1.2.3\n\n"
        "Markdown Content:\n"
        "# Release 1.2.3\n\n"
        "Version 1.2.3 fixes the documented retry bug.\n\n"
        "Upgrade notes\n"
        "-------------\n\n"
        "Existing configurations remain compatible."
    )
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_via_jina_sync(
        "https://example.org/releases/1.2.3",
        timeout_seconds=30,
    )

    assert page["content"] == (
        "Version 1.2.3 fixes the documented retry bug.\n\n"
        "Upgrade notes\n-------------\n\n"
        "Existing configurations remain compatible."
    )


def test_jina_reader_does_not_treat_a_markdown_heading_as_page_content(
    monkeypatch,
) -> None:
    response = _response(
        "Title: Release 1.2.3\n"
        "URL Source: https://example.org/releases/1.2.3\n\n"
        "Markdown Content:\n"
        "# Release 1.2.3"
    )
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_via_jina_sync(
        "https://example.org/releases/1.2.3",
        timeout_seconds=30,
    )

    assert page["content"] == ""


def test_jina_reader_preserves_headings_when_title_is_empty(monkeypatch) -> None:
    response = _response(
        "Title:\n"
        "URL Source: https://example.org/releases/1.2.3\n\n"
        "Markdown Content:\n"
        "## Python 3.11 is now required"
    )
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_via_jina_sync(
        "https://example.org/releases/1.2.3",
        timeout_seconds=30,
    )

    assert page["content"] == "## Python 3.11 is now required"


def test_jina_reader_does_not_take_next_header_line_as_empty_title(
    monkeypatch,
) -> None:
    response = _response(
        "Title:\n"
        "URL Source: https://example.org/article\n\n"
        "Markdown Content:\n"
        "正文中的可核验证据。"
    )
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    monkeypatch.setattr(
        web_fetch_tools,
        "_http_get",
        lambda *_args, **_kwargs: response,
    )

    page = topic_page_fetch.fetch_topic_page_via_jina_sync(
        "https://example.org/article",
        timeout_seconds=30,
    )

    assert page["title"] == ""


def test_fetch_sqlite_release_uses_explicit_official_heading_date(monkeypatch) -> None:
    url = "https://sqlite.org/releaselog/current.html"
    response = _response(
        '<html><head><title>SQLite Release 3.53.4 On 2026-07-24</title></head>'
        '<body><h2>SQLite Release 3.53.4 On 2026-07-24</h2>'
        '<p>Prior changes from version 3.53.0 (2026-04-09):</p>'
        '<p>Fix the WAL-reset database corruption bug.</p>'
        '<p>Changes in this specific patch release, version 3.53.4 (2026-07-24):</p>'
        '<p>Fixes for problems in 3.53.0.</p></body></html>'
    )
    response.url = url
    monkeypatch.setattr(web_fetch_tools, "_http_get", lambda *_args, **_kwargs: response)
    page = topic_page_fetch.fetch_topic_page_sync(url, timeout_seconds=30)
    assert page.get("published_at") == "2026-07-24"
    assert page.get("published_at_source") == "sqlite.release_heading"
    assert "Prior changes" in page["content"]
    assert "2026-04-09" in page["content"]
    for other_url in ("https://example.org/review", "https://sqlite.org/changes.html"):
        response.url = other_url
        other = topic_page_fetch.fetch_topic_page_sync(other_url, timeout_seconds=30)
        assert "published_at" not in other


def test_arxiv_version_history_date_is_not_first_submission_date():
    text = '<meta name="citation_date" content="2026/08/01"> arXiv:2608.05510v2 [v1] Sat, 1 Aug 2026 01:23:51 UTC [v2] Thu, 6 Aug 2026 01:23:51 UTC'
    assert topic_page_fetch.extract_published_at(text, url="https://arxiv.org/abs/2608.05510") == ("2026-08-06T01:23:51+00:00", "arxiv.version_history")
    assert topic_page_fetch.extract_published_at(text, url="https://arxiv.org/abs/2608.05510v1")[0] == "2026-08-01T01:23:51+00:00"
    assert topic_page_fetch.extract_published_at('<meta name="citation_date" content="2026/08/01">', url="https://arxiv.org/abs/2608.05510v2") == (None, None)


def _closable_response(status_code: int, *, url: str, location: str | None = None):
    response = requests.Response()
    response.status_code = status_code
    response.url = url
    response._content = b""
    response._content_consumed = True
    if location is not None:
        response.headers["Location"] = location
    closed: list[bool] = []
    response.close = lambda: closed.append(True)
    return response, closed


def test_sync_url_check_refuses_a_metadata_literal() -> None:
    assert public_fetch_url_rejection_reason_sync(
        "http://169.254.169.254/latest/meta-data/"
    ) == "network_metadata_host"


def test_fetch_refuses_a_redirect_into_the_network(monkeypatch) -> None:
    redirect, closed = _closable_response(
        302,
        url="https://example.org/redirect",
        location="http://169.254.169.254/latest/meta-data/",
    )
    requested: list[str] = []

    def _get(url, **_kwargs):
        requested.append(url)
        return redirect

    monkeypatch.setattr(web_fetch_tools, "_http_get", _get)
    monkeypatch.setattr(
        topic_page_fetch,
        "public_fetch_url_rejection_reason_sync",
        lambda url: "network_metadata_host" if "169.254.169.254" in url else None,
    )

    with pytest.raises(ValueError, match="network_metadata_host"):
        topic_page_fetch.fetch_topic_page_sync(
            "https://example.org/redirect", timeout_seconds=30, public_https=True,
        )

    assert requested == ["https://example.org/redirect"]
    assert closed == [True]


def test_fetch_releases_the_response_when_the_status_is_an_error(monkeypatch) -> None:
    response, closed = _closable_response(500, url="https://example.org/article")
    monkeypatch.setattr(web_fetch_tools, "_http_get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        topic_page_fetch, "public_fetch_url_rejection_reason_sync", lambda _url: None,
    )

    with pytest.raises(requests.HTTPError):
        topic_page_fetch.fetch_topic_page_sync(
            "https://example.org/article", timeout_seconds=30, public_https=True,
        )

    assert closed == [True]
