# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Atomic .env persistence for the TUI config.set path."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import config_persistence
from jiuwenswarm.common.config_panel import tui_models_handlers as handlers


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("API_KEY=secret\nMODEL_NAME=qwen\n", encoding="utf-8")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_env_file", lambda: path)
    return path


@pytest.fixture(autouse=True)
def restore_jina_api_key():
    """Keep os.environ clean: delenv(raising=False) does not undo a key added later."""
    original = os.environ.pop("JINA_API_KEY", None)
    yield
    os.environ.pop("JINA_API_KEY", None)
    if original is not None:
        os.environ["JINA_API_KEY"] = original


def test_persist_env_updates_writes_atomically(env_file):
    handlers.persist_env_updates({"JINA_API_KEY": "new"})

    content = env_file.read_text(encoding="utf-8")
    assert "API_KEY=secret\n" in content
    assert "MODEL_NAME=qwen\n" in content
    assert "JINA_API_KEY='new'" in content
    assert list(env_file.parent.glob(".env.*.pending")) == []


def test_persist_env_updates_propagates_write_failure(env_file, monkeypatch):
    original = env_file.read_text(encoding="utf-8")

    def fail_write(path, content):
        raise OSError("simulated disk full")

    monkeypatch.setattr(config_persistence, "atomic_write", fail_write)
    with pytest.raises(OSError, match="simulated disk full"):
        handlers.persist_env_updates({"JINA_API_KEY": "new"})

    assert env_file.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_config_set_reports_env_persist_failure(monkeypatch):
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)

    def fail_persist(updates):
        raise OSError("simulated disk full")

    monkeypatch.setattr(handlers, "persist_env_updates", fail_persist)
    channel = AsyncMock()

    await handlers.config_set_handler(channel, None, "req", {"jina_api_key": "new"}, None)

    kwargs = channel.send_response.call_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["code"] == "INTERNAL_ERROR"
    assert "JINA_API_KEY" not in os.environ


@pytest.mark.asyncio
async def test_config_set_updates_environ_after_success(monkeypatch):
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setattr(handlers, "persist_env_updates", lambda updates: None)
    channel = AsyncMock()

    await handlers.config_set_handler(channel, None, "req", {"jina_api_key": "new"}, None)

    kwargs = channel.send_response.call_args.kwargs
    assert kwargs["ok"] is True
    # autouse fixture 在用例结束后清理该变量
    assert os.environ["JINA_API_KEY"] == "new"


@pytest.mark.asyncio
async def test_env_failure_still_reloads_when_yaml_changed(monkeypatch):
    monkeypatch.setattr(handlers, "_get_crypto_provider", lambda: None)
    monkeypatch.setitem(
        handlers.CLI_CONFIG_YAML_SETTERS, "enable_swarmflow", lambda value: None
    )

    def fail_persist(updates):
        raise OSError("simulated disk full")

    monkeypatch.setattr(handlers, "persist_env_updates", fail_persist)
    reload_mock = AsyncMock()
    monkeypatch.setattr(handlers, "clear_agent_config_cache", reload_mock)
    channel = AsyncMock()

    await handlers.config_set_handler(
        channel,
        None,
        "req",
        {"enable_swarmflow": "true", "jina_api_key": "new"},
        None,
    )
    await asyncio.sleep(0.01)

    kwargs = channel.send_response.call_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["code"] == "INTERNAL_ERROR"
    reload_mock.assert_awaited_once()
