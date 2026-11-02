# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""子 Agent 共享 TTSE 查询库、工具所有权及配置生命周期回归。"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.agent_evolving.ttse.config import TTSEConfig
from openjiuwen.agent_evolving.ttse.stores import TTSERecordStore
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.prompts.builder import SystemPromptBuilder

from jiuwenswarm.agents.harness.common.rails.disabled_tools_rail import (
    DisabledToolsRail,
)
from jiuwenswarm.agents.harness.common.rails.subagent_ttse_consult_rail import (
    SubagentTTSEConsultRail,
    TTSEConsultDependencies,
)
from jiuwenswarm.agents.harness.common.tools.subagent_executor.ttse import (
    install_subagent_ttse_wiring,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def child_agent(rails=None):
    # 故意使用相同 owner，验证不会因内建 Agent card 重用而串工具。
    configured = list(rails or [])
    return SimpleNamespace(
        ability_manager=AbilityManager(owner_id="same-child-card"),
        system_prompt_builder=SystemPromptBuilder(),
        prompt_attachment_manager=SimpleNamespace(
            add_section=AsyncMock(),
            clear_section=AsyncMock(),
        ),
        configured_rails=lambda: configured,
        add_rail=configured.append,
        _initialized=False,
    )


def context(child):
    return SimpleNamespace(
        agent=child,
        session=SimpleNamespace(get_session_id=lambda: "child-session"),
        inputs=SimpleNamespace(
            system_prompt_builder=child.system_prompt_builder, tools=[]
        ),
    )


def runtime_context(child, query, state):
    ctx = context(child)
    ctx.inputs.query = query
    ctx.session.get_state = state.get
    ctx.session.update_state = state.update
    return ctx


@pytest.mark.asyncio
async def test_shared_store_real_query_concurrent_owners_and_cleanup(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger=SubagentTTSEConsultRail.__module__)
    store = TTSERecordStore(TTSEConfig(store_path=str(tmp_path / "bank.json")))
    await store.add_fact("项目测试必须使用 pytest 执行。")
    dependencies = TTSEConsultDependencies(store=store)
    children = [child_agent(), child_agent()]
    rails = [SubagentTTSEConsultRail(lambda: dependencies) for _ in children]
    for child, rail in zip(children, rails):
        child.configured_rails().append(rail)
        rail.init(child)
    tools = [
        Runner.resource_mgr.get_tool(c.ability_manager.get("ttse_consult").id)
        for c in children
    ]
    try:
        assert tools[0] is not tools[1]
        assert tools[0].card.id != tools[1].card.id
        results = await asyncio.gather(
            *[
                tool.invoke({"category": "all", "query": "项目测试 pytest"})
                for tool in tools
            ]
        )
        assert all("pytest" in result for result in results)
        messages = [
            r.getMessage()
            for r in caplog.records
            if r.name == SubagentTTSEConsultRail.__module__
        ]
        assert sum("consult_start " in message for message in messages) == 2
        assert sum("consult_completed " in message for message in messages) == 2
        assert all(
            "elapsed_ms=" in message
            for message in messages
            if "consult_completed " in message
        )
        rails[0].uninit(children[0])
        assert Runner.resource_mgr.get_tool(tools[0].card.id) is None
        assert Runner.resource_mgr.get_tool(tools[1].card.id) is tools[1]
        assert "pytest" in await tools[1].invoke({"category": "all", "query": "测试"})
    finally:
        for child, rail in zip(children, rails):
            if rail._agent is not None:
                rail.uninit(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("lazy", [False, True])
async def test_visibility_catalog_hot_reload_and_disable(tmp_path, lazy):
    store = TTSERecordStore(TTSEConfig(store_path=str(tmp_path / "bank.json")))
    current = TTSEConsultDependencies(store=store)
    progressive = SimpleNamespace(eager_tools=["tools_search"])
    child = child_agent([progressive] if lazy else [])
    rail = SubagentTTSEConsultRail(lambda: current)
    child.configured_rails().append(rail)
    rail.init(child)
    ctx = context(child)
    try:
        await rail.before_invoke(ctx)
        first = rail._tool
        await rail.before_model_call(ctx)
        assert child.ability_manager.get("ttse_consult") is first.card
        assert "ttse_consult" in child.system_prompt_builder.build()
        assert child.prompt_attachment_manager.add_section.await_count == 1
        if lazy:
            assert "ttse_consult" in progressive.eager_tools
        current = TTSEConsultDependencies(store=store, default_top_k=2)
        await rail.before_invoke(ctx)
        assert rail._tool is not first
        assert Runner.resource_mgr.get_tool(first.card.id) is None
        current = None
        ctx.inputs.tools = [SimpleNamespace(name="ttse_consult")]
        await rail.before_invoke(ctx)
        await rail.before_model_call(ctx)
        assert child.ability_manager.get("ttse_consult") is None
        assert "ttse_consult" not in child.system_prompt_builder.build()
        assert ctx.inputs.tools == []
        child.prompt_attachment_manager.clear_section.assert_awaited_once()
        if lazy:
            assert "ttse_consult" not in progressive.eager_tools
        current = TTSEConsultDependencies(store=store)
        await rail.before_invoke(ctx)
        assert rail._tool is not None
    finally:
        rail.uninit(child)
        if rail._cleanup_tasks:
            await asyncio.gather(*rail._cleanup_tasks)


def test_explicit_disabled_tools_and_missing_dependencies():
    child = child_agent([DisabledToolsRail(["ttse_consult"])])
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=object()))
    rail.init(child)
    assert child.ability_manager.get("ttse_consult") is None
    rail.uninit(child)


