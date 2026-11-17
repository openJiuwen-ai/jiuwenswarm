# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web image discovery pipeline.

Search providers return text only. This module adds the missing image channel:
discover candidate image URLs (provider fields + page ``<img>``), download them
to the agent workspace, validate the bytes, and return local paths plus source
metadata for downstream image tools and PPT image placement.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from jiuwenswarm.agents.harness.common.tools.web_search.providers import (
    any_paid_provider_available,
    run_free_chain,
    run_paid_chain,
)
from jiuwenswarm.agents.harness.common.tools.web_search.settings import (
    resolve_web_search_settings,
)
from jiuwenswarm.agents.harness.common.tools.web_search.types import WebSearchRecord

logger = logging.getLogger(__name__)

DEFAULT_MAX_IMAGES = 6
MAX_IMAGES_CAP = 12
DEFAULT_MAX_PAGES = 3
MAX_PAGES_CAP = 5
DEFAULT_MAX_BYTES_PER_IMAGE = 6 * 1024 * 1024
MAX_BYTES_PER_IMAGE_CAP = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 18 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 20
PAGE_FETCH_TIMEOUT_SECONDS = 20
# 整条链路（检索 + 抓页 + 下载）的总时限，避免最坏情况下逼近 MCP 调用超时。
TOTAL_TIME_BUDGET_SECONDS = 120
MAX_PAGE_BYTES = 2 * 1024 * 1024
# 仅滤掉图标/表情这类结构性小图；相关性交给搜索端排序，不在客户端做关键词判断。
DEFAULT_MIN_IMAGE_SIDE = 200
MAX_CANDIDATES_PER_PAGE = 12
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# 任一命中即视为不可信目标；集中一处，避免条件分支里堆叠布尔表达式。
_NON_PUBLIC_ADDRESS_FLAGS = (
    "is_private",
    "is_loopback",
    "is_link_local",
    "is_reserved",
    "is_multicast",
)

_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
}
_PAGE_HEADERS = {
    "User-Agent": _IMAGE_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
}

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)"
)
_SRCSET_SPLIT_RE = re.compile(r",\s*")
_SRCSET_ENTRY_RE = re.compile(r"(\S+)\s+([0-9.]+)([wx])", re.IGNORECASE)

_URL_ATTR_PRIORITY = (
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-actualsrc",
    "src",
    "data-url",
)
_NOISE_HINTS = (
    "sprite",
    "icon",
    "avatar",
    "emoji",
    "badge",
    "spacer",
    "blank",
    "pixel",
    "tracking",
)
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "0.0.0.0", "::1"}

_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@dataclass
class ImageCandidate:
    url: str
    source_page: str = ""
    source_title: str = ""
    alt: str = ""
    width: int = 0
    height: int = 0


@dataclass
class DownloadedImage:
    local_path: str
    source_url: str
    source_page: str
    alt: str
    width: int
    height: int
    byte_size: int
    mime_type: str
    source_title: str = ""


