# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared document runtimes: first config, per-call identity, cancellation and SDK I/O."""
import asyncio
import json
import re
import threading
from copy import copy
from unittest.mock import Mock

import pytest

from jiuwenswarm.agents.harness.common.memory.document_runtime_holder import DocumentRuntimeHolder


class Resource:
    def __init__(self):
        self.started = 0
        self.closed = False
        self.entered, self.finish = threading.Event(), threading.Event()
        self.identity = None

    def bind_security(self, identity):
        clone = copy(self)
        clone.identity = identity
        return clone

    async def initialize(self):
        self.started += 1

    async def write(self):
        self.entered.set()
        await asyncio.to_thread(self.finish.wait)
        return self.identity()

    async def close(self):
        self.closed = True


def config(root):
    return {"markdown_store": {"default": {"params": {"root": str(root)}}}}


@pytest.mark.asyncio
async def test_shared_runtime_drains_cancelled_calls_and_uses_each_identity(tmp_path, caplog):
    holder, resource = DocumentRuntimeHolder(), Resource()
    factory = Mock(return_value=resource)
    first = holder.acquire(config(tmp_path), lambda: "first", factory)
    second = holder.acquire({**config(tmp_path / "."), "different": True}, lambda: "second", factory)
    try:
        await asyncio.gather(first.initialize(), second.initialize())
        assert resource.started == 1 and factory.call_count == 1
        assert "first configuration retained" in caplog.text
        task = asyncio.create_task(second.write())
        await asyncio.to_thread(resource.entered.wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await first.close()
        assert not resource.closed
        closing = asyncio.create_task(second.close())
        await asyncio.sleep(0.02)
        assert not closing.done() and not resource.closed
        resource.finish.set()
        await closing
        assert resource.closed
    finally:
        resource.finish.set()
        await first.close()
        await second.close()
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
async def test_creation_failure_is_retryable_and_roots_are_independent(tmp_path):
    holder = DocumentRuntimeHolder()
    try:
        with pytest.raises(RuntimeError, match="assembly"):
            holder.acquire(config(tmp_path), None, Mock(side_effect=RuntimeError("assembly")))
        a, b = Resource(), Resource()
        first = holder.acquire(config(tmp_path), lambda: "one", lambda: a)
        second = holder.acquire(config(tmp_path / "other"), lambda: "two", lambda: b)
        a.finish.set()
        b.finish.set()
        assert await asyncio.gather(first.write(), second.write()) == ["one", "two"]
        await first.close()
        assert a.closed and not b.closed
        await second.close()
    finally:
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
async def test_real_sdk_members_share_runtime_and_survive_leader_exit(tmp_path):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    from jiuwen_memory.api import build_dev_authenticator, internal_context

    cfg = adapter._agent_default_config()
    cfg["globals"].update(embedder_dim=64, watch_document=True)
    cfg["llm"]["default"] = "echo"
    cfg["extractor"]["default"] = "keyword"
    cfg["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    cfg["fulltext_store"] = {"default": {"target": "memory"}}
    cfg["markdown_store"] = {"default": {"target": "local", "params": {"root": str(tmp_path)}}}
    auth = build_dev_authenticator()
    holder = DocumentRuntimeHolder()
    providers = [adapter.AgentMemoryMemoryProvider(
        config_dict=cfg, document_runtime_holder=holder, portal={"enabled": False},
        security_provider=lambda: internal_context(auth), scene="team", project="repo",
        team="repo--" + "a" * 24, member=name,
    ) for name in ("leader", "member")]
    try:
        await asyncio.gather(*(p.initialize(scope_id="local", user_id="developer",
                                           agent_id="main_agent", session_id="") for p in providers))
        assert providers[0].runtime is providers[1].runtime
        await asyncio.gather(*(p.add_team_memories([
            {"label": "decision", "content": f"ocean plan {index}"},
        ]) for index, p in enumerate(providers)))
        document = (tmp_path / "memory/teams" / ("repo--" + "a" * 24) / "MEMORY.md").read_text()
        assert all(f"ocean plan {index}" in document for index in range(2))
        await providers[0].shutdown()
        assert "ocean plan" in await providers[1].prefetch("ocean")
        assert len(holder._entries) == 1
        await providers[1].shutdown()
        assert not holder._entries
    finally:
        await asyncio.gather(*(p.shutdown() for p in providers))
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
async def test_startup_failure_releases_runtime_and_allows_retry(tmp_path):
    holder = DocumentRuntimeHolder()
    failed = Resource()
    async def fail():
        raise RuntimeError("watcher startup")
    failed.initialize = fail
    lease = holder.acquire(config(tmp_path), lambda: "user", lambda: failed)
    try:
        with pytest.raises(RuntimeError, match="watcher startup"):
            await lease.initialize()
        assert failed.closed and not holder._entries
        replacement = Resource()
        fresh = holder.acquire(config(tmp_path), lambda: "user", lambda: replacement)
        await fresh.initialize()
        assert replacement.started == 1
        await fresh.close()
    finally:
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
async def test_real_sdk_portal_survives_one_provider_exit(tmp_path):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    from jiuwen_memory.api import build_dev_authenticator, internal_context

    cfg = adapter._agent_default_config()
    cfg["globals"].update(embedder_dim=64, watch_document=False)
    cfg["llm"]["default"] = "echo"
    cfg["extractor"]["default"] = "keyword"
    cfg["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    cfg["fulltext_store"] = {"default": {"target": "memory"}}
    cfg["markdown_store"] = {"default": {"target": "local", "params": {"root": str(tmp_path)}}}
    auth = build_dev_authenticator()
    holder = DocumentRuntimeHolder()
    providers = [adapter.AgentMemoryMemoryProvider(
        config_dict=cfg, document_runtime_holder=holder,
        security_provider=lambda: internal_context(auth), scene="code", project="repo",
        portal={"port": 0},
    ) for _ in range(2)]
    try:
        for provider in providers:
            await provider.initialize(scope_id="local", user_id="developer", session_id="")
        assert providers[0].runtime is providers[1].runtime
        assert providers[0]._portal_handle is not None
        assert providers[1]._portal_handle is not None
        await providers[0].shutdown()
        assert providers[1].runtime is not None
        assert providers[1]._portal_error is None
        await providers[1].prefetch("project")
    finally:
        await asyncio.gather(*(p.shutdown() for p in providers))
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team.work.normal", "team.code.normal"])
async def test_public_swarm_rail_runs_core_agent_and_persists_team_memory(tmp_path, monkeypatch, mode):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    from openjiuwen.core.foundation.llm import (
        AssistantMessage, Model, ModelClientConfig, ModelRequestConfig, ToolCall,
    )
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness.factory import create_deep_agent
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers.team_memory import build_team_memory
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder
    from jiuwenswarm.agents.harness.common.memory import document_runtime_holder as pool

    cfg = adapter._agent_default_config()
    cfg["globals"].update(embedder_dim=64, watch_document=False)
    cfg["llm"]["default"] = "echo"
    cfg["extractor"]["default"] = "keyword"
    cfg["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    cfg["fulltext_store"] = {"default": {"target": "memory"}}
    cfg["markdown_store"] = {"default": {"target": "local", "params": {"root": str(tmp_path)}}}
    holder = DocumentRuntimeHolder()
    monkeypatch.setattr(pool, "DOCUMENT_RUNTIME_HOLDER", holder)
    monkeypatch.setattr(builder, "get_embed_config", lambda: {})
    config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {
        "mode": "sdk", "scope_id": "local", "user_id": "developer", "sdk": cfg,
    }}}}
    config["modes"] = {
        "agent": {"memory": {"enabled": False}},
        "code": {"memory": {"enabled": False}},
        "team": {"jiuwen_team": {"memory": {"enabled": False}}},
    }
    team = "repo--" + "a" * 24
    rails = build_team_memory({}, SwarmBuildContext(config=config, mode=mode,
        memory_project="repo", memory_team=team, member_name="leader"))
    assert rails is not None
    rail = rails[-1]
    history, tool_history = [], []
    collected = False
    async def invoke(_model, messages, **kwargs):
        nonlocal collected
        history.append(str(messages))
        tool_history.append(str(kwargs.get("tools", [])))
        if "collect_team_conclusions" in str(kwargs.get("tools", [])):
            if collected:
                return AssistantMessage(content="collected")
            collected = True
            return AssistantMessage(content="", tool_calls=[ToolCall(
                id="collect", name="collect_team_conclusions", type="function",
                arguments='{"items":[{"label":"decision","content":"confirmed ocean plan","confirmed":true}]}',
            )])
        return AssistantMessage(content="acknowledged")
    monkeypatch.setattr(Model, "invoke", invoke)
    model = Model(model_client_config=ModelClientConfig(client_provider="OpenAI",
        api_key="mock", api_base="http://mock-base"), model_config=ModelRequestConfig(model="mock-model"))
    agent = create_deep_agent(model, rails=rails, enable_task_loop=False,
        enable_sys_operation=False, enable_skill_discovery=False)
    await Runner.start()
    try:
        await rail._provider.initialize(**rail._identity)
        await rail._provider.add_team_memories([{"label": "context", "content": "ocean recall sentinel"}])
        result = await Runner.run_agent(agent, {"query": "review ocean"})
        await rail.drain()
        assert result["output"] == "acknowledged"
        assert any("ocean recall sentinel" in messages for messages in history)
        for name in ("team_memory_search", "team_memory_read", "team_memory_write", "team_memory_edit"):
            assert any(name in tools for tools in tool_history)
        assert collected
        document = (tmp_path / "memory/teams" / team / "MEMORY.md").read_text()
        assert "confirmed ocean plan" in document
    finally:
        await rail.aclose()
        await Runner.stop()
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
async def test_close_failure_quarantines_root_without_leaking_other_roots(tmp_path):
    from unittest.mock import AsyncMock
    holder = DocumentRuntimeHolder()
    failed, other = Resource(), Resource()
    failed.close = AsyncMock(side_effect=RuntimeError("close failed"))
    lease = holder.acquire(config(tmp_path), lambda: "one", lambda: failed)
    holder.acquire(config(tmp_path / "other"), lambda: "two", lambda: other)
    try:
        with pytest.raises(RuntimeError, match="close failed"):
            await lease.close()
        with pytest.raises(RuntimeError, match="still closing"):
            holder.acquire(config(tmp_path), lambda: "one", Resource)
    finally:
        with pytest.raises(RuntimeError, match="close failed"):
            await asyncio.to_thread(holder.close)
    assert other.closed
    failed.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_configured_identity_shares_profiles_and_keeps_project_team_scope(tmp_path, monkeypatch):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import AgentLlmExtractor
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder
    from jiuwenswarm.agents.harness.common.memory import document_runtime_holder as pool

    def extract_response(_extractor, messages):
        source = re.search(r"\[ID: ([^\]]+)\]", messages[-1].content).group(1)
        return json.dumps([
            {"source_id": source, "target": "fact", "confidence": 1.0, "tier": "semantic",
             "content": text, "memory_class": category, "project_fact": project_fact,
             "team_internal": False}
            for text, category, project_fact in [
                ("user prefers violet", "user_profile", False),
                ("ocean project uses sqlite", "long_term_memory", True),
            ]
        ])

    monkeypatch.setattr(AgentLlmExtractor, "_call_llm_with_retry", extract_response)
    cfg = adapter._agent_default_config()
    cfg["globals"].update(embedder_dim=64, watch_document=False)
    cfg["llm"]["default"] = "echo"
    cfg["extractor"]["default"] = "agent_llm"
    cfg["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    cfg["fulltext_store"] = {"default": {"target": "memory"}}
    cfg["markdown_store"] = {"default": {"target": "local", "params": {"root": str(tmp_path)}}}
    holder = DocumentRuntimeHolder()
    monkeypatch.setattr(pool, "DOCUMENT_RUNTIME_HOLDER", holder)
    monkeypatch.setattr(builder, "get_embed_config", lambda: {})
    providers = []
    cases = [
        ("team", "repo", "a", {}, (True, True, True)),
        ("agent", "repo", "a", {}, (True, True, False)),
        ("code", "repo", "a", {}, (True, True, False)),
        ("team", "repo", "b", {}, (True, True, False)),
        ("team", "elsewhere", "a", {}, (True, False, False)),
        ("code", "repo", "a", {"user_id": "other-user"}, (False, False, False)),
        ("code", "repo", "a", {"agent_id": "main_agent"}, (False, False, False)),
    ]
    try:
        for index, (scene, project, group, overrides, _) in enumerate(cases):
            settings = {"mode": "sdk", "scope_id": "local", "user_id": "developer",
                        "agent_id": "shared-memory", "sdk": cfg, **overrides}
            host_config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": settings}}}
            rail = builder.build_jiuwen_memory_rail(host_config, agent_name=f"runtime-{index}",
                scene=scene, project=project,
                team_binding={"team": project + "--" + group * 24, "member": f"member-{index}"})
            assert rail is not None
            provider = rail._provider
            providers.append(provider)
            identity = rail._identity if scene != "agent" else {
                key: getattr(rail, "_" + key) for key in ("scope_id", "user_id", "agent_id", "session_id")
            }
            await provider.initialize(**identity)
        assert all(provider.runtime is providers[0].runtime for provider in providers)
        await providers[0].sync_turn("confirmed preferences and architecture", "acknowledged")
        await providers[0].add_team_memories([{"label": "decision", "content": "ocean team approval"}])
        assert "user prefers violet" in (tmp_path / "USER.md").read_text()
        assert "ocean project uses sqlite" in (tmp_path / "memory/repo/MEMORY.md").read_text()
        for provider, (_, project, _, _, expected) in zip(providers, cases):
            recalled = await provider.prefetch("ocean", project=project)
            actual = tuple(text in recalled for text in (
                "user prefers violet", "ocean project uses sqlite", "ocean team approval"))
            assert actual == expected, (project, expected, actual)
    finally:
        await asyncio.gather(*(provider.shutdown() for provider in providers))
        await asyncio.to_thread(holder.close)
