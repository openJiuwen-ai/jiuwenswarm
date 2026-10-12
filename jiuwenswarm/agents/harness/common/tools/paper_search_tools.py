# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``paper_search``: scholarly search over arXiv, OpenAlex, Crossref, PubMed, Semantic Scholar and
OpenReview, plus Google Scholar through a configured search API.

The retrieval rules are the ones Research Harness (RH) runs in production; each
section names the RH rule it matches and the failure it prevents. Only the
standard library is used: ``urllib`` for HTTP, ``xml.etree`` for arXiv Atom and
PubMed XML.

Call chain::

    paper_search                      async tool; runs search_papers in a worker thread
      search_papers                   query hygiene -> fan-out -> dedupe -> rank -> merge -> verdict
        _fan_out                      every (query, source) pair in parallel under one time budget
          <Source>Provider.search     builds the request, parses the answer into Paper rows
            _Provider._get            pace (per-host clock) -> _CircuitBreaker.call -> fetcher
              urllib_fetch            HTTP GET with 429/5xx retry and capped Retry-After
            _KeyedProvider._get_rotating   per-key cooldown and rotation (S2 / OpenAlex)
        _dedupe                       fingerprint + title/year index, merge_papers
        rank_key                      query-term coverage first, source only as a prior
        round_robin_merge             each query gets one slot before any query gets two
        prior_art_read_stands         whether this read is healthy enough to argue absence from
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Hashable, Iterable, Mapping, Sequence

from openjiuwen.core.foundation.tool import tool

try:
    import fcntl
except ImportError:  # no flock (Windows): pacing falls back to this process alone
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: ``fetcher(url, headers) -> body text``. Providers take one so tests can inject a fake.
Fetcher = Callable[[str, dict[str, str]], str]


# ---------------------------------------------------------------------------
# Rule 1 - one request clock per rate-limited host (same as RH host_pacer).
#
# arXiv, NCBI and Semantic Scholar limit requests per client address (S2's
# keyed tier per key), not per object or per process. A per-object timestamp
# lets two concurrent searches (two team members, or two queries of one call)
# fire back-to-back and draw 429s or a temporary ban. ``pace`` keeps the last
# request time in a file under an exclusive flock, so every thread and process
# of this user waits on the same clock; where the file cannot be trusted it
# falls back to an in-process lock per clock name.
# ---------------------------------------------------------------------------

#: Minimum seconds between two requests on one clock, same values as RH. arXiv asks
#: for one request per 3 s; NCBI allows 3 req/s without a key; S2 allows 1 req/s per
#: key (RH keeps a 5% margin). RH does not pace OpenAlex or Crossref: one search is far
#: below their limits, and their 429s are handled by the retry in ``urllib_fetch``.
PACE_INTERVAL_S: dict[str, float] = {
    "arxiv": 3.1,
    "pubmed": 0.34,
    "semantic_scholar": 1.05,
    "openalex": 0.0,
    "crossref": 0.0,
    "openreview": 1.0,
    "google_scholar": 1.0,
}
_PACE_POLL_S = 0.02
#: The longest a caller waits on a holder that keeps advancing the stamp: max(10 intervals, this).
_PACE_MAX_WAIT_FLOOR_S = 30.0
_local_last: dict[str, float] = {}
_local_guard = threading.Lock()
_local_locks: dict[str, threading.Lock] = {}


def _pace_dir() -> Path | None:
    """``$XDG_CACHE_HOME/jiuwenswarm/pace`` (default ``~/.cache/...``); None when there is no home."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    try:
        cache = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".cache"
    except (RuntimeError, KeyError):
        return None
    return cache / "jiuwenswarm" / "pace"


def _open_pace_file(name: str) -> int | None:
    """Open clock file ``name``, or None when it cannot be trusted.

    Same checks as RH: the directory is created 0700 and must belong to this
    uid with no group/other write bit; neither the directory nor the file is
    followed through a symlink (O_NOFOLLOW), and a file with a second hard link
    is refused. Each check stops another account from redirecting our clock
    into a file it controls.
    """
    directory = _pace_dir() if fcntl is not None else None
    if directory is None:
        return None
    try:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass  # made by another process, or planted: the checks below read what was opened
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(dir_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                return None
            for attempt in range(3):
                try:
                    fd = os.open(f"{name}.pace", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
                    break
                except FileNotFoundError:
                    # macOS answers ENOENT to an O_CREAT that races another creator of the name.
                    if attempt == 2:
                        raise
        finally:
            os.close(dir_fd)
    except OSError:
        return None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        return None
    return fd


def _stamp(fd: int) -> float:
    try:
        return float(os.pread(fd, 64, 0).decode("ascii").strip() or 0)
    except (OSError, ValueError, UnicodeDecodeError):
        return 0.0


def _record(fd: int) -> None:
    os.ftruncate(fd, 0)
    os.pwrite(fd, repr(time.time()).encode("ascii"), 0)


def _wait_after(last: float, interval: float) -> None:
    remaining = last + interval - time.time()
    if remaining > 0:
        time.sleep(min(remaining, interval))  # a stamp in the future costs at most one interval


def _lock(fd: int, interval: float) -> bool:
    """Take the flock, waiting while a live holder keeps advancing the stamp.

    Returns False after two intervals with no advance (a stopped holder), or
    after max(10 intervals, 30 s) in all (a holder that rewrites the stamp but
    never releases), so a stuck process cannot hang every search of this user.
    """
    seen, idle_since = _stamp(fd), time.monotonic()
    deadline = idle_since + max(10 * interval, _PACE_MAX_WAIT_FLOOR_S)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, PermissionError):
            pass
        now = _stamp(fd)
        if now != seen:
            seen, idle_since = now, time.monotonic()
        elif time.monotonic() - idle_since >= 2 * interval:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PACE_POLL_S)


def _local_pace(name: str, interval: float) -> None:
    with _local_guard:
        guard = _local_locks.setdefault(name, threading.Lock())
    with guard:  # one clock's wait does not hold up another clock
        remaining = _local_last.get(name, 0.0) + interval - time.monotonic()
        if remaining > 0:
            time.sleep(min(remaining, interval))
        _local_last[name] = time.monotonic()


def pace(name: str, interval: float) -> None:
    """Return once ``interval`` seconds have passed since the last request on clock ``name``; record this one."""
    if interval <= 0:
        return
    fd = _open_pace_file(name)
    if fd is None:
        _local_pace(name, interval)
        return
    try:
        if _lock(fd, interval):
            try:
                _wait_after(_stamp(fd), interval)
                _record(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            # The holder stopped advancing the stamp: pace on its last stamp without the lock.
            _wait_after(_stamp(fd), interval)
            _record(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Rule 3 - bounded retry with backoff, and one circuit breaker per source
# (same as RH paper_source_clients._fetch_with_retry and core.circuit_breaker).
# ---------------------------------------------------------------------------

_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
#: Crossref hands out long Retry-After values on 429; honouring one uncapped would
#: stall this source past the whole fan-out budget.
_RETRY_AFTER_CAP_S = 15.0


def _retry_after_s(exc: urllib.error.HTTPError, default: float) -> float:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    try:
        seconds = max(float(raw), 0.5) if raw is not None else default
    except ValueError:
        seconds = default
    return min(seconds, _RETRY_AFTER_CAP_S)


def urllib_fetch(url: str, headers: dict[str, str], timeout: float = 30.0) -> str:
    """GET ``url`` and return the body text.

    429/5xx and network errors are transient: retry up to 3 times, waiting
    1 s, 2 s, 4 s or the server's Retry-After (capped at 15 s). Any other HTTP
    status (400, 401, 403, 404) is an answer, not a hiccup, and raises at once.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP or attempt == _MAX_RETRIES:
                raise
            delay = _retry_after_s(exc, 2.0**attempt)
            exc.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == _MAX_RETRIES:
                raise
            delay = 2.0**attempt
        logger.warning("paper_search: transient error from %s, retry %d in %.1fs", url.split("?")[0], attempt + 1, delay)
        time.sleep(delay)
    raise RuntimeError("unreachable")  # every iteration returns, raises or sleeps and retries


