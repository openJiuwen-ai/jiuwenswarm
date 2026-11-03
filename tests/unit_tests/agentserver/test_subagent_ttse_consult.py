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
        is_initialized=False,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["cn", "en"])
async def test_catalog_guidance_requires_lookup_independently_of_parent(monkeypatch, language):
    from openjiuwen.agent_evolving.ttse import render

    parent_guidance = {
        "cn": "没有相关类或附件为 `(empty)` 时直接执行。",
        "en": "If none apply or the attachment is `(empty)`, proceed.",
    }
    monkeypatch.setattr(render, "DISK_CATALOG_GUIDANCE_CN", parent_guidance["cn"])
    monkeypatch.setattr(render, "DISK_CATALOG_GUIDANCE_EN", parent_guidance["en"])
    child = child_agent()
    child.system_prompt_builder.language = language
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=SimpleNamespace(catalog_counts=dict)))
    rail.init(child)
    try:
        await rail.before_model_call(context(child))
        prompt = child.system_prompt_builder.build()
        assert parent_guidance[language] not in prompt
        if language == "cn":
            assert "第一次工具调用必须仅调用一次 `ttse_consult`" in prompt
            assert "不得因目录没有明显匹配分类或为空而跳过查询" in prompt
        else:
            assert "MUST be exactly one `ttse_consult` call and no other calls" in prompt
            assert "an unmatched or empty catalog is not a reason to skip" in prompt
    finally:
        await rail.after_invoke(context(child))
        rail.uninit(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("callable_id", [False, True])
async def test_context_session_fallback_attaches_catalog(callable_id):
    child = child_agent()
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=SimpleNamespace(catalog_counts=dict)))
    rail.init(child)
    ctx = context(child)
    ctx.session = None
    ctx.context = SimpleNamespace(session_id=(lambda: "fallback-session") if callable_id else "fallback-session")
    try:
        await rail.before_model_call(ctx)
        assert child.prompt_attachment_manager.add_section.call_args.kwargs["session_id"] == "fallback-session"
    finally:
        await rail.after_invoke(ctx)
        rail.uninit(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["cn", "en"])
@pytest.mark.parametrize("missing", ["session", "manager", "attachment_error"])
async def test_missing_catalog_does_not_claim_attachment(language, missing):
    child = child_agent()
    child.system_prompt_builder.language = language
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=SimpleNamespace(catalog_counts=dict)))
    rail.init(child)
    ctx = context(child)
    if missing == "session":
        ctx.session = None
    elif missing == "manager":
        child.prompt_attachment_manager = None
    else:
        child.prompt_attachment_manager.add_section.side_effect = RuntimeError("attachment unavailable")
    try:
        await rail.before_model_call(ctx)
        prompt = child.system_prompt_builder.build()
        if language == "cn":
            assert "未随请求提供经验分类目录" in prompt
            assert "末尾附件" not in prompt
        else:
            assert "No experience catalog is attached" in prompt
            assert "trailing attachment" not in prompt
        assert "category=all" in prompt
        assert (
            "第一次工具调用必须仅调用一次 `ttse_consult`" if language == "cn"
            else "MUST be exactly one `ttse_consult` call and no other calls"
        ) in prompt
    finally:
        await rail.after_invoke(ctx)
        rail.uninit(child)


