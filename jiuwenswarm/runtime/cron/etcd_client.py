"""Minimal etcd v3 gRPC-gateway JSON client (httpx, no grpc extra)."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class EtcdError(RuntimeError):
    """etcd request failed."""


class EtcdCasError(EtcdError):
    """Compare-and-swap lost (ModRevision mismatch)."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str | None) -> bytes:
    if not text:
        return b""
    return base64.b64decode(text.encode("ascii"))


def prefix_range_end(prefix: bytes) -> bytes:
    """etcd range_end for a prefix scan (increment last non-0xFF byte)."""
    arr = bytearray(prefix)
    for i in range(len(arr) - 1, -1, -1):
        if arr[i] < 0xFF:
            arr[i] += 1
            return bytes(arr[: i + 1])
    return b"\x00"


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
    create_revision: int = 0
    version: int = 0


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
            create_revision=int(item.get("create_revision") or 0),
            version=int(item.get("version") or 0),
        )
    except (TypeError, ValueError):
        return None


class EtcdJsonClient:
    """httpx client for etcd ``/v3/kv/*`` and ``/v3/watch`` JSON APIs."""

    def __init__(
        self,
        endpoints: list[str],
        *,
        timeout: float = 10.0,
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
                logger.warning("[etcd] request failed endpoint=%s path=%s error=%s", base, path, exc)
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

    async def range(
        self,
        key: bytes,
        *,
        range_end: bytes | None = None,
    ) -> EtcdRangeResult:
        body: dict[str, Any] = {"key": _b64(key)}
        if range_end is not None:
            body["range_end"] = _b64(range_end)
        payload = await self._post_json("/v3/kv/range", body)
        kvs: list[EtcdKv] = []
        raw_kvs = payload.get("kvs") or []
        if isinstance(raw_kvs, list):
            for item in raw_kvs:
                parsed = _parse_kv(item) if isinstance(item, dict) else None
                if parsed is not None:
                    kvs.append(parsed)
        return EtcdRangeResult(kvs=kvs, revision=self._header_revision(payload))

    async def put(self, key: bytes, value: bytes) -> int:
        payload = await self._post_json(
            "/v3/kv/put",
            {"key": _b64(key), "value": _b64(value)},
        )
        return self._header_revision(payload)

    async def delete(self, key: bytes) -> int:
        payload = await self._post_json(
            "/v3/kv/deleterange",
            {"key": _b64(key)},
        )
        return self._header_revision(payload)

    async def put_if_mod_revision(
        self,
        key: bytes,
        value: bytes,
        *,
        mod_revision: int,
    ) -> int:
        """CAS put: succeed only when key's ModRevision equals ``mod_revision``.

        ``mod_revision=0`` means the key must not exist.
        """
        body = {
            "compare": [
                {
                    "result": "EQUAL",
                    "target": "MOD",
                    "key": _b64(key),
                    "mod_revision": str(int(mod_revision)),
                }
            ],
            "success": [
                {"request_put": {"key": _b64(key), "value": _b64(value)}},
            ],
            "failure": [],
        }
        payload = await self._post_json("/v3/kv/txn", body)
        if not bool(payload.get("succeeded")):
            raise EtcdCasError(f"cas failed key={key!r} mod_revision={mod_revision}")
        return self._header_revision(payload)

    async def watch_prefix(self, prefix: bytes) -> AsyncIterator[list[EtcdKv]]:
        """Yield batches of changed kvs until the stream dies."""
        if not self._endpoints:
            raise EtcdError("etcd endpoints are empty")
        client = await self._http()
        body = {
            "create_request": {
                "key": _b64(prefix),
                "range_end": _b64(prefix_range_end(prefix)),
            }
        }
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


async def _iter_watch_events(resp: httpx.Response) -> AsyncIterator[list[EtcdKv]]:
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
