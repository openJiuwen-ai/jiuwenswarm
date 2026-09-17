# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent.reload_config must re-read instance .env before interpolating models."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

_OJ_MEMORY_MANAGER_MODULE = "openjiuwen.core.memory.lite.manager"


@contextlib.contextmanager
def _maybe_patch_aclose_memory_cache():
    import importlib

    mod = importlib.import_module(_OJ_MEMORY_MANAGER_MODULE)
    if hasattr(mod, "aclose_memory_manager_cache"):
        with patch(
            f"{_OJ_MEMORY_MANAGER_MODULE}.aclose_memory_manager_cache",
            AsyncMock(),
        ):
            yield
    else:
        yield


def _config_from_env() -> dict:
    model_name = os.getenv("MODEL_NAME")
    return {
        "react": {"model_name": model_name},
        "models": {"defaults": [{"model_client_config": {"model_name": model_name}}]},
    }


async def _apply_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    env_file: Path,
    env_overrides: dict[str, str] | None,
) -> dict:
    monkeypatch.setattr(interface_module, "get_env_file", lambda: env_file)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._refresh_multimodal_configs = MagicMock()
    with (
        patch.object(interface_module, "clear_config_cache", MagicMock()),
        _maybe_patch_aclose_memory_cache(),
        patch.object(interface_module, "get_config", _config_from_env),
    ):
        return await adapter._apply_reload_config_snapshot(None, env_overrides)


@pytest.mark.asyncio
async def test_reload_snapshot_rereads_dotenv_before_get_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing MODEL_NAME in .env must take effect on reload without a restart."""
    env_file = tmp_path / ".env"
    env_file.write_text('MODEL_NAME="gpt-5-nano"\n', encoding="utf-8")
    monkeypatch.setenv("MODEL_NAME", "deepseek/deepseek-v4-flash")

    result = await _apply_snapshot(monkeypatch, env_file, None)

    assert os.getenv("MODEL_NAME") == "gpt-5-nano"
    assert result["react"]["model_name"] == "gpt-5-nano"


@pytest.mark.asyncio
async def test_reload_snapshot_env_overrides_win_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Request env_overrides must still beat the reloaded .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text('MODEL_NAME="gpt-5-nano"\n', encoding="utf-8")
    monkeypatch.setenv("MODEL_NAME", "deepseek/deepseek-v4-flash")

    result = await _apply_snapshot(monkeypatch, env_file, {"MODEL_NAME": "claude-sonnet"})

    assert os.getenv("MODEL_NAME") == "claude-sonnet"
    assert result["react"]["model_name"] == "claude-sonnet"
