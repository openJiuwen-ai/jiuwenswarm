# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-owned trusted search URL ledger and Host producer callback."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.tools.search_tools import (
    MAX_SEARCH_MAX_RESULTS,
    normalize_search_max_results,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
)
from jiuwenswarm.agents.harness.common.rails.permissions.url_safety import (
    RecentUrlSource,
    normalize_url_for_match,
    unsafe_public_https_url_reason,
)

MAX_TRUSTED_SEARCH_URLS_PER_SESSION = 500


class SessionTrustedSearchUrls:
    """Keep canonical URLs for exactly one root-session runtime."""

    def __init__(self, root_session_id: str = "") -> None:
        self.root_session_id = str(root_session_id or "").strip()
        self._urls: OrderedDict[str, None] = OrderedDict()
        self._lock = RLock()
        self._closed = False

    def bind_session(self, root_session_id: str) -> None:
        session_id = str(root_session_id or "").strip()
        if not session_id:
            raise ValueError("trusted_search_session_missing")
        with self._lock:
            if self._closed:
                raise ValueError("trusted_search_session_closed")
            if self.root_session_id and self.root_session_id != session_id:
                raise ValueError("trusted_search_session_mismatch")
            self.root_session_id = session_id

    def record_batch(
        self,
        *,
        key: ToolInvocationKeyV1,
        urls: Iterable[str],
        max_results: int,
    ) -> tuple[int, int]:
        """Validate one Host callback and add as many new URLs as capacity permits."""

        if not isinstance(key, ToolInvocationKeyV1):
            return (0, 0)
        limit = max(0, min(int(max_results), MAX_SEARCH_MAX_RESULTS))
        if limit == 0:
            return (0, 0)
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_url in urls:
            url = normalize_url_for_match(raw_url)
            if not url or url in seen:
                continue
            try:
                unsafe_reason = unsafe_public_https_url_reason(urlparse(url))
            except ValueError:
                continue
            if unsafe_reason is not None:
                continue
            normalized.append(url)
            seen.add(url)
            if len(normalized) >= limit:
                break
        with self._lock:
            if self._closed:
                return (0, 0)
            if not self.root_session_id or key.root_session_id != self.root_session_id:
                return (0, len(normalized))
            remaining = max(0, MAX_TRUSTED_SEARCH_URLS_PER_SESSION - len(self._urls))
            accepted = 0
            dropped = 0
            for url in normalized:
                if url in self._urls:
                    continue
                if accepted >= remaining:
                    dropped += 1
                    continue
                self._urls[url] = None
                accepted += 1
            return (accepted, dropped)

    def contains(self, *, root_session_id: str, url: str) -> bool:
        canonical = normalize_url_for_match(url)
        with self._lock:
            return bool(
                not self._closed
                and canonical
                and root_session_id == self.root_session_id
                and canonical in self._urls
            )

    def sources(self, *, root_session_id: str) -> tuple[RecentUrlSource, ...]:
        with self._lock:
            if self._closed or root_session_id != self.root_session_id:
                return ()
            return tuple(
                RecentUrlSource(
                    url=url,
                    host=str(urlparse(url).hostname or "").lower(),
                    source_tool="host_search_producer",
                    trusted=True,
                )
                for url in self._urls
            )

    def dispose(self) -> None:
        """Permanently close this session ledger and discard all URLs."""

        with self._lock:
            self._urls.clear()
            self._closed = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._urls)


@dataclass(frozen=True)
class _TrustedSearchProducerBinding:
    ledger: SessionTrustedSearchUrls
    key: ToolInvocationKeyV1
    tool_name: str
    max_results: int


class _TrustedSearchProducerLease:
    """One-shot Host capability shared by copied async contexts."""

    def __init__(self, binding: _TrustedSearchProducerBinding) -> None:
        self._binding = binding
        self._lock = RLock()
        self._terminal = False

    def complete(
        self,
        *,
        tool_name: str,
        success: bool,
        urls: Iterable[str] = (),
    ) -> tuple[int, int]:
        """Consume this lease once and optionally commit structured URLs."""

        with self._lock:
            if self._terminal or tool_name != self._binding.tool_name:
                return (0, 0)
            self._terminal = True
            if not success:
                return (0, 0)
            return self._binding.ledger.record_batch(
                key=self._binding.key,
                urls=urls,
                max_results=self._binding.max_results,
            )

    def revoke(self) -> None:
        """Prevent this invocation capability from completing later."""

        with self._lock:
            self._terminal = True


_CURRENT_PRODUCER: ContextVar[_TrustedSearchProducerLease | None] = ContextVar(
    "jiuwenswarm_trusted_search_producer",
    default=None,
)


def bind_trusted_search_producer(
    *,
    ledger: SessionTrustedSearchUrls,
    key: ToolInvocationKeyV1,
    tool_name: str,
    max_results: int,
) -> None:
    """Install a Host-only callback capability for the current tool task."""

    current = _CURRENT_PRODUCER.get()
    if current is not None:
        current.revoke()
    _CURRENT_PRODUCER.set(
        _TrustedSearchProducerLease(
            _TrustedSearchProducerBinding(
                ledger=ledger,
                key=key,
                tool_name=tool_name if isinstance(tool_name, str) else "",
                max_results=normalize_search_max_results(int(max_results)),
            )
        )
    )


def clear_trusted_search_producer() -> None:
    current = _CURRENT_PRODUCER.get()
    if current is not None:
        current.revoke()
    _CURRENT_PRODUCER.set(None)


def complete_trusted_search_producer(
    *, tool_name: str, success: bool, urls: Iterable[str] = ()
) -> tuple[int, int]:
    """Complete the current Host search producer exactly once."""

    lease = _CURRENT_PRODUCER.get()
    if lease is None:
        return (0, 0)
    return lease.complete(
        tool_name=tool_name if isinstance(tool_name, str) else "",
        success=success,
        urls=urls,
    )


__all__ = [
    "MAX_TRUSTED_SEARCH_URLS_PER_SESSION",
    "SessionTrustedSearchUrls",
    "bind_trusted_search_producer",
    "clear_trusted_search_producer",
    "complete_trusted_search_producer",
]
