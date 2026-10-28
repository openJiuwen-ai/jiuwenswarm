# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Page fetch used only by topic retrieval.

The shared ``mcp_fetch_webpage`` tool keeps its original text extraction. Topic
evidence needs a publication time and the article body without navigation, so
that parsing lives here and calls the shared HTTP helper underneath.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree  # type: ignore[import-untyped]
from lxml import html as lxml_html

from jiuwenswarm.agents.harness.common.recommendation.topic_url_safety import (
    public_fetch_url_rejection_reason_sync,
)
from jiuwenswarm.agents.harness.common.tools import web_fetch_tools

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", flags=re.IGNORECASE)
_META_ATTRIBUTE_RE = re.compile(
    r"([\w:-]+)\s*=\s*([\"'])(.*?)\2",
    flags=re.IGNORECASE | re.DOTALL,
)
_JSON_LD_PUBLISHED_RE = re.compile(
    r'[\"\']datePublished[\"\']\s*:\s*[\"\']([^\"\']+)[\"\']',
    flags=re.IGNORECASE,
)
_JINA_PUBLISHED_RE = re.compile(
    r"^Published Time:\s*(\S.*?)\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_JINA_TITLE_RE = re.compile(
    r"^Title:[ \t]*(.*?)[ \t]*\r?$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_JINA_CONTENT_RE = re.compile(
    r"^Markdown Content:[ \t]*\r?$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_MARKDOWN_ATX_HEADING_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]+(?P<text>.*?)(?:[ \t]+#+[ \t]*)?\r?$",
    flags=re.MULTILINE,
)
_MARKDOWN_SETEXT_HEADING_RE = re.compile(
    r"^(?P<text>[^\r\n]+)\r?\n[ \t]{0,3}(?:=+|-+)[ \t]*\r?$",
    flags=re.MULTILINE,
)
_MSN_HOSTS = ("msn.com", "msn.cn")
_MSN_ARTICLE_SEGMENT_RE = re.compile(r"ar-(AA[0-9A-Za-z]+)")
_MSN_DETAIL_API_URL = "https://assets.msn.com/content/view/v2/Detail/zh-cn/"


def extract_published_at(text: str, *, url: str = "") -> tuple[str | None, str | None]:
    """Read explicit publication fields; never infer from arbitrary body dates."""

    parsed_url = urlparse(url)
    if parsed_url.hostname in {"arxiv.org", "www.arxiv.org"} and parsed_url.path.startswith("/abs/"):
        page_text = unescape(re.sub(r"<[^>]+>", " ", text))
        paper = re.search(r"/(\d{4}\.\d{4,5})(?:v(\d+))?/?$", parsed_url.path)
        version = paper[2] if paper else None
        if paper and not version:
            cited = re.search(r"arXiv:" + re.escape(paper[1]) + r"v(\d+)\b", page_text, re.IGNORECASE)
            version = cited[1] if cited else None
        if version:
            history = re.search(
                rf"\[v{re.escape(version)}\]\s*"
                r"([A-Za-z]{3},\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\s+UTC)",
                page_text,
            )
            if history:
                parsed_date = datetime.strptime(history[1], "%a, %d %b %Y %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc,
                )
                return parsed_date.isoformat(), "arxiv.version_history"
            if version != "1":
                return None, None
    citation_date = ""
    citation_online_date = ""
    for tag in _META_TAG_RE.findall(text or ""):
        attributes = {
            name.casefold(): unescape(value.strip())
            for name, _, value in _META_ATTRIBUTE_RE.findall(tag)
        }
        key = str(attributes.get("property") or attributes.get("name") or "").casefold()
        value = str(attributes.get("content") or "").strip()
        if key in {
            "article:published_time",
            "og:published_time",
            "datepublished",
            "date",
        }:
            if value:
                return value, f"meta.{key}"
        elif key == "citation_date" and value and not citation_date:
            citation_date = value
        elif key == "citation_online_date" and value and not citation_online_date:
            citation_online_date = value
    citation_value = citation_date or citation_online_date
    if citation_value:
        try:
            parsed_citation_date = datetime.strptime(citation_value, "%Y/%m/%d").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            parsed_citation_date = None
        if parsed_citation_date is not None:
            citation_key = "citation_date" if citation_date else "citation_online_date"
            return parsed_citation_date.isoformat(), f"meta.{citation_key}"
    json_ld = _JSON_LD_PUBLISHED_RE.search(text or "")
    if json_ld:
        return unescape(json_ld.group(1).strip()), "json_ld.datePublished"
    jina = _JINA_PUBLISHED_RE.search(text or "")
    if jina:
        return jina.group(1).strip(), "jina_reader.published_time"
    parsed = urlparse(url)
    if parsed.hostname in {"sqlite.org", "www.sqlite.org"} and parsed.path.startswith("/releaselog/"):
        heading = re.search(
            r"<h2>\s*SQLite Release [0-9.]+ On (\d{4}-\d{2}-\d{2})\s*</h2>",
            text,
            flags=re.IGNORECASE,
        )
        if heading:
            return heading.group(1), "sqlite.release_heading"
    return None, None


