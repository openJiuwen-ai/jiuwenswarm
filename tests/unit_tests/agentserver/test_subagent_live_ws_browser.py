# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""§6.H live browser WebSocket: backend-shaped events into the real store."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit_tests.agentserver.live_ws_harness import LiveWsHarness

_FRONTEND = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "channels" / "web" / "frontend"
_HARNESS_DIR = _FRONTEND / "tests" / "live_ws"


def _bundle_browser_store() -> None:
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx is required to bundle the live WS browser harness")
    commands = [
        [
            npx,
            "--yes",
            "esbuild",
            "src/stores/subagentStore.ts",
            "--bundle",
            "--platform=browser",
            "--format=esm",
            f"--outfile={_HARNESS_DIR / 'subagentStore.browser.mjs'}",
        ],
        [
            npx,
            "--yes",
            "esbuild",
            "src/features/subagent/subagentNormalizer.ts",
            "--bundle",
            "--platform=browser",
            "--format=esm",
            f"--outfile={_HARNESS_DIR / 'subagentNormalizer.browser.mjs'}",
        ],
    ]
    for command in commands:
        try:
            subprocess.run(command, cwd=_FRONTEND, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            pytest.skip(f"live WS browser harness bundle unavailable: {exc}")


def _assert_three_child_snapshot(snapshot: dict) -> None:
    assert snapshot["ids"] == ["sa-a", "sa-b", "sa-c"]
    assert snapshot["a"] == "idle"
    assert snapshot["b"] == "running"
    assert snapshot["c"] == "closed"
    assert snapshot["activitiesA"] == 1


def test_live_ws_three_child_browser() -> None:
    """Open the harness page, push the H sequence over WS, read the store snapshot."""
    _bundle_browser_store()
    harness = LiveWsHarness(_HARNESS_DIR, session_id="session-h")
    harness.start()
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            pytest.importorskip("playwright.sync_api")
            return
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(harness.http_url, wait_until="domcontentloaded")
            page.wait_for_selector("[data-ready='1']", timeout=20000)
            browser.close()
        snapshot = harness.wait_snapshot(timeout=5.0)
        _assert_three_child_snapshot(snapshot)
    finally:
        harness.close()