@dataclass
class ImageSearchOutcome:
    query: str
    output_dir: str = ""
    images: list[DownloadedImage] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    error: str = ""
    hints: list[str] = field(default_factory=list)

    def to_content(self) -> str:
        header = f"[image_search] query={self.query}"
        if self.error:
            return f"{header}\nFailed: {self.error}"
        if not self.images:
            lines = [
                header,
                "No usable image was downloaded.",
                (
                    "stats: "
                    + ", ".join(f"{key}={value}" for key, value in sorted(self.stats.items()))
                ),
                "Fallback: proceed without images, or call web_search/fetch to inspect the page.",
            ]
            return "\n".join(lines)

        lines = [
            header,
            f"Downloaded {len(self.images)} image(s) to {self.output_dir}",
        ]
        for index, image in enumerate(self.images, 1):
            alt = f" | alt: {image.alt}" if image.alt else ""
            title = f" | page: {image.source_title}" if image.source_title else ""
            lines.append(
                f"{index}. {image.local_path} | {image.width}x{image.height} | "
                f"{image.mime_type} | {image.byte_size}B | source: {image.source_page}{title}{alt}"
            )
        lines.append(
            "Images are written to local paths; use them with image tools and PPT image "
            "placement. Check the alt/page text before treating an image as matching the query."
        )
        if self.hints:
            lines.extend(self.hints)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "output_dir": self.output_dir,
            "error": self.error,
            "stats": dict(self.stats),
            "images": [
                {
                    "local_path": image.local_path,
                    "source_url": image.source_url,
                    "source_page": image.source_page,
                    "source_title": image.source_title,
                    "alt": image.alt,
                    "width": image.width,
                    "height": image.height,
                    "byte_size": image.byte_size,
                    "mime_type": image.mime_type,
                }
                for image in self.images
            ],
        }


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def load_image_search_settings() -> dict[str, Any]:
    """Read image search settings from ``web_search.image_search`` with safe caps.

    ``prefer_provider_images`` 决定是否信任搜索引擎返回的图片结果独占本次检索；
    ``allow_page_fallback`` 才允许在数量不足时抓结果页 ``<img>`` 补足。两者分开，
    是因为网页扒图的相关性来自页面而非查询，默认只作为无图可用的兜底。
    """
    max_images = DEFAULT_MAX_IMAGES
    max_pages = DEFAULT_MAX_PAGES
    max_bytes = DEFAULT_MAX_BYTES_PER_IMAGE
    max_total = DEFAULT_MAX_TOTAL_BYTES
    min_side = DEFAULT_MIN_IMAGE_SIDE
    prefer_provider_images = True
    allow_page_fallback = False
    try:
        from jiuwenswarm.common.config import get_config

        raw = (get_config().get("web_search") or {}).get("image_search") or {}
        if isinstance(raw, dict):
            max_images = int(raw.get("max_images") or max_images)
            max_pages = int(raw.get("max_pages") or max_pages)
            max_bytes = int(raw.get("max_bytes_per_image") or max_bytes)
            max_total = int(raw.get("max_total_bytes") or max_total)
            min_side = int(raw.get("min_image_side") or min_side)
            prefer_provider_images = bool(
                raw.get("prefer_provider_images", prefer_provider_images)
            )
            allow_page_fallback = bool(
                raw.get("allow_page_fallback", allow_page_fallback)
            )
    except Exception:
        logger.warning("[image_search] config load failed", exc_info=True)

    max_images = max(1, min(max_images, MAX_IMAGES_CAP))
    max_pages = max(1, min(max_pages, MAX_PAGES_CAP))
    max_bytes = max(64 * 1024, min(max_bytes, MAX_BYTES_PER_IMAGE_CAP))
    max_total = max(max_bytes, min(max_total, MAX_BYTES_PER_IMAGE_CAP * MAX_IMAGES_CAP))
    min_side = max(32, min(min_side, 1024))
    return {
        "max_images": max_images,
        "max_pages": max_pages,
        "max_bytes_per_image": max_bytes,
        "max_total_bytes": max_total,
        "min_image_side": min_side,
        "prefer_provider_images": prefer_provider_images,
        "allow_page_fallback": allow_page_fallback,
    }


def _is_non_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Any non-global scope flag means the address must not be requested."""
    return any(getattr(address, flag) for flag in _NON_PUBLIC_ADDRESS_FLAGS)


def is_public_http_url(url: str) -> bool:
    """Reject non-http(s) and obviously internal targets before downloading."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host in _BLOCKED_HOSTS:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not _is_non_public_address(address)


def _host_resolves_to_private(host: str) -> bool:
    """Return True when *host* cannot be proven to resolve to a public address.

    Resolution failure is treated as private (**fail-closed**): if we cannot prove
    the target is public, the request must not be sent. This prevents DNS 抖动 /
    污染 from turning the SSRF pre-check into a no-op.
    """
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    resolved = False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        resolved = True
        if _is_non_public_address(address):
            return True
    return not resolved


def _url_target_is_public(url: str) -> bool:
    """*url* is http(s) and neither a literal nor a DNS name pointing inward."""
    if not is_public_http_url(url):
        return False
    return not _host_resolves_to_private(urlparse(url).hostname or "")


