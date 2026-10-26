# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""News-lane sources: Bing News RSS and public hot-search lists."""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

import aiohttp

from jiuwenswarm.agents.harness.common.recommendation.topic_url_safety import (
    public_fetch_url_rejection_reason,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_retrieval import (
    NEWS_MAX_WINDOW_HOURS,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
    OpenJiuwenNativeSearchContractError,
    OpenJiuwenNativeSearchResult,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)

NEWS_FEED_PROVIDER_ID = "bing-news-rss"
NEWS_FEED_RECEIPT_VERSION = "topic-starter-v6-news-feed-v1"
NEWS_FEED_ENGINE = "bing-news-rss"
NEWS_PREFERRED_WINDOW_HOURS = 24
_FEED_URL = "https://www.bing.com/news/search"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# Bing's undocumented recency filters, as observed on the live feed: 7 = past 24 hours,
# 8 = past week. The host still filters every item to the requested window.
_INTERVAL_BY_WINDOW = ((24, "7"), (168, "8"))

FeedFetcher = Callable[[str, int], Any]
_VERSION_OR_DATE_TOKEN = re.compile(
    r"^(?:v(?:er(?:sion)?)?\.?\s*)?\d+(?:\.\d+)+$"
    r"|^v?\d+(?:\.\d+)*[a-z]?$"
    r"|^(?:19|20)\d{2}(?:年|[-/]\d{1,2}(?:[-/]\d{1,2})?)?$",
    re.IGNORECASE,
)


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_news_query(query: str) -> str:
    """Drop version numbers and dates: headline search indexes subjects, not editions."""

    tokens = [token for token in query.split() if not _VERSION_OR_DATE_TOKEN.match(token)]
    normalized = " ".join(tokens).strip()
    return normalized or query.strip()


def feed_url(query: str, *, window_hours: int) -> str:
    interval = next(
        code for bound, code in _INTERVAL_BY_WINDOW if window_hours <= bound
    )
    return (
        f"{_FEED_URL}?q={quote(query)}&format=rss"
        f"&qft=sortbydate%3d%221%22+interval%3d%22{interval}%22"
    )


def search_page_url(query: str) -> str:
    """Bing News results for a query, as a reader opens them in a browser."""

    return f"{_FEED_URL}?q={quote(query)}"


def _article_url(link: str) -> str:
    parsed = urlparse(link)
    if parsed.path.endswith("/news/apiclick.aspx"):
        target = parse_qs(parsed.query).get("url", [""])[0].strip()
        if target:
            return target
    return link.strip()


def parse_news_feed(xml_text: str) -> list[dict[str, str]]:
    """Return feed items as search rows: url, title, date (ISO), publisher, snippet."""

    root = ET.fromstring(xml_text)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        fields = {child.tag.split("}")[-1].casefold(): (child.text or "").strip() for child in item}
        url = _article_url(fields.get("link", ""))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if not parsed.hostname:
            continue
        if url in seen:
            continue
        try:
            published = parsedate_to_datetime(fields.get("pubdate", ""))
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        seen.add(url)
        rows.append(
            {
                "url": url,
                "title": fields.get("title", ""),
                "date": published.astimezone(timezone.utc).isoformat(),
                "publisher": fields.get("source", ""),
                "snippet": fields.get("description", ""),
            }
        )
    return rows


def rows_within_window(
    rows: list[dict[str, str]], *, now: float, window_hours: int
) -> list[dict[str, str]]:
    floor = now - window_hours * 3600
    kept: list[dict[str, str]] = []
    for row in rows:
        try:
            published = datetime.fromisoformat(row["date"]).timestamp()
        except (KeyError, ValueError):
            continue
        if floor <= published <= now + 3600:
            kept.append(row)
    return kept


# A feed redirect is followed by hand so every hop is checked against the same public-target
# rule; past this many hops the source is treated as broken rather than chased.
_MAX_FEED_REDIRECTS = 5


async def fetch_feed_text(
    url: str, timeout_seconds: int, headers: Mapping[str, str] | None = None
) -> str:
    """GET one public list or feed as text: no cookies, only a browser User-Agent and the given headers."""

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": _USER_AGENT, **(headers or {})}
    ) as session:
        target = url
        for _ in range(_MAX_FEED_REDIRECTS + 1):
            reason = await public_fetch_url_rejection_reason(target)
            if reason is not None:
                raise aiohttp.InvalidURL(f"unsafe fetch target: {reason}")
            async with session.get(target, allow_redirects=False) as response:
                location = response.headers.get("Location")
                if not 300 <= response.status < 400 or not location:
                    response.raise_for_status()
                    return await response.text()
                target = urljoin(target, location)
        raise aiohttp.ClientError(f"more than {_MAX_FEED_REDIRECTS} redirects: {url}")


