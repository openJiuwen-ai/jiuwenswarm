from __future__ import annotations

import os
import signal
import sys
import threading
import time

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin

from jiuwenswarm.common.config import get_config_raw
from jiuwenswarm.common.version import __version__
from jiuwenswarm.common.upgrade_executor import create_executor
from jiuwenswarm.common.version_source import (
    GitHubReleasesSource,
    GitCodeReleasesSource,
    PyPIVersionSource,
    ReleaseInfo,
)

DEFAULT_RELEASE_API_GITCODE = "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/latest"
DEFAULT_RELEASE_API_GITHUB = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
DEFAULT_RELEASE_API_PYPI = "https://pypi.org/simple/{package}/"
DEFAULT_ASSET_PATTERN_WINDOWS = "JiuwenSwarm-setup-{version}.exe"
DEFAULT_ASSET_PATTERN_MACOS = "JiuwenSwarm-{version}.dmg"
DEFAULT_ASSET_PATTERN_LINUX = "JiuwenSwarm-{version}.tar.gz"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_TEXT = "WbrW92Yn6jif-4Ks3kvzhWVv"
DESKTOP_ENV_FLAG = "JIUWENSWARM_DESKTOP"

DEFAULT_SOURCE_CONFIG: dict[str, Any] = {
    "desktop_release_api_type": "gitcode",
    "repo_owner": "openJiuwen",
    "repo_name": "jiuwenswarm",
    "release_api_url": "",
    "pypi_mirror": "https://mirrors.aliyun.com/pypi",
    "asset_name_pattern": "",
    "asset_name_pattern_windows": DEFAULT_ASSET_PATTERN_WINDOWS,
    "asset_name_pattern_macos": DEFAULT_ASSET_PATTERN_MACOS,
    "asset_name_pattern_linux": DEFAULT_ASSET_PATTERN_LINUX,
}


def get_access_token() -> str:
    return os.getenv("GITCODE_TOKEN", "").strip() or DEFAULT_TEXT


def _is_newer_version(candidate: str, current: str) -> bool:
    """Return True when *candidate* is a newer release than *current*.

    Pre-release rules (consistent with :func:`release_sort_key`):
    - Base version compared numerically first (``0.2.3`` > ``0.2.2``).
    - A stable release is always newer than a pre-release with the same base
      version (``0.2.3`` > ``0.2.3.beta1``).
    - Among pre-releases at the same base, the type decides first:
      dev < alpha < beta < rc < pre.
    - Within the same type, a larger pre-release number is newer
      (``0.2.3.beta2`` > ``0.2.3.beta1``).
    """
    from jiuwenswarm.common.version_source import release_sort_key
    return release_sort_key(candidate) > release_sort_key(current)


def _detect_install_mode() -> str:
    desktop_env = os.getenv(DESKTOP_ENV_FLAG, "").strip().lower()
    if desktop_env in {"1", "true", "yes", "on"}:
        return "desktop"
    return "desktop" if getattr(sys, "frozen", False) else "pip"


def _platform_asset_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


@dataclass
class UpdateStatus:
    current_version: str
    latest_version: str = ""
    state: str = "idle"
    has_update: bool = False
    install_mode: str = ""
    release_notes: str = ""
    published_at: str = ""
    source_type: str = ""
    asset_name: str = ""
    matched_asset: str = ""
    download_url: str = ""
    downloaded_path: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    error: str = ""
    checked_at: float = 0.0
    installing: bool = False
    restart_command: str = ""


