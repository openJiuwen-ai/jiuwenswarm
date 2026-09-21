# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read-only etcd v3 JSON client plus the data-plane watch loop.

Range and watch only -- put / delete / txn belong to the gateway cron store,
not this reader.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

import httpx
import yaml

from jiuwenbox.server.etcd_sync import COMPONENT_NAME, CONFIG_SYNC_KEY

logger = logging.getLogger(__name__)

_CONNECT_INITIAL_DELAY = 2.0
_CONNECT_MAX_DELAY = 60.0
_DEFAULT_FETCH_TIMEOUT = 10.0
METADATA_PREFIX = "_"


class EtcdError(RuntimeError):
    """etcd request failed."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str | None) -> bytes:
    if not text:
        return b""
    return base64.b64decode(text.encode("ascii"))


def _normalize_endpoint(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"http://{text}")
    if parsed.scheme not in ("http", "https"):
        return ""
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


@dataclass
class EtcdKv:
    key: bytes
    value: bytes
    mod_revision: int = 0


@dataclass
class EtcdRangeResult:
    kvs: list[EtcdKv] = field(default_factory=list)
    revision: int = 0


def _parse_kv(item: dict[str, Any]) -> EtcdKv | None:
    if not isinstance(item, dict):
        return None
    try:
        return EtcdKv(
            key=_unb64(str(item.get("key") or "")),
            value=_unb64(str(item.get("value") or "")),
            mod_revision=int(item.get("mod_revision") or 0),
        )
    except (TypeError, ValueError):
        return None


class EtcdJsonClient:
    """httpx client for etcd ``/v3/kv/range`` and ``/v3/watch`` JSON APIs."""

    def __init__(
        self,
        endpoints: list[str],
        *,
        timeout: float = _DEFAULT_FETCH_TIMEOUT,
    ) -> None:
        self._endpoints = [
            ep for ep in (_normalize_endpoint(item) for item in endpoints) if ep
        ]
        self._timeout = float(timeout)
        self._index = 0
        self._client: httpx.AsyncClient | None = None

    @property
    def endpoints(self) -> list[str]:
        return list(self._endpoints)

    def _next_base(self) -> str:
        if not self._endpoints:
            raise EtcdError("etcd endpoints are empty")
        base = self._endpoints[self._index % len(self._endpoints)]
        self._index += 1
        return base

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        client = await self._http()
        for _ in range(max(1, len(self._endpoints))):
            base = self._next_base()
            url = f"{base}{path}"
            try:
                resp = await client.post(url, json=body)
                data = resp.json() if resp.content else {}
                if not isinstance(data, dict):
                    data = {}
                if resp.status_code >= 400 or data.get("error") or data.get("message"):
                    msg = str(
                        data.get("error")
                        or data.get("message")
                        or f"HTTP {resp.status_code}"
                    )
                    raise EtcdError(f"{url}: {msg}")
                return data
            except (httpx.HTTPError, EtcdError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning(
                    "[etcd] request failed endpoint=%s path=%s error=%s",
                    base,
                    path,
                    exc,
                )
                continue
        raise EtcdError(str(last_error or "etcd request failed"))

    @staticmethod
    def _header_revision(payload: dict[str, Any]) -> int:
        header = payload.get("header") if isinstance(payload, dict) else None
        if not isinstance(header, dict):
            return 0
        try:
            return int(header.get("revision") or 0)
        except (TypeError, ValueError):
            return 0

    async def range(self, key: bytes) -> EtcdRangeResult:
        payload = await self._post_json("/v3/kv/range", {"key": _b64(key)})
        kvs: list[EtcdKv] = []
        raw_kvs = payload.get("kvs") or []
        if isinstance(raw_kvs, list):
            for item in raw_kvs:
                parsed = _parse_kv(item) if isinstance(item, dict) else None
                if parsed is not None:
                    kvs.append(parsed)
        return EtcdRangeResult(kvs=kvs, revision=self._header_revision(payload))

    async def watch(
        self,
        key: bytes,
        *,
        start_revision: int | None = None,
    ) -> AsyncIterator[list[EtcdKv]]:
        """Yield batches of changed kvs for one exact key until the stream dies."""
        if not self._endpoints:
            raise EtcdError("etcd endpoints are empty")
        client = await self._http()
        create_request: dict[str, Any] = {"key": _b64(key)}
        if start_revision is not None and int(start_revision) > 0:
            create_request["start_revision"] = str(int(start_revision))
        body = {"create_request": create_request}
        last_error: Exception | None = None
        for _ in range(max(1, len(self._endpoints))):
            base = self._next_base()
            url = f"{base}/v3/watch"
            try:
                async with client.stream(
                    "POST",
                    url,
                    json=body,
                    timeout=None,
                ) as resp:
                    if resp.status_code >= 400:
                        raise EtcdError(f"{url}: HTTP {resp.status_code}")
                    async for events in _iter_watch_events(resp):
                        yield events
                return
            except (httpx.HTTPError, EtcdError, json.JSONDecodeError) as exc:
                last_error = exc
                logger.warning("[etcd] watch failed endpoint=%s error=%s", base, exc)
                continue
        raise EtcdError(str(last_error or "etcd watch failed"))


async def _iter_watch_events(resp: Any) -> AsyncIterator[list[EtcdKv]]:
    decoder = json.JSONDecoder()
    buf = ""
    async for chunk in resp.aiter_text():
        buf += chunk
        buf = buf.lstrip()
        while buf:
            try:
                obj, idx = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                break
            buf = buf[idx:].lstrip()
            events = _extract_watch_kvs(obj)
            if events:
                yield events


def _extract_watch_kvs(obj: Any) -> list[EtcdKv]:
    if not isinstance(obj, dict):
        return []
    result = obj.get("result") if isinstance(obj.get("result"), dict) else obj
    if not isinstance(result, dict):
        return []
    raw_events = result.get("events") or []
    if not isinstance(raw_events, list):
        return []
    out: list[EtcdKv] = []
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        kv = item.get("kv") if isinstance(item.get("kv"), dict) else item
        if not isinstance(kv, dict):
            continue
        parsed = _parse_kv(kv)
        if parsed is not None:
            out.append(parsed)
    return out


@dataclass
class FetchResult:
    """One fetched revision: the jiuwenbox section plus stripped metadata."""

    section: dict[str, Any]
    metadata: dict[str, Any]
    mod_revision: int


def extract_section(
    remote_doc: Any,
    component: str = COMPONENT_NAME,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a remote document into (component section, metadata).

    Returns empty dicts when the document or the component key is not a
    mapping. Never raises. Top-level keys starting with ``_`` are metadata.
    """
    if not isinstance(remote_doc, dict):
        return {}, {}

    metadata = {
        key: value
        for key, value in remote_doc.items()
        if isinstance(key, str) and key.startswith(METADATA_PREFIX)
    }
    section = remote_doc.get(component)
    if not isinstance(section, dict):
        section = {}
    return section, metadata


