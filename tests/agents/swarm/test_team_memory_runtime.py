# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared document runtimes: first config, per-call identity, cancellation and SDK I/O."""
import asyncio
import json
import re
import threading
from copy import copy
from unittest.mock import AsyncMock, Mock

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


def test_team_memory_user_context_preserves_typed_runtime_fields():
    from openjiuwen.core.common.run_context import RunContext

    from jiuwenswarm.common.team_memory_binding import team_memory_user_context

    original = RunContext(session_id="session", context_mode="isolated", extra={"trace": "kept"})
    result = team_memory_user_context(original, "use the confirmed brief")
    assert result.session_id == original.session_id
    assert result.context_mode == original.context_mode
    assert result.extra == {
        "trace": "kept", "raw_query": "use the confirmed brief", "raw_query_origin": "human",
    }
    assert original.extra == {"trace": "kept"}


def config(root):
    return {"markdown_store": {"default": {"params": {"root": str(root)}}}}


@pytest.fixture
def collective_extraction(monkeypatch):
    from jiuwen_memory.common.llm.llm_impl.echo_llm import EchoLLM

    def chat(_llm, messages, **_kwargs):
        if len(messages) != 2 or "source_segments" not in messages[1].content:
            return messages[-1].content
        sources = json.loads(messages[1].content)["source_segments"]
        rows = []
        for source in sources:
            text = source["text"]
            if source["role"] == "user" and text == "user prefers violet":
                kind, target = "user_fact", "user_profile"
            elif text == "ocean project uses sqlite":
                kind, target = "experience", "project"
            elif source["role"] == "assistant":
                kind, target = "experience", "team"
                if text == "acknowledged":
                    text = "confirmed ocean plan"
            else:
                continue
            rows.append({"source_id": source["id"], "kind": kind, "destination": target,
                         "evidence": source["text"], "content": text, "title": "Decision"})
        return json.dumps(rows)

    monkeypatch.setattr(EchoLLM, "chat", chat)


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
async def test_repeated_close_drains_once_and_rejects_new_calls(tmp_path):
    holder, resource = DocumentRuntimeHolder(), Resource()
    resource.close = AsyncMock(wraps=resource.close)
    lease = holder.acquire(config(tmp_path), lambda: "user", lambda: resource)
    try:
        write = asyncio.create_task(lease.write())
        assert await asyncio.to_thread(resource.entered.wait, 5)
        closing = asyncio.create_task(lease.close())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="lease is closed"):
            await lease.write()
        repeated = asyncio.create_task(lease.close())
        await asyncio.sleep(0)
        assert not closing.done() and not repeated.done()
        resource.finish.set()
        result, _, _ = await asyncio.gather(write, closing, repeated)
        assert result == "user"
        resource.close.assert_awaited_once()
        await lease.close()
        resource.close.assert_awaited_once()
    finally:
        resource.finish.set()
        await lease.close()
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
async def test_acquire_waits_for_close_without_blocking_other_roots(tmp_path):
    holder = DocumentRuntimeHolder()
    retiring, replacement, other = Resource(), Resource(), Resource()
    close_entered = threading.Event()

    async def delayed_close():
        close_entered.set()
        await asyncio.to_thread(retiring.finish.wait)
        await Resource.close(retiring)

    retiring.close = AsyncMock(side_effect=delayed_close)
    first = holder.acquire(config(tmp_path), lambda: "old", lambda: retiring)
    other_lease = holder.acquire(
        config(tmp_path / "other"), lambda: "other", lambda: other,
    )
    factory = Mock(return_value=replacement)
    closing = asyncio.create_task(first.close())
    pending = []
    try:
        assert await asyncio.to_thread(close_entered.wait, 2)
        pending = [asyncio.create_task(asyncio.to_thread(
            holder.acquire, config(tmp_path), lambda: "new", factory,
        )) for _ in range(2)]
        replacements = asyncio.gather(*pending)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(replacements), timeout=0.05)
        factory.assert_not_called()
        other.finish.set()
        result = await asyncio.wait_for(
            asyncio.to_thread(lambda: asyncio.run(other_lease.write())), timeout=2,
        )
        assert result == "other", "a closing root must not block other roots"
        retiring.finish.set()
        leases = await asyncio.wait_for(replacements, timeout=2)
        await closing
        assert retiring.closed
        factory.assert_called_once()
        assert leases[0].portal_runtime_key == leases[1].portal_runtime_key
    finally:
        retiring.finish.set()
        other.finish.set()
        await asyncio.gather(closing, return_exceptions=True)
        for result in await asyncio.gather(*pending, return_exceptions=True):
            if not isinstance(result, BaseException):
                await result.close()
        await other_lease.close()
        await asyncio.to_thread(holder.close)
    assert replacement.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_binding_failure_preserves_runtime_ownership(
    tmp_path, monkeypatch, existing,
):
    holder, resource = DocumentRuntimeHolder(), Resource()
    lease = None
    if existing:
        lease = holder.acquire(config(tmp_path), lambda: "owner", lambda: resource)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                resource, "bind_security",
                Mock(side_effect=RuntimeError("bind failed")),
            )
            with pytest.raises(RuntimeError, match="bind failed"):
                holder.acquire(config(tmp_path), lambda: "rejected", lambda: resource)
        if existing:
            assert not resource.closed, "failed borrowing must preserve the runtime"
            resource.finish.set()
            assert await lease.write() == "owner"
            await lease.close()
            assert resource.closed, "failed borrowing must not leak a reference"
        else:
            assert resource.closed, "an unleased runtime must be cleaned up"
            replacement = Resource()
            lease = holder.acquire(
                config(tmp_path), lambda: "retry", lambda: replacement,
            )
            await lease.close()
            assert replacement.closed
    finally:
        if lease is not None:
            await lease.close()
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
async def test_real_sdk_members_share_runtime_and_survive_leader_exit(tmp_path, collective_extraction):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    from jiuwen_memory.api import build_dev_authenticator, internal_context
    from jiuwenswarm.agents.harness.common.memory.team_memory_members import enroll_team_memory

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
        collective_coordinates={"project": "repo", "team": "repo--" + "a" * 24}, member=name,
    ) for name in ("leader", "member")]
    try:
        await asyncio.gather(*(p.initialize(scope_id="local", user_id="developer",
                                           agent_id="main_agent", session_id="") for p in providers))
        assert providers[0].runtime is providers[1].runtime
        await enroll_team_memory(
            providers[0]._client, org="local", space="", user="developer",
            agents=("main_agent",), project_agent="main_agent",
            coordinates={"project": "repo", "team": "repo--" + "a" * 24},
        )
        await asyncio.gather(*(p.sync_turn("", "", sources=[
            {"id": str(index), "role": "assistant", "text": f"ocean plan {index}"},
        ]) for index, p in enumerate(providers)))
        document = (tmp_path / "memory/team_memory/teams" / ("repo--" + "a" * 24) / "MEMORY.md").read_text()
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
async def test_explicit_expert_membership_changes_use_stable_scope(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from jiuwenswarm.agents.harness.common.memory import team_memory_members
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext

    client = AsyncMock()

    @asynccontextmanager
    async def lease(_config, *, create=False):
        assert create is (client.attach_collective.await_count == 0)
        yield client, "service-admin", ("org", "", "user")

    monkeypatch.setattr(team_memory_members, "_membership_client", lease)
    context = SwarmBuildContext(
        config={}, memory_team="stable-team", memory_project="project-a", memory_group="design",
    )
    hook = context.extras["team.on_explicit_member_change"]
    template = {"name": "poster", "group": None}
    await hook("instance-1", template, True)
    assert {call.args[0].kind for call in client.attach_collective.await_args_list} == {
        "team", "project",
    }
    assert all(call.args[1][0].scope.agent == "expert:poster"
               for call in client.attach_collective.await_args_list)
    await hook("instance-2", template, False)
    assert {call.args[0].kind for call in client._remove_collective.await_args_list} == {
        "team", "project",
    }
    assert all(call.args[1][0].agent == "expert:poster"
               for call in client._remove_collective.await_args_list)


def test_expert_team_leader_resolves_to_template_subject_without_group_binding(monkeypatch):
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers import team_memory

    calls = []

    def fake_build(*args, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(team_memory, "build_jiuwen_memory_rail", fake_build)
    config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {"mode": "sdk"}}}}

    expert_ctx = SwarmBuildContext(
        config=config, mode="team.work.normal", memory_team="studio-team",
        memory_project="repo", memory_group="studio", memory_experts=("leader", "brief"),
        member_name="team_leader", role="leader",
    )
    cluster_ctx = SwarmBuildContext(
        config=config, mode="team.work.normal", memory_team="main-team",
        memory_project="repo", memory_group="", memory_experts=(),
        member_name="team_leader", role="leader",
    )

    assert team_memory.build_team_memory({}, expert_ctx) is not None
    assert team_memory.build_team_memory({}, cluster_ctx) is not None
    assert calls[0]["collective_agent"] == "studio:leader"
    assert "group" not in calls[0]["team_binding"]
    assert calls[1]["collective_agent"] == "main_agent"
    assert "group" not in calls[1]["team_binding"]


