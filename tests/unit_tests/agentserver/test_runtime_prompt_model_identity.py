# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime model identity belongs to the executing agent, not its session."""

from types import SimpleNamespace

import pytest

from openjiuwen.agent_teams.models.allocator import _selectable_route_tags
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.schema.config import IntelliRouterConfig, IntelliRouterDeploymentConfig
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager

from jiuwenswarm.agents.harness.common.rails import runtime_prompt_rail as runtime_module
from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import RuntimePromptRail


@pytest.fixture
def runtime_state(tmp_path, monkeypatch):
    state_path = tmp_path / "session.yaml"
    state_path.write_text(
        "model: GLM-5.3\navailable_models: [GLM-5.3, deepseek-flash]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_module, "get_runtime_state_path", lambda session_id: state_path)
    monkeypatch.setattr(RuntimePromptRail, "_configured_model_names", staticmethod(lambda: []))
    return state_path


def _rail(language="cn"):
    manager = PromptAttachmentManager()
    rail = RuntimePromptRail(language=language)
    # init binds the outer agent; callbacks may belong to its inner ReActAgent.
    rail.init(SimpleNamespace(
        config=SimpleNamespace(model_name="GLM-5.3"),
        prompt_attachment_manager=manager,
        system_prompt_builder=SystemPromptBuilder(language=language),
    ))
    rail.set_model_name("GLM-5.3")
    return rail, manager


async def _render(rail, manager, agent):
    session = SimpleNamespace(get_session_id=lambda: "shared-session")
    await rail.before_model_call(AgentCallbackContext(agent=agent, session=session))
    items = await manager.collect_for_session("shared-session")
    return manager.render(items)


@pytest.mark.asyncio
@pytest.mark.parametrize("language, label", [("cn", "当前模型："), ("en", "Current model: ")])
@pytest.mark.parametrize("config_shape", ["live", "deep_agent", "react_agent"])
async def test_member_model_overrides_session_and_outer_agent(runtime_state, language, label, config_shape):
    rail, manager = _rail(language)
    request = ModelRequestConfig(model="deepseek-flash")
    model = SimpleNamespace(model_config=request)
    if config_shape == "live":
        agent = SimpleNamespace(get_llm=lambda: model, config=SimpleNamespace(model_name="stale-model"))
    elif config_shape == "deep_agent":
        agent = SimpleNamespace(config=SimpleNamespace(model=model))
    else:
        agent = SimpleNamespace(config=SimpleNamespace(model_config_obj=request, model_name="stale-model"))

    rendered = await _render(rail, manager, agent)

    assert f"{label}deepseek-flash" in rendered
    assert f"{label}GLM-5.3" not in rendered
    assert "model: GLM-5.3" in runtime_state.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_real_agent_model_changes_refresh_identity_without_changing_other_members(runtime_state):
    rail, manager = _rail()
    other_rail, other_manager = _rail()
    agent = ReActAgent(AgentCard(id="model-identity-member", name="model-identity-member"))
    other = SimpleNamespace(config=SimpleNamespace(model_name="GLM-5.3"))
    client = ModelClientConfig(client_provider="OpenAI", api_base="https://example.invalid/v1", api_key="test")

    for name in ("deepseek-flash", "GLM-5.3", "deepseek-flash"):
        agent.set_llm(Model(model_client_config=client, model_config=ModelRequestConfig(model=name)))

        assert f"当前模型：{name}" in await _render(rail, manager, agent)
        assert "当前模型：GLM-5.3" in await _render(other_rail, other_manager, other)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source, expected",
    [("session", "GLM-5.3"), ("setter", "request-model"), ("config", "configured-model"), ("none", "unknown")],
)
async def test_missing_agent_model_keeps_existing_fallbacks(runtime_state, monkeypatch, source, expected):
    rail, manager = _rail()
    rail.set_model_name("request-model" if source in ("session", "setter") else "")
    if source != "session":
        runtime_state.unlink()
    if source == "config":
        monkeypatch.setattr(
            RuntimePromptRail, "_configured_model_names", staticmethod(lambda: ["configured-model"]),
        )

    assert f"当前模型：{expected}" in await _render(rail, manager, SimpleNamespace())


