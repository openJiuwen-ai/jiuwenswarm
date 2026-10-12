# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Use the ripgrep binary shipped inside the frozen desktop app.

agent-core's ``grep`` tool and the Code-mode repo tools look for ripgrep via
``OPENJIUWEN_RG`` / ``ICODE_RG``, then ``PATH``, and otherwise fall back to
slower search paths. Desktop users rarely have ``rg`` installed, so the frozen
build ships one under ``<bundle>/vendor/ripgrep/`` (deliberately *not* on
``PATH``) and this module points ``OPENJIUWEN_RG`` at it — but only after the
binary proved it runs. On macOS a quarantined copy that is not notarized is
either killed on launch (``SIGKILL``, exit code 137) or held by Gatekeeper,
which the check's timeout catches. A broken ``rg`` is worse than none, because
callers treat a crashing ripgrep as a failed search.

The frozen entry calls :func:`configure_bundled_ripgrep` once per process
before anything else is imported; the result is recorded in
``JIUWENSWARM_BUNDLED_RG`` so child processes inherit it without re-running
the check. :func:`report_bundled_ripgrep_status` logs that record once logging
is configured. Only the standard library is used here.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import MutableMapping
from pathlib import Path

LOGGER = logging.getLogger("jiuwenswarm.bundled_ripgrep")

#: Environment variables agent-core's resolver reads, in its order.
RG_ENV_KEYS = ("OPENJIUWEN_RG", "ICODE_RG")
#: Records the outcome for child processes: ``ok:<version>``,
#: ``unusable:<reason>``, ``absent``, or ``user:<ENV_KEY>``.
STATUS_ENV = "JIUWENSWARM_BUNDLED_RG"
#: Location of the binary relative to the frozen bundle's data root.
BUNDLE_SUBDIR = ("vendor", "ripgrep")
DEFAULT_TIMEOUT_SECONDS = 3.0


def bundled_rg_name(platform: str | None = None) -> str:
    """``rg.exe`` on Windows, ``rg`` elsewhere."""
    return "rg.exe" if (platform or sys.platform) == "win32" else "rg"


def frozen_bundle_root() -> Path | None:
    """The frozen app's data root (``sys._MEIPASS``), or ``None`` for source runs."""
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(sys.executable).resolve().parent / "_internal"


def bundled_rg_path(bundle_root: Path | None = None) -> Path | None:
    """Path of the bundled ripgrep under ``bundle_root`` if the file exists."""
    root = bundle_root if bundle_root is not None else frozen_bundle_root()
    if root is None:
        return None
    candidate = root.joinpath(*BUNDLE_SUBDIR, bundled_rg_name())
    return candidate if candidate.is_file() else None


def self_check(
    path: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[bool, str]:
    """Run ``<rg> --version``; return ``(ok, version line or reason)``."""
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        hint = (
            " (on macOS, Gatekeeper holds quarantined binaries that are not notarized)"
            if sys.platform == "darwin"
            else ""
        )
        return False, f"`rg --version` did not finish within {timeout:g}s{hint}"
    except OSError as exc:
        return False, f"could not start: {exc}"
    first_line = (result.stdout or "").strip().splitlines()[:1]
    version = first_line[0] if first_line else ""
    if result.returncode == 0 and version.startswith("ripgrep"):
        return True, version
    if result.returncode < 0:
        signal_number = -result.returncode
        hint = ""
        if sys.platform == "darwin" and signal_number == 9:
            hint = (
                " (macOS kills binaries that are quarantined or not validly "
                "signed; release builds must be signed and notarized)"
            )
        return False, f"killed by signal {signal_number}{hint}"
    detail = (result.stderr or result.stdout or "").strip()[:200]
    return False, f"exit code {result.returncode}: {detail or 'no output'}"


def configure_bundled_ripgrep(
    environ: MutableMapping[str, str] | None = None,
    *,
    bundle_root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Point ``OPENJIUWEN_RG`` at the bundled ripgrep when it works.

    Never overrides a user-set ``OPENJIUWEN_RG`` / ``ICODE_RG``, never re-runs
    the check when a parent process already recorded the outcome, and does
    nothing for source runs (no bundle). Returns the configured path, if any.
    """
    env = os.environ if environ is None else environ
    for key in RG_ENV_KEYS:
        if (env.get(key) or "").strip():
            env.setdefault(STATUS_ENV, f"user:{key}")
            return None
    if STATUS_ENV in env:
        return None
    root = bundle_root if bundle_root is not None else frozen_bundle_root()
    if root is None:
        return None
    path = bundled_rg_path(root)
    if path is None:
        env[STATUS_ENV] = "absent"
        return None
    ok, detail = self_check(path, timeout=timeout)
    if not ok:
        env[STATUS_ENV] = f"unusable:{detail}"
        LOGGER.warning(
            "Bundled ripgrep at %s is unusable (%s); search tools will use rg on "
            "PATH or their built-in fallback",
            path,
            detail,
        )
        return None
    env["OPENJIUWEN_RG"] = str(path)
    env[STATUS_ENV] = f"ok:{detail}"
    return str(path)


def report_bundled_ripgrep_status(
    logger: logging.Logger | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> str:
    """Log the recorded outcome once logging is set up; return the raw status."""
    env = os.environ if environ is None else environ
    log = logger or LOGGER
    status = env.get(STATUS_ENV, "")
    if status.startswith("ok:"):
        log.info(
            "[ripgrep] using bundled %s at %s", status[3:], env.get("OPENJIUWEN_RG", "")
        )
    elif status.startswith("unusable:"):
        log.warning(
            "[ripgrep] bundled binary is unusable (%s); using rg on PATH or the "
            "Python fallback",
            status[len("unusable:") :],
        )
    elif status.startswith("user:"):
        log.info("[ripgrep] using %s from %s", env.get(status[5:], ""), status[5:])
    elif status == "absent":
        log.info("[ripgrep] this build has no bundled ripgrep; using rg on PATH if any")
    return status


__all__ = [
    "BUNDLE_SUBDIR",
    "RG_ENV_KEYS",
    "STATUS_ENV",
    "bundled_rg_name",
    "bundled_rg_path",
    "configure_bundled_ripgrep",
    "frozen_bundle_root",
    "report_bundled_ripgrep_status",
    "self_check",
]
