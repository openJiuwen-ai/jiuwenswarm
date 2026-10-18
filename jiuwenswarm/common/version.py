from __future__ import annotations

from importlib import metadata

from jiuwenswarm.common._build_config import PACKAGE_NAME, VERSION


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


__version__ = VERSION