class CircuitOpenError(RuntimeError):
    """A call refused because the source's circuit breaker is open."""


class _CircuitBreaker:
    """Refuse a source after consecutive failures, then probe it again after a pause.

    A source that is down keeps failing after its retries; without a breaker
    every further query of the run pays the full retry ladder again and eats
    the time budget the healthy sources need. After ``threshold`` consecutive
    failures calls are refused for ``recovery_s``; the first call after that is
    a probe, and a failed probe doubles the pause (up to ``max_recovery_s``).
    """

    def __init__(self, name: str, threshold: int, recovery_s: float, max_recovery_s: float):
        self.name = name
        self._threshold = threshold
        self._initial_recovery = self._recovery = recovery_s
        self._max_recovery = max_recovery_s
        self._failures = 0
        self._tripped = False
        self._open_until = 0.0
        self._lock = threading.Lock()

    def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        with self._lock:
            remaining = self._open_until - time.monotonic()
        if remaining > 0:
            raise CircuitOpenError(f"Circuit breaker for '{self.name}' is open; retry after {remaining:.0f}s")
        try:
            result = fn(*args)
        except Exception:
            self._on_failure()
            raise
        with self._lock:
            self._failures, self._tripped, self._recovery = 0, False, self._initial_recovery
        return result

    def _on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            probe_failed = self._tripped
            if probe_failed:
                self._recovery = min(self._recovery * 2, self._max_recovery)
            if probe_failed or self._failures >= self._threshold:
                self._tripped = True
                self._open_until = time.monotonic() + self._recovery


#: (threshold, recovery_s, max_recovery_s). S2 is lenient as in RH: its 429 is rate
#: limiting, not an outage, so it takes 8 failures to trip and recovers sooner.
_BREAKER_SETTINGS: dict[str, tuple[int, float, float]] = {"semantic_scholar": (8, 30.0, 300.0)}
_DEFAULT_BREAKER = (3, 60.0, 600.0)
_breakers: dict[str, _CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def _breaker(name: str) -> _CircuitBreaker:
    """Process-wide breaker for source ``name``, so an outage seen by one call is known to the next."""
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = _CircuitBreaker(name, *_BREAKER_SETTINGS.get(name, _DEFAULT_BREAKER))
        return _breakers[name]


# ---------------------------------------------------------------------------
# Rule 2 - per-key cooldown and rotation (same as RH key_pool and the S2 /
# OpenAlex providers).
#
# S2 and OpenAlex limit each API key separately. A 429/401/403 on one key says
# nothing about the others: failing the source on it throws away the rest of
# the pool, and retrying the same key keeps hitting its limit. The key cools
# down (429: 60 s, one rate window; 401/403: 3600 s, a refused key will not
# recover within a run) and the request moves to the next live key. With no
# other live key the error propagates.
# ---------------------------------------------------------------------------

_KEY_SPLIT = re.compile(r"[,;\s]+")
_KEY_ENV: dict[str, tuple[str, ...]] = {
    "semantic_scholar": ("SEMANTIC_SCHOLAR_API_KEYS", "SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY"),
    "openalex": ("OPENALEX_API_KEYS", "OPENALEX_API_KEY"),
    "google_scholar": ("GOOGLE_SCHOLAR_API_KEYS", "GOOGLE_SCHOLAR_API_KEY"),
}
_KEY_COOLDOWN_S = {429: 60.0, 401: 3600.0, 403: 3600.0}


class KeyPool:
    """Round-robin API keys with a per-key cooldown (thread-safe)."""

    def __init__(self, keys: Iterable[str]):
        self.keys = tuple(dict.fromkeys(key for key in keys if key))
        self._index = 0
        self._cooling_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self.keys)

    def get(self) -> str:
        """Next key that is not cooling down; empty when none is live."""
        now = time.monotonic()
        with self._lock:
            for _ in range(len(self.keys)):
                key = self.keys[self._index % len(self.keys)]
                self._index += 1
                if now >= self._cooling_until.get(key, 0.0):
                    return key
        return ""

    def cooldown(self, key: str, seconds: float) -> None:
        with self._lock:
            self._cooling_until[key] = time.monotonic() + seconds


_pools: dict[str, KeyPool] = {}
_pools_lock = threading.Lock()


