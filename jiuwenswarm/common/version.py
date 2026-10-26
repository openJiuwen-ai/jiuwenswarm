from __future__ import annotations

from importlib import metadata
import json
from datetime import date
from pathlib import Path

from jiuwenswarm.common._build_config import PACKAGE_NAME, VERSION


_BUILD_METADATA_PATH = Path(__file__).resolve().parents[1] / "resources" / "build-metadata.json"


def get_runtime_build_date() -> str:
    """Read the immutable packaging date; old packages and source have no date."""
    try:
        payload = json.loads(_BUILD_METADATA_PATH.read_text(encoding="utf-8"))
        value = payload.get("build_date") if isinstance(payload, dict) else None
        if not isinstance(value, str) or len(value) != 10:
            return ""
        return date.fromisoformat(value).isoformat()
    except (OSError, ValueError):
        return ""


def get_runtime_version() -> str:
    """Return the version embedded in the running distribution when available.

    Daily wheel builds may add a timestamp (or another build suffix) to the
    distribution metadata without changing the checked-in source version.
    Reading that metadata lets the Web and TUI clients identify the exact
    artifact they are running.  Source checkouts and frozen bundles do not
    always have ``*.dist-info`` metadata, so they continue to use the
    generated build configuration as a fallback.
    """
    try:
        installed = metadata.version(PACKAGE_NAME).strip()
    except (metadata.PackageNotFoundError, ValueError):
        return VERSION
    return installed or VERSION


def get_runtime_version_label() -> str:
    """Return a language-neutral version and packaging date for diagnostics."""
    version = get_runtime_version()
    build_date = get_runtime_build_date()
    return f"{version} · {build_date}" if build_date else version


__version__ = VERSION