class BingNewsFeed:
    """Search-provider-shaped facade over the Bing News RSS feed."""

    provider_id = NEWS_FEED_PROVIDER_ID

    def __init__(
        self,
        *,
        feed_fetcher: FeedFetcher = fetch_feed_text,
        now: Callable[[], float] | None = None,
        window_hours: int = NEWS_MAX_WINDOW_HOURS,
    ) -> None:
        self._feed_fetcher = feed_fetcher
        self._now = now
        self._window_hours = window_hours

    async def list_items(
        self, query: str, *, max_results: int, timeout_seconds: int
    ) -> list[dict[str, str]]:
        """Bounded, zero-model listing filtered to the configured window."""

        text = await self._feed_fetcher(feed_url(query, window_hours=self._window_hours), timeout_seconds)
        rows = parse_news_feed(text)
        now = self._now() if self._now is not None else datetime.now(tz=timezone.utc).timestamp()
        return rows_within_window(rows, now=now, window_hours=self._window_hours)[:max_results]

    async def __call__(
        self, query: str, max_results: int, timeout_seconds: int
    ) -> OpenJiuwenNativeSearchResult:
        host_request_id = f"news-feed-{uuid.uuid4().hex}"
        _rejected = (
            not isinstance(query, str)
            or not query.strip()
            or not 1 <= max_results <= 20
            or timeout_seconds <= 0
        )
        if _rejected:
            raise OpenJiuwenNativeSearchContractError(
                "search_request_contract_failed", host_request_id=host_request_id
            )
        try:
            rows = await self.list_items(query, max_results=max_results, timeout_seconds=timeout_seconds)
        except (aiohttp.ClientError, ET.ParseError, OSError) as exc:
            raise RuntimeError(f"news feed request failed: {type(exc).__name__}") from None
        if not rows:
            raise OpenJiuwenNativeSearchContractError(
                "news_feed_no_recent_items", host_request_id=host_request_id
            )
        receipt = {
            "receipt_version": NEWS_FEED_RECEIPT_VERSION,
            "provider_id": NEWS_FEED_PROVIDER_ID,
            "host_request_id": host_request_id,
            "requested_query": query,
            "engine": NEWS_FEED_ENGINE,
            "result_urls": [row["url"] for row in rows],
            "result_count": len(rows),
        }
        return OpenJiuwenNativeSearchResult(engine=NEWS_FEED_ENGINE, rows=tuple(rows), receipt=receipt)


def _news_policy_id() -> str:
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import POLICY_ID
    return POLICY_ID

HOT_LIST_PULL_EVENT_TYPE = "v6_hot_list_pull"
HOT_LIST_PULL_RECEIPT_VERSION = "topic-starter-v6-hot-list-pull-v1"
HOT_LIST_REUSE_SECONDS = 30 * 60
# The matcher reads the top of the list, ads and the pinned term already left out.
HOT_LIST_MATCH_LIMIT = 50
# Each list answers within a second; a pull runs beside the generators, which take far longer.
HOT_LIST_TIMEOUT_SECONDS = 5
WEIBO_PROVIDER_ID = "weibo-hot-band"
WEIBO_HOT_BAND_URL = "https://weibo.com/ajax/statuses/hot_band"
BAIDU_PROVIDER_ID = "baidu-realtime"
BAIDU_REALTIME_URL = "https://top.baidu.com/board?tab=realtime"
_BAIDU_DATA = re.compile(r"<!--s-data:(.*?)-->", re.DOTALL)