@pytest.mark.asyncio
async def test_logical_group_model_does_not_claim_the_session_model(runtime_state):
    rail, manager = _rail()
    agent = SimpleNamespace(config=SimpleNamespace(model_config_obj=ModelRequestConfig(model="*")))

    rendered = await _render(rail, manager, agent)

    assert "当前模型：*" in rendered
    assert "当前模型：GLM-5.3" not in rendered


@pytest.mark.asyncio
async def test_unavailable_live_model_uses_member_config_before_session(runtime_state):
    rail, manager = _rail()

    def unavailable_model():
        raise ValueError("model not initialized")

    agent = SimpleNamespace(
        get_llm=unavailable_model,
        config=SimpleNamespace(model_config_obj=ModelRequestConfig(model="deepseek-flash")),
    )

    assert "当前模型：deepseek-flash" in await _render(rail, manager, agent)


def _deployment(route_id, model_name, fallback_tag):
    return IntelliRouterDeploymentConfig(
        route_id=route_id, model_name=model_name, provider="openai", fallback_tag=fallback_tag,
    )


def _group_agent(router):
    model = SimpleNamespace(
        model_config=ModelRequestConfig(model="*"),
        model_client_config=SimpleNamespace(intelli_router=router),
    )
    return SimpleNamespace(get_llm=lambda: model)


def _group_router(*extra_deployments, **kwargs):
    return IntelliRouterConfig(
        deployments=[
            _deployment("route_0", "GLM-5.3-Flash", "GLM-5.3-Flash"),
            _deployment("route_1", "GLM-5.3", "GLM-5.3"),
            *extra_deployments,
        ],
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language, label", [("cn", "可用模型："), ("en", "Available models: ")])
async def test_model_group_lists_only_group_models(runtime_state, language, label):
    rail, manager = _rail(language)

    rendered = await _render(rail, manager, _group_agent(_group_router()))

    assert f"{label}GLM-5.3, GLM-5.3-Flash\n" in rendered
    assert "deepseek-flash" not in rendered


@pytest.mark.asyncio
async def test_pinned_model_group_member_lists_only_its_model(runtime_state):
    rail, manager = _rail()
    router = _group_router(strategy="tag-filtered", strategy_kwargs={"fallback_tag": "GLM-5.3"})

    rendered = await _render(rail, manager, _group_agent(router))

    assert "可用模型：GLM-5.3\n" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_kwargs", [{}, {"fallback_tag": "GLM-5.3-FLASH"}, {"fallback_tag": "GLM-5.3"}])
async def test_group_model_names_match_team_selectable_models(strategy_kwargs):
    # Letter-case handling differs across openjiuwen versions; whatever the
    # installed version does, the prompt must agree with spawn_teammate.
    strategy = "tag-filtered" if strategy_kwargs else "ordered-failover"
    router = _group_router(
        _deployment("route_2", "glm-5.3-flash", "glm-5.3-flash"),
        strategy=strategy, strategy_kwargs=strategy_kwargs,
    )
    ctx = AgentCallbackContext(agent=_group_agent(router), session=None)

    names = RuntimePromptRail._agent_group_model_names(ctx)

    assert names == _selectable_route_tags(router.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_kind", ["single_model", "untagged_group", "unavailable"])
async def test_non_group_agent_keeps_session_model_list(runtime_state, agent_kind):
    rail, manager = _rail()
    if agent_kind == "single_model":
        client = ModelClientConfig(client_provider="OpenAI", api_base="https://example.invalid/v1", api_key="test")
        agent = SimpleNamespace(get_llm=lambda: Model(
            model_client_config=client, model_config=ModelRequestConfig(model="deepseek-flash"),
        ))
    elif agent_kind == "untagged_group":
        agent = _group_agent(IntelliRouterConfig(deployments=[_deployment("route_0", "GLM-5.3", None)]))
    else:
        def unavailable_model():
            raise ValueError("model not initialized")

        agent = SimpleNamespace(get_llm=unavailable_model)

    assert "可用模型：GLM-5.3, deepseek-flash\n" in await _render(rail, manager, agent)
