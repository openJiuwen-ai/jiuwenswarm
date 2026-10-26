# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""进程内网页正文缓存：按 agent 隔离，LRU 淘汰，超时清理。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Union
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_AGENT_TIMEOUT_SECONDS: int = 2 * 3600
_MAX_AGENTS: int = 32


@dataclass
class CacheEntry:
    """缓存条目。

    Attributes:
        url: 规范化后的 URL（与缓存键一致）。
        title: 网页标题（可选，仅用于诊断日志）。
        content: 网页正文（markdown 或纯文本）。
        update_time: 网页更新时间 epoch 秒；未知则为 None。
        cached_at: 写入缓存的 epoch 秒。
        source: 来源标记，如 "paid:petal"。
    """
    url: str
    content: str
    update_time: Optional[float] = None
    cached_at: float = field(default_factory=time.time)
    title: str = ""
    source: str = ""


def normalize_url(url: str) -> str:
    """URL 规范化：去除 fragment、去除尾部斜杠、转小写 host。"""
    from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import _normalize_url
    raw = _normalize_url(url).strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = (parts.netloc or "").lower()
    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


class WebContentCache:
    """单个 agent 的网页正文缓存，并发安全，无条目上限。"""

    def __init__(self) -> None:
        self._store: dict[str, CacheEntry] = {}
        self._order: list[str] = []
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.bypassed = 0

    async def get(self, url: str) -> Optional[CacheEntry]:
        key = normalize_url(url)
        if not key:
            return None
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)
            self.hits += 1
            return entry

    async def put(self, entry: CacheEntry) -> None:
        key = normalize_url(entry.url)
        if not key or not (entry.content or "").strip():
            return
        async with self._lock:
            existing = self._store.get(key)
            if existing is not None:
                if entry.update_time is None and existing.update_time is not None:
                    entry.update_time = existing.update_time
            self._store[key] = entry
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
            self._order.clear()
            self.hits = 0
            self.misses = 0
            self.bypassed = 0

    def __len__(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, Union[int, float]]:
        total = self.hits + self.misses + self.bypassed
        hit_rate = (self.hits / total * 100) if total > 0 else 0.0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "bypassed": self.bypassed,
            "total": total,
            "hit_rate_pct": round(hit_rate, 1),
            "entries": len(self._store),
        }


class AgentCacheRegistry:
    """按 agent 管理缓存实例。

    - 最多 _MAX_AGENTS 个 agent 并发缓存，超出时淘汰最久未活跃的 agent。
    - agent 超时未活跃（_AGENT_TIMEOUT_SECONDS）时清理其缓存。
    - 每个 agent 的缓存无条目上限。
    """

    def __init__(
        self,
        *,
        max_agents: int = _MAX_AGENTS,
        timeout_seconds: int = _AGENT_TIMEOUT_SECONDS,
    ) -> None:
        self._caches: dict[str, WebContentCache] = {}
        self._last_active: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._max_agents = max_agents
        self._timeout_seconds = timeout_seconds

    async def get_cache(self, agent_id: str) -> WebContentCache:
        aid = (agent_id or "default").strip() or "default"
        async with self._lock:
            now = time.time()
            self._last_active[aid] = now
            self._cleanup_inactive_locked(now)
            cache = self._caches.get(aid)
            if cache is None:
                if len(self._caches) >= self._max_agents:
                    lru_aid = min(self._last_active, key=lambda k: self._last_active[k])
                    self._caches.pop(lru_aid, None)
                    self._last_active.pop(lru_aid, None)
                    logger.info(
                        "[AgentCacheRegistry] evicted agent=%s (capacity=%d)",
                        lru_aid, self._max_agents,
                    )
                cache = WebContentCache()
                self._caches[aid] = cache
            return cache

    def _cleanup_inactive_locked(self, now: float) -> None:
        inactive = [
            aid for aid, t in self._last_active.items()
            if now - t > self._timeout_seconds
        ]
        for aid in inactive:
            self._caches.pop(aid, None)
            self._last_active.pop(aid, None)
            logger.info(
                "[AgentCacheRegistry] cleaned inactive agent=%s (timeout=%ds)",
                aid, self._timeout_seconds,
            )

    def get_cache_sync(self, agent_id: str) -> WebContentCache:
        """同步获取缓存，用于无法 await 的场景（如 @harness_element provider）。

        不加锁，并发竞争最差情况是多创建一个 WebContentCache 被覆盖，不影响正确性。
        """
        aid = (agent_id or "default").strip() or "default"
        now = time.time()
        self._last_active[aid] = now
        self._cleanup_inactive_locked(now)
        cache = self._caches.get(aid)
        if cache is None:
            if len(self._caches) >= self._max_agents:
                lru_aid = min(self._last_active, key=lambda k: self._last_active[k])
                self._caches.pop(lru_aid, None)
                self._last_active.pop(lru_aid, None)
                logger.info(
                    "[AgentCacheRegistry] evicted agent=%s (capacity=%d)",
                    lru_aid, self._max_agents,
                )
            cache = WebContentCache()
            self._caches[aid] = cache
        return cache

    async def clear_all(self) -> None:
        async with self._lock:
            self._caches.clear()
            self._last_active.clear()

    def stats(self) -> dict[str, Union[int, list[str]]]:
        return {
            "agents": len(self._caches),
            "agent_ids": list(self._caches.keys()),
            "max_agents": self._max_agents,
            "timeout_seconds": self._timeout_seconds,
        }


_REGISTRY = AgentCacheRegistry()


def get_agent_cache_registry() -> AgentCacheRegistry:
    return _REGISTRY


def reset_registry_for_tests() -> None:
    """仅供测试使用：重置全局 registry。"""
    global _REGISTRY
    _REGISTRY = AgentCacheRegistry()


def parse_update_time(raw: object) -> Optional[float]:
    """从 Petal web_pages[*].update_time 字段解析 epoch 秒。"""
    import datetime as _dt

    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return None
            if s.isdigit():
                return float(s)
            try:
                return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
                "%Y/%m/%d",
                "%d/%m/%Y",
            ):
                try:
                    return _dt.datetime.strptime(s, fmt).timestamp()
                except ValueError:
                    continue
    except Exception:
        logger.debug("parse_update_time failed for raw=%r", raw, exc_info=True)
    return None
