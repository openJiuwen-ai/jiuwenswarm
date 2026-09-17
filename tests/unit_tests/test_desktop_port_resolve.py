# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Desktop port conflict resolution (session-local, no persistence)."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from jiuwenswarm.dotenv_early import (
    CLI_PORTS_ENV_FLAG,
    DESKTOP_PRESERVED_ENV_KEYS,
    load_dotenv_runtime,
)
from jiuwenswarm.instance_manager.config import BASE_PORTS, calculate_instance_ports


@pytest.fixture
def desktop_app(monkeypatch):
    """Import desktop_app without requiring optional pywebview in CI.

    ``desktop_app`` does ``import webview`` at module level, but unit CI installs
    the core package without the ``desktop`` extra. Stub the module first so
    port-resolution helpers remain testable.
    """
    if "webview" not in sys.modules:
        monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    # Drop a previously imported desktop_app that may have failed mid-import.
    sys.modules.pop("jiuwenswarm.channels.desktop.desktop_app", None)
    from jiuwenswarm.channels.desktop import desktop_app as mod

    return mod


def test_load_dotenv_runtime_preserves_desktop_ports(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEB_PORT=11111\nGATEWAY_PORT=22222\nAGENT_SERVER_PORT=33333\nFRONTEND_PORT=44444\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    monkeypatch.delenv(CLI_PORTS_ENV_FLAG, raising=False)
    monkeypatch.setenv("WEB_PORT", "20000")
    monkeypatch.setenv("GATEWAY_PORT", "20001")
    monkeypatch.setenv("AGENT_SERVER_PORT", "19092")
    monkeypatch.setenv("AGENT_PORT", "19092")
    monkeypatch.setenv("FRONTEND_PORT", "6173")

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["WEB_PORT"] == "20000"
    assert os.environ["GATEWAY_PORT"] == "20001"
    assert os.environ["AGENT_SERVER_PORT"] == "19092"
    assert os.environ["AGENT_PORT"] == "19092"
    assert os.environ["FRONTEND_PORT"] == "6173"


def test_load_dotenv_runtime_drops_stale_agent_server_url(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_SERVER_URL=ws://127.0.0.1:18092\nAGENT_SERVER_PORT=33333\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    monkeypatch.delenv(CLI_PORTS_ENV_FLAG, raising=False)
    monkeypatch.setenv("AGENT_SERVER_PORT", "19092")
    monkeypatch.delenv("AGENT_SERVER_URL", raising=False)

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["AGENT_SERVER_PORT"] == "19092"
    assert "AGENT_SERVER_URL" not in os.environ


def test_load_dotenv_runtime_non_desktop_keeps_agent_server_url(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_SERVER_URL=ws://127.0.0.1:18092\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    monkeypatch.delenv(CLI_PORTS_ENV_FLAG, raising=False)
    monkeypatch.delenv("AGENT_SERVER_URL", raising=False)

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["AGENT_SERVER_URL"] == "ws://127.0.0.1:18092"


def test_load_dotenv_runtime_non_desktop_allows_override(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WEB_PORT=11111\n", encoding="utf-8")

    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    monkeypatch.delenv(CLI_PORTS_ENV_FLAG, raising=False)
    monkeypatch.setenv("WEB_PORT", "20000")

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["WEB_PORT"] == "11111"


def test_load_dotenv_runtime_preserves_cli_ports(tmp_path: Path, monkeypatch):
    """Issue #2749: jiuwenswarm-start injected ports must beat stale .env.

    Scenario: a prior fallback left GATEWAY_PORT=20001 in .env, while this
    launch's banner / child env decided on index-0 defaults (19001). Without
    JIUWENSWARM_CLI_PORTS preservation, load_dotenv(override=True) would make
    Gateway bind 20001 while the start banner still prints 19001.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEB_PORT=11111\nGATEWAY_PORT=22222\nAGENT_SERVER_PORT=33333\nFRONTEND_PORT=44444\n"
        "AGENT_SERVER_URL=ws://127.0.0.1:18092\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    monkeypatch.setenv(CLI_PORTS_ENV_FLAG, "1")
    monkeypatch.setenv("WEB_PORT", "19000")
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    monkeypatch.setenv("AGENT_SERVER_PORT", "18092")
    monkeypatch.setenv("AGENT_PORT", "18092")
    monkeypatch.setenv("FRONTEND_PORT", "5173")
    monkeypatch.delenv("AGENT_SERVER_URL", raising=False)

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["WEB_PORT"] == "19000"
    assert os.environ["GATEWAY_PORT"] == "19001"
    assert os.environ["AGENT_SERVER_PORT"] == "18092"
    assert os.environ["AGENT_PORT"] == "18092"
    assert os.environ["FRONTEND_PORT"] == "5173"
    assert "AGENT_SERVER_URL" not in os.environ


def test_load_dotenv_runtime_cli_ports_drops_stale_agent_server_url(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_SERVER_URL=ws://127.0.0.1:18092\nAGENT_SERVER_PORT=33333\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    monkeypatch.setenv(CLI_PORTS_ENV_FLAG, "1")
    monkeypatch.setenv("AGENT_SERVER_PORT", "18092")
    monkeypatch.delenv("AGENT_SERVER_URL", raising=False)

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["AGENT_SERVER_PORT"] == "18092"
    assert "AGENT_SERVER_URL" not in os.environ


def test_resolve_desktop_ports_uses_default_group(desktop_app, monkeypatch):
    default = calculate_instance_ports(0)
    monkeypatch.setattr(
        desktop_app,
        "find_available_ports",
        lambda **_kwargs: (default, 0),
    )
    ports = desktop_app.resolve_desktop_ports()
    assert ports == default
    assert ports["frontend"] == BASE_PORTS["frontend"]
    assert ports["web"] == BASE_PORTS["web"]


def test_resolve_desktop_ports_falls_back_to_next_group(desktop_app, monkeypatch):
    alt = calculate_instance_ports(1)
    monkeypatch.setattr(
        desktop_app,
        "find_available_ports",
        lambda **_kwargs: (alt, 1),
    )
    ports = desktop_app.resolve_desktop_ports()
    assert ports == alt
    assert ports["frontend"] == BASE_PORTS["frontend"] + 1000
    assert ports["web"] == BASE_PORTS["web"] + 1000


def test_resolve_desktop_ports_exhausted_raises(desktop_app, monkeypatch):
    monkeypatch.setattr(desktop_app, "find_available_ports", lambda **_kwargs: None)
    with pytest.raises(RuntimeError, match="No available desktop port group"):
        desktop_app.resolve_desktop_ports()


def test_build_child_env_injects_full_port_group(desktop_app, monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    monkeypatch.setenv("AGENT_SERVER_URL", "ws://127.0.0.1:18092")
    ports = calculate_instance_ports(1)
    env = desktop_app._build_child_env("app", ports)

    assert env[desktop_app.DESKTOP_ENV_FLAG] == "1"
    assert env["WEB_PORT"] == str(ports["web"])
    assert env["GATEWAY_PORT"] == str(ports["gateway"])
    assert env["AGENT_SERVER_PORT"] == str(ports["agent_server"])
    assert env["AGENT_PORT"] == str(ports["agent_server"])
    assert env["FRONTEND_PORT"] == str(ports["frontend"])
    assert env["WEB_HOST"] == desktop_app.BACKEND_HOST
    assert "AGENT_SERVER_URL" not in env
    # Keys we rely on for dotenv preservation stay in sync with the helper.
    for key in DESKTOP_PRESERVED_ENV_KEYS:
        assert key in env


def test_cleanup_stale_update_artifacts_is_product_name_agnostic(
    desktop_app,
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    updates_dir = workspace / ".updates"
    updates_dir.mkdir(parents=True)
    stale_names = (
        "TomorrowDesk-preview.exe",
        "AnotherProduct-nightly.dmg",
        "future-name.dmg.part",
    )
    for name in stale_names:
        (updates_dir / name).write_bytes(b"stale")
    keep = updates_dir / "release-notes.txt"
    keep.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(desktop_app, "get_user_workspace_dir", lambda: workspace)

    desktop_app._cleanup_stale_update_artifacts()

    assert all(not (updates_dir / name).exists() for name in stale_names)
    assert keep.exists()
