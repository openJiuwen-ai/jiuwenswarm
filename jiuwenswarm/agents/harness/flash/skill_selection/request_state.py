"""Bounded per-invocation reuse; no state is shared between user requests."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SearchCacheKey:
    """Include every query, configuration and permission boundary in reuse."""

    settings: tuple
    service_id: int
    generation: int
    permission_key: str
    query: str
    keywords: tuple[str, ...]


@dataclass
class RequestState:
    # Model tool calls can arrive together. Serializing only this invocation
    # makes query/load reuse deterministic; other sessions remain independent.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    results: OrderedDict = field(default_factory=OrderedDict)
    tickets: OrderedDict = field(default_factory=OrderedDict)
    searches: int = 0
    loaded: bool = False

    @staticmethod
    def remember(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > 32:
            cache.popitem(last=False)
