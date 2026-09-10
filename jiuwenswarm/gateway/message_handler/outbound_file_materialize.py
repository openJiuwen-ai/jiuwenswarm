# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Enterprise outbound download: MinIO URL on chat SSE → Gateway proxy chat.file.

Agent ``send_file_to_user`` (OBS path) emits ``chat.file`` with ``files[].url``
(in-cluster MinIO). Gateway does **not** land the object or mint HMAC tokens.
It rewrites the URL to same-origin ``/file-api/download?url=...``; any replica
SSRF-checks the host and streams MinIO → browser. Personal edition never
enters this path (local HMAC ``?token=`` is unchanged).
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from typing import Any, Iterator
from urllib.parse import quote, urlparse

import requests
from fastapi.responses import JSONResponse, Response, StreamingResponse

from jiuwenswarm.edition import is_enterprise
from jiuwenswarm.gateway.channel_manager.web.file_http import (
    content_disposition,
    guess_mime,
    safe_filename,
)

logger = logging.getLogger(__name__)

_OBS_PROXY_TIMEOUT = 120


def chat_file_needs_obs_materialize(payload: dict[str, Any] | None) -> bool:
    """True when enterprise chat.file carries a raw MinIO url (not yet proxied)."""
    if not is_enterprise() or not isinstance(payload, dict):
        return False
    event_type = str(payload.get("event_type") or "").strip()
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return False
    # Deep adapter must stamp event_type=chat.file. Empty is tolerated if files[].url
    # is present (legacy OutputSchema payload omitted event_type).
    if event_type not in ("", "chat.file"):
        return False
    for item in files:
        if not isinstance(item, dict):
            continue
        if _is_obs_proxy_download_url(str(item.get("download_url") or "")):
            continue
        url = str(item.get("url") or item.get("uri") or "").strip()
        token = str(item.get("download_token") or "").strip()
        if url.startswith(("http://", "https://")) and not token:
            return True
    return False


def _is_obs_proxy_download_url(download_url: str) -> bool:
    raw = (download_url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw if "://" in raw else f"http://local{raw}")
    except Exception:
        return False
    path = parsed.path or raw.split("?", 1)[0]
    query = parsed.query if parsed.query else (raw.split("?", 1)[1] if "?" in raw else "")
    return path.rstrip("/").endswith("/file-api/download") and "url=" in query


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def allowed_minio_hosts() -> set[str]:
    """Exact ``host`` / ``host:port`` values allowed for OBS proxy (SSRF).

    Does **not** add bare hostname aliases when the configured endpoint includes
    a port — otherwise ``minio:9000`` would also allow ``minio:8080``.
    """
    hosts: set[str] = set()
    try:
        from jiuwenswarm.channels.web.minio_upload import load_minio_upload_config

        cfg = load_minio_upload_config()
    except Exception:
        logger.debug("[OutboundFile] load_minio_upload_config failed", exc_info=True)
        return hosts

    endpoint = _normalize_host(str(getattr(cfg, "endpoint", "") or ""))
    if endpoint:
        hosts.add(endpoint)

    public = str(getattr(cfg, "public_base_url", "") or "").strip()
    if public:
        parsed = urlparse(public if "://" in public else f"http://{public}")
        netloc = _normalize_host(parsed.netloc or parsed.path)
        if netloc:
            hosts.add(netloc)
    return {h for h in hosts if h}


