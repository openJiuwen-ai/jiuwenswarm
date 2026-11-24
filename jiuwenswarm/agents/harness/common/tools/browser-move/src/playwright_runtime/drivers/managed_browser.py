#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Managed isolated browser launcher for CDP attach."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

from playwright_runtime.profiles import BrowserProfile

logger = logging.getLogger(__name__)


def _default_chrome_user_data_dir() -> str:
    """Return the platform-standard Chrome user data directory path.

    Does not guarantee the directory exists — only that this is where Chrome
    stores its default profile on the current OS.
    """
    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(local_app_data) / "Google" / "Chrome" / "User Data")
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
    return str(Path.home() / ".config" / "google-chrome")


def _kill_chrome_by_user_data_dir(user_data_dir: str) -> int:
    """Kill Chrome processes whose command line references user_data_dir.

    Returns the number of PIDs sent a kill signal. Failures are silently
    swallowed so a misconfigured environment never blocks startup.
    """
    normalized = str(Path(user_data_dir).expanduser().resolve()).lower().replace("\\", "/")
    killed = 0

    if os.name == "nt":
        ps_script = (
            "Get-WmiObject Win32_Process -Filter \"name='chrome.exe'\" "
            "| Select-Object -Property CommandLine,ProcessId "
            "| ConvertTo-Json -Depth 1"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                items = json.loads(result.stdout.strip())
                if isinstance(items, dict):
                    items = [items]
                for item in items or []:
                    cmdline = str(item.get("CommandLine") or "").lower().replace("\\", "/")
                    pid = item.get("ProcessId")
                    if not pid or normalized not in cmdline:
                        continue
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        timeout=10,
                    )
                    killed += 1
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"--user-data-dir={user_data_dir}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    pid_str = line.strip()
                    if pid_str.isdigit():
                        subprocess.run(["kill", "-9", pid_str], capture_output=True, timeout=5)
                        killed += 1
        except Exception:  # noqa: BLE001
            pass

    return killed


def _cleanup_chrome_singleton_files(user_data_dir: str) -> None:
    """Remove stale Chrome singleton lock files left after a forced kill."""
    base = Path(user_data_dir).expanduser()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        target = base / name
        try:
            if target.is_symlink() or target.exists():
                target.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "Failed to remove stale Chrome singleton file %s: %s", target, e
            )


