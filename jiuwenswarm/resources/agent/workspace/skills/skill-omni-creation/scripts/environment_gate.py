#!/usr/bin/env python3
"""Cross-platform runtime gate for the web/image pipeline.

This module uses only the Python standard library.  It can therefore run before
requests, Pillow, BeautifulSoup, or Playwright are importable.  It selects a
stable interpreter, re-executes the caller when necessary, repairs missing
Python packages and the Playwright Chromium binary, and aborts before any stage
files are touched when the environment cannot be repaired safely.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

_REEXEC_FLAG = "SKILL_OMNI_ENV_REEXEC"
_STATUS_DIR = Path(__file__).resolve().parent / "work"
_STATUS_FILE = _STATUS_DIR / "environment_status.json"
_OPERATION_TIMEOUT_SECONDS = 600

_PROFILE_MODULES: dict[str, dict[str, str]] = {
    "requests": {"requests": "requests"},
    "images": {"requests": "requests", "PIL": "Pillow"},
    "web": {"requests": "requests", "bs4": "beautifulsoup4", "playwright": "playwright"},
    "web-images": {
        "requests": "requests",
        "PIL": "Pillow",
        "bs4": "beautifulsoup4",
        "playwright": "playwright",
    },
}


class EnvironmentGateError(RuntimeError):
    """Raised when the runtime cannot be made ready."""


def _log(message: str) -> None:
    logger.info("[environment_gate] %s", message)


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _absolute_executable(path: Path | str) -> Path:
    # Do not resolve symlinks here. POSIX virtual-environment python binaries
    # are commonly symlinks to the base interpreter; resolving them would lose
    # the venv path and make re-execution silently leave the environment.
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _is_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _is_executable_python(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        result = subprocess.run(
            [str(path), "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_OPERATION_TIMEOUT_SECONDS,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _project_roots(project_dir: str | None) -> list[Path]:
    starts: list[Path] = []
    if project_dir:
        starts.append(_resolved(project_dir))
    env_project = os.environ.get("JIUWENSWARM_PROJECT_DIR") or os.environ.get("PROJECT_DIR")
    if env_project:
        starts.append(_resolved(env_project))
    starts.append(Path.cwd().resolve())

    roots: list[Path] = []
    seen: set[Path] = set()
    for start in starts:
        current = start if start.is_dir() else start.parent
        for candidate in (current, *current.parents):
            if candidate not in seen:
                seen.add(candidate)
                roots.append(candidate)
    return roots


def _find_project_venv(project_dir: str | None) -> tuple[Path | None, Path]:
    roots = _project_roots(project_dir)
    preferred_root = roots[0] if roots else Path.cwd().resolve()
    for root in roots:
        candidate = _venv_python(root / ".venv")
        if _is_executable_python(candidate):
            return _absolute_executable(candidate), root
    return None, preferred_root


def _create_project_venv(root: Path) -> Path | None:
    venv_dir = root / ".venv"
    target = _venv_python(venv_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        _log(f"No usable virtual environment found; creating {venv_dir}")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            check=False,
            timeout=_OPERATION_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and _is_executable_python(target):
            return _absolute_executable(target)
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"Virtual-environment creation failed: {exc}")
    return None


def select_interpreter(project_dir: str | None = None, *, create_venv: bool = True) -> Path:
    """Select active venv, current venv, project .venv, or create project .venv."""
    active = os.environ.get("VIRTUAL_ENV")
    if active:
        candidate = _venv_python(_resolved(active))
        if _is_executable_python(candidate):
            return _absolute_executable(candidate)

    current = _absolute_executable(sys.executable)

    # A project-local .venv is more stable than an unrelated interpreter that
    # merely happens to be a virtual environment. An explicitly activated
    # VIRTUAL_ENV still has the highest priority above this branch.
    project_python, root = _find_project_venv(project_dir)
    if project_python:
        return project_python

    if _is_venv() and _is_executable_python(current):
        return current

    if create_venv:
        created = _create_project_venv(root)
        if created:
            return created

    if _is_executable_python(current):
        return current
    raise EnvironmentGateError("No usable Python interpreter was found.")


def _same_executable(left: Path, right: Path) -> bool:
    return os.path.normcase(str(_absolute_executable(left))) == os.path.normcase(str(_absolute_executable(right)))


def _reexec_with_selected(selected: Path) -> None:
    current = _absolute_executable(sys.executable)
    if _same_executable(selected, current):
        return
    if os.environ.get(_REEXEC_FLAG) == "1":
        raise EnvironmentGateError(
            f"Interpreter selection loop detected: current={current}, selected={selected}"
        )

    caller = Path(sys.argv[0]).resolve()
    os.environ[_REEXEC_FLAG] = "1"
    _log(f"Re-executing with selected interpreter: {selected}")
    try:
        # Replace this process outright rather than spawning a child and
        # exiting with its return code — avoids a lingering wrapper process
        # and keeps termination out of this non-entry-point function.
        os.execv(str(selected), [str(selected), str(caller), *sys.argv[1:]])
    except OSError as exc:
        raise EnvironmentGateError(f"Could not start selected interpreter {selected}: {exc}") from exc


def _run(command: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    _log("Running: " + subprocess.list2cmdline(command))
    return subprocess.run(
        command,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _missing_packages(profile: str) -> list[str]:
    importlib.invalidate_caches()
    mapping = _PROFILE_MODULES[profile]
    return sorted({package for module, package in mapping.items() if importlib.util.find_spec(module) is None})


def _ensure_pip() -> None:
    check = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode == 0:
        return
    _log("pip is unavailable; attempting ensurepip.")
    result = _run([sys.executable, "-m", "ensurepip", "--upgrade"], timeout=_OPERATION_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise EnvironmentGateError(
            "pip is unavailable and ensurepip failed. Install Python with venv/pip support, then rerun."
        )


def _install_python_packages(packages: Iterable[str]) -> None:
    packages = list(dict.fromkeys(packages))
    if not packages:
        return
    _ensure_pip()
    result = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *packages,
        ],
        timeout=900,
    )
    if result.returncode != 0:
        raise EnvironmentGateError(
            "Python dependency installation failed. Check network/proxy/certificate settings and write permissions."
        )
    remaining = _missing_packages(_active_profile)
    if remaining:
        raise EnvironmentGateError("Packages remain unavailable after installation: " + ", ".join(remaining))


def _chromium_launch_error() -> str | None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.exists():
                return f"Chromium executable does not exist: {executable}"
            browser = playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            browser.close()
        return None
    except Exception as exc:  # Playwright emits platform-specific exception types.
        return str(exc)


def _linux_distribution() -> tuple[str, str]:
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return "", ""
    values: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values.get("ID", "").lower(), values.get("ID_LIKE", "").lower()


def _has_noninteractive_admin() -> bool:
    if os.name == "nt" or not sys.platform.startswith("linux"):
        return False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    sudo = shutil.which("sudo")
    if not sudo:
        return False
    result = subprocess.run(
        [sudo, "-n", "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _install_linux_system_deps() -> bool:
    distro, like = _linux_distribution()
    debian_like = distro in {"debian", "ubuntu", "linuxmint", "pop"} or "debian" in like or "ubuntu" in like
    if not debian_like or not _has_noninteractive_admin():
        return False

    command = [sys.executable, "-m", "playwright", "install-deps", "chromium"]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        command = [shutil.which("sudo") or "sudo", "-n", *command]
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    try:
        result = _run(command, timeout=900, env=env)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _ensure_chromium() -> None:
    error = _chromium_launch_error()
    if error is None:
        return

    _log(f"Chromium is not ready: {error}")
    install = _run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        timeout=900,
    )
    if install.returncode != 0:
        raise EnvironmentGateError(
            "Playwright Chromium download failed. Check network/proxy/certificate settings."
        )

    error = _chromium_launch_error()
    if error is None:
        return

    if sys.platform.startswith("linux") and _install_linux_system_deps():
        error = _chromium_launch_error()
        if error is None:
            return

    if sys.platform.startswith("linux"):
        distro, like = _linux_distribution()
        raise EnvironmentGateError(
            "Chromium was downloaded but cannot start because Linux system libraries are missing. "
            f"Detected distribution: {distro or 'unknown'} ({like or 'no ID_LIKE'}). "
            "Automatic OS-package installation is only attempted on Debian/Ubuntu-like systems "
            "when root or passwordless sudo is available. Run this interpreter with "
            "'-m playwright install-deps chromium' using suitable privileges, then retry. "
            f"Launch error: {error}"
        )
    raise EnvironmentGateError(f"Chromium remains unable to start after installation: {error}")


def _write_status(*, profile: str, ready: bool, error: str | None = None) -> None:
    try:
        _STATUS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "ready": ready,
            "profile": profile,
            "python": str(_absolute_executable(sys.executable)),
            "platform": platform.platform(),
            "error": error,
        }
        tmp = _STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _STATUS_FILE)
    except OSError:
        pass


_active_profile = "web-images"


def ensure_environment(
    profile: str,
    *,
    project_dir: str | None = None,
    auto_install: bool = True,
    create_venv: bool = True,
    reexec: bool = True,
) -> Path:
    """Make a profile ready or terminate before pipeline state can be changed."""
    global _active_profile
    if profile not in _PROFILE_MODULES:
        raise ValueError(f"Unknown environment profile: {profile}")
    _active_profile = profile

    try:
        selected = select_interpreter(project_dir, create_venv=create_venv)
        if reexec:
            _reexec_with_selected(selected)

        missing = _missing_packages(profile)
        if missing:
            if not auto_install:
                raise EnvironmentGateError("Missing Python packages: " + ", ".join(missing))
            _log("Installing missing Python packages into: " + str(_absolute_executable(sys.executable)))
            _install_python_packages(missing)

        if profile in {"web", "web-images"}:
            if auto_install:
                _ensure_chromium()
            else:
                error = _chromium_launch_error()
                if error is not None:
                    raise EnvironmentGateError(error)

        selected = _absolute_executable(sys.executable)
        _write_status(profile=profile, ready=True)
        _log(f"ENVIRONMENT_READY profile={profile} python={selected}")
        return selected
    except EnvironmentGateError as exc:
        _write_status(profile=profile, ready=False, error=str(exc))
        _log(f"ENVIRONMENT_BLOCKED profile={profile}: {exc}")
        raise
    except subprocess.TimeoutExpired as exc:
        message = f"Environment repair command timed out: {exc}"
        _write_status(profile=profile, ready=False, error=message)
        _log(f"ENVIRONMENT_BLOCKED profile={profile}: {message}")
        raise EnvironmentGateError(message) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Select, repair, and validate the Skill web/image runtime.")
    parser.add_argument(
        "--profile",
        choices=sorted(_PROFILE_MODULES),
        default="web-images",
        help="Dependency profile to validate.",
    )
    parser.add_argument("--project-dir", default=None, help="Project root used to discover or create .venv.")
    parser.add_argument("--check", action="store_true", help="Check only; do not install packages or Chromium.")
    parser.add_argument("--no-create-venv", action="store_true", help="Do not create a project .venv when none exists.")
    args = parser.parse_args()

    try:
        ensure_environment(
            args.profile,
            project_dir=args.project_dir,
            auto_install=not args.check,
            create_venv=not args.no_create_venv,
        )
    except EnvironmentGateError:
        sys.exit(2)


if __name__ == "__main__":
    main()
