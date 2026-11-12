#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""确保 Chromium 系浏览器在 CDP 端口可用。

读取 ``profiles.json`` 中当前选中的 profile：
- 若 CDP 端口已就绪，直接返回；
- 若未就绪，按 profile 中的 binary / user-data-dir / debug-port 启动浏览器。

**不依赖** Playwright Python 包（启动逻辑走原生 subprocess，避免又一轮安装）。
启动成功后返回 CDP URL，下游脚本可直接用 ``playwright sync_api`` connect_over_cdp。

用法::

    python ensure_browser.py --json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# 确保可 import lib
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.append(str(_SCRIPT_DIR))

from lib.cdp_client import (  # noqa: E402
    emit,
    emit_progress,
    is_cdp_ready,
    load_browser_profile,
    output_json,
    resolve_cdp_url,
    wait_for_cdp,
)
from lib.flow_state import make_failure, make_success  # noqa: E402


# 多浏览器候选顺序：Windows Edge 优先（系统自带）
_CHROMIUM_CANDIDATES_WIN = [
    # Edge
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    str(Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
    # Chrome
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
    # Chromium
    str(Path.home() / "AppData" / "Local" / "Chromium" / "Application" / "chromium.exe"),
]

_CHROMIUM_CANDIDATES_MAC = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    str(Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"),
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

_CHROMIUM_CANDIDATES_LINUX = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/opt/google/chrome/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def _candidate_binaries() -> list[str]:
    """按 OS 优先级返回可执行浏览器路径候选。"""
    if os.name == "nt":
        candidates = list(_CHROMIUM_CANDIDATES_WIN)
    elif sys.platform == "darwin":
        candidates = list(_CHROMIUM_CANDIDATES_MAC)
    else:
        candidates = list(_CHROMIUM_CANDIDATES_LINUX)
    for name in ("msedge", "google-chrome", "chrome", "chromium"):
        resolved = shutil.which(name)
        if resolved and resolved not in candidates:
            candidates.append(resolved)
    return candidates


def _detect_browser_family(binary_path: str) -> str:
    """根据二进制路径返回浏览器族系。"""
    name = Path(binary_path).name.lower()
    if "msedge" in name or "edge" in name:
        return "edge"
    if "chromium" in name:
        return "chromium"
    return "chrome"


def _kill_browser_by_user_data_dir(user_data_dir: str) -> int:
    """强制结束占用 user_data_dir 的浏览器进程（仅 Windows + Linux）。"""
    if not user_data_dir:
        return 0
    normalized = str(Path(user_data_dir).expanduser().resolve()).lower().replace("\\", "/")
    killed = 0
    if os.name == "nt":
        powershell = shutil.which("powershell") or "powershell"
        taskkill = shutil.which("taskkill") or "taskkill"
        for proc_name in ("chrome.exe", "msedge.exe"):
            ps_script = (
                f"Get-WmiObject Win32_Process -Filter \"name='{proc_name}'\" "
                "| Select-Object -Property CommandLine,ProcessId "
                "| ConvertTo-Json -Depth 1"
            )
            try:
                result = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script],
                    capture_output=True, text=True, timeout=15,
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
                            [taskkill, "/F", "/PID", str(pid)],
                            capture_output=True, timeout=10,
                        )
                        killed += 1
            except Exception as e:
                logging.warning(f"结束 Windows 浏览器进程失败: {e}")
    else:
        pgrep = shutil.which("pgrep") or "pgrep"
        kill = shutil.which("kill") or "kill"
        for proc_name in ("chrome", "chromium", "msedge"):
            try:
                result = subprocess.run(
                    [pgrep, "-f", f"{proc_name}.*--user-data-dir={user_data_dir}"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        if line.strip().isdigit():
                            subprocess.run([kill, "-9", line.strip()], capture_output=True, timeout=5)
                            killed += 1
            except Exception as e:
                logging.warning(f"结束 Linux 浏览器进程失败: {e}")
    return killed


def _cleanup_browser_singleton_files(user_data_dir: str) -> None:
    """清理浏览器残留的 singleton 锁文件。"""
    base = Path(user_data_dir).expanduser()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        target = base / name
        try:
            if target.is_symlink() or target.exists():
                target.unlink(missing_ok=True)
        except OSError:
            pass


def _dedicated_user_data_dir() -> str:
    """返回 jiuwenswarm 专用浏览器 user-data-dir（与用户日常浏览器隔离）。"""
    return str(Path.home() / ".jiuwenswarm" / ".browser" / "user-data")


def _resolve_start_args(
    profile: dict[str, Any],
) -> Optional[tuple[str, list[str], str, str]]:
    """根据 profile 决定启动哪个浏览器及参数。

    优先级：profile.browser_binary 显式配置 > 自动检测。
    user-data-dir 优先级：profile.user_data_dir > jiuwenswarm 专用目录。

    使用专用 user-data-dir 而非浏览器默认目录，确保：
    - 浏览器以独立窗口启动（而非在已有实例中新增标签页）
    - CDP 远程调试端口能正常绑定
    - 不影响用户日常浏览器会话

    返回 (binary_path, args, user_data_dir, family) 或 None（无法启动）。
    """
    binary = (profile.get("browser_binary") or "").strip()
    if binary and Path(binary).exists():
        family = _detect_browser_family(binary)
    else:
        for cand in _candidate_binaries():
            if Path(cand).exists():
                binary = cand
                family = _detect_browser_family(cand)
                break
        else:
            return None

    # 优先使用 profile 中配置的 user_data_dir；未配置时使用专用目录，
    # 避免与用户日常浏览器共享 user-data-dir 导致浏览器合并实例（开标签页而非新窗口）
    user_data_dir = (profile.get("user_data_dir") or "").strip()
    if not user_data_dir:
        user_data_dir = _dedicated_user_data_dir()
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    host = (profile.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(profile.get("debug_port") or 9333)
    if port <= 0:
        port = 9333

    args = [
        binary,
        f"--remote-debugging-address={host}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "about:blank",
    ]
    extra = profile.get("extra_args") or []
    if isinstance(extra, list):
        args.extend(str(x) for x in extra if str(x).strip())

    return binary, args, user_data_dir, family


def _spawn_browser(args: list[str]) -> subprocess.Popen:
    """启动浏览器子进程。"""
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
    )


def ensure_browser(timeout_s: float = 20.0) -> dict[str, Any]:
    """确保浏览器已启动并暴露 CDP endpoint。返回结果 dict。"""
    emit_progress(0, 4, "正在解析浏览器配置...")
    profile = load_browser_profile()
    cdp_url = resolve_cdp_url()

    emit_progress(1, 4, f"检查 CDP 端点 {cdp_url} ...")
    if is_cdp_ready(cdp_url, timeout_s=1.5):
        emit_progress(2, 4, "CDP 已就绪，无需启动")
        return make_success(
            "browser_ready",
            cdp_url=cdp_url,
            browser_family=(
                _detect_browser_family(profile.get("browser_binary", ""))
                if profile else "unknown"
            ),
            started_now=False,
        )

    emit_progress(2, 4, "CDP 未就绪，尝试启动浏览器...")
    resolved = _resolve_start_args(profile)
    if resolved is None:
        return make_failure(
            "no_browser",
            "未找到可用的浏览器二进制。请安装 Edge/Chrome/Chromium 后重试。",
            cdp_url=cdp_url,
        )

    binary, args, user_data_dir, family = resolved

    # kill 占用同 user_data_dir 的残留进程（使用 _resolve_start_args 解析出的实际路径）
    if user_data_dir:
        _kill_browser_by_user_data_dir(user_data_dir)
        time.sleep(1.0)
        _cleanup_browser_singleton_files(user_data_dir)

    emit_progress(3, 4, f"启动 {family}: {Path(binary).name}")
    try:
        proc = _spawn_browser(args)
    except Exception as exc:
        return make_failure(
            "spawn_failed",
            f"无法启动浏览器进程: {exc}",
            cdp_url=cdp_url,
            browser_family=family,
        )

    emit_progress(4, 4, "等待 CDP 端口就绪...")
    if wait_for_cdp(cdp_url, timeout_s=timeout_s, poll_s=0.5):
        return make_success(
            "browser_started",
            cdp_url=cdp_url,
            browser_family=family,
            started_now=True,
            pid=proc.pid,
        )

    try:
        proc.terminate()
    except Exception as e:
        logging.warning(f"终止浏览器进程失败: {e}")
    return make_failure(
        "cdp_timeout",
        f"CDP 端口在 {timeout_s:.1f}s 内未就绪: {cdp_url}",
        cdp_url=cdp_url,
        browser_family=family,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确保浏览器已启动并暴露 CDP")
    parser.add_argument("--timeout", type=float, default=20.0, help="CDP 等待超时秒数")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出（默认）")
    args = parser.parse_args(argv)

    result = ensure_browser(timeout_s=args.timeout)
    output_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
