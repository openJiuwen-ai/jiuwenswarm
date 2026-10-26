#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Managed isolated browser launcher for CDP attach."""

from __future__ import annotations

import json
import logging
import os
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
    return "chrome" in Path(lowered).name


class ManagedBrowserDriver:
    """Launch and manage a dedicated local Chrome process."""

    def __init__(self, profile: BrowserProfile) -> None:
        self.profile = profile
        self._process: Optional[subprocess.Popen] = None

    @property
    def cdp_endpoint(self) -> str:
        host = self.profile.host or "127.0.0.1"
        return f"http://{host}:{int(self.profile.debug_port)}"

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
