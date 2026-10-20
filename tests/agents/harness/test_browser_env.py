# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Browser tip values must be visible to agent-core's os.getenv readers."""

from __future__ import annotations

import os

import pytest

from jiuwenswarm.agents.harness.common import browser_env
from jiuwenswarm.common.local_env_config import BUSINESS_MIRROR_KEYS


def _isolate_copy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let monkeypatch restore keys that materialize clears on a tip miss."""
    for key in browser_env._TIP_COPY_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_tip_managed_driver_is_written_back_and_stale_cdp_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_copy_keys(monkeypatch)
    monkeypatch.delenv("BROWSER_DRIVER", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setenv("PLAYWRIGHT_MCP_CDP_ENDPOINT", "http://127.0.0.1:9222")

    def _tip(name: str):
        if name == "BROWSER_DRIVER":
            return "managed"
        if name == "BROWSER_PROFILE_NAME":
            return "desk"
        return None

    monkeypatch.setattr(browser_env, "read_env_if_set", _tip)

    assert browser_env.materialize_browser_driver_env() == "managed"
    assert os.environ["BROWSER_DRIVER"] == "managed"
    assert os.environ["BROWSER_PROFILE_NAME"] == "desk"
    assert "PLAYWRIGHT_CDP_URL" not in os.environ
    assert "PLAYWRIGHT_MCP_CDP_ENDPOINT" not in os.environ


def test_copied_tip_keys_are_mirrored_business_keys() -> None:
    """Host and port must be Track B keys, or a miss would delete a bare value."""
    assert set(browser_env._TIP_COPY_KEYS) <= BUSINESS_MIRROR_KEYS


def test_missing_tip_value_clears_leftover_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER_PROFILE_NAME", "previous-session")
    monkeypatch.setenv("BROWSER_TIMEOUT_S", "30")
    monkeypatch.setenv("BROWSER_MANAGED_HOST", "10.0.0.8")
    monkeypatch.setenv("BROWSER_MANAGED_PORT", "9444")
    monkeypatch.setenv("BROWSER_ALLOW_SHORT_TIMEOUT_OVERRIDE", "1")
    monkeypatch.setattr(browser_env, "read_env_if_set", lambda _name: None)

    browser_env.materialize_browser_driver_env()

    for key in browser_env._TIP_COPY_KEYS:
        assert key not in os.environ


def test_missing_driver_defaults_to_managed(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_copy_keys(monkeypatch)
    monkeypatch.delenv("BROWSER_DRIVER", raising=False)
    monkeypatch.setattr(browser_env, "read_env_if_set", lambda _name: None)

    assert browser_env.materialize_browser_driver_env() == "managed"
    assert os.environ["BROWSER_DRIVER"] == "managed"


def test_explicit_remote_keeps_cdp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_copy_keys(monkeypatch)
    monkeypatch.setenv("BROWSER_DRIVER", "remote")
    monkeypatch.setenv("PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9333")
    monkeypatch.setattr(browser_env, "read_env_if_set", lambda _name: None)

    assert browser_env.materialize_browser_driver_env() == "remote"
    assert os.environ["PLAYWRIGHT_CDP_URL"] == "http://127.0.0.1:9333"


def test_tip_driver_wins_over_process_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_copy_keys(monkeypatch)
    monkeypatch.setenv("BROWSER_DRIVER", "remote")
    monkeypatch.setattr(
        browser_env,
        "read_env_if_set",
        lambda name: "managed" if name == "BROWSER_DRIVER" else None,
    )

    assert browser_env.materialize_browser_driver_env() == "managed"
    assert os.environ["BROWSER_DRIVER"] == "managed"


def test_sync_keeps_configured_chrome_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_copy_keys(monkeypatch)
    monkeypatch.delenv("BROWSER_DRIVER", raising=False)
    monkeypatch.setenv("BROWSER_MANAGED_BINARY", r"C:\stale\chrome.exe")
    monkeypatch.setenv("PLAYWRIGHT_MCP_ARGS", "-y @playwright/mcp@latest --headless")
    monkeypatch.setattr(browser_env, "read_env_if_set", lambda _name: None)
    monkeypatch.setattr(browser_env, "read_env", lambda name, default="": "")

    browser_env.sync_browser_runtime_environment(
        {"browser": {"chrome_path": r"C:\custom\chrome.exe", "headless": False}}
    )

    assert os.environ["BROWSER_MANAGED_BINARY"] == r"C:\custom\chrome.exe"
    assert "BROWSER_MANAGED_ARGS" not in os.environ
    assert "--headless" not in os.environ["PLAYWRIGHT_MCP_ARGS"].split()
    assert os.environ["BROWSER_DRIVER"] == "managed"