def _playwright_browsers_roots() -> list[Path]:
    """Return candidate root directories containing bundled Playwright browsers."""
    roots: list[Path] = []
    env_root = (os.getenv("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())
    if sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    elif os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        roots.append(Path(local_app_data) / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _version_sort_key(path: Path) -> int:
    """Extract the numeric build id from parent dirs like chromium-1200 / chromium_headless_shell-987.

    ``path`` is the executable file (e.g. .../chromium-1200/chrome-mac/Chromium.app/
    Contents/MacOS/Chromium), so scan path parts from the deepest upward and take
    the first directory name carrying a numeric token.
    """
    for part in reversed(path.parts):
        for token in part.replace("_", "-").split("-"):
            if token.isdigit():
                return int(token)
    return 0


def _arch_match_sort_key(path: Path) -> int:
    """Return 1 when the browser build matches the current CPU architecture."""
    posix = path.as_posix().lower()
    machine = (platform.machine() or "").lower()
    if "arm" in machine or "aarch64" in machine:
        return 1 if "arm64" in posix else 0
    # x86_64 builds ship as chrome-mac-x64 / chrome-win64 / chrome-linux64;
    # legacy untagged dirs (chrome-mac, chrome-win, chrome-linux) also run.
    x86_64_markers = ("x64", "x86_64", "win64", "linux64")
    if any(marker in posix for marker in x86_64_markers):
        return 1
    return 1 if "arm64" not in posix else 0


def _bundled_chromium_candidates() -> list[str]:
    """Resolve bundled Playwright Chromium / chrome-headless-shell binaries.

    Prefers the full Chromium binary (headed capable), then chrome-headless-shell.
    Using a bundled browser instead of the system Google Chrome avoids macOS
    LaunchServices routing external links into the running headless instance.
    """
    patterns: list[str] = []
    if sys.platform == "darwin":
        patterns = [
            # Newer Playwright layouts (chrome-mac-x64 / chrome-mac-arm64).
            "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
            # Older layout.
            "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
            "chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
        ]
    elif os.name == "nt":
        patterns = [
            "chromium-*/chrome-win/chrome.exe",
            "chromium-*/chrome-win64/chrome.exe",
            "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
        ]
    else:
        patterns = [
            "chromium-*/chrome-linux/chrome",
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        ]

    found: list[Path] = []
    for root in _playwright_browsers_roots():
        if not root.exists():
            continue
        for pattern in patterns:
            found.extend(root.glob(pattern))

    # Arch-matching newest build first; headless-shell kept after the full build.
    full = sorted(
        (p for p in found if "headless_shell" not in p.as_posix().lower()),
        key=lambda p: (_arch_match_sort_key(p), _version_sort_key(p)),
        reverse=True,
    )
    shell = sorted(
        (p for p in found if "headless_shell" in p.as_posix().lower()),
        key=lambda p: (_arch_match_sort_key(p), _version_sort_key(p)),
        reverse=True,
    )
    ordered = full + shell

    unique: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        text = str(item)
        if text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _candidate_chrome_binaries() -> list[str]:
    names = [
        "chrome",
        "google-chrome",
        "google-chrome-stable",
    ]
    resolved = [shutil.which(name) for name in names]
    binaries = [item for item in resolved if item]

    if os.name == "nt":
        windows_roots = [
            os.getenv("LOCALAPPDATA"),
            os.getenv("ProgramFiles"),
            os.getenv("ProgramFiles(x86)"),
            os.getenv("ProgramW6432"),
            str(Path.home() / "AppData" / "Local"),
        ]
        install_paths = []
        for root in windows_roots:
            if not root:
                continue
            install_paths.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    elif sys.platform == "darwin":
        # Prefer bundled Playwright Chromium over the system Google Chrome:
        # a headless system-Chrome instance can hijack user external links via
        # macOS LaunchServices bundle routing.
        bundled = _bundled_chromium_candidates()
        if bundled:
            binaries = bundled + binaries
            install_paths = []
        else:
            logger.warning(
                "No bundled Playwright Chromium found; falling back to system "
                "Google Chrome for managed browsing (external-link hijack risk on macOS)."
            )
            install_paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
    else:
        install_paths = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/chrome",
        ]

    for path in install_paths:
        if Path(path).exists():
            binaries.append(path)

    # Preserve order and remove duplicates.
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in binaries:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _is_chrome_identifier(value: str) -> bool:
    lowered = (value or "").strip().replace("\\", "/").lower()
    if not lowered:
        return False
    name = Path(lowered).name
    # Accept Chromium-family executables: "chrome", "Google Chrome for Testing",
    # "chromium" (note: "chromium" does not contain the substring "chrome"),
    # and "chrome-headless-shell".
    return "chrome" in name or "chromium" in name


class ManagedBrowserDriver:
    """Launch and manage a dedicated local Chrome process."""

    def __init__(self, profile: BrowserProfile) -> None:
        self.profile = profile
        self._process: Optional[subprocess.Popen] = None

    @property
    def cdp_endpoint(self) -> str:
        host = self.profile.host or "127.0.0.1"
        return f"http://{host}:{int(self.profile.debug_port)}"

    @property
    def pid(self) -> Optional[int]:
        """PID of the running browser process, or None when not started."""
        if self._process is None:
            return None
        return self._process.pid

    def is_running(self) -> bool:
        """Whether the managed browser process is still alive."""
        return self._process is not None and self._process.poll() is None

    def _resolve_binary(self) -> str:
        explicit = (self.profile.browser_binary or "").strip()
        if explicit:
            if not _is_chrome_identifier(explicit):
                raise RuntimeError("Managed mode supports Chrome only. "
                                   "Set BROWSER_MANAGED_BINARY to a Chrome executable.")
            candidate = Path(explicit).expanduser()
            if candidate.exists():
                return str(candidate)
            resolved = shutil.which(explicit)
            if resolved:
                return resolved
            raise RuntimeError(
                f"Chrome needs to be installed. Configured Chrome binary not found: {explicit}"
            )

        candidates = _candidate_chrome_binaries()
        if not candidates:
            raise RuntimeError(
                "Chrome needs to be installed. No Chrome binary was found on this machine."
            )
        return candidates[0]

    def _build_args(self, binary: str) -> list[str]:
        user_data_dir = Path(self.profile.user_data_dir).expanduser()
        user_data_dir.mkdir(parents=True, exist_ok=True)

        host = (self.profile.host or "127.0.0.1").strip() or "127.0.0.1"
        port = int(self.profile.debug_port)
        if port <= 0:
            raise RuntimeError(f"Invalid debug port for managed browser profile: {port}")

        args = [
            binary,
            f"--remote-debugging-address={host}",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        args.extend(self.profile.extra_args)
        return args

    def _is_endpoint_ready(self) -> bool:
        endpoint = f"{self.cdp_endpoint}/json/version"
        try:
            with urlopen(endpoint, timeout=1.5) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
                if isinstance(payload, dict):
                    return bool(payload.get("webSocketDebuggerUrl") or payload.get("Browser"))
        except (URLError, TimeoutError, OSError, ValueError):
            return False
        return False

    def start(self, timeout_s: float = 20.0, kill_existing: bool = False) -> str:
        if self._process is not None and self._process.poll() is None:
            if self._is_endpoint_ready():
                return self.cdp_endpoint

        if kill_existing:
            user_data_dir = str(Path(self.profile.user_data_dir).expanduser())
            _kill_chrome_by_user_data_dir(user_data_dir)
            time.sleep(1.5)
            _cleanup_chrome_singleton_files(user_data_dir)

        binary = self._resolve_binary()
        args = self._build_args(binary)
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )

        deadline = time.time() + max(1.0, float(timeout_s))
        while time.time() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(
                    f"Managed browser process exited early with code {self._process.returncode}"
                )
            if self._is_endpoint_ready():
                return self.cdp_endpoint
            time.sleep(0.25)

        raise RuntimeError(f"Managed browser CDP endpoint not ready after {timeout_s:.1f}s: {self.cdp_endpoint}")

    def stop(self, wait_timeout_s: float = 5.0) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is not None:
            return

        try:
            process.terminate()
            process.wait(timeout=max(0.5, float(wait_timeout_s)))
        except Exception as terminate_err:
            logger.warning(
                "Managed browser terminate failed (pid=%s), escalating to kill: %s",
                getattr(process, "pid", None), terminate_err,
            )
            try:
                process.kill()
            except Exception as kill_err:
                logger.error(
                    "Managed browser kill failed (pid=%s); process may be leaked: %s",
                    getattr(process, "pid", None), kill_err,
                )