def _normalized_title_text(value: str) -> str:
    return unescape(re.sub(r"\s+", " ", value)).strip().casefold()


def _extract_jina_reader_content(text: str, *, title: str) -> str:
    marker = _JINA_CONTENT_RE.search(text)
    content = text[marker.end():] if marker else text
    normalized_title = _normalized_title_text(title)
    if normalized_title:
        def remove_repeated_title(match: re.Match[str]) -> str:
            heading = _normalized_title_text(match.group("text"))
            return "" if heading == normalized_title else match.group(0)

        content = _MARKDOWN_ATX_HEADING_RE.sub(remove_repeated_title, content)
        content = _MARKDOWN_SETEXT_HEADING_RE.sub(remove_repeated_title, content)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def _extract_html_content(value: str, *, title: str) -> str:
    if not value.strip():
        return ""
    try:
        document = lxml_html.fromstring(
            value.encode("utf-8"),
            parser=lxml_html.HTMLParser(encoding="utf-8"),
        )
    except (etree.ParserError, ValueError):
        return web_fetch_tools.strip_tags(value)

    for node in document.xpath("//script|//style|//noscript|//template|//title|//nav"):
        node.drop_tree()
    normalized_title = _normalized_title_text(title)
    if normalized_title:
        for node in document.xpath("//h1|//h2|//h3|//h4|//h5|//h6"):
            heading = _normalized_title_text(" ".join(node.itertext()))
            if heading == normalized_title:
                node.drop_tree()
    for selector in ("//main", "//article", "//body"):
        nodes = document.xpath(selector)
        content = re.sub(
            r"\s+",
            " ",
            " ".join(text for node in nodes for text in node.itertext()),
        ).strip()
        if content:
            return content
    return re.sub(r"\s+", " ", " ".join(document.itertext())).strip()


