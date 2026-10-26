# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.reload_result import ReloadResult
from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool
from jiuwenswarm.common.local_env_config import (
    ENV_CONFIG_DICT,
    bind_task_env_overlay,
    clear_staged_env,
    get_active_env,
    get_local_config,
    reset_task_env_overlay,
)


@pytest.fixture(autouse=True)
def _reset_env_state():
    saved_environ = dict(os.environ)
    ENV_CONFIG_DICT.clear()
    clear_staged_env()
    TenantAgentPool.reset_instance()
    yield
    ENV_CONFIG_DICT.clear()
    clear_staged_env()
    TenantAgentPool.reset_instance()
    os.environ.clear()
    os.environ.update(saved_environ)


@pytest.mark.asyncio
async def test_office_reload_does_not_pollute_assistant_get_local_config():
    """office reload must not leak office-only keys into assistant overlay reads."""
    TenantAgentPool.reset_instance()
    pool = TenantAgentPool.get_instance()

    office_manager = AgentManager(agent_id="office", service_id="default")
    office_manager._latest_env_overrides = {"MODEL_NAME": "office-model"}
    assistant_manager = AgentManager(agent_id="assistant", service_id="default")
    assistant_manager._latest_env_overrides = {"MODEL_NAME": "assistant-model"}

    async def _fake_get(key):
        if key == ("office", "default", "default"):
            return office_manager
        if key == ("assistant", "default", "default"):
            return assistant_manager
        return None

    pool._agent_wrappers.get = _fake_get

    await pool.reload_tenant_config(
        "office",
        "default",
        {},
        {"MODEL_NAME": "office-hot-reload"},
    )

    assert assistant_manager._latest_env_overrides["MODEL_NAME"] == "assistant-model"
    assert office_manager._latest_env_overrides["MODEL_NAME"] == "office-hot-reload"

    token = bind_task_env_overlay(dict(assistant_manager._latest_env_overrides))
    try:
        assert get_local_config("MODEL_NAME") == "assistant-model"
    finally:
        reset_task_env_overlay(token)

    ENV_CONFIG_DICT["MODEL_NAME"] = "global-pollution"
    token = bind_task_env_overlay(dict(assistant_manager._latest_env_overrides))
    try:
        assert get_local_config("MODEL_NAME") == "assistant-model"
    finally:
        reset_task_env_overlay(token)


@pytest.mark.asyncio
async def test_assistant_overlay_ignores_global_after_office_reload():
    office_manager = AgentManager(agent_id="office", service_id="default")
    mock_agent = MagicMock()
    mock_agent.reload_agent_config = AsyncMock(return_value=ReloadResult(applied=True))
    office_manager.agents["default"] = {"agent": mock_agent}

    await office_manager.reload_agents_config({}, {"OFFICE_ONLY": "secret"})

    assistant_manager = AgentManager(agent_id="assistant", service_id="default")
    assistant_manager._latest_env_overrides = {}

    token = bind_task_env_overlay(dict(assistant_manager._latest_env_overrides))
    try:
        assert get_local_config("OFFICE_ONLY") is None
    finally:
        reset_task_env_overlay(token)


def test_get_config_cache_bypasses_when_task_overlay_bound(monkeypatch):
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.common.local_env_config import (
        apply_env_overrides_to_active,
        bind_agent_env_ns,
        reset_agent_env_ns,
    )

    config_module.clear_config_cache()
    calls = {"count": 0}
    original_read = config_module.get_merged_config_dict

    def _counting_read():
        calls["count"] += 1
        return original_read()

    monkeypatch.setattr(config_module, "get_merged_config_dict", _counting_read)
    apply_env_overrides_to_active(
        {"MODEL_NAME": "cached-model"},
        service_id="default",
        agent_id="office",
    )

    token = bind_agent_env_ns("default", "office")
    try:
        first = config_module.get_config()
        second = config_module.get_config()
        assert first is second
        assert calls["count"] == 1

        overlay_token = bind_task_env_overlay({"MODEL_NAME": "overlay-model"})
        try:
            third = config_module.get_config()
            assert third is not first
            assert calls["count"] == 2
        finally:
            reset_task_env_overlay(overlay_token)
    finally:
        reset_agent_env_ns(token)
        config_module.clear_config_cache()
