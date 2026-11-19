#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""CDP 连接与浏览器状态工具。

封装 connect_over_cdp、CDP 端口可达性探测、浏览器 profile 解析等通用逻辑。
所有脚本通过 ``from lib.cdp_client import ...`` 复用。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import urlopen


def _ensure_utf8_stdout() -> None:
    """Windows/GBK 控制台下强制 stdout 使用 UTF-8，避免 ¥ 等字符打印报错。

    GPT 时间线证据：auto_open_model 输出含 ¥（价格预览）时在 GBK 控制台触发
    ``UnicodeEncodeError: 'gbk' codec can't encode character '\\xa5'``，
    导致 JSON 结果写不出（stdout 为空）并让上层误判为失败。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception as e:
        logging.warning("stdout reconfigure failed: %s", e)
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception as e:
        logging.warning("stderr reconfigure failed: %s", e)


# 模块加载即尝试切到 UTF-8，保证后续所有 print（stdout/stderr）不受 GBK 限制
_ensure_utf8_stdout()


def emit(step: str, message: str) -> None:
    """向 stderr 输出进度行，格式 ``[step] <message>``。

    Skill 编排层可通过 grep ``[step]`` 提取进度。
    """
    _ensure_utf8_stdout()
    print(f"[step] {step} {message}", file=sys.stderr, flush=True)


def emit_progress(current: int, total: int, message: str) -> None:
    """输出带百分比的进度行，便于 Skill 在终端流中向用户展示。"""
    _ensure_utf8_stdout()
    pct = int(round(current / max(total, 1) * 100))
    line = f"[step] {current}/{total} ({pct}%) {message}"
    print(line, file=sys.stderr, flush=True)


def output_json(payload: dict[str, Any]) -> None:
    """向 stdout 输出最终 JSON 结果（强制 UTF-8 写入）。"""
    _ensure_utf8_stdout()
    data = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.buffer.write((data + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:
        # 极端兜底：直接输出 ASCII JSON，保证 stdout 始终有可解析内容
        print(json.dumps(payload, ensure_ascii=True))


# ---------------------------------------------------------------------------
# 浏览器 profile 解析
# ---------------------------------------------------------------------------


def _default_profile_paths() -> list[Path]:
    """按优先级返回 profiles.json 候选路径。"""
    paths: list[Path] = []
    env = (os.getenv("BROWSER_PROFILE_STORE_PATH") or "").strip()
    if env:
        paths.append(Path(env).expanduser())
    env_state = (os.getenv("BROWSER_RUNTIME_STATE_DIR") or "").strip()
    if env_state:
        paths.append(Path(env_state) / ".browser" / "profiles.json")
    paths.append(Path.home() / ".jiuwenswarm" / ".browser" / "profiles.json")
    workspace = Path.home() / ".jiuwenswarm" / "agent" / "workspace"
    paths.append(workspace / "browser-move" / ".browser" / "profiles.json")
    # 去重保序
    seen: set[str] = set()
    result: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def load_browser_profile() -> dict[str, Any]:
    """读取 profiles.json 中当前选中的 profile。返回空 dict 表示未配置。"""
    for p in _default_profile_paths():
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        selected = (data.get("selected_profile") or "").strip()
        profiles = data.get("profiles") or []
        if not isinstance(profiles, list):
            continue
        for prof in profiles:
            if not isinstance(prof, dict):
                continue
            if selected and prof.get("name") != selected:
                continue
            return {
                "name": prof.get("name", ""),
                "cdp_url": prof.get("cdp_url", ""),
                "browser_binary": prof.get("browser_binary", ""),
                "user_data_dir": prof.get("user_data_dir", ""),
                "debug_port": int(prof.get("debug_port") or 0),
                "host": prof.get("host", "127.0.0.1") or "127.0.0.1",
                "extra_args": list(prof.get("extra_args") or []),
            }
        # 未匹配到 selected 但有 profile，取第一个
        if profiles:
            prof = profiles[0]
            if isinstance(prof, dict):
                return {
                    "name": prof.get("name", ""),
                    "cdp_url": prof.get("cdp_url", ""),
                    "browser_binary": prof.get("browser_binary", ""),
                    "user_data_dir": prof.get("user_data_dir", ""),
                    "debug_port": int(prof.get("debug_port") or 0),
                    "host": prof.get("host", "127.0.0.1") or "127.0.0.1",
                    "extra_args": list(prof.get("extra_args") or []),
                }
    return {}


def resolve_cdp_url() -> str:
    """按优先级解析 CDP URL：环境变量 > profiles.json > 127.0.0.1:9333。"""
    env = (os.getenv("PLAYWRIGHT_MCP_CDP_ENDPOINT") or "").strip()
    if env:
        return env.rstrip("/")
    prof = load_browser_profile()
    cdp = (prof.get("cdp_url") or "").strip()
    if cdp:
        return cdp.rstrip("/")
    port = int(prof.get("debug_port") or 9333)
    host = (prof.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    return f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# CDP 可达性探测
# ---------------------------------------------------------------------------


def is_cdp_ready(cdp_url: str, timeout_s: float = 1.5) -> bool:
    """探测 CDP endpoint 是否可访问。"""
    base = (cdp_url or "").strip().rstrip("/")
    if not base:
        return False
    try:
        with urlopen(f"{base}/json/version", timeout=timeout_s) as response:  # nosec B310
            payload = response.read().decode("utf-8", errors="ignore")
            data = json.loads(payload)
            if isinstance(data, dict):
                return bool(data.get("webSocketDebuggerUrl") or data.get("Browser"))
    except (URLError, TimeoutError, ValueError):
        return False
    return False


def wait_for_cdp(cdp_url: str, timeout_s: float = 15.0, poll_s: float = 0.5) -> bool:
    """轮询等待 CDP 端口就绪。返回 True 表示已就绪。"""
    deadline = time.time() + max(1.0, float(timeout_s))
    while time.time() < deadline:
        if is_cdp_ready(cdp_url, timeout_s=poll_s):
            return True
        time.sleep(poll_s)
    return False


# ---------------------------------------------------------------------------
# Playwright 连接
# ---------------------------------------------------------------------------


def import_playwright():
    """惰性导入 playwright，未安装时给出明确指引。"""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "未安装 playwright Python 包。请在 jiuwenswarm 环境中执行："
            "`python -m pip install playwright`（无需 playwright install，"
            "本脚本通过 CDP 连接已有浏览器，不下载浏览器二进制）。"
        ) from exc


def connect_page(cdp_url: str, timeout_ms: int = 15_000, new_page: bool = False):
    """通过 CDP 连接已有浏览器并返回 (pw, browser, page)。

    不创建新 context，使用浏览器默认的 context（保留登录态）。
    - ``new_page=False``（默认）：复用已有 page（适合后续自动化脚本）
    - ``new_page=True``：创建新 page（适合 navigate.py，每次跳转在新标签页打开）
    """
    sync_playwright = import_playwright()
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
    if not browser.contexts:
        raise RuntimeError(f"CDP 连接成功但未发现浏览器 context：{cdp_url}")
    context = browser.contexts[0]
    if new_page:
        page = context.new_page()
    elif context.pages:
        page = context.pages[0]
    else:
        page = context.new_page()
    page.set_default_timeout(timeout_ms)
    return pw, browser, page
