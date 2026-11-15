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


@pytest.mark.asyncio
async def test_reload_snapshot_aclose_gated_by_memory_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memory manager close must fire only when the effective fingerprint changes.

    The gate baseline is the standalone _last_closed_memory_fingerprint, not the
    rail-rebuild baseline _memory_embedding_fingerprint: the latter stays empty
    on memory-disabled paths (custom code spec, memory-off modes), so using it
    would judge "changed" on every reload and tear down the memory manager each
    time. An empty fingerprint (snapshot with no embed/jiuwen memory section)
    skips the gate instead of counting as a change. The close itself must be
    agent-scoped (aclose_memory_managers_for with this adapter's agent_id and
    workspace root), never the process-wide aclose_memory_manager_cache, and an
    instance-less adapter (root adapter, which only fans out) must skip the
    close entirely.
    """
    import importlib

    mod = importlib.import_module(_OJ_MEMORY_MANAGER_MODULE)
    if not hasattr(mod, "aclose_memory_managers_for"):
        pytest.skip("openjiuwen agent-scoped memory manager close not available")

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(interface_module, "get_env_file", lambda: env_file)

    config_base = {
        "react": {},
        "embed": {
            "embed_model": "m",
            "embed_base_url": "http://x",
            "embed_api_key": "k",
        },
    }
    aclose_for = AsyncMock(return_value=1)
    aclose_all = AsyncMock()
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._refresh_multimodal_configs = MagicMock()
    adapter._instance = MagicMock()
    adapter._instance.card.id = "agent-1"
    adapter._workspace_dir = str(tmp_path)
    adapter._resolved_language = "zh"
    with (
        patch.object(interface_module, "clear_config_cache", MagicMock()),
        patch(f"{_OJ_MEMORY_MANAGER_MODULE}.aclose_memory_managers_for", aclose_for),
        patch(f"{_OJ_MEMORY_MANAGER_MODULE}.aclose_memory_manager_cache", aclose_all),
        patch.object(interface_module, "get_config", lambda: config_base),
    ):
        # 裸 adapter（memory-off mode 的基线形态）从未 close 过：首次 reload 带
        # 非空指纹 → 按本 adapter 粒度 close 一次，且不做进程级全清。
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 1
        assert aclose_all.await_count == 0
        agent_id, workspace = aclose_for.await_args.args
        assert agent_id == "agent-1"
        assert str(workspace.root_path) == str(tmp_path)

        # 指纹未变 → 不再 close。
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 1
        assert aclose_all.await_count == 0

        # 指纹变化（embed endpoint 变更）→ 再次按粒度 close。
        config_base["embed"]["embed_base_url"] = "http://y"
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 2
        assert aclose_all.await_count == 0

        # 快照缺 embed/jiuwen 记忆段（指纹为空）→ 跳过门控，不判为变化。
        del config_base["embed"]
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 2
        assert aclose_all.await_count == 0

        # 未建 instance 的 adapter（root adapter 只扇出不持有 rail）：即使指纹
        # 相对基线变化也不 close——它没有自己的 manager，扇出的 session adapter
        # 会各自按自己的门控处理。
        del adapter._instance
        config_base["embed"] = {
            "embed_model": "m2",
            "embed_base_url": "http://z",
            "embed_api_key": "k2",
        }
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 2
        assert aclose_all.await_count == 0


@pytest.mark.asyncio
async def test_reload_snapshot_aclose_failure_keeps_baseline_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed close must not advance the gate baseline.

    If the baseline advanced past a failed close (import of
    aclose_memory_managers_for failing on an older openjiuwen, Workspace
    construction or the close itself raising), later reloads with the same
    fingerprint would skip the close branch forever: the stale manager stays
    in INDEX_CACHE and the new embedding config silently never takes effect.
    Keeping the old baseline makes the next same-fingerprint reload retry the
    close, and once a retry succeeds the baseline advances and unchanged
    fingerprints stop closing again.
    """
    import importlib

    mod = importlib.import_module(_OJ_MEMORY_MANAGER_MODULE)
    if not hasattr(mod, "aclose_memory_managers_for"):
        pytest.skip("openjiuwen agent-scoped memory manager close not available")

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(interface_module, "get_env_file", lambda: env_file)

    config_base = {
        "react": {},
        "embed": {
            "embed_model": "m",
            "embed_base_url": "http://x",
            "embed_api_key": "k",
        },
    }
    aclose_for = AsyncMock(side_effect=RuntimeError("close boom"))
    aclose_all = AsyncMock()
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._refresh_multimodal_configs = MagicMock()
    adapter._instance = MagicMock()
    adapter._instance.card.id = "agent-1"
    adapter._workspace_dir = str(tmp_path)
    adapter._resolved_language = "zh"
    with (
        patch.object(interface_module, "clear_config_cache", MagicMock()),
        patch(f"{_OJ_MEMORY_MANAGER_MODULE}.aclose_memory_managers_for", aclose_for),
        patch(f"{_OJ_MEMORY_MANAGER_MODULE}.aclose_memory_manager_cache", aclose_all),
        patch.object(interface_module, "get_config", lambda: config_base),
    ):
        # 首次 reload：进入关闭分支但 close 抛异常 → 基线不推进（保持空串）。
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 1
        assert adapter._last_closed_memory_fingerprint == ""
        assert aclose_all.await_count == 0

        # 同指纹再次 reload：基线未推进，重试 close 而非永久放弃。
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 2
        assert adapter._last_closed_memory_fingerprint == ""

        # 重试成功 → 基线推进到当前指纹。
        aclose_for.side_effect = None
        aclose_for.return_value = 1
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 3
        assert adapter._last_closed_memory_fingerprint == (
            adapter._memory_config_fingerprint(config_base)
        )

        # 同指纹不再 close（门控恢复正常降频语义）。
        await adapter._apply_reload_config_snapshot(None, None)
        assert aclose_for.await_count == 3