@pytest.mark.asyncio
async def test_revoke_expert_group_memory_only_revokes_team_dimension(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from jiuwen_memory.api import Dimension, Scope
    from jiuwenswarm.agents.harness.common.memory import team_memory_members
    from jiuwenswarm.common.team_memory_binding import team_memory_key

    client = AsyncMock()
    expected_team = team_memory_key("org", "", "user", "studio")
    expected_dimension = Dimension("org", "", "team", expected_team)
    active_scopes = [Scope(org="org", space="", user="user", agent="studio:leader")]
    client.active_collective_members.return_value = active_scopes

    @asynccontextmanager
    async def lease():
        yield client, "service-admin", ("org", "", "user")

    monkeypatch.setattr(team_memory_members, "_membership_client", lease)
    await team_memory_members.revoke_expert_group_memory("studio")

    client.active_collective_members.assert_awaited_once_with(expected_dimension, "service-admin")
    client._remove_collective.assert_awaited_once_with(
        expected_dimension, active_scopes, "service-admin",
    )


@pytest.mark.asyncio
async def test_swarm_http_registers_and_revokes_through_separate_principal(tmp_path, collective_extraction):
    from http.server import ThreadingHTTPServer
    from threading import Thread

    from jiuwen_memory.api import Scope, register_trusted_principal
    from jiuwen_memory.api.collective import COLLECTIVE_MANAGER_ID, Dimension
    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import _agent_default_config
    from jiuwen_memory_entry.core.profiles import load_config
    from jiuwen_memory_entry.http_server.__main__ import HttpServer

    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers.team_memory import build_team_memory

    gateway_key = "test-swarm-gateway"
    cfg = _agent_default_config()
    cfg["globals"].update(embedder_dim=64, watch_document=False)
    cfg["llm"]["default"] = "echo"
    cfg["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    cfg["fulltext_store"] = {"default": {"target": "memory"}}
    cfg["markdown_store"] = {"default": {"target": "local", "params": {"root": str(tmp_path)}}}
    cfg["security"] = {"default": {"target": "standard", "params": {
        "authenticator": "gateway", "authorizer": "default",
    }}}
    cfg["authenticator"] = {"gateway": {"target": "trusted", "params": {
        "key_store": "principals", "gateway_key": gateway_key,
    }}}
    cfg["key_store"] = {"principals": {"target": "memory"}}
    cfg["authorizer"] = {"default": {"target": "standard", "params": {
        "grant_store": "default", "delegation_store": "default",
    }}}
    cfg["grant_store"] = {"default": {"target": "memory"}}
    cfg["delegation_store"] = {"default": {"target": "memory"}}

    service = HttpServer.build(load_config([{"memory_api": cfg}]))
    register_trusted_principal(service.authenticator, Scope(org="local", user="developer"))
    register_trusted_principal(
        service.authenticator, Scope(org="local", user=COLLECTIVE_MANAGER_ID), role="admin",
    )
    http = ThreadingHTTPServer(("127.0.0.1", 0), service.handler_cls())
    worker = Thread(target=http.serve_forever, daemon=True)
    worker.start()
    rail = None
    try:
        config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {
            "mode": "http", "base_url": f"http://127.0.0.1:{http.server_port}", "api_key": gateway_key,
        }}}}
        context = SwarmBuildContext(
            config=config, mode="team.work.normal", memory_team="studio-team",
            memory_project="repo", memory_group="studio", memory_experts=("leader",),
            member_name="leader",
        )
        from jiuwenswarm.agents.harness.common.memory.team_memory_members import prepare_team_memory

        assert await prepare_team_memory(context)
        rail = build_team_memory({}, context)[-1]
        await rail._provider.initialize(**rail._identity)
        client = rail._provider._client
        dimension = Dimension("local", "", "team", "studio-team")
        for enrolled in (dimension, Dimension("local", "", "project", "repo")):
            authors = await client.active_collective_members(enrolled, COLLECTIVE_MANAGER_ID)
            assert {"main_agent", "studio:leader"} <= {author.agent for author in authors}
        assert not await client.active_collective_members(
            Dimension("local", "", "group", "studio"), COLLECTIVE_MANAGER_ID,
        )
        assert "group" not in rail._provider._collective_coordinates
        result = await rail._provider.sync_turn("", "", sources=[
            {"id": "delivery", "role": "assistant", "text": "ocean architecture"},
        ])
        assert result and result["stored"]
        document = tmp_path / "memory" / "team_memory" / "teams" / "studio-team" / "MEMORY.md"
        assert "# Decision\nocean architecture" in document.read_text(encoding="utf-8")
        assert "ocean architecture" in await rail._provider.prefetch("ocean")
        callback = context.extras["team.on_explicit_member_change"]
        await callback("leader", {}, False)
        authors = await client.active_collective_members(dimension, COLLECTIVE_MANAGER_ID)
        assert [author.agent for author in authors] == ["main_agent"]
        project = Dimension("local", "", "project", "repo")
        authors = await client.active_collective_members(project, COLLECTIVE_MANAGER_ID)
        assert [author.agent for author in authors] == ["main_agent"]
        assert "ocean architecture" not in await rail._provider.prefetch("ocean")
        await callback("leader", {}, True)
        assert "ocean architecture" in await rail._provider.prefetch("ocean")
    finally:
        if rail is not None:
            await rail._provider.shutdown()
        await asyncio.to_thread(http.shutdown)
        worker.join(timeout=5)
        http.server_close()
        await asyncio.to_thread(service.close, wait=True)


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
async def test_public_swarm_rail_runs_core_agent_and_persists_team_memory(
    tmp_path, monkeypatch, mode, collective_extraction,
):
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
    from jiuwenswarm.agents.harness.common.memory.team_memory_members import prepare_team_memory

    context = SwarmBuildContext(config=config, mode=mode,
        memory_project="repo", memory_team=team, memory_group="studio",
        memory_experts=("leader", "reviewer"), member_name="team_leader")
    assert await prepare_team_memory(context)
    rails = build_team_memory({}, context)
    assert rails is not None
    rail = rails[-1]
    assert "group" not in rail._provider._collective_coordinates
    history, tool_history = [], []
    async def invoke(_model, messages, **kwargs):
        history.append(str(messages))
        tool_history.append(str(kwargs.get("tools", [])))
        return AssistantMessage(content="acknowledged")

    monkeypatch.setattr(Model, "invoke", invoke)
    model = Model(model_client_config=ModelClientConfig(client_provider="OpenAI",
        api_key="mock", api_base="http://mock-base"), model_config=ModelRequestConfig(model="mock-model"))
    agent = create_deep_agent(model, rails=rails, enable_task_loop=False,
        enable_sys_operation=False, enable_skill_discovery=False)
    await Runner.start()
    try:
        await rail._provider.initialize(**rail._identity)
        await rail._provider.sync_turn("", "", sources=[
            {"id": "sentinel", "role": "assistant", "text": "ocean recall sentinel"},
        ])
        from jiuwenswarm.common.team_memory_binding import team_memory_user_context

        result = await Runner.run_agent(agent, {
            "query": "review ocean",
            "run": {"context": team_memory_user_context(None, "review ocean")},
        })
        await rail.drain()
        assert result["output"] == "acknowledged"
        assert any("ocean recall sentinel" in messages for messages in history)
        for name in ("team_memory_search", "team_memory_read", "team_memory_write", "team_memory_edit"):
            assert any(name in tools for tools in tool_history)
        assert all("collect_team_conclusions" not in tools for tools in tool_history)
        document = (tmp_path / "memory/team_memory/teams" / team / "MEMORY.md").read_text()
        assert "confirmed ocean plan" in document
    finally:
        await rail.aclose()
        await Runner.stop()
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
async def test_close_failure_quarantines_root_without_leaking_other_roots(tmp_path):
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
async def test_configured_identity_shares_profiles_and_keeps_project_team_scope(
    tmp_path, monkeypatch, collective_extraction,
):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    from jiuwen_memory.construction.extractor_impl.agent_llm_extractor import AgentLlmExtractor
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder
    from jiuwenswarm.agents.harness.common.memory import document_runtime_holder as pool

    original_extract = AgentLlmExtractor._call_llm_with_retry
    from jiuwenswarm.agents.harness.common.memory.team_memory_members import (
        prepare_project_memory, prepare_team_memory,
    )
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext

    def extract_response(extractor, messages, **options):
        if "source_segments" in messages[-1].content:
            return original_extract(extractor, messages, **options)
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
        ("code", "repo", "a", {"agent_id": "main_agent"}, (True, True, False)),
    ]
    try:
        for index, (scene, project, group, overrides, _) in enumerate(cases):
            settings = {"mode": "sdk", "scope_id": "local", "user_id": "developer",
                        "agent_id": "shared-memory", "sdk": cfg, **overrides}
            host_config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": settings}}}
            team = project + "--" + group * 24
            if scene == "team":
                assert await prepare_team_memory(SwarmBuildContext(
                    config=host_config, mode="team.work.normal", memory_project=project, memory_team=team,
                ))
            else:
                assert await prepare_project_memory(host_config, project)
            rail = builder.build_jiuwen_memory_rail(host_config, agent_name=f"runtime-{index}",
                scene=scene, project=project,
                team_binding={"team": team, "member": f"member-{index}"})
            assert rail is not None
            provider = rail._provider
            providers.append(provider)
            identity = rail._identity if scene != "agent" else {
                key: getattr(rail, "_" + key) for key in ("scope_id", "user_id", "agent_id", "session_id")
            }
            await provider.initialize(**identity)
        assert all(provider.runtime is providers[0].runtime for provider in providers)
        await providers[0].sync_turn("", "", sources=[
            {"id": "profile", "role": "user", "text": "user prefers violet"},
            {"id": "project", "role": "assistant", "text": "ocean project uses sqlite"},
            {"id": "team", "role": "assistant", "text": "ocean team approval"},
        ])
        assert "user prefers violet" in (tmp_path / "USER.md").read_text()
        assert "ocean project uses sqlite" in (tmp_path / "memory/team_memory/projects/repo/MEMORY.md").read_text()
        for provider, (_, project, _, _, expected) in zip(providers, cases):
            recalled = await provider.prefetch("ocean", project=project)
            actual = tuple(text in recalled for text in (
                "user prefers violet", "ocean project uses sqlite", "ocean team approval"))
            assert actual == expected, (project, expected, actual)
    finally:
        await asyncio.gather(*(provider.shutdown() for provider in providers))
        await asyncio.to_thread(holder.close)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_scene", ["agent", "code", "team"])