def _close_quietly(response: Any) -> None:
    closer = getattr(response, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        logger.debug("[image_search] response close failed", exc_info=True)


def _open_public_response(
    url: str,
    *,
    timeout: int,
    headers: dict[str, str],
) -> Any | None:
    """Open *url* while validating every redirect hop **before** requesting it.

    ``allow_redirects=True`` lets the HTTP client reach an internal target before
    ``response.history`` can be inspected, so the SSRF check would only stop the
    body from being saved — the probe itself already happened. Following hops
    manually keeps the check on the request side.
    """
    from jiuwenswarm.common.http_proxy_config import requests_get

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not _url_target_is_public(current):
            logger.warning("[image_search] blocked redirect target=%s", current)
            return None
        response = requests_get(
            current,
            timeout=timeout,
            headers=headers,
            stream=True,
            allow_redirects=False,
        )
        status = getattr(response, "status_code", 200)
        if status not in _REDIRECT_STATUSES:
            if status >= 400:
                _close_quietly(response)
                return None
            return response
        location = str((getattr(response, "headers", None) or {}).get("Location", "") or "")
        _close_quietly(response)
        if not location.strip():
            return None
        current = urljoin(current, location.strip())
    logger.warning("[image_search] too many redirects url=%s", url)
    return None


def _parse_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(tag):
        name = match.group(1).lower()
        value = match.group(2).strip()
        if value[:1] in {"'", '"'}:
            value = value[1:-1]
        attrs[name] = value
    return attrs


def _pick_srcset(srcset: str) -> str:
    best_url = ""
    best_weight = -1.0
    for entry in _SRCSET_SPLIT_RE.split(srcset or ""):
        entry = entry.strip()
        if not entry:
            continue
        match = _SRCSET_ENTRY_RE.match(entry)
        if match:
            weight = float(match.group(2))
            url = match.group(1)
        else:
            weight = 0.0
            url = entry.split(" ")[0]
        if weight > best_weight:
            best_weight = weight
            best_url = url
    return best_url


def _looks_noisy(url: str, alt: str) -> bool:
    """Structural asset filter only (sprites/icons/svg); relevance is not judged here."""
    haystack = f"{url} {alt}".lower()
    if any(hint in haystack for hint in _NOISE_HINTS):
        return True
    return url.lower().split("?")[0].endswith(".svg")


def extract_image_candidates(
    html: str,
    base_url: str,
    *,
    limit: int = MAX_CANDIDATES_PER_PAGE,
    min_side: int = DEFAULT_MIN_IMAGE_SIDE,
) -> list[ImageCandidate]:
    """Extract image candidates from one HTML document in document order.

    只做结构性校验（scheme/内网地址/占位图/最小尺寸/去重）。相关性由搜索结果
    自身的排序决定——页面在结果里的名次就是它的相关度，客户端再做关键词打分只会
    引入误判（实测会把无关大图排到前面）。
    """
    candidates: list[ImageCandidate] = []
    seen: set[str] = set()

    for match in _IMG_TAG_RE.finditer(html or ""):
        attrs = _parse_attrs(match.group(0))
        raw_url = ""
        for key in _URL_ATTR_PRIORITY:
            if attrs.get(key):
                raw_url = attrs[key]
                break
        if not raw_url and attrs.get("srcset"):
            raw_url = _pick_srcset(attrs["srcset"])
        raw_url = raw_url.strip()
        if not raw_url or raw_url.startswith(("data:", "blob:", "javascript:", "about:", "#")):
            continue

        url = urljoin(base_url, raw_url)
        if not is_public_http_url(url) or url in seen:
            continue
        alt = (attrs.get("alt") or attrs.get("title") or "").strip()[:120]
        if _looks_noisy(url, alt):
            continue

        width = _to_int(attrs.get("width"))
        height = _to_int(attrs.get("height"))
        if _is_below_min_side(width, height, min_side):
            continue
        seen.add(url)
        candidates.append(
            ImageCandidate(
                url=url,
                source_page=base_url,
                source_title="",
                alt=alt,
                width=width,
                height=height,
            )
        )
        if len(candidates) >= limit:
            break

    return candidates


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip().rstrip("px")))
    except (TypeError, ValueError):
        return 0


def _is_below_min_side(width: int, height: int, min_side: int) -> bool:
    """Reject only when both dimensions are known and below the floor."""
    if not width or not height:
        return False
    return width < min_side and height < min_side


def _sniff_mime(data: bytes) -> str:
    for signature, mime in _MAGIC_SIGNATURES:
        if data.startswith(signature):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return ""