def _env_pool(provider: str) -> KeyPool:
    """Process-wide pool read from the provider's env vars, so a cooldown outlives one tool call."""
    keys = tuple(
        dict.fromkeys(
            key
            for env_name in _KEY_ENV[provider]
            for key in _KEY_SPLIT.split(os.environ.get(env_name, "").strip())
            if key
        )
    )
    with _pools_lock:
        pool = _pools.get(provider)
        if pool is None or pool.keys != keys:
            pool = _pools[provider] = KeyPool(keys)
        return pool


# ---------------------------------------------------------------------------
# Paper rows and the per-source providers
# ---------------------------------------------------------------------------


@dataclass
class Paper:
    """One paper as a source reported it; after ``merge_papers``, as all sources reported it."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    abstract: str = ""
    doi: str = ""
    arxiv_id: str = ""
    s2_id: str = ""
    openalex_id: str = ""
    pmid: str = ""
    url: str = ""
    citation_count: int | None = None
    open_pdf: bool = False
    provider: str = ""  # the source whose metadata currently wins field merges
    sources: list[str] = field(default_factory=list)


def _squash(value: Any) -> str:
    return " ".join(str(value or "").split())


def _xml_text(element: ET.Element | None) -> str:
    """All text under ``element`` (PubMed titles carry inline markup such as <i>)."""
    return "" if element is None else _squash(" ".join(element.itertext()))


def _year(value: Any) -> int | None:
    text = str(value or "")
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() and 1000 <= int(text[:4]) <= 9999 else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _in_years(year: int | None, year_from: int | None, year_to: int | None) -> bool:
    """A row with no year fails any bound, as in RH: it cannot be shown to be in range."""
    if year_from is not None and (year is None or year < year_from):
        return False
    return not (year_to is not None and (year is None or year > year_to))


_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_DOI_SHAPE = re.compile(r"^10\.\d{4,9}/\S+$")
# A DOI read off a landing-page URL can carry the page's view; the DOI registry knows
# 10.1051/itmconf/20268401001, not 10.1051/itmconf/20268401001/pdf.
_DOI_VIEW_SUFFIX = re.compile(r"/(?:pdf|epdf|full|fulltext|abstract|html)$", re.IGNORECASE)


def normalize_doi(value: str) -> str:
    """Strip the resolver/``doi:`` prefix (OpenAlex reports DOIs as https://doi.org/... URLs)
    and a trailing page view such as ``/pdf``."""
    return _DOI_VIEW_SUFFIX.sub("", _DOI_PREFIX.sub("", (value or "").strip()))


class _Provider:
    """One scholarly source. ``search`` returns that source's rows for one query."""

    name = ""

    def __init__(self, fetcher: Fetcher | None = None):
        self._fetcher = fetcher or urllib_fetch

    def _get(self, url: str, headers: dict[str, str], clock: str | None = None) -> str:
        pace(clock or self.name, PACE_INTERVAL_S[self.name])
        return _breaker(self.name).call(self._fetcher, url, headers)

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        raise NotImplementedError


class _KeyedProvider(_Provider):
    """A source whose optional API keys rotate under Rule 2."""

    def __init__(self, fetcher: Fetcher | None = None, keys: Sequence[str] | None = None):
        super().__init__(fetcher)
        self.pool = KeyPool(keys) if keys is not None else _env_pool(self.name)

    def _clock(self, key: str) -> str:
        return self.name

    def _get_rotating(self, build: Callable[[str], tuple[str, dict[str, str]]]) -> str:
        """Fetch with the next live key; on 401/403/429 cool that key and retry on another."""
        key = self.pool.get() or (self.pool.keys[0] if self.pool.keys else "")
        for _ in range(max(1, len(self.pool))):
            url, headers = build(key)
            try:
                return self._get(url, headers, self._clock(key))
            except urllib.error.HTTPError as exc:
                if not key or exc.code not in _KEY_COOLDOWN_S:
                    raise
                self.pool.cooldown(key, _KEY_COOLDOWN_S[exc.code])
                other = self.pool.get()
                if not other or other == key:
                    raise
                key = other
        raise RuntimeError(f"{self.name} key rotation exhausted")


_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class ArxivProvider(_Provider):
    """arXiv export API (Atom XML), queried with the phrase and token-AND forms of Rule 7."""

    name = "arxiv"

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        papers: list[Paper] = []
        seen: set[str] = set()
        for form in arxiv_query_forms(query):
            params = {
                "search_query": form,
                "start": "0",
                "max_results": str(limit),
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
            body = self._get(
                f"https://export.arxiv.org/api/query?{urllib.parse.urlencode(params)}",
                {"Accept": "application/atom+xml"},
            )
            for entry in ET.fromstring(body).findall("atom:entry", _ATOM_NS):
                year = _year(_xml_text(entry.find("atom:published", _ATOM_NS)))
                if not _in_years(year, year_from, year_to):
                    continue  # the export API has no year filter; RH filters here
                entry_id = _xml_text(entry.find("atom:id", _ATOM_NS))
                arxiv_id = entry_id.rsplit("/abs/", 1)[-1] if "/abs/" in entry_id else ""
                seen_key = arxiv_id or entry_id
                if not seen_key or seen_key in seen:
                    continue  # the same entry comes back under several query forms
                seen.add(seen_key)
                papers.append(
                    Paper(
                        title=_xml_text(entry.find("atom:title", _ATOM_NS)),
                        authors=[
                            name
                            for name in (_xml_text(a.find("atom:name", _ATOM_NS)) for a in entry.findall("atom:author", _ATOM_NS))
                            if name
                        ],
                        year=year,
                        venue="arXiv",
                        abstract=_xml_text(entry.find("atom:summary", _ATOM_NS)),
                        doi=normalize_doi(_xml_text(entry.find("arxiv:doi", _ATOM_NS))),
                        arxiv_id=arxiv_id,
                        url=entry_id,
                        open_pdf=True,
                        provider=self.name,
                    )
                )
                if len(papers) >= limit:
                    return papers
        return papers


class CrossrefProvider(_Provider):
    """Crossref REST ``/works`` (JSON)."""

    name = "crossref"

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        params = {"query": query, "rows": str(limit), "sort": "relevance"}
        filters = []
        if year_from is not None:
            filters.append(f"from-pub-date:{year_from}-01-01")
        if year_to is not None:
            filters.append(f"until-pub-date:{year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)
        payload = json.loads(
            self._get(f"https://api.crossref.org/works?{urllib.parse.urlencode(params)}", {"Accept": "application/json"})
        )
        papers = []
        for item in (payload.get("message") or {}).get("items") or []:
            title = _squash((item.get("title") or [""])[0])
            if not title:
                continue  # RH skips untitled Crossref rows
            papers.append(
                Paper(
                    title=title,
                    authors=[
                        _squash(f"{author.get('given') or ''} {author.get('family') or ''}")
                        for author in item.get("author") or []
                        if author.get("given") or author.get("family")
                    ],
                    year=_crossref_year(item),
                    venue=_squash((item.get("container-title") or [""])[0]),
                    abstract=_squash(re.sub(r"<[^>]+>", " ", item.get("abstract") or "")),  # JATS markup
                    doi=normalize_doi(item.get("DOI") or ""),
                    url=item.get("URL") or "",
                    citation_count=_int(item.get("is-referenced-by-count")),
                    provider=self.name,
                )
            )
        return papers


def _crossref_year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued"):
        parts = (item.get(key) or {}).get("date-parts") or [[]]
        if parts and parts[0]:
            return _year(parts[0][0])
    return None


class OpenAlexProvider(_KeyedProvider):
    """OpenAlex ``/works`` (JSON); optional ``api_key`` query parameter rotates under Rule 2."""

    name = "openalex"

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        params = {"search": query, "per-page": str(limit)}
        filters = []
        if year_from is not None:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to is not None:
            filters.append(f"to_publication_date:{year_to}-12-31")
        if filters:
            params["filter"] = ",".join(filters)

        def build(key: str) -> tuple[str, dict[str, str]]:
            keyed = {**params, "api_key": key} if key else params
            return f"https://api.openalex.org/works?{urllib.parse.urlencode(keyed)}", {"Accept": "application/json"}

        payload = json.loads(self._get_rotating(build))
        papers = []
        for item in payload.get("results") or []:
            location = item.get("primary_location") or {}
            papers.append(
                Paper(
                    title=_squash(item.get("title")),
                    authors=[
                        (authorship.get("author") or {}).get("display_name")
                        for authorship in item.get("authorships") or []
                        if (authorship.get("author") or {}).get("display_name")
                    ],
                    year=_year(item.get("publication_year")),
                    venue=_squash((location.get("source") or {}).get("display_name")),
                    abstract=_openalex_abstract(item.get("abstract_inverted_index") or {}),
                    doi=normalize_doi(item.get("doi") or ""),
                    openalex_id=str(item.get("id") or "").rsplit("/", 1)[-1],
                    url=location.get("landing_page_url") or item.get("doi") or "",
                    citation_count=_int(item.get("cited_by_count")),
                    open_pdf=bool((item.get("best_oa_location") or {}).get("pdf_url")),
                    provider=self.name,
                )
            )
        return papers


def _openalex_abstract(inverted: dict[str, list[int]]) -> str:
    """OpenAlex ships abstracts as {word: [positions]}; put the words back in order."""
    positions = {pos: word for word, places in inverted.items() for pos in places or [] if isinstance(pos, int)}
    return " ".join(positions[pos] for pos in sorted(positions))


class PubMedProvider(_Provider):
    """NCBI E-utilities: ESearch for PMIDs, then EFetch for the records (XML)."""

    name = "pubmed"
    _BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        doi = normalize_doi(query) if _DOI_SHAPE.match(normalize_doi(query)) else ""
        term = f"{doi}[AID]" if doi else query
        if year_from is not None or year_to is not None:
            term += f" AND {year_from or 1800}:{year_to or 3000}[pdat]"
        common = {"db": "pubmed", "retmode": "xml", "tool": "jiuwenswarm"}
        found = self._get(
            f"{self._BASE}/esearch.fcgi?{urllib.parse.urlencode({**common, 'term': term, 'retmax': str(limit), 'sort': 'relevance'})}",
            {"Accept": "application/xml"},
        )
        pmids = [_xml_text(node) for node in ET.fromstring(found).findall(".//IdList/Id")]
        pmids = [pmid for pmid in pmids if pmid]
        if not pmids:
            return []
        fetched = self._get(
            f"{self._BASE}/efetch.fcgi?{urllib.parse.urlencode({**common, 'id': ','.join(pmids)})}",
            {"Accept": "application/xml"},
        )
        papers = [_pubmed_paper(article) for article in ET.fromstring(fetched).findall(".//PubmedArticle")]
        papers = [paper for paper in papers if paper.title]
        if doi:
            # Rule 8 (same as RH PubMedProvider.fetch_by_doi): the [AID] search is fuzzy.
            # A record carrying another DOI is another paper; accepting it would put
            # that paper's title and authors under the DOI the caller asked about.
            papers = [paper for paper in papers if paper.doi.lower() == doi.lower()]
        return papers


_PUBMED_YEAR_PATHS = (
    "./MedlineCitation/Article/Journal/JournalIssue/PubDate/Year",
    "./MedlineCitation/Article/ArticleDate/Year",
    "./MedlineCitation/DateCompleted/Year",
    "./MedlineCitation/DateRevised/Year",
)


def _pubmed_paper(article: ET.Element) -> Paper:
    base = "./MedlineCitation/Article"
    authors = []
    for author in article.findall(f"{base}/AuthorList/Author"):
        name = _xml_text(author.find("CollectiveName")) or _squash(
            f"{_xml_text(author.find('ForeName')) or _xml_text(author.find('Initials'))} {_xml_text(author.find('LastName'))}"
        )
        if name:
            authors.append(name)
    year = next((y for y in (_year(_xml_text(article.find(path))) for path in _PUBMED_YEAR_PATHS) if y), None)
    if year is None:
        medline_date = re.search(r"\b(1\d{3}|2\d{3})\b", _xml_text(article.find(f"{base}/Journal/JournalIssue/PubDate/MedlineDate")))
        year = int(medline_date.group(1)) if medline_date else None
    abstract = " ".join(
        f"{node.get('Label')}: {_xml_text(node)}" if node.get("Label") else _xml_text(node)
        for node in article.findall(f"{base}/Abstract/AbstractText")
        if _xml_text(node)
    )
    doi = next(
        (_xml_text(node) for node in article.findall("./PubmedData/ArticleIdList/ArticleId") if (node.get("IdType") or "").lower() == "doi"),
        "",
    )
    pmid = _xml_text(article.find("./MedlineCitation/PMID"))
    return Paper(
        title=_xml_text(article.find(f"{base}/ArticleTitle")),
        authors=authors,
        year=year,
        venue=_xml_text(article.find(f"{base}/Journal/Title")) or _xml_text(article.find(f"{base}/Journal/ISOAbbreviation")),
        abstract=abstract,
        doi=normalize_doi(doi),
        pmid=pmid,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        provider="pubmed",
    )


class SemanticScholarProvider(_KeyedProvider):
    """Semantic Scholar Graph API ``/paper/search`` (JSON); optional ``x-api-key`` rotates under Rule 2."""

    name = "semantic_scholar"
    _FIELDS = "paperId,title,authors,year,venue,abstract,externalIds,url,citationCount,openAccessPdf"

    def _clock(self, key: str) -> str:
        # S2 limits each key, so each key gets its own clock: seats with different keys
        # must not queue behind each other. Keyless requests share the host clock. The
        # key is hashed so it never appears in a file name.
        return f"{self.name}-{hashlib.sha256(key.encode()).hexdigest()[:12]}" if key else self.name

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        params = {"query": query, "limit": str(limit), "fields": self._FIELDS}
        if year_from is not None or year_to is not None:
            params["year"] = f"{year_from or 1900}-{year_to or 2100}"
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?{urllib.parse.urlencode(params)}"
        payload = json.loads(
            self._get_rotating(lambda key: (url, {"Accept": "application/json", **({"x-api-key": key} if key else {})}))
        )
        papers = []
        for item in payload.get("data") or []:
            external = item.get("externalIds") or {}
            papers.append(
                Paper(
                    title=_squash(item.get("title")),
                    authors=[author["name"] for author in item.get("authors") or [] if author.get("name")],
                    year=_year(item.get("year")),
                    venue=_squash(item.get("venue")),
                    abstract=_squash(item.get("abstract")),
                    doi=normalize_doi(external.get("DOI") or ""),
                    arxiv_id=str(external.get("ArXiv") or ""),
                    s2_id=str(item.get("paperId") or ""),
                    url=item.get("url") or "",
                    citation_count=_int(item.get("citationCount")),
                    open_pdf=bool((item.get("openAccessPdf") or {}).get("url")),
                    provider=self.name,
                )
            )
        return papers


class OpenReviewProvider(_Provider):
    """OpenReview API v2 note search (JSON): submissions to ICLR, NeurIPS, TMLR and other
    OpenReview venues, including ones no journal index lists.

    The API wants ``term`` with ``type=terms``; a ``query`` parameter is answered with 400.
    """

    name = "openreview"

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        params = {"term": query, "type": "terms", "content": "all", "source": "forum", "limit": str(limit)}
        payload = json.loads(self._get(f"https://api2.openreview.net/notes/search?{urllib.parse.urlencode(params)}",
                                       {"Accept": "application/json"}))
        papers = []
        for note in payload.get("notes") or []:
            content = note.get("content") or {}

            def value(key: str, content: dict = content) -> Any:
                field_value = content.get(key)
                return field_value.get("value") if isinstance(field_value, dict) else field_value

            millis = note.get("pdate") or note.get("cdate")
            year = time.gmtime(millis / 1000).tm_year if isinstance(millis, (int, float)) else None
            if not _squash(value("title")) or not _in_years(year, year_from, year_to):
                continue
            forum = note.get("forum") or note.get("id") or ""
            papers.append(Paper(
                title=_squash(value("title")),
                # v2 notes carry author names as strings or as {"fullname", "username"} records
                authors=[a.get("fullname", "") if isinstance(a, dict) else str(a) for a in value("authors") or []],
                year=year,
                venue=_squash(value("venue")),
                abstract=_squash(value("abstract")),
                url=f"https://openreview.net/forum?id={forum}" if forum else "",
                open_pdf=bool(forum),
                provider=self.name,
            ))
        return papers


class GoogleScholarProvider(_KeyedProvider):
    """Google Scholar through a search API that answers in the SerpAPI result shape
    (``organic_results`` with ``publication_info``). Used only when GOOGLE_SCHOLAR_API_URL
    is set; GOOGLE_SCHOLAR_API_KEY(S) rotate under Rule 2."""

    name = "google_scholar"

    def __init__(self, fetcher: Fetcher | None = None, keys: Sequence[str] | None = None, api_url: str = ""):
        super().__init__(fetcher, keys)
        self.api_url = (api_url or os.environ.get("GOOGLE_SCHOLAR_API_URL", "")).rstrip("/")

    def search(self, query: str, limit: int, year_from: int | None = None, year_to: int | None = None) -> list[Paper]:
        params = {"q": query, "num": str(limit)}
        if year_from is not None:
            params["as_ylo"] = str(year_from)
        if year_to is not None:
            params["as_yhi"] = str(year_to)
        url = f"{self.api_url}?{urllib.parse.urlencode(params)}"
        payload = json.loads(self._get_rotating(
            lambda key: (url, {"Accept": "application/json", **({"X-API-Key": key} if key else {})})))
        papers = []
        for item in payload.get("organic_results") or payload.get("results") or []:
            info = item.get("publication_info") or {}
            summary = str(info.get("summary") or "")
            year = _year(item.get("year")) or _year((re.findall(r"\b(?:19|20)\d{2}\b", summary) or [None])[-1])
            if not _squash(item.get("title")) or not _in_years(year, year_from, year_to):
                continue
            papers.append(Paper(
                title=_squash(item.get("title")),
                authors=[a["name"] for a in info.get("authors") or [] if isinstance(a, dict) and a.get("name")],
                year=year,
                venue=_squash(summary.split(" - ")[1].split(",")[0]) if summary.count(" - ") >= 1 else "",
                abstract=_squash(item.get("snippet")),
                doi=normalize_doi(str(item.get("doi") or "")),
                url=str(item.get("link") or ""),
                citation_count=_int(((item.get("inline_links") or {}).get("cited_by") or {}).get("total")),
                open_pdf=any(str(r.get("file_format", "")).upper() == "PDF" for r in item.get("resources") or []),
                provider=self.name,
            ))
        return papers


def default_providers(fetcher: Fetcher | None = None) -> list[_Provider]:
    """The keyless sources, plus Google Scholar when its search API is configured.

    S2/OpenAlex keys are read from the environment when set.
    """
    providers: list[_Provider] = [
        ArxivProvider(fetcher),
        OpenAlexProvider(fetcher),
        CrossrefProvider(fetcher),
        PubMedProvider(fetcher),
        SemanticScholarProvider(fetcher),
        OpenReviewProvider(fetcher),
    ]
    if os.environ.get("GOOGLE_SCHOLAR_API_URL"):
        providers.append(GoogleScholarProvider(fetcher))
    return providers


# ---------------------------------------------------------------------------
# Rule 7 - query hygiene (same as RH retrieval_hygiene).
# ---------------------------------------------------------------------------

#: What an index accepts as a query. A claim stated as a sentence still searches; a
#: claim stated as a paragraph is rejected (OpenReview answers 400) or throttled, and
#: one such rejection counts as a source down, which can void the whole read.
QUERY_CHAR_BUDGET = 240
_WS = re.compile(r"\s+")
_QUERY_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")


def normalize_query(query: str) -> str:
    return _WS.sub(" ", (query or "").strip())


def is_query_shaped(query: str) -> bool:
    """Whether this text can be handed to an index as it stands."""
    return 0 < len(normalize_query(query)) <= QUERY_CHAR_BUDGET


def arxiv_query_forms(query: str) -> list[str]:
    """arXiv ``search_query`` forms to union: the quoted phrase and the AND of its tokens.

    A multi-word phrase under a single ``all:"A B"`` misses papers where the
    words are reordered or split across title and abstract; the token-AND forms
    recover them, and the phrase form keeps exact hits at the top.
    """
    q = normalize_query(query)
    if not q:
        return []
    forms = [f'all:"{q}"', f"all:{q}"]
    tokens = _QUERY_TOKEN.findall(q)
    if len(tokens) >= 2:
        forms += [
            f'abs:"{q}"',
            " AND ".join(f"all:{token}" for token in tokens),
            " AND ".join(f"abs:{token}" for token in tokens),
        ]
    return list(dict.fromkeys(forms))


# ---------------------------------------------------------------------------
# Rule 5 - dedupe fingerprint (same as RH PaperRecord.fingerprint and
# normalize_arxiv_id) and field merge by source priority.
# ---------------------------------------------------------------------------

_TITLE_NOISE = re.compile(r"[^a-z0-9]+")
# arXiv ids arrive as "2608.14036" (S2), "2608.14036v1" (arXiv API), "arXiv:2608.14036"
# or an abs/pdf URL. Left alone, one paper gets two fingerprints and survives dedupe twice.
_ARXIV_ID = re.compile(r"(?:arxiv[:\s]*)?(?:abs/|pdf/)?(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?", re.IGNORECASE)

#: Which source's value wins when two sources disagree on a field (RH METADATA_FIELD_PRIORITY).
FIELD_PRIORITY = {"openalex": 130, "crossref": 125, "arxiv": 120, "semantic_scholar": 112, "pubmed": 110,
                  "openreview": 100, "google_scholar": 90}
_MERGED_FIELDS = (
    "title", "authors", "year", "venue", "abstract", "doi", "arxiv_id",
    "s2_id", "openalex_id", "pmid", "url", "citation_count",
)


def normalize_title(value: str) -> str:
    return " ".join(_TITLE_NOISE.sub(" ", (value or "").strip().lower()).split())


def normalize_arxiv_id(value: str) -> str:
    """Canonical arXiv id without version or prefix: ``arXiv:2608.14036v1`` -> ``2608.14036``.

    Several distinct ids in one field is corrupt input (a list passed where an
    id was expected). Canonicalizing would keep the first and drop the rest,
    turning visible corruption into a plausible wrong id, so the raw value is
    kept (lowercased). Unparseable input is also kept rather than dropped.
    """
    raw = str(value or "").strip()
    matches = _ARXIV_ID.findall(raw)
    if len(set(matches)) != 1:
        return raw.lower()
    return matches[0].lower()


def fingerprint(paper: Paper) -> str:
    """Identity key: DOI, then arXiv id, S2 id, OpenAlex id, PMID; title+year only when no id exists."""
    for value in (paper.doi, normalize_arxiv_id(paper.arxiv_id), paper.s2_id, paper.openalex_id, paper.pmid):
        cleaned = (value or "").strip().lower()
        if cleaned:
            return cleaned
    return f"title:{normalize_title(paper.title)}:{paper.year or ''}"


def title_year_key(paper: Paper) -> str:
    """Second identity key: the same paper often has a DOI in one source and only an arXiv id in another.

    Empty for an untitled row, so two untitled rows from the same year are never merged.
    """
    title = normalize_title(paper.title)
    return f"{title}::{paper.year or ''}" if title else ""


def merge_papers(base: Paper, incoming: Paper) -> Paper:
    """Fill ``base``'s empty fields from ``incoming``; on a conflict the higher-priority source wins."""
    incoming_wins = FIELD_PRIORITY.get(incoming.provider, 0) >= FIELD_PRIORITY.get(base.provider, 0)
    merged = replace(base, authors=list(base.authors), sources=list(dict.fromkeys(base.sources + incoming.sources)))
    for name in _MERGED_FIELDS:
        value = getattr(incoming, name)
        if value in (None, "", []):
            continue
        if getattr(merged, name) in (None, "", []) or incoming_wins:
            setattr(merged, name, value)
    merged.open_pdf = base.open_pdf or incoming.open_pdf
    if incoming_wins:
        merged.provider = incoming.provider
    return merged


def _dedupe(rows: list[tuple[int, Paper]], query_count: int) -> tuple[list[Paper], list[list[int]]]:
    """Merge rows into unique papers; return them and, per query, the indices of the papers it found."""
    papers: list[Paper] = []
    index: dict[str, int] = {}
    per_query: list[list[int]] = [[] for _ in range(query_count)]
    for query_index, row in rows:
        if row.arxiv_id:
            row.arxiv_id = normalize_arxiv_id(row.arxiv_id)  # canonical before keying, and in the output
        keys = [key for key in (fingerprint(row), title_year_key(row)) if key]
        position = next((index[key] for key in keys if key in index), None)
        if position is None:
            papers.append(row)
            position = len(papers) - 1
        else:
            papers[position] = merge_papers(papers[position], row)
        for key in (*keys, fingerprint(papers[position])):
            index.setdefault(key, position)
        if position not in per_query[query_index]:
            per_query[query_index].append(position)
    return papers, per_query


# ---------------------------------------------------------------------------
# Rule 6 - relevance-first ranking (same as RH rank_record).
#
# A ranking led by source priority fills a capped result list with the
# top-priority source's fuzzy tail and drops exact hits from lower-priority
# sources (RH measured 1/87 gold-reference recall that way while S2 alone had
# the missing papers at rank 1). Query-term coverage leads; the source is a
# prior that only breaks near-ties.
# ---------------------------------------------------------------------------

#: Source prior (RH SEARCH_PROVIDER_PRIORITY); scaled by 1/2000 so it never outweighs one query term.
SOURCE_PRIOR = {"openalex": 115, "semantic_scholar": 110, "arxiv": 100, "pubmed": 98, "crossref": 95}


def query_token_coverage(paper: Paper, query: str) -> float:
    """Fraction of the query's tokens present in the title plus the first 500 characters of the abstract."""
    query_tokens = set(normalize_title(query).split())
    if not query_tokens:
        return 0.0
    haystack = set(normalize_title(paper.title).split()) | set(normalize_title(paper.abstract[:500]).split())
    return len(query_tokens & haystack) / len(query_tokens)


def rank_key(paper: Paper, query: str) -> tuple[float, str]:
    """Sort key (higher first): coverage, then small bonuses for PDF, identifiers, citations and source."""
    identifiers = sum(1 for value in (paper.doi, paper.arxiv_id, paper.s2_id, paper.openalex_id, paper.pmid) if value)
    has_pdf = 1 if paper.open_pdf or paper.arxiv_id else 0
    prior = max((SOURCE_PRIOR.get(source, 0) for source in paper.sources), default=0)
    score = (
        query_token_coverage(paper, query)
        + has_pdf * 0.05
        + identifiers * 0.01
        + math.log1p(paper.citation_count or 0) / 15.0 * 0.1
        + prior / 2000.0
    )
    return round(score, 6), normalize_title(paper.title)


# ---------------------------------------------------------------------------
# Rule 9 - round-robin merge across queries (same as RH _merge_capture_candidates).
# ---------------------------------------------------------------------------


def round_robin_merge(per_query: Sequence[Sequence[Hashable]], limit: int) -> list[Hashable]:
    """Take each query's next unseen item in turn until ``limit`` items are picked.

    Ranking the union by score lets the densest query spend the whole budget:
    the narrow queries that named the specific prior work come back unread
    under a page of generic hits. Scores are not comparable across queries and
    sources anyway, so every query places one item before any places two.
    """
    pointers = [0] * len(per_query)
    picked: list[Hashable] = []
    taken: set[Hashable] = set()
    advancing = True
    while advancing and len(picked) < limit:
        advancing = False
        for position, items in enumerate(per_query):
            while pointers[position] < len(items) and items[pointers[position]] in taken:
                pointers[position] += 1
            if pointers[position] >= len(items):
                continue
            item = items[pointers[position]]
            pointers[position] += 1
            taken.add(item)
            picked.append(item)
            advancing = True
            if len(picked) >= limit:
                break
    return picked


# ---------------------------------------------------------------------------
# Rule 10 - whether the read stands (same as RH research_delivery_define
# _coverage(frozen=True) and the prior-art identifiability check).
# ---------------------------------------------------------------------------

#: Lanes that are not an external index: the local pool, the aggregate label, a stale cache replay.
_NOT_EXTERNAL = frozenset({"local", "multi", "stale_cache"})
#: With any error, the read stands only if at least this many external sources answered ...
FROZEN_MIN_HEALTHY_LANES = 3
#: ... and at most this many answered nothing.
FROZEN_MAX_DOWN_LANES = 2


def prior_art_read_stands(
    queried: Iterable[str],
    answered: Iterable[str],
    errors: Sequence[Any],
    rows: Iterable[Mapping[str, Any]],
) -> bool:
    """Whether a search result is healthy enough to argue "no prior work" from.

    * A source is down only if it answered no query at all; one lost query out
      of several is a limitation, not an outage.
    * A cache hit replays an old search and answers for no source that is down now.
    * Without errors, any external source suffices. With errors, one or two
      sources out of many is a limitation, but fewer than three answering
      sources is thin retrieval and does not stand.
    * At least one row must carry a title and a DOI or arXiv id, so a reader can
      find the evidence. Untitled or id-less rows do not void the others: that
      would price the index, not the evidence.
    """
    external = {name for name in queried if name not in _NOT_EXTERNAL}
    down = external - set(answered)
    healthy = external - down - {"cache"}
    if not external:
        return False
    if errors and (len(healthy) < FROZEN_MIN_HEALTHY_LANES or len(down) > FROZEN_MAX_DOWN_LANES):
        return False
    return any(
        str(row.get("title") or "").strip() and any(str(row.get(key) or "").strip() for key in ("doi", "arxiv_id"))
        for row in rows
    )


# ---------------------------------------------------------------------------
# Rule 4 - parallel fan-out under one time budget (same as RH SearchAggregator).
# ---------------------------------------------------------------------------

#: One slow or retrying source (Crossref 429 backoff, an SSL EOF loop) must not stall
#: the answer; after this many seconds the call returns what has arrived.
DEFAULT_TIME_BUDGET_S = 45.0
MAX_RESULTS_CAP = 50
_ABSTRACT_CHARS = 500
_AUTHORS_SHOWN = 10


def _fan_out(
    providers: Sequence[_Provider],
    queries: Sequence[str],
    limit: int,
    year_from: int | None,
    year_to: int | None,
    budget_s: float,
) -> tuple[list[tuple[int, Paper]], set[str], list[str]]:
    """Run every (query, source) pair in parallel; return rows, sources that answered, and errors.

    A pair still running when the budget runs out is reported as an error and
    abandoned (its thread finishes in the background, bounded by its own HTTP
    timeout); the rows that did arrive are returned.
    """
    executor = ThreadPoolExecutor(max_workers=len(providers) * len(queries), thread_name_prefix="paper_search")
    tasks = {
        executor.submit(provider.search, query, limit, year_from, year_to): (query_index, provider.name)
        for query_index, query in enumerate(queries)
        for provider in providers
    }
    try:
        done, _ = wait(tasks, timeout=budget_s if budget_s > 0 else None)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    rows: list[tuple[int, Paper]] = []
    answered: set[str] = set()
    errors: list[str] = []
    for future, (query_index, name) in tasks.items():  # submission order keeps merges deterministic
        if future not in done:
            errors.append(f"{name}: did not finish within {budget_s:.0f}s search time budget; partial results returned")
            continue
        try:
            papers = future.result()
        except Exception as exc:  # one source failing must not lose the others' rows
            errors.append(f"{name}: {str(exc) or type(exc).__name__}")
            continue
        answered.add(name)
        for paper in papers:
            paper.provider, paper.sources = name, [name]
            rows.append((query_index, paper))
    return rows, answered, errors


def _row(paper: Paper) -> dict[str, Any]:
    abstract = paper.abstract if len(paper.abstract) <= _ABSTRACT_CHARS else paper.abstract[:_ABSTRACT_CHARS] + "..."
    return {
        "title": paper.title,
        # An index sometimes lists the venue or its organiser as an author
        # ("Association for Computational Linguistics 2026"); a person's name has no digit.
        "authors": [a for a in paper.authors if a and not re.search(r"\d", a)][:_AUTHORS_SHOWN],
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "s2_id": paper.s2_id,
        "url": paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else ""),
        "abstract": abstract,
        "sources": paper.sources,
    }


def search_papers(
    query: str,
    extra_queries: Sequence[str] | None = None,
    max_results: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    venue_filter: str = "",
    providers: Sequence[_Provider] | None = None,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
) -> dict[str, Any]:
    """Search every source for ``query`` (and ``extra_queries``) and return the merged, ranked read.

    Raises:
        ValueError: when no query is 1-240 characters long (Rule 7).
    """
    candidates = [normalize_query(q) for q in [query, *(extra_queries or [])]]
    queries = list(dict.fromkeys(q for q in candidates if is_query_shaped(q)))
    rejected = [q for q in candidates if q and not is_query_shaped(q)]
    if not queries:
        raise ValueError(
            f"no searchable query: each query must be 1-{QUERY_CHAR_BUDGET} characters; "
            "restate the claim as a short keyword query"
        )
    limit = max(1, min(int(max_results), MAX_RESULTS_CAP))
    providers = list(providers) if providers is not None else default_providers()

    rows, answered, errors = _fan_out(providers, queries, limit, year_from, year_to, time_budget_s)
    papers, per_query = _dedupe(rows, len(queries))
    if venue_filter:
        per_query = [[i for i in ids if venue_filter.lower() in papers[i].venue.lower()] for ids in per_query]
    ranked = [
        sorted(ids, key=lambda i, q=queries[position]: rank_key(papers[i], q), reverse=True)
        for position, ids in enumerate(per_query)
    ]
    result_rows = [_row(papers[i]) for i in round_robin_merge(ranked, limit)]

    queried = [provider.name for provider in providers]
    answered_in_order = [name for name in queried if name in answered]
    return {
        "queries": queries,
        "queries_rejected": rejected,
        "papers": result_rows,
        "providers_queried": queried,
        "providers_answered": answered_in_order,
        "provider_errors": errors,
        "read_stands": prior_art_read_stands(queried, answered_in_order, errors, result_rows),
    }


@tool(
    name="paper_search",
    description=(
        "Search scholarly literature across arXiv, OpenAlex, Crossref, PubMed and Semantic Scholar "
        "(no API key required). Returns JSON: deduplicated papers ranked by query relevance "
        "(title, authors, year, venue, doi, arxiv_id, s2_id, url, truncated abstract, sources), "
        "providers_answered, provider_errors, and read_stands - true only when enough sources "
        "answered and at least one paper is identifiable by DOI or arXiv id, i.e. when the result "
        "is sound enough to support a 'no prior work' claim. Pass alternative phrasings in "
        "extra_queries; every query gets a result slot before any query gets two."
    ),
)
async def paper_search(
    query: str,
    extra_queries: list[str] | None = None,
    max_results: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    venue_filter: str = "",
) -> str:
    """Search papers across the scholarly sources.

    Args:
        query: Keyword query, at most 240 characters (state a claim as keywords, not a paragraph).
        extra_queries: Optional alternative phrasings or sub-questions, each at most 240 characters.
        max_results: Number of papers to return (1-50).
        year_from: Earliest publication year to keep.
        year_to: Latest publication year to keep.
        venue_filter: Keep only papers whose venue contains this text (case-insensitive).
    """
    try:
        result = await asyncio.to_thread(
            search_papers,
            query,
            extra_queries,
            max_results,
            year_from,
            year_to,
            venue_filter,
        )
    except ValueError as exc:
        return f"[ERROR]: {exc}"
    return json.dumps(result, ensure_ascii=False)


__all__ = [
    "paper_search",
    "search_papers",
    "prior_art_read_stands",
    "round_robin_merge",
    "arxiv_query_forms",
    "is_query_shaped",
    "normalize_arxiv_id",
    "fingerprint",
    "rank_key",
    "pace",
    "urllib_fetch",
    "KeyPool",
    "Paper",
    "ArxivProvider",
    "OpenAlexProvider",
    "CrossrefProvider",
    "PubMedProvider",
    "SemanticScholarProvider",
    "default_providers",
]
