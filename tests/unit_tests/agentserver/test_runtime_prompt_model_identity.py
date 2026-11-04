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
async def test_logical_group_model_does_not_claim_the_session_model(runtime_state, monkeypatch):
    monkeypatch.setattr(runtime_module, "_routed_model_placeholder", lambda: "<routed-model/>")
    rail, manager = _rail()
    agent = SimpleNamespace(config=SimpleNamespace(model_config_obj=ModelRequestConfig(model="*")))

    rendered = await _render(rail, manager, agent)

    assert "当前模型：<routed-model/>" in rendered
    assert "当前模型：*" not in rendered
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


class _Catalog:
    """ModelCatalog stand-in serving one named group."""

    def get_group(self, group_id):
        if group_id != "mgp_1":
            raise KeyError(group_id)
        return {"model_group_id": "mgp_1", "display_name": "test"}

    def group_display_name(self, group_id):
        return str(self.get_group(group_id).get("display_name") or "").strip() or group_id



_PLACEHOLDER = "<routed-model/>"


def _placeholder(monkeypatch, value):
    """Pin whether the installed openjiuwen fills a routed-model placeholder."""
    monkeypatch.setattr(runtime_module, "_routed_model_placeholder", lambda: value)


def _group(strategy="ordered-failover", group_id="mgp_1"):
    return _group_agent(_group_router(model_group_id=group_id, strategy=strategy))


async def _render_in(rail, manager, agent, session):
    await rail.before_model_call(AgentCallbackContext(agent=agent, session=session))
    items = await manager.collect_for_session("shared-session")
    return manager.render(items)


class _Session:
    """Weak-referenceable session, like the core Session."""

    @staticmethod
    def get_session_id():
        return "shared-session"


def _session():
    return _Session()


@pytest.mark.asyncio
@pytest.mark.parametrize("language, label", [("cn", "当前模型："), ("en", "Current model: ")])
async def test_first_call_leaves_the_placeholder_for_the_router(runtime_state, monkeypatch, language, label):
    """Before any call is routed, the router fills in the model it routes to."""
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)
    _placeholder(monkeypatch, _PLACEHOLDER)
    rail, manager = _rail(language)

    rendered = await _render_in(rail, manager, _group(), _session())

    assert f"{label}{_PLACEHOLDER}\n" in rendered


@pytest.mark.asyncio
async def test_every_call_leaves_the_placeholder_for_the_router(runtime_state, monkeypatch):
    """Each call names its own routed model, never an earlier call's."""
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)
    _placeholder(monkeypatch, _PLACEHOLDER)
    rail, manager = _rail()
    session = _session()

    for _ in range(3):
        assert f"当前模型：{_PLACEHOLDER}\n" in await _render_in(rail, manager, _group(), session)


@pytest.mark.asyncio
async def test_model_group_agent_never_claims_the_session_model(runtime_state, monkeypatch):
    """A callback with no visible model still never names the session model."""
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)
    _placeholder(monkeypatch, _PLACEHOLDER)
    rail, manager = _rail()
    session = _session()
    await _render_in(rail, manager, _group(), session)

    rendered = await _render_in(rail, manager, SimpleNamespace(), session)

    assert f"当前模型：{_PLACEHOLDER}\n" in rendered
    assert "当前模型：GLM-5.3" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("language, expected", [("cn", "当前模型：模型组 test"), ("en", "Current model: model group test")])
async def test_without_router_fill_the_first_call_names_the_group(runtime_state, monkeypatch, language, expected):
    """With an openjiuwen that cannot fill the placeholder, the group is named."""
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)
    _placeholder(monkeypatch, "")
    rail, manager = _rail(language)

    rendered = await _render_in(rail, manager, _group(), _session())

    assert expected in rendered
    assert "*" not in rendered.split("\n- ", 2)[1]


@pytest.mark.asyncio
async def test_unknown_model_group_falls_back_to_its_id(runtime_state, monkeypatch):
    """A group missing from the catalog is named by its id."""
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)
    _placeholder(monkeypatch, "")
    rail, manager = _rail()

    rendered = await _render_in(rail, manager, _group(group_id="mgp_other"), _session())

    assert "当前模型：模型组 mgp_other" in rendered


@pytest.mark.asyncio
async def test_outer_agent_renders_the_same_runtime_state_as_the_inner_one(runtime_state, monkeypatch):
    """before_invoke on the outer agent reads the inner agent's model, so no extra delta is persisted."""
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.ModelCatalog", _Catalog)
    _placeholder(monkeypatch, _PLACEHOLDER)
    rail, manager = _rail()
    inner = _group()
    inner.config = SimpleNamespace(model_client_config=inner.get_llm().model_client_config)
    outer = SimpleNamespace(config=SimpleNamespace(model=None), react_agent=inner)
    session = _session()

    await rail.before_invoke(AgentCallbackContext(agent=outer, session=session))
    from_outer = manager.render(await manager.collect_for_session("shared-session"))
    from_inner = await _render_in(rail, manager, inner, session)

    assert from_outer == from_inner
    assert "可用模型：GLM-5.3, GLM-5.3-Flash" in from_outer


@pytest.mark.asyncio
async def test_outer_agent_of_a_plain_model_never_reads_the_inner_model(runtime_state):
    """Outside a model group the outer agent is rendered as before; the inner model is not touched."""
    rail, manager = _rail()

    def _must_not_load():
        raise AssertionError("a plain agent's inner model must not be loaded")

    inner = SimpleNamespace(
        config=SimpleNamespace(model_client_config=SimpleNamespace(intelli_router=None)),
        get_llm=_must_not_load,
    )
    outer = SimpleNamespace(config=SimpleNamespace(model=None), react_agent=inner)

    await rail.before_invoke(AgentCallbackContext(agent=outer, session=_session()))
    rendered = manager.render(await manager.collect_for_session("shared-session"))

    assert "当前模型：GLM-5.3\n" in rendered
    assert "routed-model" not in rendered