def msn_detail_api_url(url: str) -> str | None:
    """The detail API of an msn.com / msn.cn article page, whose HTML is a script shell."""

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    if not any(hostname == host or hostname.endswith(f".{host}") for host in _MSN_HOSTS):
        return None
    article = _MSN_ARTICLE_SEGMENT_RE.fullmatch(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    return f"{_MSN_DETAIL_API_URL}{article.group(1)}" if article else None


def _fetch_verified_response(
    url: str,
    timeout_seconds: int,
    *,
    public_https: bool,
) -> requests.Response:
    """Fetch one page, following redirects only to public targets.

    The initial URL check happens before the request, but requests follows a 3xx by default
    without inspecting the target. With ``public_https`` each hop is resolved again, so a
    public URL that redirects into the link-local metadata range is refused.
    """

    if not public_https:
        return web_fetch_tools.fetch_response(url, timeout_seconds, public_https=False)
    current = url
    for _ in range(requests.models.DEFAULT_REDIRECT_LIMIT + 1):
        reason = public_fetch_url_rejection_reason_sync(current)
        if reason is not None:
            raise ValueError(f"unsafe fetch target: {reason}")
        response = web_fetch_tools.fetch_response(
            current,
            timeout_seconds,
            public_https=False,
            allow_redirects=False,
        )
        if not response.is_redirect:
            return response
        try:
            current = urljoin(response.url, response.headers["Location"])
        finally:
            response.close()
    raise requests.TooManyRedirects("Exceeded the default redirect limit")


def fetch_topic_page_via_jina_sync(
    url: str,
    timeout_seconds: int,
    *,
    public_https: bool = False,
) -> dict[str, Any]:
    """Read a blocked page through Jina, keeping its title and publication time."""

    reader_url = f"https://r.jina.ai/{url}"
    with _fetch_verified_response(
        reader_url,
        timeout_seconds,
        public_https=public_https,
    ) as response:
        response.raise_for_status()
        text = web_fetch_tools.decode_response_text(response).strip()
        published_at, published_at_source = extract_published_at(text, url=url)
        title_match = _JINA_TITLE_RE.search(text)
        title = unescape(title_match.group(1).strip()) if title_match else ""
        result: dict[str, Any] = {
            "url": url,
            "status_code": response.status_code,
            "title": title,
            "content": _extract_jina_reader_content(text, title=title),
            "fetch_reader": "jina-reader",
        }
        if published_at and published_at_source:
            result["published_at"] = published_at
            result["published_at_source"] = published_at_source
        return result


def fetch_topic_page_sync(
    url: str,
    timeout_seconds: int,
    *,
    public_https: bool = False,
) -> dict[str, Any]:
    """Fetch one topic source, with publication time and the article body."""

    with _fetch_verified_response(
        url,
        timeout_seconds,
        public_https=public_https,
    ) as response:
        if response.status_code in {401, 403, 429}:
            return fetch_topic_page_via_jina_sync(
                url,
                timeout_seconds,
                public_https=public_https,
            )
        response.raise_for_status()

        text = web_fetch_tools.decode_response_text(response)
        published_at, published_at_source = extract_published_at(text, url=response.url)
        content_type = response.headers.get("Content-Type", "")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
        title = web_fetch_tools.strip_tags(title_match.group(1)) if title_match else ""

        if "html" in content_type.lower():
            text = _extract_html_content(text, title=title)
        else:
            text = re.sub(r"\s+", " ", text).strip()

        result: dict[str, Any] = {
            "url": response.url,
            "status_code": response.status_code,
            "title": title,
            "content": text,
            "fetch_reader": "direct",
        }
        if published_at and published_at_source:
            result["published_at"] = published_at
            result["published_at_source"] = published_at_source
        return result


def fetch_msn_detail_sync(
    url: str,
    api_url: str,
    timeout_seconds: int,
    *,
    public_https: bool = False,
) -> dict[str, Any]:
    """Read an msn article from its detail API: body, publication time and original URL."""

    with _fetch_verified_response(
        api_url,
        timeout_seconds,
        public_https=public_https,
    ) as response:
        response.raise_for_status()
        try:
            detail = json.loads(web_fetch_tools.decode_response_text(response))
        except ValueError:
            detail = {}
        title = str(detail.get("title") or "")
        source_href = str(detail.get("sourceHref") or "").strip()
        parsed_href = urlparse(source_href)
        result: dict[str, Any] = {
            "url": source_href if parsed_href.scheme in {"http", "https"} and parsed_href.netloc else url,
            "status_code": response.status_code,
            "title": title,
            "content": _extract_html_content(str(detail.get("body") or ""), title=title),
            "fetch_reader": "msn-detail-api",
            "api_url": api_url,
        }
        published_at = str(detail.get("publishedDateTime") or "").strip()
        if published_at:
            result["published_at"] = published_at
            result["published_at_source"] = "msn_detail_api.publishedDateTime"
        return result