def _image_dimensions(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return 0, 0


def _read_limited(response: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"image exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class ImageDownloader:
    """Download and validate one image; isolated for testability."""

    def __init__(self, *, timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    def download(
        self,
        candidate: ImageCandidate,
        target_dir: Path,
        *,
        max_bytes: int,
        min_side: int = DEFAULT_MIN_IMAGE_SIDE,
    ) -> DownloadedImage | None:
        try:
            response = _open_public_response(
                candidate.url,
                timeout=self._timeout,
                headers=_IMAGE_HEADERS,
            )
            if response is None:
                return None
            try:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if content_type and not content_type.startswith("image/"):
                    return None
                data = _read_limited(response, max_bytes)
            finally:
                _close_quietly(response)
        except Exception as exc:
            logger.debug("[image_search] download failed url=%s error=%s", candidate.url, exc)
            return None

        if not data:
            return None
        mime = _sniff_mime(data)
        if mime not in _MIME_EXTENSIONS:
            return None
        width, height = _image_dimensions(data)
        if _is_below_min_side(width, height, min_side):
            return None

        name = hashlib.sha1(candidate.url.encode("utf-8")).hexdigest()[:12]
        local_path = target_dir / f"{name}{_MIME_EXTENSIONS[mime]}"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(data)
        except OSError as exc:
            logger.warning("[image_search] write failed path=%s error=%s", local_path, exc)
            return None

        return DownloadedImage(
            local_path=str(local_path),
            source_url=candidate.url,
            source_page=candidate.source_page,
            alt=candidate.alt,
            width=width,
            height=height,
            byte_size=len(data),
            mime_type=mime,
            source_title=candidate.source_title,
        )


def _fetch_html(url: str, timeout_seconds: int = PAGE_FETCH_TIMEOUT_SECONDS) -> str:
    try:
        response = _open_public_response(
            url,
            timeout=timeout_seconds,
            headers=_PAGE_HEADERS,
        )
        if response is None:
            return ""
        try:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if content_type and "html" not in content_type and "xml" not in content_type:
                return ""
            data = _read_limited(response, MAX_PAGE_BYTES)
            encoding = getattr(response, "encoding", None) or "utf-8"
        finally:
            _close_quietly(response)
        try:
            return data.decode(encoding, errors="ignore")
        except (LookupError, AttributeError):
            return data.decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.debug("[image_search] page fetch failed url=%s error=%s", url, exc)
        return ""


async def _collect_records(
    query: str,
    *,
    search_mode: str,
    cache: Any | None,
) -> tuple[list[WebSearchRecord], list[str]]:
    from jiuwenswarm.agents.harness.common.tools.web_search.orchestrator import (
        normalize_search_mode,
    )

    settings = resolve_web_search_settings()
    mode, preferred_source = normalize_search_mode(search_mode)
    tried: list[str] = []

    if mode != "free" and any_paid_provider_available(settings.paid_provider_order):
        run, tried = await run_paid_chain(
            query,
            settings,
            preferred_provider=preferred_source,
            cache=cache,
        )
        if run is not None and run.records:
            return run.records, tried

    free_run = await run_free_chain(query, settings)
    if free_run.records:
        return free_run.records, tried + [f"{free_run.provider}(free)"]
    return [], tried


async def search_images(
    query: str,
    *,
    max_images: int | None = None,
    max_pages: int | None = None,
    search_mode: str = "default",
    cache: Any | None = None,
    output_dir: str | Path | None = None,
) -> ImageSearchOutcome:
    """Search the web, download images, and prepare multimodal input."""
    started = time.monotonic()
    settings = load_image_search_settings()
    requested_images = _positive_int(max_images)
    if requested_images is not None:
        settings["max_images"] = min(requested_images, MAX_IMAGES_CAP)
    requested_pages = _positive_int(max_pages)
    if requested_pages is not None:
        settings["max_pages"] = min(requested_pages, MAX_PAGES_CAP)

    outcome = ImageSearchOutcome(query=query)
    if not query.strip():
        outcome.error = "query is empty"
        return outcome
    records, tried = await _collect_records(query, search_mode=search_mode, cache=cache)
    outcome.stats["records"] = len(records)
    if not records:
        outcome.error = f"no search results (providers_tried: {', '.join(tried) or 'none'})"
        return outcome

    # 1) 搜索引擎返回的图片结果：已按查询相关性排序，直接按原顺序使用。
    candidates: list[ImageCandidate] = []
    seen: set[str] = set()
    for record in records:
        for url in record.images:
            if url in seen or not is_public_http_url(url):
                continue
            seen.add(url)
            candidates.append(
                ImageCandidate(
                    url=url,
                    source_page=record.url,
                    source_title=record.title,
                    alt=record.title,
                )
            )
    outcome.stats["provider_images"] = len(candidates)

    # 2) 兜底：抓结果页 <img>。相关性来自页面名次而非查询，只在无图可用或显式
    #    允许补足时才做，避免引擎已排序的好图被网页噪声稀释。
    need_fallback = (
        not settings["prefer_provider_images"]
        or not candidates
        or (
            settings["allow_page_fallback"]
            and len(candidates) < settings["max_images"]
        )
    )
    if need_fallback:
        page_meta = [
            (record.url, record.title)
            for record in records[: settings["max_pages"]]
            if record.url
        ]
        html_pages = await asyncio.gather(
            *[asyncio.to_thread(_fetch_html, page_url) for page_url, _ in page_meta],
            return_exceptions=True,
        )
        for (page_url, page_title), html in zip(page_meta, html_pages):
            if isinstance(html, BaseException) or not html:
                continue
            for candidate in extract_image_candidates(
                html,
                page_url,
                min_side=settings["min_image_side"],
            ):
                if candidate.url in seen:
                    continue
                candidate.source_title = page_title
                seen.add(candidate.url)
                candidates.append(candidate)
        outcome.stats["page_candidates"] = (
            len(candidates) - outcome.stats["provider_images"]
        )
    else:
        outcome.stats["page_candidates"] = 0

    target_dir = _resolve_output_dir(query, output_dir)
    outcome.output_dir = str(target_dir)

    downloader = ImageDownloader()
    total_bytes = 0
    for candidate in candidates:
        if len(outcome.images) >= settings["max_images"]:
            break
        if time.monotonic() - started >= TOTAL_TIME_BUDGET_SECONDS:
            outcome.hints.append("Stopped early: total time budget reached.")
            break
        if total_bytes >= settings["max_total_bytes"]:
            outcome.hints.append("Stopped early: total image byte budget reached.")
            break
        image = await asyncio.to_thread(
            downloader.download,
            candidate,
            target_dir,
            max_bytes=min(
                settings["max_bytes_per_image"],
                settings["max_total_bytes"] - total_bytes,
            ),
            min_side=settings["min_image_side"],
        )
        if image is None:
            continue
        total_bytes += image.byte_size
        outcome.images.append(image)

    outcome.stats["downloaded"] = len(outcome.images)
    outcome.stats["total_bytes"] = total_bytes
    logger.info(
        "[image_search] query=%r records=%d provider_images=%d page_candidates=%d "
        "downloaded=%d bytes=%d",
        query,
        outcome.stats.get("records", 0),
        outcome.stats.get("provider_images", 0),
        outcome.stats.get("page_candidates", 0),
        outcome.stats.get("downloaded", 0),
        total_bytes,
    )
    return outcome


def _resolve_output_dir(query: str, output_dir: str | Path | None) -> Path:
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    workspace = Path(get_agent_workspace_dir()).expanduser().resolve()
    if output_dir is not None:
        raw = str(output_dir).strip()
        # 模型偶尔把提示词里的模板占位符当路径传入（如 "{output_dir}/images"），
        # 不能真按该名字建目录，回落到默认检索图片目录。
        if raw and "{" not in raw and "}" not in raw:
            candidate = Path(raw).expanduser()
            target = candidate if candidate.is_absolute() else workspace / candidate
            target = target.resolve()
            # 写盘位置由模型决定，必须收敛在工作区内，避免任意目录落盘。
            if target == workspace or target.is_relative_to(workspace):
                return target
            logger.warning(
                "[image_search] output_dir outside workspace, fallback: %s", raw,
            )

    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:8]
    return workspace / "search_images" / digest


__all__ = [
    "DownloadedImage",
    "ImageCandidate",
    "ImageDownloader",
    "ImageSearchOutcome",
    "extract_image_candidates",
    "is_public_http_url",
    "load_image_search_settings",
    "search_images",
]
