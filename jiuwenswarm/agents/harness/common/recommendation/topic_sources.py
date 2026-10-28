# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Packaged starter corpus and live-retrieval admission for topic recommendation."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from typing import IO, Any, Protocol, Sequence
from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.recommendation.topic_url_safety import (
    public_fetch_url_rejection_reason,
)
from jiuwenswarm.agents.harness.common.tools.search_tools import (
    run_free_search_structured,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_page_fetch import (
    fetch_msn_detail_sync,
    fetch_topic_page_sync,
    msn_detail_api_url,
)

RETRIEVAL_CONFIG_VERSION = "topic-starter-live-retrieval-v3"
MAX_SEARCH_RESULTS = 5
MAX_FETCHES_PER_CANDIDATE = 3
SEARCH_TIMEOUT_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 30
EVIDENCE_TEXT_LIMIT = 2_000

SearchFn = Callable[
    [str, int, int],
    Awaitable[tuple[str, list[dict[str, str]]]],
]
FetchFn = Callable[[str, int], Awaitable[dict[str, Any]]]


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class SourceAssessment:
    kind: str
    publisher: str


SourceClassifier = Callable[[str, str], SourceAssessment]


async def default_search(
    query: str,
    max_results: int,
    timeout_seconds: int,
) -> tuple[str, list[dict[str, str]]]:
    return await asyncio.to_thread(
        run_free_search_structured,
        query,
        max_results,
        timeout_seconds,
    )


async def default_fetch(url: str, timeout_seconds: int) -> dict[str, Any]:
    # Search results are remote input: never fetch a target that resolves inside the network.
    reason = await public_fetch_url_rejection_reason(url)
    if reason is not None:
        raise ValueError(f"unsafe fetch target: {reason}")
    # An msn article page renders its text by script; its detail API is that page's fetch.
    api_url = msn_detail_api_url(url)
    if api_url:
        return await asyncio.to_thread(
            fetch_msn_detail_sync, url, api_url, timeout_seconds, public_https=True,
        )
    return await asyncio.to_thread(
        fetch_topic_page_sync, url, timeout_seconds, public_https=True,
    )


def default_source_classifier(url: str, _content: str) -> SourceAssessment:
    """Conservative host classification; unknown publishers fail closed."""

    hostname = (urlparse(url).hostname or "").casefold().removeprefix("www.")
    authoritative_suffixes = (
        ".gov.cn",
        ".gov",
        ".edu.cn",
        ".edu",
        ".ac.cn",
        "who.int",
        "un.org",
    )
    kind = (
        "authoritative"
        if hostname
        and any(
            hostname == suffix.lstrip(".") or hostname.endswith(suffix)
            for suffix in authoritative_suffixes
        )
        else "unknown"
    )
    return SourceAssessment(kind=kind, publisher=hostname)


def _parse_published_at(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _date_is_fresh(page: dict[str, Any], freshness_class: str, now: float) -> bool:
    if freshness_class == "evergreen_corpus":
        return True
    published_at = _parse_published_at(page.get("published_at"))
    if published_at is None or not str(page.get("published_at_source") or "").strip():
        return False
    age = now - published_at
    if age < 0:
        return False
    limit_days = 7 if freshness_class == "current_event" else 90
    return age <= limit_days * 24 * 60 * 60


def _lexically_admitted(candidate: dict[str, Any], content: str) -> bool:
    identity = str(candidate.get("identity_anchor") or "")
    context = [
        str(anchor)
        for anchor in (candidate.get("context_anchors") or [])
        if isinstance(anchor, str) and anchor
    ]
    if not identity:
        return True
    if identity not in content:
        return False
    return not context or any(anchor in content for anchor in context)


def _admitted_source_set(
    sources: list[dict[str, Any]],
    freshness_class: str,
) -> list[dict[str, Any]]:
    if freshness_class == "evergreen_corpus":
        return [
            source
            for source in sources
            if source.get("source_kind") in {"authoritative", "stable"}
        ][:1]
    primary = [
        source
        for source in sources
        if source.get("source_kind") in {"authoritative", "original"}
    ]
    if primary:
        return primary[:1]
    reliable: list[dict[str, Any]] = []
    publishers: set[str] = set()
    leading_texts: set[str] = set()
    for source in sources:
        if source.get("source_kind") != "reliable_report":
            continue
        publisher = str(source.get("publisher") or "")
        leading = str((source.get("content_boundary") or {}).get("text") or "")[:200]
        if not publisher:
            continue
        if publisher in publishers:
            continue
        if leading in leading_texts:
            continue
        publishers.add(publisher)
        leading_texts.add(leading)
        reliable.append(source)
        if len(reliable) == 2:
            return reliable
    return []


def _source_record(
    page: dict[str, Any],
    assessment: SourceAssessment,
    *,
    search_engine: str,
    now: float,
) -> dict[str, Any]:
    content = str(page.get("content") or "").strip()
    boundary_text = content[:EVIDENCE_TEXT_LIMIT]
    record: dict[str, Any] = {
        "source_url": str(page.get("url") or ""),
        "source_kind": assessment.kind,
        "publisher": assessment.publisher,
        "search_engine": search_engine,
        "fetch_reader": str(page.get("fetch_reader") or "direct"),
        "retrieved_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "content_boundary": {
            "start": 0,
            "end": len(boundary_text),
            "text": boundary_text,
        },
    }
    published_at = page.get("published_at")
    if isinstance(published_at, str) and published_at.strip():
        record["published_at"] = published_at
        record["published_at_source"] = str(
            page.get("published_at_source") or ""
        )
    return record


async def _ground_candidate(
    candidate: dict[str, Any],
    *,
    search: SearchFn,
    fetch: FetchFn,
    classify_source: SourceClassifier,
    now: float,
    fetch_cache: dict[str, asyncio.Task[dict[str, Any]]],
) -> dict[str, Any] | None:
    freshness_class = str(candidate.get("freshness_class") or "")
    if freshness_class not in {
        "current_event",
        "recent_development",
        "evergreen_corpus",
    }:
        return None
    primary_query = str(candidate.get("primary_query") or "").strip()
    fallback_query = str(candidate.get("fallback_query") or "").strip()
    if not primary_query:
        return None

    admitted: list[dict[str, Any]] = []
    search_results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    fetch_count = 0
    selected: list[dict[str, Any]] = []
    for query_kind, query in (("primary", primary_query), ("fallback", fallback_query)):
        if not query or (query_kind == "fallback" and selected):
            continue
        try:
            search_engine, rows = await search(
                query,
                MAX_SEARCH_RESULTS,
                SEARCH_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - candidate-level fail closed
            rows = []
            search_engine = ""
        bounded_rows = rows[:MAX_SEARCH_RESULTS] if isinstance(rows, list) else []
        search_results.extend(
            {
                "query_kind": query_kind,
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "snippet": str(row.get("snippet") or ""),
            }
            for row in bounded_rows
            if isinstance(row, dict)
        )
        for row in bounded_rows:
            if fetch_count >= MAX_FETCHES_PER_CANDIDATE or not isinstance(row, dict):
                break
            url = str(row.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            if not parsed.netloc:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            fetch_count += 1
            try:
                fetch_task = fetch_cache.get(url)
                if fetch_task is None:
                    fetch_task = asyncio.create_task(
                        fetch(url, FETCH_TIMEOUT_SECONDS)
                    )
                    fetch_cache[url] = fetch_task
                page = await fetch_task
            except Exception:  # noqa: BLE001 - one URL does not fail the batch
                continue
            try:
                status_code = int(page.get("status_code") or 0)
            except (TypeError, ValueError):
                continue
            content = str(page.get("content") or "").strip()
            if not 200 <= status_code < 300 or not content:
                continue
            if not _date_is_fresh(page, freshness_class, now):
                continue
            if not _lexically_admitted(candidate, content):
                continue
            final_url = str(page.get("url") or url)
            assessment = classify_source(final_url, content)
            if not assessment.publisher or assessment.kind == "unknown":
                continue
            admitted.append(
                _source_record(
                    page,
                    assessment,
                    search_engine=search_engine,
                    now=now,
                )
            )
        selected = _admitted_source_set(admitted, freshness_class)
        if fetch_count >= MAX_FETCHES_PER_CANDIDATE:
            break

    if not selected:
        return None
    primary = selected[0]
    bundle: dict[str, Any] = {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "fetch_status": "success",
        "source_url": primary["source_url"],
        "source_kind": primary["source_kind"],
        "search_engine": primary["search_engine"],
        "fetch_reader": primary["fetch_reader"],
        "retrieved_at": primary["retrieved_at"],
        "retrieval_recency_capability": "post_filter_only",
        "retrieval_config_version": RETRIEVAL_CONFIG_VERSION,
        "content_boundary": primary["content_boundary"],
        "search_results": search_results,
        "sources": selected,
    }
    if "published_at" in primary:
        bundle["published_at"] = primary["published_at"]
        bundle["published_at_source"] = primary["published_at_source"]
    return {**candidate, "evidence_bundle": bundle}


async def ground_candidate(
    candidate: dict[str, Any],
    *,
    search: SearchFn = default_search,
    fetch: FetchFn = default_fetch,
    classify_source: SourceClassifier = default_source_classifier,
    now: float,
) -> dict[str, Any] | None:
    """Ground one frozen candidate without letting retrieval change its plan."""

    return await _ground_candidate(
        candidate,
        search=search,
        fetch=fetch,
        classify_source=classify_source,
        now=now,
        fetch_cache={},
    )


async def ground_candidates(
    candidates: list[dict[str, Any]],
    *,
    search: SearchFn = default_search,
    fetch: FetchFn = default_fetch,
    classify_source: SourceClassifier = default_source_classifier,
    now: float,
) -> list[dict[str, Any]]:
    """Ground a batch with one URL fetch cache and no cross-candidate refill."""

    fetch_cache: dict[str, asyncio.Task[dict[str, Any]]] = {}
    results = await asyncio.gather(
        *(
            _ground_candidate(
                candidate,
                search=search,
                fetch=fetch,
                classify_source=classify_source,
                now=now,
                fetch_cache=fetch_cache,
            )
            for candidate in candidates
        )
    )
    return [result for result in results if result is not None]

CORPUS_VERSION = "topic-starter-corpus-v1"
CORPUS_RESOURCE = "resources/recommendation/topic_corpus.jsonl"
TOPIC_BUCKETS = frozenset({"生活", "科学", "文化", "实用", "好奇"})
HOOK_TYPES = frozenset({"surprise", "relatability", "self_curiosity"})
FRESHNESS_CLASSES = frozenset(
    {"current_event", "recent_development", "evergreen_corpus"}
)
RETRIEVAL_TOP_K = 24
I3_RETRIEVAL_PROMPT_VERSION = "topic-starter-i3-retrieval-v1"
_I3_SYSTEM_PROMPT = f"""\
You are the fixed starter retriever ({I3_RETRIEVAL_PROMPT_VERSION}).
Rank at most top_k entries from the supplied corpus for the supplied interests.
Use only starter_id values from corpus and interest_summary_id values from
ranked_interests. Return exactly
{{"hits":[{{"starter_id":"...","interest_summary_id":"..."}}]}}.
For a cold request with no ranked_interests, interest_summary_id must be null.
Never return starter text, rewrites, explanations, scores, or extra fields.
"""
_CORPUS_FIELDS = frozenset(
    {
        "starter_id",
        "text",
        "topic",
        "hook_type",
        "freshness_class",
        "created_at",
        "expires_at",
        "corpus_version",
        "origin",
    }
)


class FixedStarterCorpusError(ValueError):
    """The package-local corpus does not satisfy its frozen contract."""


class FixedStarterRetrievalError(ValueError):
    """The I3 output violates the frozen ID-only retrieval contract."""

    reason = "retrieval_contract_failed"


@dataclass(frozen=True, slots=True)
class FixedStarter:
    starter_id: str
    text: str
    topic: str
    hook_type: str
    freshness_class: str
    created_at: str
    expires_at: str | None
    corpus_version: str
    origin: str


SourceLocator = tuple[str, int]


@dataclass(frozen=True, slots=True)
class RankedInterest:
    interest_summary_id: str
    interest_summary: str
    rank: int
    allowed_anchors: tuple[str, ...]
    source_locators: tuple[SourceLocator, ...]


@dataclass(frozen=True, slots=True)
class FixedStarterRankingRequest:
    ranked_interests: tuple[RankedInterest, ...]
    corpus: tuple[FixedStarter, ...]
    corpus_version: str
    top_k: int


class FixedStarterRankingAdapter(Protocol):
    """Replaceable I3 model boundary; Ark and test stubs share this contract."""

    async def rank_fixed_starters(
        self,
        request: FixedStarterRankingRequest,
    ) -> Any:
        """Rank the fixed starter corpus for one request."""
        ...


I3StageInvoker = Callable[[str, str, str], Awaitable[Any]]


class ArkFixedStarterRankingAdapter:
    """I3-specific Ark prompt adapter over the shared model invocation seam."""

    def __init__(self, invoke_stage: I3StageInvoker) -> None:
        self._invoke_stage = invoke_stage

    async def rank_fixed_starters(self, request: FixedStarterRankingRequest) -> Any:
        payload = {
            "corpus_version": request.corpus_version,
            "top_k": request.top_k,
            "ranked_interests": [
                {
                    "interest_summary_id": interest.interest_summary_id,
                    "interest_summary": interest.interest_summary,
                    "rank": interest.rank,
                    "allowed_anchors": list(interest.allowed_anchors),
                }
                for interest in request.ranked_interests
            ],
            "corpus": [
                {
                    "starter_id": starter.starter_id,
                    "text": starter.text,
                    "topic": starter.topic,
                    "hook_type": starter.hook_type,
                }
                for starter in request.corpus
            ],
        }
        return await self._invoke_stage(
            "fixed_starter_retriever",
            _I3_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )


@dataclass(frozen=True, slots=True)
class StarterHit:
    starter_id: str
    interest_summary_id: str | None
    rank: int
    text: str
    topic: str
    hook_type: str
    freshness_class: str
    created_at: str
    expires_at: str | None
    corpus_version: str
    origin: str
    matched_anchor: str | None
    source_locators: tuple[SourceLocator, ...]


def _open_corpus() -> IO[str]:
    resource = resources.files("jiuwenswarm").joinpath(CORPUS_RESOURCE)
    return resource.open(encoding="utf-8")


def _required_string(row: dict[str, Any], field: str, line_number: int) -> str:
    value = row[field]
    if not isinstance(value, str) or not value.strip():
        raise FixedStarterCorpusError(
            f"line {line_number}: {field} must be a non-empty string"
        )
    return value


def _parse_starter(row: Any, line_number: int) -> FixedStarter:
    if not isinstance(row, dict) or set(row) != _CORPUS_FIELDS:
        raise FixedStarterCorpusError(
            f"line {line_number}: starter schema does not match corpus v1"
        )
    starter = FixedStarter(
        starter_id=_required_string(row, "starter_id", line_number),
        text=_required_string(row, "text", line_number),
        topic=_required_string(row, "topic", line_number),
        hook_type=_required_string(row, "hook_type", line_number),
        freshness_class=_required_string(row, "freshness_class", line_number),
        created_at=_required_string(row, "created_at", line_number),
        expires_at=row["expires_at"],
        corpus_version=_required_string(row, "corpus_version", line_number),
        origin=_required_string(row, "origin", line_number),
    )
    if starter.topic not in TOPIC_BUCKETS:
        raise FixedStarterCorpusError(
            f"line {line_number}: unsupported topic bucket {starter.topic!r}"
        )
    if starter.hook_type not in HOOK_TYPES:
        raise FixedStarterCorpusError(
            f"line {line_number}: unsupported hook_type {starter.hook_type!r}"
        )
    if starter.freshness_class not in FRESHNESS_CLASSES:
        raise FixedStarterCorpusError(
            "line "
            f"{line_number}: unsupported freshness_class "
            f"{starter.freshness_class!r}"
        )
    if starter.expires_at is not None and not isinstance(starter.expires_at, str):
        raise FixedStarterCorpusError(
            f"line {line_number}: expires_at must be a string or null"
        )
    if starter.origin != "project_authored":
        raise FixedStarterCorpusError(
            f"line {line_number}: origin must be 'project_authored'"
        )
    return starter


def load_fixed_starter_corpus(
    corpus_version: str,
) -> tuple[FixedStarter, ...]:
    """Load and validate one immutable version of the package-local corpus."""

    starters: list[FixedStarter] = []
    with _open_corpus() as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                raise FixedStarterCorpusError(
                    f"line {line_number}: blank JSONL rows are not allowed"
                )
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise FixedStarterCorpusError(
                    f"line {line_number}: invalid JSON"
                ) from exc
            starters.append(_parse_starter(row, line_number))

    if not 100 <= len(starters) <= 150:
        raise FixedStarterCorpusError(
            "corpus must contain between 100 and 150 starters"
        )
    starter_ids = [starter.starter_id for starter in starters]
    if len(starter_ids) != len(set(starter_ids)):
        raise FixedStarterCorpusError("starter_id must be unique")
    versions = {starter.corpus_version for starter in starters}
    if versions != {corpus_version}:
        raise FixedStarterCorpusError(
            f"corpus version mismatch: expected {corpus_version!r}"
        )
    bucket_counts = Counter(starter.topic for starter in starters)
    if set(bucket_counts) != TOPIC_BUCKETS or any(
        not 20 <= count <= 30 for count in bucket_counts.values()
    ):
        raise FixedStarterCorpusError(
            "each topic bucket must contain between 20 and 30 starters"
        )
    return tuple(starters)


def _retrieval_contract_failed(message: str) -> FixedStarterRetrievalError:
    return FixedStarterRetrievalError(f"{FixedStarterRetrievalError.reason}: {message}")


def _validate_ranked_interests(
    ranked_interests: Sequence[RankedInterest],
) -> tuple[RankedInterest, ...]:
    frozen = tuple(ranked_interests)
    interest_ids = [interest.interest_summary_id for interest in frozen]
    if any(not interest_id for interest_id in interest_ids):
        raise _retrieval_contract_failed("interest_summary_id must be non-empty")
    if len(interest_ids) != len(set(interest_ids)):
        raise _retrieval_contract_failed("interest_summary_id must be unique")
    if any(not interest.source_locators for interest in frozen):
        raise _retrieval_contract_failed("ranked interests must retain source locators")
    return frozen


def _validate_and_materialize_hits(
    response: Any,
    request: FixedStarterRankingRequest,
) -> tuple[StarterHit, ...]:
    if not isinstance(response, dict) or set(response) != {"hits"}:
        raise _retrieval_contract_failed("response must contain only hits")
    raw_hits = response["hits"]
    if not isinstance(raw_hits, list):
        raise _retrieval_contract_failed("hits must be a list")
    if len(raw_hits) > request.top_k:
        raise _retrieval_contract_failed("response exceeds requested top_k")

    corpus_by_id = {starter.starter_id: starter for starter in request.corpus}
    interests_by_id = {
        interest.interest_summary_id: interest for interest in request.ranked_interests
    }
    seen_starter_ids: set[str] = set()
    materialized: list[StarterHit] = []
    for rank, raw_hit in enumerate(raw_hits, start=1):
        if not isinstance(raw_hit, dict) or set(raw_hit) != {
            "starter_id",
            "interest_summary_id",
        }:
            raise _retrieval_contract_failed(
                "each hit must contain only starter_id and interest_summary_id"
            )
        starter_id = raw_hit["starter_id"]
        interest_summary_id = raw_hit["interest_summary_id"]
        if not isinstance(starter_id, str) or not starter_id:
            raise _retrieval_contract_failed("starter_id must be non-empty")
        if starter_id in seen_starter_ids:
            raise _retrieval_contract_failed("starter_id must not repeat")
        seen_starter_ids.add(starter_id)
        starter = corpus_by_id.get(starter_id)
        if starter is None:
            raise _retrieval_contract_failed(
                "starter_id is not in the requested corpus version"
            )

        matched_anchor: str | None = None
        source_locators: tuple[SourceLocator, ...] = ()
        if interest_summary_id is None:
            if request.ranked_interests:
                raise _retrieval_contract_failed(
                    "warm request hit must reference a ranked interest"
                )
        else:
            if not isinstance(interest_summary_id, str):
                raise _retrieval_contract_failed(
                    "interest_summary_id must be a string or null"
                )
            interest = interests_by_id.get(interest_summary_id)
            if interest is None:
                raise _retrieval_contract_failed(
                    "interest_summary_id is not in the ranked input"
                )
            matched_anchor = next(
                (
                    anchor
                    for anchor in interest.allowed_anchors
                    if anchor and anchor in starter.text
                ),
                None,
            )
            if matched_anchor is None:
                raise _retrieval_contract_failed(
                    "warm hit does not contain an allowed interest anchor"
                )
            source_locators = interest.source_locators

        materialized.append(
            StarterHit(
                starter_id=starter.starter_id,
                interest_summary_id=interest_summary_id,
                rank=rank,
                text=starter.text,
                topic=starter.topic,
                hook_type=starter.hook_type,
                freshness_class=starter.freshness_class,
                created_at=starter.created_at,
                expires_at=starter.expires_at,
                corpus_version=starter.corpus_version,
                origin=starter.origin,
                matched_anchor=matched_anchor,
                source_locators=source_locators,
            )
        )
    return tuple(materialized)


async def retrieve_fixed_starters(
    ranked_interests: Sequence[RankedInterest],
    corpus_version: str,
    *,
    adapter: FixedStarterRankingAdapter,
    top_k: int = RETRIEVAL_TOP_K,
) -> tuple[StarterHit, ...]:
    """Run independent I3 ranking, then validate IDs and exact-fill corpus text."""

    if not _plain_int(top_k) or not 1 <= top_k <= RETRIEVAL_TOP_K:
        raise _retrieval_contract_failed(
            f"top_k must be between 1 and {RETRIEVAL_TOP_K}"
        )
    request = FixedStarterRankingRequest(
        ranked_interests=_validate_ranked_interests(ranked_interests),
        corpus=load_fixed_starter_corpus(corpus_version),
        corpus_version=corpus_version,
        top_k=top_k,
    )
    response = await adapter.rank_fixed_starters(request)
    return _validate_and_materialize_hits(response, request)