def parse_etcd_endpoints(raw: Any) -> list[str]:
    """Parse a comma-separated (or list) endpoint string into http(s) URLs."""
    if isinstance(raw, (list, tuple)):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        text = str(raw or "").strip()
        parts = [part.strip() for part in text.split(",") if part.strip()] if text else []
    return [ep for ep in (_normalize_endpoint(item) for item in parts) if ep]


class PolicySyncClient:
    """Pull and watch one etcd key; decode the jiuwenbox section."""

    def __init__(
        self,
        *,
        etcd_endpoints: list[str],
        key: str = CONFIG_SYNC_KEY,
        fetch_timeout_seconds: float = _DEFAULT_FETCH_TIMEOUT,
        etcd_client_factory: Callable[..., EtcdJsonClient] | None = None,
    ) -> None:
        self._endpoints = parse_etcd_endpoints(etcd_endpoints)
        self._key = key or CONFIG_SYNC_KEY
        self._timeout = float(fetch_timeout_seconds)
        self._factory = etcd_client_factory or EtcdJsonClient
        self._client: EtcdJsonClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._endpoints)

    def _ensure_client(self) -> EtcdJsonClient:
        if self._client is None:
            self._client = self._factory(self._endpoints, timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    async def _fetch_once_with_revision(self) -> tuple[FetchResult | None, int]:
        key_bytes = self._key.encode("utf-8")
        result = await self._ensure_client().range(key_bytes)
        kv = _pick_exact(result.kvs, key_bytes)
        if kv is None:
            return None, int(result.revision or 0)
        return self._decode(kv), int(result.revision or 0)

    async def watch_loop(
        self,
        on_event: Callable[[FetchResult], Awaitable[None]],
    ) -> None:
        """Pull once, then watch; reconnect with backoff when the stream dies.

        The watch starts at the range snapshot revision + 1, closing the
        range-to-watch race even when applying the initial value is slow.
        Events are filtered to the exact key.
        """
        delay = _CONNECT_INITIAL_DELAY
        while True:
            try:
                if not self._endpoints:
                    raise RuntimeError("etcd endpoints are empty")

                fetched, snapshot_revision = await self._fetch_once_with_revision()
                if fetched is not None:
                    await on_event(fetched)

                key_bytes = self._key.encode("utf-8")
                start_revision = (
                    snapshot_revision + 1 if snapshot_revision > 0 else None
                )
                async for events in self._ensure_client().watch(
                    key_bytes,
                    start_revision=start_revision,
                ):
                    kv = _pick_exact(events, key_bytes)
                    if kv is not None:
                        delay = _CONNECT_INITIAL_DELAY
                        await on_event(self._decode(kv))
                raise RuntimeError("etcd watch stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the watcher alive
                logger.warning(
                    "[PolicySync] watch/connect retry in %.1fs: %s", delay, exc
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _CONNECT_MAX_DELAY)

    @staticmethod
    def _decode(kv: EtcdKv) -> FetchResult:
        mod_revision = int(kv.mod_revision or 0)
        try:
            document = yaml.safe_load(kv.value.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            logger.error("[PolicySync] failed to parse remote config: %s", exc)
            return FetchResult(section={}, metadata={}, mod_revision=mod_revision)
        section, metadata = extract_section(document, COMPONENT_NAME)
        return FetchResult(
            section=section, metadata=metadata, mod_revision=mod_revision
        )


def _pick_exact(kvs: list[EtcdKv], key_bytes: bytes) -> EtcdKv | None:
    matches = (kv for kv in kvs if kv.key == key_bytes)
    return max(matches, key=lambda kv: kv.mod_revision, default=None)


__all__ = [
    "EtcdError",
    "EtcdJsonClient",
    "EtcdKv",
    "EtcdRangeResult",
    "FetchResult",
    "PolicySyncClient",
    "extract_section",
    "parse_etcd_endpoints",
]
