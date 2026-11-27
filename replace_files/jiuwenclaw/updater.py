from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jiuwenclaw.config import get_config_raw
from jiuwenclaw.utils import get_user_workspace_dir
from jiuwenclaw.version import __version__


DEFAULT_RELEASE_API = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
DEFAULT_ASSET_PATTERN = "jiuwenclaw-setup-{version}.exe"
DEFAULT_SHA256_PATTERN = "jiuwenclaw-setup-{version}.exe.sha256"
DEFAULT_TIMEOUT_SECONDS = 20
DOWNLOAD_CHUNK_SIZE = 1024 * 512


def _updates_dir() -> Path:
    path = get_user_workspace_dir() / ".updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_version(raw: str) -> str:
    return (raw or "").strip().lstrip("vV")


def _version_key(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", _normalize_version(version))
    return tuple(int(part) for part in numbers) or (0,)


def _is_newer_version(candidate: str, current: str) -> bool:
    candidate_key = _version_key(candidate)
    current_key = _version_key(current)
    max_len = max(len(candidate_key), len(current_key))
    candidate_padded = candidate_key + (0,) * (max_len - len(candidate_key))
    current_padded = current_key + (0,) * (max_len - len(current_key))
    return candidate_padded > current_padded


def _parse_sha256(raw: str) -> str:
    token = (raw or "").strip().split()
    if not token:
        return ""
    digest = token[0].strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest):
        return digest
    return ""


@dataclass
class UpdateStatus:
    current_version: str
    latest_version: str = ""
    state: str = "idle"
    has_update: bool = False
    release_notes: str = ""
    published_at: str = ""
    asset_name: str = ""
    download_url: str = ""
    sha256_url: str = ""
    downloaded_path: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    error: str = ""
    checked_at: float = 0.0
    installing: bool = False


