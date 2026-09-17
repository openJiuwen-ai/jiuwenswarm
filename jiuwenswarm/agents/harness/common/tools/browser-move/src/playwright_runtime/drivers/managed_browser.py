# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Managed isolated browser launcher for CDP attach.

Supports Chromium-family browsers used by Playwright MCP CDP mode:
Chrome (preferred) and Microsoft Edge. Selection policy:

1. Explicit ``BROWSER_MANAGED_BINARY`` / profile ``browser_binary`` wins
2. ``BROWSER_MANAGED_TYPE`` can pin to ``chrome`` / ``msedge`` (default ``auto``)
3. Auto-detect order: Chrome, then Edge
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.request import urlopen

from playwright_runtime.profiles import BrowserProfile

logger = logging.getLogger(__name__)

# Playwright MCP / CDP browser names.
BROWSER_KIND_CHROME = "chrome"
BROWSER_KIND_MSEDGE = "msedge"
SUPPORTED_BROWSER_KINDS = (BROWSER_KIND_CHROME, BROWSER_KIND_MSEDGE)


def detect_browser_kind(value: str) -> Optional[str]:
    """Infer Playwright browser name from an executable path or command."""
    raw = (value or "").strip()
    if not raw:
        return None
    lowered = raw.replace("\\", "/").lower()
    name = Path(lowered).name

    is_edge_path = "/microsoft/edge/" in lowered or "microsoft edge.app" in lowered
    if (
        "msedge" in name
        or name in {"microsoft edge", "microsoft-edge", "microsoft-edge-stable"}
        or is_edge_path
    ):
        return BROWSER_KIND_MSEDGE

    if "chrome" in name or "/google/chrome/" in lowered or "google chrome.app" in lowered:
        return BROWSER_KIND_CHROME

    return None


def playwright_browser_name_for_binary(binary: str) -> str:
    """Map a resolved binary to PLAYWRIGHT_MCP_BROWSER (default chrome)."""
    return detect_browser_kind(binary) or BROWSER_KIND_CHROME


def _preferred_browser_kinds() -> List[str]:
    raw = (os.getenv("BROWSER_MANAGED_TYPE") or "auto").strip().lower()
    if raw in {"chrome", "google-chrome", "google_chrome"}:
        return [BROWSER_KIND_CHROME]
    if raw in {"msedge", "edge", "microsoft-edge", "microsoft_edge"}:
        return [BROWSER_KIND_MSEDGE]
    # auto / empty / unknown → Chrome first, then Edge
    return [BROWSER_KIND_CHROME, BROWSER_KIND_MSEDGE]


def _default_user_data_dir(kind: str = BROWSER_KIND_CHROME) -> str:
    """Return the platform-standard user data directory for Chrome or Edge."""
    if kind == BROWSER_KIND_MSEDGE:
        if os.name == "nt":
            local_app_data = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return str(Path(local_app_data) / "Microsoft" / "Edge" / "User Data")
        if sys.platform == "darwin":
            return str(Path.home() / "Library" / "Application Support" / "Microsoft Edge")
        return str(Path.home() / ".config" / "microsoft-edge")

    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return str(Path(local_app_data) / "Google" / "Chrome" / "User Data")
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
    return str(Path.home() / ".config" / "google-chrome")


def _default_chrome_user_data_dir() -> str:
    """Backward-compatible alias for Chrome's default user-data directory."""
    return _default_user_data_dir(BROWSER_KIND_CHROME)


def _windows_process_names_for_kinds(kinds: Iterable[str]) -> list[str]:
    names: list[str] = []
    for kind in kinds:
        if kind == BROWSER_KIND_MSEDGE:
            names.append("msedge.exe")
        else:
            names.append("chrome.exe")
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique or ["chrome.exe", "msedge.exe"]