def assert_minio_url_allowed(url: str) -> None:
    """Raise ValueError if url host is not a configured MinIO host (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported url scheme: {parsed.scheme!r}")
    host = _normalize_host(parsed.netloc)
    if not host:
        raise ValueError("url missing host")
    allowed = allowed_minio_hosts()
    if not allowed:
        raise ValueError("MinIO not configured; cannot validate outbound download url")
    if host not in allowed:
        raise ValueError(f"outbound download url host not allowed: {host}")


def _requests_verify() -> bool:
    raw = os.environ.get("JIUWENCLAW_SSL_VERIFY", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def build_obs_proxy_download_url(obs_url: str, *, name: str = "") -> str:
    """Same-origin download href; browser never talks to minio-headless."""
    path = f"/file-api/download?url={quote(obs_url, safe='')}"
    clean = str(name or "").strip()
    if clean:
        path += f"&name={quote(clean, safe='')}"
    return path


def _guess_mime(file_name: str) -> str:
    guessed = guess_mime(file_name)
    if guessed and guessed != "application/octet-stream":
        return guessed
    extra, _ = mimetypes.guess_type(file_name)
    return extra or "application/octet-stream"


def materialize_outbound_files(files: list[Any]) -> list[dict[str, Any]]:
    """Rewrite raw MinIO urls to Gateway proxy hrefs (no disk, no HMAC)."""
    out: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        existing = str(item.get("download_url") or "").strip()
        if _is_obs_proxy_download_url(existing):
            out.append(dict(item))
            continue
        token = str(item.get("download_token") or "").strip()
        url = str(item.get("url") or item.get("uri") or "").strip()
        name = str(item.get("name") or item.get("filename") or "").strip() or "download.bin"
        if token and existing:
            out.append(dict(item))
            continue
        if not url.startswith(("http://", "https://")):
            continue
        try:
            assert_minio_url_allowed(url)
        except ValueError:
            logger.warning(
                "[OutboundFile] skip outbound url (host not allowed) name=%s",
                name,
            )
            continue
        size = item.get("size")
        try:
            size_out = int(size) if size is not None else 0
        except (TypeError, ValueError):
            size_out = 0
        out.append(
            {
                "name": name,
                "size": size_out,
                "mime_type": str(item.get("mime_type") or _guess_mime(name)),
                "download_url": build_obs_proxy_download_url(url, name=name),
            }
        )
    return out


def _open_obs_upstream(
    *,
    obs_url: str,
    upstream_headers: dict[str, str],
) -> requests.Response:
    """Blocking MinIO GET (call via ``asyncio.to_thread`` from async routes)."""
    return requests.get(
        obs_url,
        stream=True,
        timeout=_OBS_PROXY_TIMEOUT,
        verify=_requests_verify(),
        headers=upstream_headers or None,
        allow_redirects=False,
    )


def proxy_obs_download_response(
    *,
    obs_url: str,
    filename: str,
    inline: bool,
    head: bool,
    range_header: str | None,
) -> Response:
    """Stream an allowlisted MinIO GET to the browser. Any Gateway replica.

    Prefer :func:`proxy_obs_download_response_async` from FastAPI handlers so the
    initial upstream connect does not block the event loop.
    """
    try:
        assert_minio_url_allowed(obs_url)
    except ValueError:
        logger.warning("[OutboundFile] proxy rejected url host")
        return JSONResponse({"error": "forbidden_path"}, status_code=403)

    file_name = safe_filename(filename) if filename else "download.bin"
    upstream_headers: dict[str, str] = {}
    if range_header:
        upstream_headers["Range"] = range_header
    elif head:
        # Presigned URL is GET; avoid pulling the whole object for card HEAD checks.
        upstream_headers["Range"] = "bytes=0-0"

    try:
        resp = _open_obs_upstream(obs_url=obs_url, upstream_headers=upstream_headers)
    except Exception:
        logger.exception("[OutboundFile] OBS proxy request failed")
        return JSONResponse({"error": "obs_fetch_failed"}, status_code=502)

    return _build_obs_proxy_response(
        resp, file_name=file_name, inline=inline, head=head
    )


async def proxy_obs_download_response_async(
    *,
    obs_url: str,
    filename: str,
    inline: bool,
    head: bool,
    range_header: str | None,
) -> Response:
    """Async wrapper: SSRF check + offload blocking connect to a worker thread."""
    try:
        assert_minio_url_allowed(obs_url)
    except ValueError:
        logger.warning("[OutboundFile] proxy rejected url host")
        return JSONResponse({"error": "forbidden_path"}, status_code=403)

    file_name = safe_filename(filename) if filename else "download.bin"
    upstream_headers: dict[str, str] = {}
    if range_header:
        upstream_headers["Range"] = range_header
    elif head:
        upstream_headers["Range"] = "bytes=0-0"

    try:
        resp = await asyncio.to_thread(
            _open_obs_upstream,
            obs_url=obs_url,
            upstream_headers=upstream_headers,
        )
    except Exception:
        logger.exception("[OutboundFile] OBS proxy request failed")
        return JSONResponse({"error": "obs_fetch_failed"}, status_code=502)

    return _build_obs_proxy_response(
        resp, file_name=file_name, inline=inline, head=head
    )


def _build_obs_proxy_response(
    resp: requests.Response,
    *,
    file_name: str,
    inline: bool,
    head: bool,
) -> Response:
    if resp.status_code not in (200, 206):
        status = resp.status_code if resp.status_code in (403, 404, 416) else 502
        resp.close()
        return JSONResponse({"error": "obs_fetch_failed"}, status_code=status)

    mime_type = (
        (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        or _guess_mime(file_name)
    )
    headers = {
        "Content-Type": mime_type,
        "Accept-Ranges": resp.headers.get("Accept-Ranges") or "bytes",
        "Content-Disposition": content_disposition(file_name, inline=inline),
        "Cache-Control": "no-store",
    }
    content_length = resp.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length
    content_range = resp.headers.get("Content-Range")
    if content_range:
        headers["Content-Range"] = content_range

    if head:
        resp.close()
        return Response(status_code=resp.status_code, headers=headers)

    def _iter() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return StreamingResponse(_iter(), status_code=resp.status_code, headers=headers)