@pytest.mark.asyncio
async def test_reused_child_refreshes_query_and_summary_log_context(caplog, tmp_path):
    caplog.set_level(logging.INFO, logger=SubagentTTSEConsultRail.__module__)
    store = TTSERecordStore(TTSEConfig(store_path=str(tmp_path / "bank.json")))
    child = child_agent()
    parent = SimpleNamespace(create_subagent=lambda *args, **kwargs: child)
    install_subagent_ttse_wiring(parent, lambda: TTSEConsultDependencies(store=store))
    parent.create_subagent("general-purpose", "first-task")
    rail = child.configured_rails()[0]
    rail.init(child)
    try:
        await rail._tool.invoke({"category": "all", "query": "test"})
        await rail.after_invoke(context(child))
        caplog.clear()
        assert parent.create_subagent(subagent_type="custom", subsession_id="second-task") is child
        assert child.configured_rails() == [rail]
        await rail.before_invoke(context(child))
        await rail._tool.invoke({"category": "all", "query": "test"})
        await rail.after_invoke(context(child))
        assert "subagent_type='custom' subsession_id='second-task'" in caplog.text
        assert "consult_start" in caplog.text and "task_summary" in caplog.text
        assert "first-task" not in caplog.text
    finally:
        rail.uninit(child)


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
    child.is_initialized = True
    registered = []
    closed_streams = []

    async def register(rail):
        await asyncio.sleep(0)
        rail.init(child)
        registered.append(rail)

    async def invoke(query):
        assert registered
        return query

    async def stream(query):
        assert registered
        try:
            yield query
        finally:
            closed_streams.append(query)

    child.is_registered_rail = lambda rail: rail in registered
    child.register_rail = AsyncMock(side_effect=register)
    child.invoke = invoke
    child.stream = stream
    parent = SimpleNamespace(create_subagent=lambda *args: child)
    install_subagent_ttse_wiring(parent, lambda: None)
    parent.create_subagent("custom", "session")
    child.register_rail.assert_not_awaited()

    async def collect():
        return [chunk async for chunk in child.stream("stream")]

    results = await asyncio.gather(child.invoke("invoke"), collect())
    assert results == ["invoke", ["stream"]]
    child.register_rail.assert_awaited_once()
    assert await child.invoke("again") == "again"
    child.register_rail.assert_awaited_once()
    early_stream = child.stream("early")
    assert await anext(early_stream) == "early"
    await early_stream.aclose()
    assert closed_streams == ["stream", "early"]
    registered[0].uninit(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["invoke", "stream"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_deferred_registration_failure_rolls_back_and_preserves_task(entrypoint, cancel, caplog):
    child = child_agent()
    child.is_initialized = True
    configured = child.configured_rails()
    fail = True
    executions = []

    async def register(rail):
        rail.init(child)
        configured.append(rail)
        if fail:
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("callback registration failed")

    async def unregister(rail):
        configured.remove(rail)
        rail.uninit(child)

    async def invoke():
        executions.append(child.ability_manager.get("ttse_consult") is not None)
        return "done"

    async def stream():
        yield await invoke()

    child.is_registered_rail = lambda rail: rail in configured
    child.register_rail = AsyncMock(side_effect=register)
    child.unregister_rail = AsyncMock(side_effect=unregister)
    child.invoke, child.stream = invoke, stream
    parent = SimpleNamespace(create_subagent=lambda *args: child)
    install_subagent_ttse_wiring(parent, lambda: TTSEConsultDependencies(store=object()))
    parent.create_subagent("custom", "failed-registration")

    async def execute():
        if entrypoint == "invoke":
            return await child.invoke()
        return [chunk async for chunk in child.stream()]

    try:
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await execute()
            assert executions == []
        else:
            assert await execute() == ("done" if entrypoint == "invoke" else ["done"])
            assert executions == [False]
            assert "deferred rail registration failed" in caplog.text
        child.unregister_rail.assert_awaited_once()
        assert child.ability_manager.get("ttse_consult") is None
        assert configured == []
        fail = False
        await execute()
        assert executions[-1] is True
    finally:
        for rail in list(configured):
            await child.unregister_rail(rail)


def test_uninit_clears_real_attachments_without_running_loop():
    from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager

    child = child_agent()
    child.prompt_attachment_manager = PromptAttachmentManager()
    store = SimpleNamespace(catalog_counts=dict)
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=store))
    rail.init(child)
    manager = child.prompt_attachment_manager
    try:
        asyncio.run(rail.before_model_call(context(child)))
        assert asyncio.run(manager.collect_for_session("child-session"))
    finally:
        rail.uninit(child)
    assert asyncio.run(manager.collect_for_session("child-session")) == []
    assert not rail._cleanup_tasks


@pytest.mark.asyncio
async def test_retrieval_uses_live_shared_store_mode(tmp_path, monkeypatch):
    config = TTSEConfig(store_path=str(tmp_path / "bank.json"), consult_retrieve_mode="bm25")
    store = TTSERecordStore(config)
    await store.add_fact("Use pytest for tests.")
    rank = AsyncMock(return_value=([], "test"))
    monkeypatch.setattr(store.index, "rank_track", rank)
    monkeypatch.setattr(store.index, "ensure_chroma", AsyncMock())
    child = child_agent()
    rail = SubagentTTSEConsultRail(lambda: TTSEConsultDependencies(store=store))
    rail.init(child)
    try:
        for mode in ("bm25", "embed", "hybrid"):
            config.consult_retrieve_mode = mode
            rank.reset_mock()
            await rail._tool.invoke({"category": "all", "query": "pytest"})
            assert rank.await_count == 2
            assert all(call.kwargs["retrieve_mode"] == mode for call in rank.await_args_list)
    finally:
        rail.uninit(child)


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
@pytest.mark.parametrize("originally_eager", [False, True])
async def test_shared_progressive_rail_survives_other_child_cleanup(originally_eager):
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        ProgressiveToolRail,
    )

    progressive = ProgressiveToolRail(eager_tools=["ttse_consult"] if originally_eager else [])
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
    assert ("ttse_consult" in progressive.eager_tools) is originally_eager


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