def _kill_browser_by_user_data_dir(
    user_data_dir: str,
    kinds: Optional[Iterable[str]] = None,
) -> int:
    """Kill browser processes whose command line references user_data_dir."""
    normalized = str(Path(user_data_dir).expanduser().resolve()).lower().replace("\\", "/")
    killed = 0
    process_kinds = list(kinds) if kinds else list(SUPPORTED_BROWSER_KINDS)

    if os.name == "nt":
        process_names = _windows_process_names_for_kinds(process_kinds)
        filter_expr = " or ".join(f"name='{name}'" for name in process_names)
        ps_script = (
            f"Get-WmiObject Win32_Process -Filter \"{filter_expr}\" "
            "| Select-Object -Property CommandLine,ProcessId "
            "| ConvertTo-Json -Depth 1"
        )
        try:
            powershell_bin = shutil.which("powershell")
            if powershell_bin:
                result = subprocess.run(
                    [powershell_bin, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0 and result.stdout.strip():
                    items = json.loads(result.stdout.strip())
                    if isinstance(items, dict):
                        items = [items]
                    taskkill_bin = shutil.which("taskkill")
                    for item in items or []:
                        cmdline = str(item.get("CommandLine") or "").lower().replace("\\", "/")
                        pid = item.get("ProcessId")
                        if not pid or normalized not in cmdline:
                            continue
                        if taskkill_bin:
                            subprocess.run(
                                [taskkill_bin, "/F", "/PID", str(pid)],
                                capture_output=True,
                                timeout=10,
                            )
                        killed += 1
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            pgrep_bin = shutil.which("pgrep")
            if pgrep_bin:
                result = subprocess.run(
                    [pgrep_bin, "-f", f"--user-data-dir={user_data_dir}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                kill_bin = shutil.which("kill")
                if result.returncode == 0 and kill_bin:
                    for line in result.stdout.strip().splitlines():
                        pid_str = line.strip()
                        if pid_str.isdigit():
                            subprocess.run([kill_bin, "-9", pid_str], capture_output=True, timeout=5)
                            killed += 1
        except Exception:  # noqa: BLE001
            pass

    return killed


def _kill_chrome_by_user_data_dir(user_data_dir: str) -> int:
    """Backward-compatible alias that targets Chrome and Edge."""
    return _kill_browser_by_user_data_dir(user_data_dir)


def _cleanup_chrome_singleton_files(user_data_dir: str) -> None:
    """Remove stale Chromium singleton lock files left after a forced kill."""
    base = Path(user_data_dir).expanduser()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        target = base / name
        try:
            if target.is_symlink() or target.exists():
                target.unlink(missing_ok=True)
        except OSError:
            pass


def _dedupe_paths(binaries: Iterable[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in binaries:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
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
    return _dedupe_paths(binaries)


def _candidate_edge_binaries() -> list[str]:
    names = [
        "msedge",
        "microsoft-edge",
        "microsoft-edge-stable",
    ]
    resolved = [shutil.which(name) for name in names]
    binaries = [item for item in resolved if item]

    if os.name == "nt":
        windows_roots = [
            os.getenv("ProgramFiles"),
            os.getenv("ProgramFiles(x86)"),
            os.getenv("ProgramW6432"),
            os.getenv("LOCALAPPDATA"),
            str(Path.home() / "AppData" / "Local"),
        ]
        install_paths = []
        for root in windows_roots:
            if not root:
                continue
            install_paths.append(str(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    elif sys.platform == "darwin":
        install_paths = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            str(Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    else:
        install_paths = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/opt/microsoft/msedge/msedge",
        ]

    for path in install_paths:
        if Path(path).exists():
            binaries.append(path)
    return _dedupe_paths(binaries)


def _candidate_binaries_for_kind(kind: str) -> list[str]:
    if kind == BROWSER_KIND_MSEDGE:
        return _candidate_edge_binaries()
    return _candidate_chrome_binaries()


def _is_chrome_identifier(value: str) -> bool:
    """Backward-compatible: True for Chrome only (not Edge)."""
    return detect_browser_kind(value) == BROWSER_KIND_CHROME


class ManagedBrowserDriver:
    """Launch and manage a dedicated local Chrome/Edge process."""

    def __init__(self, profile: BrowserProfile) -> None:
        self.profile = profile
        self._process: Optional[subprocess.Popen] = None
        self._owns_process: bool = False
        self._resolved_binary: str = ""
        self._resolved_kind: str = BROWSER_KIND_CHROME

    @property
    def cdp_endpoint(self) -> str:
        host = self.profile.host or "127.0.0.1"
        return f"http://{host}:{int(self.profile.debug_port)}"

    @property
    def owns_process(self) -> bool:
        return self._owns_process

    @property
    def resolved_binary(self) -> str:
        return self._resolved_binary

    @property
    def playwright_browser_name(self) -> str:
        return self._resolved_kind

    def is_endpoint_ready(self) -> bool:
        return self._is_endpoint_ready()

    def _resolve_binary(self) -> str:
        preferred = _preferred_browser_kinds()
        explicit = (self.profile.browser_binary or "").strip()
        if explicit:
            kind = detect_browser_kind(explicit)
            if kind is None:
                raise RuntimeError(
                    "Managed mode supports Chrome and Microsoft Edge only. "
                    "Set BROWSER_MANAGED_BINARY to a Chrome or Edge executable."
                )
            if preferred != list(SUPPORTED_BROWSER_KINDS) and kind not in preferred:
                wanted = preferred[0]
                raise RuntimeError(
                    f"Configured browser is {kind}, but BROWSER_MANAGED_TYPE requires {wanted}."
                )
            candidate = Path(explicit).expanduser()
            if candidate.is_file():
                self._resolved_kind = kind
                self._resolved_binary = str(candidate)
                return self._resolved_binary
            resolved = shutil.which(explicit)
            if resolved:
                self._resolved_kind = detect_browser_kind(resolved) or kind
                self._resolved_binary = resolved
                return self._resolved_binary
            label = "Edge" if kind == BROWSER_KIND_MSEDGE else "Chrome"
            raise RuntimeError(f"Configured {label} binary not found: {explicit}")

        for kind in preferred:
            candidates = _candidate_binaries_for_kind(kind)
            if candidates:
                self._resolved_kind = kind
                self._resolved_binary = candidates[0]
                return self._resolved_binary

        if preferred == [BROWSER_KIND_MSEDGE]:
            raise RuntimeError(
                "Microsoft Edge needs to be installed. No Edge binary was found on this machine."
            )
        if preferred == [BROWSER_KIND_CHROME]:
            raise RuntimeError(
                "Chrome needs to be installed. No Chrome binary was found on this machine."
            )
        raise RuntimeError(
            "Chrome or Microsoft Edge needs to be installed. "
            "No supported browser binary was found on this machine."
        )

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
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
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
        except (OSError, ValueError):
            return False
        return False

    def start(self, timeout_s: float = 30.0, kill_existing: bool = False) -> str:
        if self._process is not None and self._process.poll() is None:
            if self._is_endpoint_ready():
                return self.cdp_endpoint

        if self._is_endpoint_ready():
            self._process = None
            self._owns_process = False
            return self.cdp_endpoint

        if kill_existing:
            user_data_dir = str(Path(self.profile.user_data_dir).expanduser())
            _kill_browser_by_user_data_dir(user_data_dir, _preferred_browser_kinds())
            time.sleep(1.5)
            _cleanup_chrome_singleton_files(user_data_dir)

        binary = self._resolve_binary()
        try:
            logger.info(
                "ManagedBrowser: launching %s via %s",
                self._resolved_kind,
                binary,
            )
        except Exception:  # noqa: BLE001
            pass

        args = self._build_args(binary)
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
        self._owns_process = True

        deadline = time.time() + max(1.0, float(timeout_s))
        while time.time() < deadline:
            if self._process.poll() is not None:
                self._owns_process = False
                raise RuntimeError(
                    f"Managed browser process exited early with code {self._process.returncode}"
                )
            if self._is_endpoint_ready():
                return self.cdp_endpoint
            time.sleep(0.25)

        self._owns_process = False
        raise RuntimeError(f"Managed browser CDP endpoint not ready after {timeout_s:.1f}s: {self.cdp_endpoint}")

    def stop(self, wait_timeout_s: float = 5.0) -> None:
        process = self._process
        self._process = None
        owns_process = self._owns_process
        self._owns_process = False
        if process is None:
            return
        if not owns_process:
            return
        if process.poll() is not None:
            return

        try:
            process.terminate()
            process.wait(timeout=max(0.5, float(wait_timeout_s)))
        except Exception:
            try:
                process.kill()
            except Exception as e:
                logger.warning("process.kill failed: %s", e)

    def clear(self):
        # Reap a Chrome child that exited (e.g. user closed the window) but
        # whose Popen handle still sits on the driver.
        if self._process is None:
            return
        process = self._process
        self._process = None
        self._owns_process = False
        if process is None:
            return
        logger.info("ManagedBrowser: clear handle process")
        process.poll()