@pytest.mark.asyncio
async def test_cleanup_preserves_replacement_and_cancellation():
    store = SimpleNamespace(catalog_counts=dict)
    child = child_agent()
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=store))
    rail.init(child)
    replacement = rail._tool.card.model_copy(update={"id": "replacement"})
    child.ability_manager.remove("ttse_consult")
    child.ability_manager.add(replacement)
    rail.uninit(child)
    assert child.ability_manager.get("ttse_consult") is replacement

    def cancelled():
        raise asyncio.CancelledError

    rail = SubagentTTSEConsultRail(cancelled)
    with pytest.raises(asyncio.CancelledError):
        rail.init(child)


@pytest.mark.parametrize(
    "agent_type",
    [
        "general-purpose",
        "research_agent",
        "browser_agent",
        "statusline-setup",
        "custom",
    ],
)
def test_creation_wiring_preserves_arguments_and_is_idempotent(agent_type):
    child = child_agent()
    calls = []

    def create(*args, **kwargs):
        calls.append((args, kwargs))
        return child

    parent = SimpleNamespace(create_subagent=create)
    assert install_subagent_ttse_wiring(parent, lambda: None)
    wrapped = parent.create_subagent
    assert install_subagent_ttse_wiring(parent, lambda: None)
    assert parent.create_subagent is wrapped
    model = object()
    assert parent.create_subagent(agent_type, "session", model=model) is child
    parent.create_subagent(agent_type, "session")
    assert len(child.configured_rails()) == 1
    rail = child.configured_rails()[0]
    assert rail._subagent_type == agent_type
    assert rail._subsession_id == "session"
    assert calls[0] == ((agent_type, "session"), {"model": model})


@pytest.mark.asyncio
async def test_already_initialized_child_registers_before_execution():
    child = child_agent()
    child._initialized = True
    child._ensure_initialized = AsyncMock()
    child.register_rail = AsyncMock(side_effect=lambda rail: rail.init(child))
    parent = SimpleNamespace(create_subagent=lambda *args: child)
    install_subagent_ttse_wiring(parent, lambda: None)
    parent.create_subagent("custom", "session")
    await asyncio.gather(child._ensure_initialized(), child._ensure_initialized())
    child.register_rail.assert_awaited_once()


@pytest.mark.parametrize("mode", ["agent", "flash", "team"])
def test_adapter_dependency_provider_tracks_parent_and_switches(monkeypatch, mode):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._session_instance_mode = mode
    config = {"enabled": True, "inject_enabled": True}
    monkeypatch.setattr(adapter, "_resolved_ttse_config", lambda: config)
    store = object()
    adapter._ttse_rail = SimpleNamespace(
        _ttse_store=store,
        _ttse_config=TTSEConfig(consult_top_k=3),
    )
    assert adapter._subagent_ttse_consult_dependencies().store is store
    assert adapter._subagent_ttse_consult_dependencies().default_top_k == 3
    adapter._config_cache["disabled_tools"] = ["ttse_consult"]
    assert adapter._subagent_ttse_consult_dependencies() is None
    adapter._config_cache.clear()
    adapter._disabled_tools_rail = DisabledToolsRail(["ttse_consult"])
    assert adapter._subagent_ttse_consult_dependencies() is None
    adapter._disabled_tools_rail = None
    config["inject_enabled"] = False
    assert adapter._subagent_ttse_consult_dependencies() is None
    config["inject_enabled"] = True
    adapter._is_code_agent = True
    assert adapter._subagent_ttse_consult_dependencies() is None
    adapter._is_code_agent = False
    adapter._ttse_rail = None
    assert adapter._subagent_ttse_consult_dependencies() is None