class UpdaterService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._download_thread: threading.Thread | None = None
        self._status = UpdateStatus(
            current_version=__version__,
            install_mode=_detect_install_mode(),
        )

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            status = asdict(self._status)
        status["platform"] = sys.platform
        status["platform_supported"] = True
        return status

    def get_runtime_config(self) -> dict[str, Any]:
        config = self._load_config()
        return {
            "enabled": config["enabled"],
            "desktop_release_api_type": config["desktop_release_api_type"],
            "release_api_type": config["release_api_type"],
            "install_mode": config["install_mode"],
            "repo_owner": config["repo_owner"],
            "repo_name": config["repo_name"],
            "release_api_url": config["release_api_url"],
            "asset_name_pattern": config["asset_name_pattern_windows"],
            "asset_name_pattern_windows": config["asset_name_pattern_windows"],
            "asset_name_pattern_macos": config["asset_name_pattern_macos"],
            "asset_name_pattern_linux": config["asset_name_pattern_linux"],
            "timeout_seconds": config["timeout_seconds"],
            "pypi_mirror": config["pypi_mirror"],
            "access_token": self._mask_token(config["access_token"]),
        }

    @staticmethod
    def _mask_token(token: str) -> str:
        if len(token) <= 8:
            return token[:2] + "****" + token[-2:] if len(token) > 4 else "****"
        return token[:4] + "****" + token[-4:]

    def check(self, manual: bool = False) -> dict[str, Any]:
        config = self._load_config()
        if not config["enabled"]:
            self._update_status(state="disabled", error="Updater is disabled.")
            return self.get_status()

        self._update_status(state="checking", error="")
        try:
            self._check(config)
        except Exception as exc:
            self._update_status(
                latest_version="",
                has_update=False,
                release_notes="",
                published_at="",
                source_type="",
                asset_name="",
                matched_asset="",
                download_url="",
                state="error",
                error=f"Update check failed: {exc}",
                checked_at=time.time(),
            )
        return self.get_status()

    def start_download(self) -> dict[str, Any]:
        status = self.get_status()
        install_mode = status.get("install_mode", "desktop")

        if status["state"] in ("downloading", "upgrading"):
            return status

        if not status.get("has_update"):
            self._update_status(
                state="error",
                error="No update available. Please run an update check first.",
            )
            return self.get_status()

        if install_mode == "desktop" and not status.get("download_url"):
            self._update_status(
                state="error",
                error="No download URL resolved. Please run an update check first.",
            )
            return self.get_status()

        config = self._load_config()
        executor = create_executor(
            install_mode,
            {**config, **status},
            self._executor_callback,
        )

        pip_state = "upgrading" if install_mode == "pip" else "downloading"
        self._update_status(
            state=pip_state,
            error="",
            downloaded_bytes=0,
            total_bytes=0,
            installing=False,
        )

        thread = threading.Thread(
            target=executor.install,
            daemon=True,
            name="JiuwenSwarm-Updater-download",
        )
        self._download_thread = thread
        thread.start()
        return self.get_status()

    def start_upgrade(self) -> dict[str, Any]:
        status = self.get_status()
        install_mode = status.get("install_mode", "desktop")

        # Desktop installs are driven by the desktop app via the pywebview
        # install_update API (it owns the window and can close it).  This WS
        # method only handles pip-mode restarts.
        if install_mode != "pip":
            self._update_status(
                state="error",
                error=(
                    "Desktop upgrades are handled by the desktop app via "
                    "install_update. This method is only for pip mode."
                ),
            )
            return self.get_status()

        config = self._load_config()
        executor = create_executor(
            install_mode,
            {**config, **status},
            self._executor_callback,
        )

        self._update_status(
            state="restarting",
            installing=True,
            error="",
        )

        try:
            executor.upgrade()
        except Exception as exc:
            self._update_status(
                state="error",
                error=f"Upgrade failed: {exc}",
            )
            return self.get_status()

        threading.Timer(3.0, os.kill, args=[os.getpid(), signal.SIGTERM]).start()

        return self.get_status()

    def _executor_callback(self, updates: dict[str, Any]) -> None:
        self._update_status(**updates)

    @staticmethod
    def _create_version_source(config: dict[str, Any]) -> Any:
        api_type = config["release_api_type"]
        timeout = config["timeout_seconds"]
        api_url = config["release_api_url"]

        creators = {
            "github": lambda: GitHubReleasesSource(
                owner=config["repo_owner"],
                repo=config["repo_name"],
                token=os.getenv("GITHUB_TOKEN", ""),
                api_url=api_url,
                timeout_seconds=timeout,
            ),
            "gitcode": lambda: GitCodeReleasesSource(
                owner=config["repo_owner"],
                repo=config["repo_name"],
                access_token=config["access_token"],
                api_url=api_url,
                timeout_seconds=timeout,
            ),
            "pypi": lambda: PyPIVersionSource(
                package=config["repo_name"],
                mirror=config["pypi_mirror"],
                timeout_seconds=timeout,
            ),
        }

        creator = creators.get(api_type)
        if creator is None:
            raise ValueError(f"Unsupported release_api_type: {api_type}")
        return creator()

    def _check(self, config: dict[str, Any]) -> None:
        source = self._create_version_source(config)
        try:
            release = source.fetch_latest()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch latest release from {config['release_api_type']}: {exc}"
            ) from exc

        latest_version = release.version
        if not latest_version:
            raise RuntimeError("Latest release version is missing.")

        install_mode = _detect_install_mode()
        has_update = _is_newer_version(latest_version, __version__)

        if not has_update:
            self._update_status(
                latest_version=latest_version,
                has_update=False,
                install_mode=install_mode,
                release_notes=release.release_notes,
                published_at=release.published_at,
                source_type=release.source_type,
                matched_asset="",
                checked_at=time.time(),
                state="up_to_date",
                error="",
                installing=False,
            )
            return

        if install_mode == "desktop":
            self._resolve_desktop_asset(config, release)
        else:
            self._resolve_pip_asset(config, release)

    def _resolve_desktop_asset(self, config: dict[str, Any], release: ReleaseInfo) -> None:
        platform_key = _platform_asset_key()
        pattern_key = f"asset_name_pattern_{platform_key}"
        default_pattern = {
            "windows": DEFAULT_ASSET_PATTERN_WINDOWS,
            "macos": DEFAULT_ASSET_PATTERN_MACOS,
            "linux": DEFAULT_ASSET_PATTERN_LINUX,
        }.get(platform_key, DEFAULT_ASSET_PATTERN_WINDOWS)
        asset_name_pattern = config.get(pattern_key) or default_pattern
        asset_name = asset_name_pattern.format(version=release.version)

        matched = next((a for a in release.assets if a.name == asset_name), None)
        if not matched:
            raise RuntimeError(f"Desktop installer not found: {asset_name}")

        self._update_status(
            latest_version=release.version,
            has_update=True,
            install_mode="desktop",
            release_notes=release.release_notes,
            published_at=release.published_at,
            source_type=release.source_type,
            asset_name=asset_name,
            matched_asset=asset_name,
            download_url=matched.download_url,
            checked_at=time.time(),
            state="update_available",
            error="",
            installing=False,
        )

    def _resolve_pip_asset(self, config: dict[str, Any], release: ReleaseInfo) -> None:
        whl = next((a for a in release.assets if a.name.endswith(".whl")), None)
        if not whl:
            raise RuntimeError(
                "No .whl package found in the release assets. "
                "For pip installations the release must include a .whl file."
            )

        self._update_status(
            latest_version=release.version,
            has_update=True,
            install_mode="pip",
            release_notes=release.release_notes,
            published_at=release.published_at,
            source_type=release.source_type,
            asset_name=whl.name,
            matched_asset=whl.name,
            download_url=whl.download_url,
            checked_at=time.time(),
            state="update_available",
            error="",
            installing=False,
        )

    @staticmethod
    def _load_config() -> dict[str, Any]:
        raw = get_config_raw() or {}
        updater = raw.get("updater") or {}

        api_type = str(updater.get("desktop_release_api_type") or "gitcode").strip().lower()
        desktop_api_type = api_type
        if _detect_install_mode() != "desktop":
            api_type = "pypi"
        owner = str(updater.get("repo_owner") or "openJiuwen").strip()
        repo = str(updater.get("repo_name") or "jiuwenswarm").strip()
        release_api_url = str(updater.get("release_api_url") or "").strip()
        if not release_api_url:
            if api_type == "github":
                release_api_url = DEFAULT_RELEASE_API_GITHUB.format(owner=owner, repo=repo)
            elif api_type == "pypi":
                pypi_mirror = str(updater.get("pypi_mirror") or "").strip()
                if pypi_mirror:
                    release_api_url = urljoin(pypi_mirror, f"simple/{repo}/")
                else:
                    release_api_url = DEFAULT_RELEASE_API_PYPI.format(package=repo)
            else:
                release_api_url = DEFAULT_RELEASE_API_GITCODE.format(owner=owner, repo=repo)
        timeout_seconds = updater.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        try:
            timeout_seconds = max(5, int(timeout_seconds))
        except (TypeError, ValueError):
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS

        global_asset_name_pattern = updater.get("asset_name_pattern")

        return {
            "enabled": bool(updater.get("enabled", True)),
            "desktop_release_api_type": desktop_api_type,
            "release_api_type": api_type,
            "install_mode": _detect_install_mode(),
            "repo_owner": owner,
            "repo_name": repo,
            "release_api_url": release_api_url,
            "asset_name_pattern_windows": str(
                updater.get("asset_name_pattern_windows")
                or global_asset_name_pattern
                or DEFAULT_ASSET_PATTERN_WINDOWS
            ),
            "asset_name_pattern_macos": str(
                updater.get("asset_name_pattern_macos")
                or global_asset_name_pattern
                or DEFAULT_ASSET_PATTERN_MACOS
            ),
            "asset_name_pattern_linux": str(
                updater.get("asset_name_pattern_linux")
                or global_asset_name_pattern
                or DEFAULT_ASSET_PATTERN_LINUX
            ),
            "timeout_seconds": timeout_seconds,
            "access_token": get_access_token(),
            "pypi_mirror": str(updater.get("pypi_mirror") or "").strip(),
        }

    def _update_status(self, **updates: Any) -> None:
        with self._lock:
            for key, value in updates.items():
                setattr(self._status, key, value)
