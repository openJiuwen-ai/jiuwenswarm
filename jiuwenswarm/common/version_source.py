from __future__ import annotations

import json
import logging
import re
import socket

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from jiuwenswarm.common.version import __version__

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20
GITHUB_API = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
GITCODE_API = "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/latest"
PYPI_SIMPLE_API = "https://pypi.org/simple/{package}/"

# Matches pre-release markers: 0.2.0-beta1, 0.2.0.beta.1, 0.2.0alpha, 0.2.0rc2, 0.2.0.dev0, etc.
_PRERELEASE_PATTERN = re.compile(
    r"\d[.\-_]?(?:alpha|beta|rc|dev|pre|a|b)(?:\.?\d+)?(?:\b|$)",
    re.IGNORECASE,
)


def is_prerelease_version(version: str) -> bool:
    """Return True when *version* looks like a pre-release (alpha / beta / rc / dev)."""
    return bool(_PRERELEASE_PATTERN.search((version or "").strip().lstrip("vV")))


def strip_prerelease_suffix(version: str) -> str:
    """Remove the pre-release suffix so that ``0.2.0.beta1`` becomes ``0.2.0``."""
    normalized = (version or "").strip().lstrip("vV")
    # Capture the base numeric version (e.g. 0.2.0) and discard the optional
    # pre-release tail (e.g. .beta1, -rc.2, rc2, alpha, etc.).
    m = re.match(
        r"(\d+(?:\.\d+)*)"
        r"(?:[.\-_]?(?:alpha|beta|rc|dev|pre|a|b)(?:[.\-_]?\d+)?)*"
        r"$",
        normalized,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return normalized


def release_sort_key(version: str) -> tuple[tuple[int, ...], int, tuple[int, ...]]:
    """Total-order key so that newer versions sort higher.

    Ordering rules:
    - Base version compared numerically first (``0.2.3`` > ``0.2.2``).
    - At the same base, a stable release ranks above any pre-release
      (``0.2.3`` > ``0.2.3.beta1``).
    - Among pre-releases at the same base, the pre-release type decides first:
      dev < alpha < beta < rc < pre (``0.2.3.alpha2`` < ``0.2.3.beta1``).
    - Within the same pre-release type, a larger number is newer
      (``0.2.3.beta2`` > ``0.2.3.beta1``).
    """
    normalized = (version or "").strip().lstrip("vV")
    base = strip_prerelease_suffix(normalized)
    base_key = tuple(int(n) for n in re.findall(r"\d+", base)) or (0,)
    is_pre = is_prerelease_version(normalized)
    # Stable (5) outranks any pre-release at the same base.
    pre_type_rank = 5 if not is_pre else _PRERELEASE_TYPE_ORDER.get(
        _detect_prerelease_type(normalized), 0
    )
    pre_num: tuple[int, ...] = (0,)
    if is_pre:
        m = re.search(
            r"(?:alpha|beta|rc|dev|pre|a|b)\D*(\d+)",
            normalized,
            re.IGNORECASE,
        )
        if m:
            pre_num = (int(m.group(1)),)
    return (base_key, pre_type_rank, pre_num)


def _detect_prerelease_type(version: str) -> str:
    """Return the lowercase pre-release type marker found in *version*.

    Returns one of ``"dev"``, ``"alpha"``, ``"beta"``, ``"rc"``, ``"pre"``
    or ``""`` when the single-letter forms ``a``/``b`` are used.
    """
    lowered = (version or "").lower()
    for marker in ("dev", "alpha", "beta", "rc", "pre"):
        if marker in lowered:
            return marker
    if re.search(r"(?<![a-z])a(?=\d|[.\-_])", lowered):
        return "alpha"
    if re.search(r"(?<![a-z])b(?=\d|[.\-_])", lowered):
        return "beta"
    return ""


# Pre-release type ordering: dev < alpha < beta < rc < pre < stable.
_PRERELEASE_TYPE_ORDER: dict[str, int] = {
    "dev": 0,
    "alpha": 1,
    "beta": 2,
    "rc": 3,
    "pre": 4,
    "": 0,
}


def _is_draft_entry(data: dict) -> bool:
    """Return True when a release dict is an unpublished draft."""
    return bool(data.get("draft"))


def _is_prerelease_entry(data: dict) -> bool:
    """Return True when a release dict is a pre-release or draft."""
    return bool(
        data.get("prerelease")
        or data.get("is_prerelease")
        or data.get("draft")
    )


def _unwrap_list(raw: Any) -> list | None:
    """Normalise a list-API response that may be wrapped in a dict.

    Some hosts return ``{"data": [...]}`` or ``{"items": [...]}`` instead of a
    bare JSON array.  Returns the unwrapped list, or *raw* if it is already a
    list.  Returns None when the shape is unrecognised.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("data", "items", "releases", "results", "value"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                return candidate
    return None


def _with_query_params(url: str, **params: str | int) -> str:
    """Return *url* with query parameters added without dropping existing ones."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items()})
    return urlunsplit(parts._replace(query=urlencode(query)))


@dataclass
class ReleaseAsset:
    name: str
    download_url: str
    size: int = 0


@dataclass
class ReleaseInfo:
    version: str
    release_notes: str = ""
    published_at: str = ""
    assets: list[ReleaseAsset] = field(default_factory=list)
    source_type: str = ""
    prerelease: bool = False


class VersionSource(ABC):
    def __init__(self, name: str = "", timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds
        self._name = name

    @abstractmethod
    def fetch_latest(self) -> ReleaseInfo:
        ...

    def fetch_assets(self) -> list[ReleaseAsset]:
        return self.fetch_latest().assets

    @staticmethod
    def _clean_version(raw: str) -> str:
        cleaned = (raw or "").strip().lstrip("vV")
        # Match the full version including any pre-release suffix (e.g.
        # "0.2.3.beta1") so beta releases keep their suffix for asset matching
        # and version comparison.
        match = re.search(
            r"\d+(?:\.\d+)*(?:[.\-_]?(?:alpha|beta|rc|dev|pre|a|b)(?:\.?\d+)?)*",
            cleaned,
            re.IGNORECASE,
        )
        return match.group() if match else ""

    @classmethod
    def _best_version_from_texts(cls, values: list[str]) -> str:
        versions = [cls._clean_version(value) for value in values]
        versions = [version for version in versions if version]
        if not versions:
            return ""
        return max(versions, key=release_sort_key)

    @classmethod
    def _best_version_from_release_data(cls, data: dict, assets_raw: list) -> str:
        """Resolve a release version from tags, names, and asset filenames.

        Some release APIs can omit or normalize the tag for pre-releases.  The
        desktop installers still carry the canonical version in their filenames,
        so include asset names when selecting the newest release.
        """
        candidates = [
            str(data.get("tag_name") or ""),
            str(data.get("tag") or ""),
            str(data.get("version") or ""),
            str(data.get("name") or ""),
        ]
        candidates.extend(
            str(item.get("name") or "")
            for item in assets_raw
            if isinstance(item, dict)
        )
        return cls._best_version_from_texts(candidates)

    def _fetch_newest_from_list(
        self, list_url: str, headers: dict[str, str]
    ) -> ReleaseInfo | None:
        """Fetch the releases list and return the newest entry (incl. pre-releases).

        Drafts are skipped; pre-releases are included so that beta channels are
        detected.  Returns None when the list cannot be fetched or is empty.
        """
        try:
            raw = self._fetch_json(list_url, headers)
        except Exception as exc:
            logger.warning(
                "Failed to fetch releases list from %s (falling back to "
                "/latest, which excludes pre-releases): %s",
                list_url, exc,
            )
            return None
        entries = _unwrap_list(raw)
        if entries is None:
            return None
        best: ReleaseInfo | None = None
        best_key: tuple = ()
        for entry in entries:
            if not isinstance(entry, dict) or _is_draft_entry(entry):
                continue
            release = self._parse_release(entry)
            if release is None:
                continue
            key = release_sort_key(release.version)
            if best is None or key > best_key:
                best = release
                best_key = key
        return best

    def _parse_release(self, data: dict) -> ReleaseInfo | None:
        """Parse a single release dict.  Override in subclasses."""
        raise NotImplementedError

    def _fetch_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return json.loads(self._fetch_text(url, headers))

    def _fetch_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        request = Request(url, headers=headers or {})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} when requesting {url}") from exc
        except socket.timeout as exc:
            raise RuntimeError(
                f"Timeout ({self._timeout}s) when requesting {url}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"Network error when requesting {url}: {exc.reason}"
            ) from exc


class GitHubReleasesSource(VersionSource):
    def __init__(
        self,
        owner: str,
        repo: str,
        token: str = "",
        api_url: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(name=repo, timeout_seconds=timeout_seconds)
        self._api_url = api_url or GITHUB_API.format(owner=owner, repo=repo)
        self._token = token
        # Derive the releases-list URL from the latest-release URL so that
        # pre-releases are also discoverable (the /latest endpoint excludes them).
        self._list_url = _with_query_params(
            self._api_url.removesuffix("/latest"),
            per_page=100,
        )

    def fetch_latest(self) -> ReleaseInfo:
        headers = self._build_headers()
        release = self._fetch_newest_from_list(self._list_url, headers)
        if release is not None:
            return release

        # Fallback to /latest (stable-only) when the list endpoint is unusable.
        data = self._fetch_json(self._api_url, headers)
        release = self._parse_release(data)
        if release is None:
            raise RuntimeError("GitHub release tag_name is missing or empty.")
        return release

    def _parse_release(self, data: dict) -> ReleaseInfo | None:
        published_at = str(data.get("published_at") or "")
        body = str(data.get("body") or "")
        prerelease = _is_prerelease_entry(data)
        assets_raw = data.get("assets") or []
        version = self._best_version_from_release_data(data, assets_raw)
        if not version:
            return None
        assets = [
            ReleaseAsset(
                name=str(item.get("name", "")),
                download_url=str(item.get("browser_download_url", "")),
                size=int(item.get("size", 0)),
            )
            for item in assets_raw
            if isinstance(item, dict) and item.get("name")
        ]
        return ReleaseInfo(
            version=version,
            release_notes=body,
            published_at=published_at,
            assets=assets,
            source_type="github",
            prerelease=prerelease,
        )

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": f"{self._name}-Updater/{__version__}",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


class GitCodeReleasesSource(VersionSource):
    def __init__(
        self,
        owner: str,
        repo: str,
        access_token: str = "",
        api_url: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(name=repo, timeout_seconds=timeout_seconds)
        self._api_url = api_url or GITCODE_API.format(owner=owner, repo=repo)
        self._access_token = access_token
        # Derive the releases-list URL from the latest-release URL.
        self._list_url = _with_query_params(
            self._api_url.removesuffix("/latest"),
            per_page=100,
        )

    def fetch_latest(self) -> ReleaseInfo:
        headers = self._build_headers()
        release = self._fetch_newest_from_list(self._list_url, headers)
        if release is not None:
            return release

        # Fallback to the /latest endpoint when the list endpoint is unusable.
        data = self._fetch_json(self._api_url, headers)
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected GitCode API response type: {type(data)}")
        release = self._parse_release(data)
        if release is None:
            raise RuntimeError("GitCode release tag_name is missing or empty.")
        return release

    def _parse_release(self, data: dict) -> ReleaseInfo | None:
        release_notes = str(data.get("body") or data.get("description") or "")
        published_at = str(
            data.get("published_at")
            or data.get("created_at")
            or ""
        )
        prerelease = _is_prerelease_entry(data)

        assets_raw = data.get("assets") or []
        version = VersionSource._best_version_from_release_data(data, assets_raw)
        if not version:
            return None
        assets = [
            ReleaseAsset(
                name=str(item.get("name", "")),
                download_url=str(item.get("url") or item.get("browser_download_url", "")),
                size=int(item.get("size", 0)),
            )
            for item in assets_raw
            if isinstance(item, dict) and item.get("name")
        ]

        return ReleaseInfo(
            version=version,
            release_notes=release_notes,
            published_at=published_at,
            assets=assets,
            source_type="gitcode",
            prerelease=prerelease,
        )

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": f"{self._name}-Updater/{__version__}",
        }
        if self._access_token:
            headers["PRIVATE-TOKEN"] = self._access_token
        return headers


class PyPIVersionSource(VersionSource):
    def __init__(
        self,
        package: str = "jiuwenswarm",
        mirror: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(name=package, timeout_seconds=timeout_seconds)
        if mirror:
            self._base_url = mirror.rstrip("/")
            self._api_url = urljoin(self._base_url + "/", f"simple/{package}/")
        else:
            self._base_url = "https://pypi.org"
            self._api_url = PYPI_SIMPLE_API.format(package=package)

    def fetch_latest(self) -> ReleaseInfo:
        data = self._fetch_simple_json()
        if data is None:
            raise RuntimeError("Failed to fetch PyPI simple API response.")
        files = data.get("files") or []
        if not isinstance(files, list):
            files = []

        whl_entries = [
            f for f in files
            if isinstance(f, dict) and f.get("filename", "").endswith(".whl")
        ]
        if not whl_entries:
            raise RuntimeError("No .whl files found in PyPI simple API response.")

        versions = set()
        for entry in whl_entries:
            fn = str(entry.get("filename", ""))
            m = re.match(rf"{re.escape(self._name)}-([\d.]+(?:[.\-_]?(?:alpha|beta|rc|dev|pre|a|b)(?:\.?\d+)?)*)-", fn)
            if m:
                versions.add(m.group(1))
        if not versions:
            raise RuntimeError("Could not parse any version from .whl filenames.")

        # Pick the newest version overall, including pre-releases (beta channel).
        latest_version = max(versions, key=release_sort_key)
        latest_whls = [e for e in whl_entries if latest_version in e.get("filename", "")]
        latest_whl = latest_whls[-1] if latest_whls else whl_entries[-1]

        published_at = str(latest_whl.get("upload-time") or "")
        assets = [
            ReleaseAsset(
                name=str(e.get("filename", "")),
                download_url=self._resolve_url(str(e.get("url", ""))),
                size=int(e.get("size", 0)),
            )
            for e in whl_entries
        ]

        return ReleaseInfo(
            version=latest_version,
            published_at=published_at,
            assets=assets,
            source_type="pypi",
        )

    def _fetch_simple_json(self) -> Any:
        req = Request(self._api_url)
        req.add_header("Accept", "application/vnd.pypi.simple.v1+json")
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, json.JSONDecodeError):
            return self._fetch_simple_html()

    def _fetch_simple_html(self) -> Any:
        raw = self._fetch_text(self._api_url)
        links = re.findall(
            r'<a\s+(?:[^>]*?\s+)?href="([^"]*)"[^>]*>([^<]+)</a>',
            raw,
        )
        files = []
        for href, text in links:
            filename = text.strip()
            if not filename:
                continue
            files.append({
                "filename": filename,
                "url": href,
            })
        return {"files": files}

    def _resolve_url(self, url: str) -> str:
        return urljoin(self._api_url, url)