@pytest.mark.asyncio
async def test_shared_progressive_rail_survives_other_child_cleanup():
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )

    progressive = ProgressiveToolRail(eager_tools=[])
    children = [child_agent([progressive]), child_agent([progressive])]
    rails = [
        SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=object()))
        for _ in children
    ]
    for child, rail in zip(children, rails):
        rail.init(child)
    try:
        await rails[0].after_invoke(context(children[0]))
        assert "ttse_consult" in progressive.eager_tools
    finally:
        for child, rail in zip(children, rails):
            rail.uninit(child)
    assert "ttse_consult" not in progressive.eager_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("consult", [False, True])
@pytest.mark.parametrize("mode", ["agent", "flash"])
async def test_scripted_model_controls_consult_invocation(tmp_path, monkeypatch, consult, mode):
    """Only model tool calls trigger retrieval; the runtime does not enforce the prompt."""

    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
    )
    from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.schema.config import SubAgentConfig

    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )

    store = TTSERecordStore(TTSEConfig(store_path=str(tmp_path / "bank.json")))
    await store.add_fact("部署前必须运行 pytest。")
    events = []

    async def business():
        events.append("business")
        return "completed"

    async def scripted(self, messages, *, tools=None, **kwargs):
        events.append("model")
        model_calls = events.count("model")
        if model_calls == 1:
            assert "ttse_consult" in [t.name for t in tools]
            prompt = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "ttse_consult" in prompt
            assert "经验目录" in prompt
            assert "source=runtime" not in prompt
            assert "pytest" not in prompt
            assert child._jiuwenswarm_ttse_consult_rail._consult_calls == 0
            if consult:
                return AssistantMessage(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="consult-call",
                            type="function",
                            name="ttse_consult",
                            arguments='{"category": "all", "query": "pytest"}',
                        )
                    ],
                )
        if model_calls == (2 if consult else 1):
            if consult:
                prompt = "\n".join(str(getattr(m, "content", "")) for m in messages)
                assert "部署前必须运行 pytest。" in prompt
                assert child._jiuwenswarm_ttse_consult_rail._consult_calls == 1
            return AssistantMessage(
                content="",
                tool_calls=[
                    ToolCall(
                        id="business-call",
                        type="function",
                        name="business",
                        arguments="{}",
                    )
                ],
            )
        return AssistantMessage(content="done")

    monkeypatch.setattr(Model, "invoke", scripted)
    model = Model(
        ModelClientConfig(
            client_provider="OpenAI",
            api_key="offline",
            api_base="https://example.invalid",
        ),
        ModelRequestConfig(model="offline"),
    )
    tool = LocalFunction(
        ToolCard(
            name="business",
            id="ttse-test-business",
            input_params={
                "type": "object",
                "properties": {},
            },
        ),
        business,
    )
    parent = create_deep_agent(
        model,
        workspace=str(tmp_path),
        add_general_purpose_agent=False,
        enable_read_image_multimodal=False,
        subagents=[
            SubAgentConfig(
                agent_card=AgentCard(name="custom", id=f"ttse-test-child-{mode}-{consult}"),
                system_prompt="Execute the assigned task.",
                tools=[tool],
                enable_task_loop=False,
                rails=[ProgressiveToolRail(eager_tools=["business"])],
            )
        ],
    )
    if mode == "flash":
        from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter

        adapter = JiuwenSwarmFlashAdapter()
    else:
        adapter = JiuWenSwarmDeepAdapter()
    adapter._session_instance_mode = mode
    monkeypatch.setattr(
        adapter, "_resolved_ttse_config", lambda: {"enabled": True, "inject_enabled": True}
    )
    adapter._ttse_rail = SimpleNamespace(_ttse_store=store, _ttse_config=TTSEConfig())
    install_subagent_ttse_wiring(parent, adapter._subagent_ttse_consult_dependencies)
    child = parent.create_subagent("custom", f"ttse-scripted-{mode}-{consult}")
    try:
        await child.invoke({"query": "部署任务"})
        assert events == (["model"] if consult else []) + ["model", "business", "model"]
        assert child.ability_manager.get("ttse_consult") is None
    finally:
        for rail in list(child.configured_rails()):
            await child.unregister_rail(rail)
        child.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_retrieval_failure_does_not_abort_task(caplog):
    caplog.set_level(logging.INFO, logger=SubagentTTSEConsultRail.__module__)
    child = child_agent()
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=object()))
    rail.init(child)
    try:
        result = await rail._tool.invoke({"category": "all", "query": "test"})
        assert "continue with the current task" in result
        assert "consult_start " in caplog.text
        assert "consult_failed " in caplog.text
        assert "consult_completed " not in caplog.text
        await rail.after_invoke(context(child))
        assert "consult_calls=1" in caplog.text
        assert child.ability_manager.get("ttse_consult") is None
        await rail.before_invoke(context(child))
        assert rail._tool is not None
    finally:
        rail.uninit(child)


