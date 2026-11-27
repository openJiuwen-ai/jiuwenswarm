# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Start Chrome/Chromium with remote debugging enabled (cross-platform).

Reads browser settings from `config/config.yaml`:

browser:
  chrome_path: "<path or command>"
  remote_debugging_address: "127.0.0.1"
  remote_debugging_port: 9222
  user_data_dir: ""
  profile_directory: "Default"

`chrome_path` can also be a map by OS:

browser:
  chrome_path:
    windows: "C:\\path\\to\\chrome.exe"
    macos: "/Applications/Google Chrome.app"
    linux: "/usr/bin/google-chrome"
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml
from openjiuwen.harness.tools.browser_move.playwright_runtime.profiles import (
    BrowserProfile,
    BrowserProfileStore,
)


logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _config_path(custom_path: str = "") -> Path:
    if custom_path:
        return Path(custom_path).expanduser().resolve()
    return _repo_root() / "config" / "config.yaml"



def _browser_runtime_state_root() -> Path:
    configured = (os.getenv("BROWSER_RUNTIME_STATE_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".jiuwenclaw"


def _profile_store_path() -> Path:
    configured = (os.getenv("BROWSER_PROFILE_STORE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return _browser_runtime_state_root() / ".browser" / "profiles.json"


def _profile_name(profile_directory: str) -> str:
    configured = (os.getenv("BROWSER_PROFILE_NAME") or "").strip()
    if configured:
        return configured
    fallback = (profile_directory or "").strip()
    return fallback or "jiuwenclaw"



def _persist_browser_profile(
    *,
    host: str,
    port: int,
    chrome_exec: str,
    user_data_dir: str,
    profile_directory: str,
) -> None:
    store_path = _profile_store_path()
    store = BrowserProfileStore(store_path)
    profile = BrowserProfile(
        name=_profile_name(profile_directory),
        driver_type="remote",
        cdp_url=f"http://{host}:{port}",
        browser_binary=chrome_exec,
        user_data_dir=user_data_dir,
        debug_port=port,
        host=host,
        extra_args=[f"--profile-directory={profile_directory}"] if profile_directory else [],
    )
    store.upsert_profile(profile, select=True)
    logger.info(
        "Persisted browser profile for manual browser start: "
        f"profile={profile.name}, cdp_url={profile.cdp_url}, store_path={store_path}"
    )


def _load_browser_config(config_file: str = "") -> dict[str, Any]:
    cfg_file = _config_path(config_file)
    if not cfg_file.exists():
        return {}
    with cfg_file.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    browser_cfg = data.get("browser")
    return browser_cfg if isinstance(browser_cfg, dict) else {}


def _os_key() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"


def _resolve_chrome_path(raw_value: Any, os_name: str) -> str:
    if isinstance(raw_value, dict):
        for key in (os_name, "default"):
            value = raw_value.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(raw_value, str):
        return raw_value.strip()
    return ""


def _default_chrome_candidates(os_name: str) -> list[str]:
    if os_name == "windows":
        local_app_data = os.getenv("LOCALAPPDATA", "")
        program_files = os.getenv("PROGRAMFILES", "C:\\Program Files")
        program_files_x86 = os.getenv("PROGRAMFILES(X86)", "C:\\Program Files (x86)")
        return [
            str(Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            str(Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe"),
            "chrome.exe",
        ]
    if os_name == "macos":
        return [
            "/Applications/Google Chrome.app",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "Google Chrome",
            "google-chrome",
            "chromium",
        ]
    return [
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
    ]


def _normalize_chrome_executable(path_or_cmd: str, os_name: str) -> str:
    if not path_or_cmd:
        return ""

    expanded = os.path.expandvars(os.path.expanduser(path_or_cmd))
    path = Path(expanded)

    if os_name == "macos" and expanded.endswith(".app"):
        candidate = path / "Contents" / "MacOS" / "Google Chrome"
        if candidate.exists():
            return str(candidate)

    if path.exists():
        return str(path)

    resolved = shutil.which(expanded)
    if resolved:
        return resolved

    return ""


def _resolve_user_data_dir(raw_value: Any, os_name: str) -> str:
    if isinstance(raw_value, str) and raw_value.strip():
        return os.path.expandvars(os.path.expanduser(raw_value.strip()))

    if os_name == "windows":
        local_app_data = os.getenv("LOCALAPPDATA", "")
        return str(Path(local_app_data) / "ChromeCDPProfile")

    return str(Path.home() / "chrome-cdp-profile")


def _parse_cdp_from_env(default_host: str, default_port: int) -> tuple[str, int]:
    raw = (os.getenv("PLAYWRIGHT_CDP_URL") or "").strip()
    if not raw:
        return default_host, default_port

    # format: http://host:port
    try:
        no_scheme = raw.split("://", 1)[-1]
        host_port = no_scheme.split("/", 1)[0]
        host, port_text = host_port.rsplit(":", 1)
        return host, int(port_text)
    except Exception:
        return default_host, default_port


def _creation_flags_for_windows() -> int:
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def start_browser(*, dry_run: bool = False, config_file: str = "") -> int:
    browser_cfg = _load_browser_config(config_file)
    os_name = _os_key()
    logger.info(
        "Starting browser service from browser_start_client: "
        f"config_file={_config_path(config_file)}, os={os_name}"
    )

    chrome_cfg = _resolve_chrome_path(browser_cfg.get("chrome_path"), os_name)
    if not chrome_cfg:
        chrome_cfg = os.getenv("CHROME_PATH", "").strip()

    if not chrome_cfg:
        raise FileNotFoundError(
            "Chrome path is required. Please set browser.chrome_path in config/config.yaml "
            "or CHROME_PATH env."
        )
    chrome_exec = _normalize_chrome_executable(chrome_cfg, os_name)
    logger.info(
        "Resolved Chrome executable for browser service: "
        f"configured={chrome_cfg}, resolved={chrome_exec or '(not found)'}"
    )
    if not chrome_exec:
        raise FileNotFoundError(
            f"Chrome executable not found for configured path: {chrome_cfg}"
        )

    host = str(browser_cfg.get("remote_debugging_address") or "127.0.0.1").strip()
    port = int(browser_cfg.get("remote_debugging_port") or 9222)
    host, port = _parse_cdp_from_env(host, port)

    user_data_dir = _resolve_user_data_dir(browser_cfg.get("user_data_dir"), os_name)
    profile_directory = str(browser_cfg.get("profile_directory") or "Default").strip()

    logger.info(
        "Resolved browser launch parameters: "
        f"host={host}, port={port}, user_data_dir={user_data_dir}, "
        f"profile_directory={profile_directory or '(empty)'}"
    )

    args = [
        chrome_exec,
        f"--remote-debugging-address={host}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
    ]
    if profile_directory:
        args.append(f"--profile-directory={profile_directory}")

    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os_name == "windows":
        kwargs["creationflags"] = _creation_flags_for_windows()
    else:
        kwargs["start_new_session"] = True

    if dry_run:
        print("Dry run: browser launch command prepared.")
        print(" ".join(args))
        return 0

    logger.info(
        "Launching browser process with remote debugging enabled: "
        f"command={args}"
    )
    proc = subprocess.Popen(args, **kwargs)
    logger.info(f"Browser process launched successfully: pid={proc.pid}")
    _persist_browser_profile(
        host=host,
        port=port,
        chrome_exec=chrome_exec,
        user_data_dir=user_data_dir,
        profile_directory=profile_directory,
    )
    print(f"Chrome started (pid={proc.pid}) at {host}:{port}")
    print(f"Executable: {chrome_exec}")
    print(f"Profile dir: {user_data_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Chrome with CDP enabled.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved launch command without starting Chrome.",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Optional path to config yaml (default: config/config.yaml).",
    )
    args = parser.parse_args()
    try:
        return start_browser(dry_run=args.dry_run, config_file=args.config)
    except Exception as exc:
        print(f"Failed to start Chrome: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