async def test_shared_sdk_video_imports_later_sessions(tmp_path, monkeypatch, first_scene):
    adapter = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")
    from jiuwen_memory.common.normalizer.normalizer_impl.video_normalizer import VideoNormalizer
    from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog
    from jiuwenswarm.agents.harness.common.memory import document_runtime_holder as pool
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as builder
    from jiuwenswarm.common import utils

    sessions, documents = tmp_path / "sessions", tmp_path / "documents"
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(builder, "get_embed_config", lambda: {})
    for name in ("JIUWEN_MEMORY_VIDEO_ROOTS", "VIDEO_ENABLED", "AUDIO_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    holder = DocumentRuntimeHolder()
    monkeypatch.setattr(pool, "DOCUMENT_RUNTIME_HOLDER", holder)
    cfg = adapter._agent_default_config()
    cfg["globals"].update(embedder_dim=64, watch_document=True)
    cfg.pop("normalizer")  # Allow the built-in video route to replace the text default.
    cfg["llm"]["default"] = "echo"
    cfg["extractor"]["default"] = "keyword"
    cfg["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    cfg["fulltext_store"] = {"default": {"target": "memory"}}
    cfg["markdown_store"] = {"default": {"target": "local", "params": {"root": str(documents)}}}
    settings = {"permissions": {"enabled": False, "mode": "manual"}, "models": {
        kind: {"enabled": True, "model_client_config": {
            "client_provider": "OpenAI", "api_key": "test", "model_name": kind,
            "api_base": "https://example.invalid/v1",
        }} for kind in ("audio", "video")
    }, "memory": {"builtin": {"provider": "jiuwen", "jiuwen": {
        "mode": "sdk", "scope_id": "local", "user_id": "developer", "sdk": cfg,
    }}}}
    processed, watchers = [], []
    original_start = LocalWatchdog.start

    def start(watcher):
        original_start(watcher)
        watchers.append(watcher)

    def process_video(_normalizer, path, run_root):
        assert path.is_relative_to(run_root) and not path.is_relative_to(sessions)
        content = path.read_text()
        processed.append(path)
        return ([{"id": "clip", "time_range": [0, 1], "visual_summary": content}],
                [{"task_id": "event", "time_span": [0, 1], "child_clip_ids": ["clip"],
                  "narrative_summary": content + " event"}])

    monkeypatch.setattr(LocalWatchdog, "start", start)
    monkeypatch.setattr(VideoNormalizer, "_run_pipeline", process_video)
    providers = []
    try:
        for scene in (first_scene, "agent", "agent"):
            rail = builder.build_jiuwen_memory_rail(
                settings, scene=scene, project="repo",
                team_binding={"team": "repo--" + "a" * 24, "member": "leader"},
            )
            assert rail is not None
            provider = rail._provider
            providers.append(provider)
            if scene == "agent":
                names = {schema["name"] for schema in provider.get_tool_schemas()}
                assert {"agent_memory_video_submit", "agent_memory_video_status"} <= names
            await provider.initialize(scope_id="local", user_id="developer",
                                      agent_id="main_agent", session_id="")
            assert provider.runtime is providers[0].runtime
            if len(providers) == 1:
                assert sessions.is_dir() and not list(sessions.iterdir())
                continue
            uploads = sessions / f"session-{len(providers)}" / "uploads"
            uploads.mkdir(parents=True)
            video = uploads / "same-name.mp4"
            video.write_text(f"Video from session {len(providers)}")
            result = json.loads(await provider.handle_tool_call(
                "agent_memory_video_submit", {"uri": video.as_uri()},
            ))
            assert "job_id" in result, result
            async with asyncio.timeout(10):
                while True:
                    status = json.loads(await provider.handle_tool_call(
                        "agent_memory_video_status", {"job_id": result["job_id"]},
                    ))
                    if status["status"] not in {"pending", "running"}:
                        break
                    await asyncio.sleep(0.02)
            assert status["status"] == "succeeded", status
            assert len(status["unit_ids"]) == 2
        assert len(watchers) == 1 and watchers[0].started
        assert len(processed) == 2 and all(not path.exists() for path in processed)
        markdown = (documents / "memory/default/MEMORY.md").read_text()
        assert "Video from session 2" in markdown and "Video from session 3" in markdown
        outside = tmp_path / "outside.mp4"
        outside.write_text("not admitted")
        denied = json.loads(await providers[-1].handle_tool_call(
            "agent_memory_video_submit", {"uri": outside.as_uri()},
        ))
        assert "error" in denied and "job_id" not in denied
    finally:
        await asyncio.gather(*(provider.shutdown() for provider in providers))
        await asyncio.to_thread(holder.close)
    assert not watchers[0].started