class WindowsUpdaterService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._download_thread: threading.Thread | None = None
        self._status = UpdateStatus(current_version=__version__)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            status = asdict(self._status)
        status["platform"] = sys.platform
        status["platform_supported"] = sys.platform == "win32"
        return status

    def get_runtime_config(self) -> dict[str, Any]:
        config = self._load_config()
        return {
            "enabled": config["enabled"],
            "repo_owner": config["repo_owner"],
            "repo_name": config["repo_name"],
            "release_api_url": config["release_api_url"],
            "asset_name_pattern": config["asset_name_pattern"],
            "sha256_name_pattern": config["sha256_name_pattern"],
            "timeout_seconds": config["timeout_seconds"],
        }

    def check(self, manual: bool = False) -> dict[str, Any]:
        if sys.platform != "win32":
            self._update_status(
                state="unsupported",
                error="Windows updater is only available on Windows.",
            )
            return self.get_status()

        config = self._load_config()
        if not config["enabled"]:
            self._update_status(state="disabled", error="Updater is disabled.")
            return self.get_status()

        self._update_status(state="checking", error="")
        try:
            release = self._fetch_json(
                config["release_api_url"], config["timeout_seconds"]
            )
            latest_version = _normalize_version(str(release.get("tag_name") or ""))
            if not latest_version:
                raise RuntimeError("Latest release tag is missing.")

            asset_name = config["asset_name_pattern"].format(version=latest_version)
            assets = release.get("assets") or []
            asset = next(
                (
                    item
                    for item in assets
                    if isinstance(item, dict) and item.get("name") == asset_name
                ),
                None,
            )
            if asset is None:
                raise RuntimeError(f"Release asset not found: {asset_name}")

            sha256_url = ""
            sha256_name = config["sha256_name_pattern"].format(version=latest_version)
            sha_asset = next(
                (
                    item
                    for item in assets
                    if isinstance(item, dict) and item.get("name") == sha256_name
                ),
                None,
            )
            if isinstance(sha_asset, dict):
                sha256_url = str(sha_asset.get("browser_download_url") or "")

            has_update = _is_newer_version(latest_version, __version__)
            next_state = "update_available" if has_update else "up_to_date"
            self._update_status(
                latest_version=latest_version,
                has_update=has_update,
                release_notes=str(release.get("body") or ""),
                published_at=str(release.get("published_at") or ""),
                asset_name=asset_name,
                download_url=str(asset.get("browser_download_url") or ""),
                sha256_url=sha256_url,
                checked_at=time.time(),
                state=next_state,
                error="",
                installing=False,
            )
        except Exception as exc:  # noqa: BLE001
            error_prefix = (
                "Manual update check failed"
                if manual
                else "Startup update check failed"
            )
            self._update_status(
                state="error", error=f"{error_prefix}: {exc}", checked_at=time.time()
            )
        return self.get_status()

    def start_download(self) -> dict[str, Any]:
        if sys.platform != "win32":
            self._update_status(
                state="unsupported",
                error="Windows updater is only available on Windows.",
            )
            return self.get_status()

        status = self.get_status()
        if status["state"] == "downloading":
            return status

        if not status["has_update"] or not status["download_url"]:
            status = self.check(manual=True)
            if not status["has_update"] or not status["download_url"]:
                return status

        self._update_status(
            state="downloading",
            error="",
            downloaded_bytes=0,
            total_bytes=0,
            installing=False,
        )
        thread = threading.Thread(
            target=self._download_worker,
            daemon=True,
            name="jiuwenclaw-updater-download",
        )
        self._download_thread = thread
        thread.start()
        return self.get_status()

    def mark_installing(self, installer_path: str) -> dict[str, Any]:
        self._update_status(
            state="installing",
            installing=True,
            downloaded_path=installer_path,
            error="",
        )
        return self.get_status()

    def _download_worker(self) -> None:
        status = self.get_status()
        download_url = str(status["download_url"])
        asset_name = str(status["asset_name"])
        sha256_url = str(status["sha256_url"])
        final_path = _updates_dir() / asset_name
        partial_path = final_path.with_suffix(final_path.suffix + ".part")
        try:
            self._download_file(download_url, partial_path)
            if sha256_url:
                sha_raw = self._fetch_text(
                    sha256_url, self._load_config()["timeout_seconds"]
                )
                expected_sha = _parse_sha256(sha_raw)
                if not expected_sha:
                    raise RuntimeError("Invalid SHA256 sidecar format.")
                actual_sha = self._sha256_file(partial_path)
                if actual_sha != expected_sha:
                    raise RuntimeError("Downloaded installer SHA256 mismatch.")

            partial_path.replace(final_path)
            size = final_path.stat().st_size
            self._update_status(
                state="downloaded",
                downloaded_path=str(final_path),
                downloaded_bytes=size,
                total_bytes=size,
                error="",
            )
        except Exception as exc:  # noqa: BLE001
            if partial_path.exists():
                partial_path.unlink(missing_ok=True)
            self._update_status(
                state="error",
                error=f"Update download failed: {exc}",
                downloaded_bytes=0,
            )

    def _download_file(self, url: str, destination: Path) -> None:
        request = Request(url, headers=self._request_headers())
        destination.parent.mkdir(parents=True, exist_ok=True)
        timeout_seconds = self._load_config()["timeout_seconds"]
        with (
            urlopen(request, timeout=timeout_seconds) as response,
            open(destination, "wb") as handle,
        ):
            total_header = response.headers.get("Content-Length")
            total_bytes = (
                int(total_header) if total_header and total_header.isdigit() else 0
            )
            self._update_status(total_bytes=total_bytes)

            downloaded = 0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                self._update_status(
                    downloaded_bytes=downloaded, total_bytes=total_bytes
                )

    def _fetch_json(self, url: str, timeout_seconds: int) -> dict[str, Any]:
        return json.loads(self._fetch_text(url, timeout_seconds))

    def _fetch_text(self, url: str, timeout_seconds: int) -> str:
        request = Request(url, headers=self._request_headers())
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} when requesting {url}") from exc
        except URLError as exc:
            raise RuntimeError(
                f"Network error when requesting {url}: {exc.reason}"
            ) from exc

    @staticmethod
    def _request_headers() -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"JiuwenClaw-Updater/{__version__}",
        }
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _load_config() -> dict[str, Any]:
        raw = get_config_raw() or {}
        updater = raw.get("updater") or {}
        owner = str(updater.get("repo_owner") or "CharlieZhao95").strip()
        repo = str(updater.get("repo_name") or "jiuwenclaw").strip()
        release_api_url = str(updater.get("release_api_url") or "").strip()
        if not release_api_url:
            release_api_url = DEFAULT_RELEASE_API.format(owner=owner, repo=repo)

        timeout_seconds = updater.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        try:
            timeout_seconds = max(5, int(timeout_seconds))
        except (TypeError, ValueError):
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS

        return {
            "enabled": bool(updater.get("enabled", True)),
            "repo_owner": owner,
            "repo_name": repo,
            "release_api_url": release_api_url,
            "asset_name_pattern": str(
                updater.get("asset_name_pattern") or DEFAULT_ASSET_PATTERN
            ),
            "sha256_name_pattern": str(
                updater.get("sha256_name_pattern") or DEFAULT_SHA256_PATTERN
            ),
            "timeout_seconds": timeout_seconds,
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()

    def _update_status(self, **updates: Any) -> None:
        with self._lock:
            for key, value in updates.items():
                setattr(self._status, key, value)