HotListFetcher = Callable[[str, int, Mapping[str, str]], Awaitable[str]]


class _UnreadableList(ValueError):
    pass


@dataclass(frozen=True)
class HotListEntry:
    """One listed search term at the position the list shows it."""

    entry_id: str
    provider_id: str
    rank: int
    word: str
    summary: str | None = None
    listed_at: float | None = None


@dataclass(frozen=True)
class HotListPull:
    receipt_id: str
    provider_id: str
    list_url: str
    pulled_at: float
    entries: tuple[HotListEntry, ...]


def hot_event_key(word: str) -> str:
    """One event however a list writes its term: width, case, spacing and punctuation set aside."""

    return "".join(char for char in unicodedata.normalize("NFKC", word).casefold() if char.isalnum())


def load_hot_list_pull(store: TopicStarterStore, receipt_id: Any) -> HotListPull | None:
    """A recorded successful pull by its receipt ID."""

    if not isinstance(receipt_id, str) or not receipt_id:
        return None
    rows = store.query_events(
        event_type=HOT_LIST_PULL_EVENT_TYPE, topic_id=receipt_id, policy_id=_news_policy_id(), limit=1,
    )
    payload = rows[0].get("payload") if rows else None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    return _recorded_pull(payload)


def news_card_event(store: TopicStarterStore, manifest: Mapping[str, Any], card_id: Any) -> str | None:
    """The event a pool's news card was made for: its matched hot-list term, as an event key.

    A report belongs to the term it was found for, so another site's report of
    the same event is the same event.
    """

    slot = manifest.get("news_slot")
    if not isinstance(slot, Mapping) or slot.get("candidate_id") != card_id:
        return None
    pull = load_hot_list_pull(store, slot.get("hot_list_pull_receipt_id"))
    entry = next(
        (item for item in pull.entries if item.entry_id == slot.get("hot_entry_id")), None,
    ) if pull is not None else None
    return hot_event_key(entry.word) if entry is not None else None


def parse_weibo_hot_band(text: str) -> list[HotListEntry]:
    """The hot band's listed terms; ads have no position and the pinned term sits apart."""

    try:
        band = json.loads(text)["data"]["band_list"]
    except (ValueError, KeyError, TypeError) as exc:
        raise _UnreadableList from exc
    if not isinstance(band, list):
        raise _UnreadableList
    entries: list[HotListEntry] = []
    for item in band:
        if not isinstance(item, dict) or item.get("is_ad"):
            continue
        word = str(item.get("word") or "").strip()
        rank = item.get("realpos")
        if not word or not _plain_int(rank):
            continue
        onboard = item.get("onboard_time")
        entries.append(
            HotListEntry(
                entry_id=f"{WEIBO_PROVIDER_ID}:{rank}",
                provider_id=WEIBO_PROVIDER_ID,
                rank=rank,
                word=word,
                listed_at=float(onboard) if type(onboard) in (int, float) else None,
            )
        )
    return entries


def parse_baidu_realtime(html: str) -> list[HotListEntry]:
    """The board's numbered terms with their summaries, read from the page's embedded data."""

    match = _BAIDU_DATA.search(html)
    try:
        content = json.loads(match.group(1))["data"]["cards"][0]["content"] if match else None
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise _UnreadableList from exc
    if not isinstance(content, list):
        raise _UnreadableList
    entries: list[HotListEntry] = []
    for item in content:
        if not isinstance(item, dict) or item.get("isTop"):
            continue
        word = str(item.get("word") or "").strip()
        index = item.get("index")
        if not word or not _plain_int(index):
            continue
        entries.append(
            HotListEntry(
                entry_id=f"{BAIDU_PROVIDER_ID}:{index + 1}",
                provider_id=BAIDU_PROVIDER_ID,
                rank=index + 1,
                word=word,
                summary=str(item.get("desc") or "").strip() or None,
            )
        )
    return entries


