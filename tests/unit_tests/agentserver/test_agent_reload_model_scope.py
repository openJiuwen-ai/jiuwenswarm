# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""模型配置（reload_scopes 含 "model"）是所有 channel 共享的全局配置段。

web 通道保存模型配置时, reload_options 带 target_channel_id="web" +
reload_scopes=["model"]。若 AgentManager 仍按 target_channel 窄化只 reload
web, IM 长连接通道（xiaoyi 等）的 agent 不会收到热更新, 其 session adapter
继续用旧错误模型, 用户只能 /new_session 才能恢复。

回归: model scope 变更时, 即使带 target_channel_id 也必须 fan-out 到全部
channel 的 agent。
"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime import agent_manager as agent_manager_module


class FakeAgent:
    def __init__(self) -> None:
        self.reload_calls: list[dict] = []

    async def reload_agent_config(self, *args, **kwargs):
        self.reload_calls.append({"args": args, "kwargs": kwargs})


class FakeTeamManager:
    def __init__(self, channel_id, calls):
        self.channel_id = channel_id
        self.calls = calls

    async def update_evolution_config(self, config):
        self.calls.append((self.channel_id, config))


def _build_manager(monkeypatch, *, agents: dict[str, dict[str, FakeAgent]]):
    manager = agent_manager_module.AgentManager()
    manager.agents = agents
    team_updates: list = []
    monkeypatch.setattr(
        agent_manager_module,
        "get_team_manager",
        lambda channel_id: FakeTeamManager(channel_id, team_updates),
    )
    return manager, team_updates


@pytest.mark.asyncio
async def test_model_scope_fans_out_to_all_channels_even_with_target_channel(monkeypatch):
    """target_channel_id='web' + reload_scopes={'model'} → 全 channel 都要 reload."""
    web_agent = FakeAgent()
    xiaoyi_agent = FakeAgent()
    tui_agent = FakeAgent()
    manager, team_updates = _build_manager(
        monkeypatch,
        agents={
            "web": {"agent": web_agent},
            "xiaoyi": {"agent": xiaoyi_agent},
            "tui": {"code": tui_agent},
        },
    )

    config = {"models": {"defaults": [{"model_name": "GLM-5.2"}]}}
    env = {"MODEL_NAME": "GLM-5.2"}
    await manager.reload_agents_config(
        config,
        env,
        target_channel_id="web",
        reload_scopes={"model"},
    )

    # Every channel's agent got reloaded — including xiaoyi (the IM long-conn channel
    # that was previously left stale, forcing users to /new_session to recover).
    assert len(web_agent.reload_calls) == 1
    assert len(xiaoyi_agent.reload_calls) == 1, (
        "xiaoyi agent must be reloaded on model-scope change; leaving it stale is the bug"
    )
    assert len(tui_agent.reload_calls) == 1
    # All fan-out reloads carry the same config + env (no per-session scoping here).
    for agent in (web_agent, xiaoyi_agent, tui_agent):
        call = agent.reload_calls[0]
        assert call["kwargs"]["config_base"] is config
        assert call["kwargs"]["env_overrides"] == env
        assert "target_session_id" not in call["kwargs"]
    # Team evolution config is also refreshed for every channel.
    assert {cid for cid, _ in team_updates} == {"web", "xiaoyi", "tui"}


@pytest.mark.asyncio
async def test_non_model_scope_still_narrows_to_target_channel(monkeypatch):
    """Non-model scopes (e.g. permissions/agent_runtime) keep the old target-channel
    narrowing — model is the only global scope that must fan out."""
    web_agent = FakeAgent()
    xiaoyi_agent = FakeAgent()
    manager, _ = _build_manager(
        monkeypatch,
        agents={"web": {"agent": web_agent}, "xiaoyi": {"agent": xiaoyi_agent}},
    )

    config = {"permissions": {"enabled": True}}
    await manager.reload_agents_config(
        config,
        {},
        target_channel_id="web",
        reload_scopes={"permissions"},
    )

    assert len(web_agent.reload_calls) == 1
    assert xiaoyi_agent.reload_calls == [], (
        "non-model scopes must still narrow to target_channel (no regression)"
    )


@pytest.mark.asyncio
async def test_model_scope_dedup_skips_identical_reload(monkeypatch):
    """Same model-scope reload repeated → deduped by fingerprint (no double reload)."""
    web_agent = FakeAgent()
    xiaoyi_agent = FakeAgent()
    manager, _ = _build_manager(
        monkeypatch,
        agents={"web": {"agent": web_agent}, "xiaoyi": {"agent": xiaoyi_agent}},
    )

    config = {"models": {"defaults": [{"model_name": "GLM-5.2"}]}}
    await manager.reload_agents_config(
        config, {}, target_channel_id="web", reload_scopes={"model"},
    )
    # Identical config+env+scopes → deduped, agents not reloaded a second time.
    await manager.reload_agents_config(
        config, {}, target_channel_id="web", reload_scopes={"model"},
    )

    assert len(web_agent.reload_calls) == 1
    assert len(xiaoyi_agent.reload_calls) == 1


@pytest.mark.asyncio
async def test_model_scope_change_is_not_deduped_against_non_model_reload(monkeypatch):
    """A prior non-model reload (target_channel=web) must not dedup a later model
    reload that fans out to all channels — the scope changed the effective topology."""
    web_agent = FakeAgent()
    xiaoyi_agent = FakeAgent()
    manager, _ = _build_manager(
        monkeypatch,
        agents={"web": {"agent": web_agent}, "xiaoyi": {"agent": xiaoyi_agent}},
    )

    same_config = {"models": {"defaults": [{"model_name": "GLM-5.2"}]}}
    # First: non-model reload scoped to web only.
    await manager.reload_agents_config(
        same_config, {}, target_channel_id="web", reload_scopes={"permissions"},
    )
    assert len(web_agent.reload_calls) == 1
    assert xiaoyi_agent.reload_calls == []
    # Second: model reload fans out — must NOT be deduped against the first.
    await manager.reload_agents_config(
        same_config, {}, target_channel_id="web", reload_scopes={"model"},
    )
    assert len(web_agent.reload_calls) == 2
    assert len(xiaoyi_agent.reload_calls) == 1, (
        "model-scope reload must reach xiaoyi even after a prior web-only permissions reload"
    )