@pytest.mark.asyncio
async def test_task_summary_identifies_no_consultation(caplog):
    caplog.set_level(logging.INFO, logger=SubagentTTSEConsultRail.__module__)
    child = child_agent()
    rail = SubagentTTSEConsultRail(
        lambda: None,
        subagent_type="research_agent",
        subsession_id="child-log-test",
    )
    rail.init(child)
    await rail.before_invoke(context(child))
    await rail.after_invoke(context(child))
    assert (
        "task_summary subagent_type='research_agent' subsession_id='child-log-test' consult_calls=0"
        in caplog.text
    )
    assert "consult_start " not in caplog.text
    rail.uninit(child)


def test_custom_agent_explicit_disallow_is_preserved(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.agent_adapter.deep import subagent_load
    from jiuwenswarm.server.runtime.agent_config_service import AgentDefinition

    monkeypatch.setattr(subagent_load, "get_config", dict)
    definition = AgentDefinition(
        name="restricted",
        description="test",
        prompt="test",
        source="project",
        tools=["*"],
        disallowed_tools=["ttse_consult"],
    )
    spec = interface_deep._agent_def_to_subagent_config(
        definition,
        MagicMock(),
        str(tmp_path),
    )
    assert any(
        "ttse_consult" in getattr(rail, "_disabled_tools", ()) for rail in spec.rails
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("language", ["cn", "en"])
async def test_lifecycle_exposes_catalog_without_invoking_consult(tmp_path, resume, language):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    store = TTSERecordStore(TTSEConfig(store_path=str(tmp_path / "bank.json")))
    await store.add_fact("Experience body must only be returned by a model lookup.")
    child = child_agent()
    child.system_prompt_builder.language = language
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=store))
    rail.init(child)
    state = {}
    ctx = runtime_context(child, InteractiveInput() if resume else "new task", state)
    try:
        invoke = rail._tool.invoke = AsyncMock()
        await rail.before_invoke(ctx)
        await rail.before_model_call(ctx)
        await rail.before_model_call(ctx)
        invoke.assert_not_awaited()
        assert rail._consult_calls == 0
        assert state == {}
        assert "ttse_consult" in [tool.name for tool in ctx.inputs.tools]
        attachment = child.prompt_attachment_manager.add_section.call_args.kwargs["content"]
        assert "Experience body must only" not in attachment
        assert "source=runtime" not in attachment
        prompt = child.system_prompt_builder.build()
        if language == "cn":
            assert "第一次工具调用必须仅调用一次 `ttse_consult`" in prompt
            assert "必须收到并阅读查询结果后" in prompt
            assert "不要求每个任务都查询" not in prompt
        else:
            assert "MUST be exactly one `ttse_consult` call and no other calls" in prompt
            assert "Receive and read its result before loading skills" in prompt
            assert "not required for every task" not in prompt
        await rail.after_invoke(ctx)
        assert child.ability_manager.get("ttse_consult") is None
    finally:
        rail.uninit(child)
        if rail._cleanup_tasks:
            await asyncio.gather(*rail._cleanup_tasks)