_SOURCES: tuple[tuple[str, str, Mapping[str, str], Callable[[str], list[HotListEntry]]], ...] = (
    (WEIBO_PROVIDER_ID, WEIBO_HOT_BAND_URL, {"Referer": "https://weibo.com/"}, parse_weibo_hot_band),
    (BAIDU_PROVIDER_ID, BAIDU_REALTIME_URL, {}, parse_baidu_realtime),
)


class HotListFeed:
    """Pull the current hot list, reusing a pull from the last half hour."""

    def __init__(
        self,
        store: TopicStarterStore,
        *,
        fetcher: HotListFetcher | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._fetcher = fetcher or fetch_feed_text
        self._now = now

    def cached_pull(self) -> HotListPull | None:
        """The last successful pull still inside the reuse window."""

        return self._recent_pull(self._now())

    async def pull(self) -> HotListPull | None:
        """The newest list, or None when every source gave nothing."""

        now = self._now()
        recent = self._recent_pull(now)
        if recent is not None:
            return recent
        for provider_id, url, headers, parse in _SOURCES:
            receipt_id = f"hot-list-pull-{uuid.uuid4().hex}"
            started = time.perf_counter()
            entries: list[HotListEntry] = []
            failure: str | None = None
            try:
                entries = parse(await self._fetcher(url, HOT_LIST_TIMEOUT_SECONDS, headers))
            except aiohttp.ClientResponseError:
                failure = "http_error"
            except asyncio.TimeoutError:
                failure = "timeout"
            except (aiohttp.ClientError, OSError):
                failure = "request_failed"
            except _UnreadableList:
                failure = "unreadable_list"
            if failure is None and not entries:
                failure = "empty_list"
            self._record(
                receipt_id=receipt_id,
                provider_id=provider_id,
                url=url,
                entries=entries,
                failure=failure,
                pulled_at=now,
                latency_ms=(time.perf_counter() - started) * 1_000,
            )
            if failure is None:
                return HotListPull(receipt_id, provider_id, url, now, tuple(entries))
        return None

    def _record(
        self,
        *,
        receipt_id: str,
        provider_id: str,
        url: str,
        entries: list[HotListEntry],
        failure: str | None,
        pulled_at: float,
        latency_ms: float,
    ) -> None:
        self._store.record_event({
            "session_id": "topic-starter-hot-list",
            "slate_id": provider_id,
            "event_type": HOT_LIST_PULL_EVENT_TYPE,
            "topic_id": receipt_id,
            "policy_id": _news_policy_id(),
            "selection_mode": "hot_list_pull",
            "propensity_reason": "not_applicable",
            "payload": {
                "receipt_version": HOT_LIST_PULL_RECEIPT_VERSION,
                "receipt_id": receipt_id,
                "provider_id": provider_id,
                "list_url": url,
                "status": "failure" if failure else "success",
                "failure_reason": failure,
                "entry_count": len(entries),
                "latency_ms": round(latency_ms, 1),
                "pulled_at": pulled_at,
                "entries": [asdict(entry) for entry in entries],
            },
            "created_at": pulled_at,
        })

    def _recent_pull(self, now: float) -> HotListPull | None:
        rows = self._store.query_events(
            event_type=HOT_LIST_PULL_EVENT_TYPE, policy_id=_news_policy_id(), limit=10, newest_first=True,
        )
        for row in rows:
            payload: Any = row.get("payload")
            if not isinstance(payload, dict) or payload.get("status") != "success":
                continue
            if now - float(payload.get("pulled_at") or 0) >= HOT_LIST_REUSE_SECONDS:
                return None
            return _recorded_pull(payload)
        return None


def _recorded_pull(payload: Mapping[str, Any]) -> HotListPull:
    return HotListPull(
        receipt_id=str(payload["receipt_id"]),
        provider_id=str(payload["provider_id"]),
        list_url=str(payload["list_url"]),
        pulled_at=float(payload["pulled_at"]),
        entries=tuple(HotListEntry(**entry) for entry in payload["entries"]),
    )
