"""Bounded, public-card-only Hub catalog cache with nonblocking refresh.

Loaders yield cumulative snapshots; a replacement never overwrites a complete
catalog until its last page succeeds. Authenticated callers must supply their own
session key and use public=False, or bypass this cache entirely.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import sqlite3
import time
import secrets
import re
import weakref
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

_ALLOWED = frozenset(
    (
        'catalog_kind items skills success query count source category_id category_name plugin_type '
        'request_id user_id complete has_more total page page_size asset_id kind package_name '
        'display_name short_description public_latest_version icon_uri tags name summary version '
        'author category native_score updated_at is_team_skill short_desc latest_version score '
        'publisher_name install_count like_count view_count id description icon groups title'
    ).split()
)


def _unsafe_url_leaf(value):
    if not isinstance(value, str):
        return False
    for match in re.finditer(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", value):
        try:
            parsed = urlsplit(match.group())
            if parsed.query or parsed.username or parsed.password:
                return True
        except ValueError:
            return True
    return False


def safe_metadata(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, dict):
        return {k: safe_metadata(v, depth + 1) for k, v in value.items()
                if k in _ALLOWED and not _unsafe_url_leaf(v)}
    if isinstance(value, (list, tuple)):
        return [safe_metadata(v, depth + 1) for v in value[:10000] if not _unsafe_url_leaf(v)]
    if isinstance(value, str):
        return "" if _unsafe_url_leaf(value) else value[:16000]
    return value if value is None or isinstance(value, (bool, int, float)) else None


class CatalogCards(list):
    def __init__(self, cards, cache):
        super().__init__(cards)
        self.cache = cache


class HubCatalogCache:
    def __init__(self, path: Path, *, ttl=600, stale_ttl=86400, max_entries=128, max_bytes=100 * 1024 * 1024):
        self.path, self.ttl, self.stale_ttl = Path(path), ttl, stale_ttl
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.entries = OrderedDict()
        self.tasks = {}
        self.failures = {}
        self.invalidated = set()
        self.semaphore = asyncio.Semaphore(2)
        from .hub_avatar_cache import HubAvatarCache
        self.avatars = HubAvatarCache()
        self.closed = False
        self.db = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.execute('PRAGMA auto_vacuum=FULL')
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS catalog (key TEXT PRIMARY KEY, updated REAL, accessed REAL, payload TEXT)"
            )
            self.db.execute('SELECT key FROM catalog LIMIT 1').fetchall()
        except (sqlite3.Error, OSError):
            if self.db:
                self.db.close()
            self.db = None
            try:
                self.path.unlink(missing_ok=True)
                self.db = sqlite3.connect(self.path)
                self.db.execute('PRAGMA auto_vacuum=FULL')
                self.db.execute(
                    "CREATE TABLE catalog (key TEXT PRIMARY KEY, updated REAL, accessed REAL, payload TEXT)"
                )
            except (sqlite3.Error, OSError):
                self.db = None

    def _put(self, key, payload, public, updated=None):
        payload = safe_metadata(payload)
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode()) > min(self.max_bytes, 8 * 1024 * 1024):
            raise ValueError('catalog too large')
        now = time.time() if updated is None else updated
        self.entries[key] = (now, payload)
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries:
            expired_key, _ = self.entries.popitem(last=False)
            self.invalidated.discard(expired_key)
        if public and self.db:
            try:
                self.db.execute('INSERT OR REPLACE INTO catalog VALUES (?, ?, ?, ?)', (key, now, time.time(), encoded))
                self.db.execute('DELETE FROM catalog WHERE updated < ?', (time.time() - self.stale_ttl,))
                while True:
                    size, count = self.db.execute(
                        "SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0), COUNT(*) FROM catalog"
                    ).fetchone()
                    if size <= self.max_bytes - min(1024 * 1024, self.max_bytes // 10) and count <= self.max_entries:
                        break
                    self.db.execute('DELETE FROM catalog WHERE key=(SELECT key FROM catalog ORDER BY accessed LIMIT 1)')
                self.db.commit()
            except sqlite3.Error:
                pass

    def read(self, key, loader, *, public=True, refresh=False, require_complete=False, kind=None):
        # Hash keys before storage; raw query/context/auth material never reaches disk.
        key = hashlib.sha256(str(key).encode()).hexdigest()
        now = time.time()
        ttl = self.ttl if public else min(self.ttl, 60)
        stale_ttl = self.stale_ttl if public else min(self.stale_ttl, 300)
        entry = self.entries.get(key)
        if entry is None and public and self.db:
            try:
                row = self.db.execute('SELECT updated, payload FROM catalog WHERE key=?', (key,)).fetchone()
                if row and now - row[0] <= stale_ttl and len(row[1]) <= 8 * 1024 * 1024:
                    data = json.loads(row[1])
                    if isinstance(data, dict):
                        self._put(key, data, False, row[0])
                        entry = self.entries.get(key)
            except (sqlite3.Error, ValueError, TypeError):
                pass
        if entry and now - entry[0] > stale_ttl:
            self.entries.pop(key, None)
            entry = None
        if entry:
            self.entries.move_to_end(key)
        failure = self.failures.get(key)
        needs_load = (
            refresh
            or key in self.invalidated
            or entry is None
            or now - entry[0] >= ttl
            or (require_complete and not entry[1].get("complete"))
        )
        can_schedule = key not in self.tasks and not self.closed and len(self.tasks) < self.max_entries
        retry_ready = not failure or now >= failure[1]
        if needs_load and can_schedule and retry_ready:
            self.tasks[key] = asyncio.create_task(self._refresh(key, loader, public, entry, kind))
        state = (
            "fresh"
            if entry and key not in self.invalidated and now - entry[0] < ttl
            else ("stale" if entry else "miss")
        )
        if failure:
            state = 'stale' if entry else 'error'
        payload = entry[1] if entry else None
        sidecar = {
            "state": state,
            "refreshing": key in self.tasks,
            "updated_at": entry[0] if entry else None,
            "complete": bool(payload and payload.get("complete")),
            "has_more": bool(payload.get("has_more")) if payload else True,
        }
        if failure:
            sidecar['error'] = 'refresh_failed'
        if payload and 'total' in payload:
            sidecar['total'] = payload['total']
        return copy.deepcopy(payload), sidecar

    async def _refresh(self, key, loader, public, old, kind=None):
        try:
            async with self.semaphore:
                async with asyncio.timeout(90):
                    async for snapshot in loader():
                        if not isinstance(snapshot, dict) or snapshot.get('success') is False:
                            raise ValueError('Hub failed')
                        if not old or not old[1].get('complete') or snapshot.get('complete'):
                            self._put(key, {**snapshot, **({'catalog_kind': kind} if kind else {})}, public)
                    self.failures.pop(key, None)
                    self.invalidated.discard(key)
        except Exception as exc:
            if not public and getattr(exc, 'status_code', None) in (401, 403):
                self.entries.pop(key, None)
            count = self.failures.get(key, (0, 0))[0] + 1
            delay = min(300, 5 * 2 ** min(count - 1, 6))
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except (ValueError, TypeError):
                    try:
                        from email.utils import parsedate_to_datetime
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            self.failures[key] = (count, time.time() + min(86400, delay))
            while len(self.failures) > self.max_entries:
                self.failures.pop(next(iter(self.failures)))
        finally:
            self.tasks.pop(key, None)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        if self.db:
            self.db.close()
            self.db = None


_caches = {}
_port_scopes = weakref.WeakKeyDictionary()
_memory_scope_secret = secrets.token_bytes(32)


def catalog_memory_scope(base_url, credentials, context=None):
    """Stable within this process only; credentials and contexts never reach disk."""
    message = json.dumps([base_url, credentials, context], sort_keys=True, default=str).encode()
    return 'memory:' + hmac.new(_memory_scope_secret, message, hashlib.sha256).hexdigest()


def get_hub_catalog_cache():
    from jiuwenswarm.common.utils import get_workspace_dir
    path = get_workspace_dir() / 'cache' / 'marketplace' / 'catalog-cache.sqlite'
    key = (str(path), id(asyncio.get_running_loop()))
    if key not in _caches or _caches[key].closed:
        _caches[key] = HubCatalogCache(path)
    return _caches[key]


async def close_hub_catalog_cache():
    for cache in list(_caches.values()):
        await cache.close()
    _caches.clear()


async def cached_asset_catalog(port, kind, *, refresh=False, preload=False):
    from dataclasses import asdict
    from .hub_asset_port import HubAssetSummary, HubSearchRequest
    transport = getattr(port, 'transport', port)
    private = bool(getattr(transport, 'token', '') or getattr(transport, 'system_token', ''))
    # Unknown injected transports are process-local; their results never reach public disk.
    base_url = getattr(port, 'base_url', None)
    parsed = urlsplit(base_url or '')
    public = bool(base_url) and not private and not (parsed.username or parsed.password or parsed.query)
    if public:
        identity = base_url
    else:
        from .hub_client import HubClient, HttpHubTransport
        credentials = {'token': getattr(transport, 'token', ''), 'system_token': getattr(transport, 'system_token', '')}
        if isinstance(port, HubClient) and isinstance(transport, HttpHubTransport):
            identity = catalog_memory_scope(base_url, credentials)
        else:
            try:
                scope = _port_scopes.setdefault(port, secrets.token_hex(16))
            except TypeError:
                scope = secrets.token_hex(16)
            identity = catalog_memory_scope(base_url, credentials, scope)

    key = json.dumps([identity, kind, 'search_assets_thumbnails_v1', '', 1, 100])

    async def loader():
        items, seen = [], set()
        for page_no in range(1, 3 if preload else 101):
            page = await port.search_assets(HubSearchRequest(kind=kind, page=page_no, page_size=100))
            additions = [item for item in page.items if item.kind == kind and item.asset_id not in seen]
            if page.items and not additions:
                raise ValueError('pagination made no progress')
            new_cards = [asdict(item) for item in additions]
            items.extend(new_cards)
            seen.update(item.asset_id for item in additions)
            complete = not page.items or len(items) >= page.total
            needs_images = public and any(card.get('icon_uri') for card in new_cards)
            # Keep the previous complete snapshot visible until its thumbnails are ready.
            yield {
                "items": items,
                "total": page.total,
                "complete": complete and not needs_images,
                "has_more": not complete,
            }
            if needs_images:
                await get_hub_catalog_cache().avatars.fill(new_cards)
                yield {'items': items, 'total': page.total, 'complete': complete, 'has_more': not complete}
            if complete:
                return
        if not preload:
            raise ValueError("Hub pagination exceeded 100 pages")
    data, state = get_hub_catalog_cache().read(
        key, loader, public=public, refresh=refresh, require_complete=not preload, kind=kind
    )
    items = []
    raw_items = (data or {}).get('items', [])
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict) or item.get('kind') != kind:
            continue
        asset_id = item.get('asset_id')
        if not isinstance(asset_id, str) or not asset_id.strip():
            continue
        # Sanitization may remove a text field containing a signed URL. Rebuild
        # the required card schema with safe defaults; one malformed disk card
        # must never make an otherwise usable catalog fail.
        fields = {
            name: item.get(name) if isinstance(item.get(name), str) else ""
            for name in ("display_name", "short_description", "public_latest_version", "icon_uri", "package_name")
        }
        tags = item.get('tags')
        fields['tags'] = tuple(tag for tag in tags if isinstance(tag, str)) if isinstance(tags, (list, tuple)) else ()
        items.append(HubAssetSummary(kind=kind, asset_id=asset_id, **fields))
    return items, state


def invalidate_hub_catalog(kind: str):
    """Mark only the published kind stale, preserving all visible cards."""
    for cache in _caches.values():
        stale = time.time() - cache.ttl - 1
        for key, (updated, payload) in list(cache.entries.items()):
            if payload.get('catalog_kind') == kind:
                cache.invalidated.add(key)
        if cache.db:
            try:
                for key, encoded in cache.db.execute('SELECT key, payload FROM catalog').fetchall():
                    try:
                        payload = json.loads(encoded)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(payload, dict) and payload.get('catalog_kind') == kind:
                        cache.db.execute('UPDATE catalog SET updated=MIN(updated, ?) WHERE key=?', (stale, key))
                cache.db.commit()
            except sqlite3.Error:
                pass


async def start_hub_catalog_preload(skill_manager=None):
    """Schedule the four configured defaults, returning without network waits."""
    from .hub_asset_port import create_default_hub_asset_port
    port = create_default_hub_asset_port()
    for kind in ('agent_template', 'agent_group', 'plugin', 'mcp'):
        await cached_asset_catalog(port, kind, preload=True)
    if skill_manager is not None:
        await skill_manager.handle_skills_swarm_skills_hub_recommend({
            'cache_mode': 'prefer_cache', 'top_k': 50,
        })
