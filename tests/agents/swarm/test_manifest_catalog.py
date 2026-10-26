# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the swarm harness-element manifest catalog.

Covers the serializable descriptor catalog: parity with the registry name
constants and the openjiuwen registries, reflective factory resolution, params
schema validation, JSON round-trip, idempotent registration, interface-method
introspection, and the unification of class rails with factory rails.
"""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from openjiuwen.harness.schema import deep_agent_spec as das
from openjiuwen.agent_teams.schema.deep_agent_spec import RailSpec

from jiuwenswarm.agents.swarm import register_swarm_providers, registry
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import LocalSectionName
from jiuwenswarm.server.runtime.a2ui.config import A2UIConfig
from openjiuwen.agent_teams.harness.manifest import (
    ElementKind,
    HarnessElementDescriptor,
    factory_ref,
    get_catalog,
    list_elements,
    register_from_catalog,
    resolve_factory,
)

logger = logging.getLogger(__name__)


def _registry_swarm_names() -> set[str]:
    """Collect every ``swarm.*`` element-name constant exported by ``registry``."""
    return {
        value
        for value in vars(registry).values()
        if isinstance(value, str) and value.startswith("swarm.")
    }


def _swarm_keys(registry_dict: dict) -> set[str]:
    """Return the ``swarm.*`` keys of an openjiuwen provider/type registry."""
    return {key for key in registry_dict if key.startswith("swarm.")}


def _swarm_catalog() -> dict[str, HarnessElementDescriptor]:
    """Return only the ``swarm.*`` descriptors from the shared global catalog.

    The catalog is process-global and also holds openjiuwen's built-in elements
    (``task_planning`` / ``team.tool`` / ...), so swarm tests scope to the
    ``swarm.*`` namespace they own.
    """
    return {
        name: descriptor
        for name, descriptor in get_catalog().items()
        if name.startswith("swarm.")
    }


class _FakePromptBuilder:
    def __init__(self) -> None:
        self.language = "cn"
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def test_every_registry_name_has_descriptor() -> None:
    """The catalog covers exactly the registry's ``swarm.*`` name constants."""
    register_swarm_providers()
    names = _registry_swarm_names()
    catalog = _swarm_catalog()

    for name in names:
        assert name in catalog, f"missing descriptor for {name}"
    extra = set(catalog) - names
    assert not extra, f"descriptors without a registry constant: {extra}"
    logger.info("parity: %d registry names == %d descriptors", len(names), len(catalog))


def test_catalog_matches_openjiuwen_registries() -> None:
    """Every descriptor is registered into the matching openjiuwen registry."""
    register_swarm_providers()

    for descriptor in _swarm_catalog().values():
        if descriptor.kind is ElementKind.TOOL:
            assert descriptor.name in das._TOOL_PROVIDER_REGISTRY, descriptor.name
        elif descriptor.kind is ElementKind.RAIL:
            assert descriptor.name in das._RAIL_PROVIDER_REGISTRY, descriptor.name
        elif descriptor.kind is ElementKind.SUBAGENT:
            assert descriptor.name in das._SUBAGENT_PROVIDER_REGISTRY, descriptor.name


def test_factory_ref_resolves_for_every_descriptor() -> None:
    """Each ``factory_ref`` resolves back to a callable/class with a stable ref."""
    for descriptor in _swarm_catalog().values():
        target = resolve_factory(descriptor.factory_ref)
        assert callable(target), descriptor.name
        assert factory_ref(target) == descriptor.factory_ref, descriptor.name


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        (
            registry.CODE_CONFIRM_INTERRUPT,
            {"tool_names": ["switch_mode"]},
            {"tool_names": 123},
        ),
        (registry.MEMBER_SKILL_TOOLKIT, {"skills": ["alpha"]}, {"skills": 7}),
        (registry.EXPLORE_AGENT, {"max_iterations": 20}, {"max_iterations": "many"}),
        (
            registry.PERMISSION_INTERRUPT,
            {"permissions_config": {"enabled": True}, "model_name": "gpt-4"},
            {"model_name": 123},
        ),
        (registry.CODE_SKILL_USE, {"skill_mode": "all"}, {"skill_mode": 5}),
    ],
)
def test_input_schema_validates(name: str, good: dict, bad: dict) -> None:
    """Parameterized elements expose an input model that validates good/bad input."""
    descriptor = get_catalog()[name]
    assert descriptor.input_model_ref is not None, name

    model = resolve_factory(descriptor.input_model_ref)
    model.model_validate(good)
    with pytest.raises(ValidationError):
        model.model_validate(bad)
    assert descriptor.input_schema == model.model_json_schema()


def test_every_input_schema_property_is_source_tagged() -> None:
    """Every input field declares its source (params / context) in the schema."""
    for descriptor in _swarm_catalog().values():
        for prop_name, prop in descriptor.input_schema.get("properties", {}).items():
            where = f"{descriptor.name}.{prop_name}"
            assert prop.get("source") in {"params", "context"}, where
            if prop["source"] == "context":
                assert "context_attr" in prop or "resolver_ref" in prop, where


def test_every_resolver_ref_resolves() -> None:
    """Every context resolver_ref reflects back to a callable."""
    for descriptor in _swarm_catalog().values():
        for prop in descriptor.input_schema.get("properties", {}).values():
            ref = prop.get("resolver_ref")
            if ref is not None:
                assert callable(resolve_factory(ref)), ref


def test_input_resolve_extracts_from_params_and_context() -> None:
    """``Input.resolve`` pulls each field from params / context as declared."""
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers.code_rails import CodeProjectMemoryInput
    from jiuwenswarm.agents.swarm.providers.member_rails import RuntimePromptInput

    ctx = SwarmBuildContext(language="cn", channel="web", project_dir="/tmp/proj")

    prompt = RuntimePromptInput.resolve({}, ctx)
    assert (prompt.language, prompt.channel) == ("cn", "web")

    # Context-sourced field (project_dir resolver) plus a params-sourced field.
    memory = CodeProjectMemoryInput.resolve({}, ctx)
    assert memory.project_dir == "/tmp/proj"
    assert memory.additional_directories == []
    assert CodeProjectMemoryInput.resolve(
        {"additional_directories": ["/x"]}, ctx
    ).additional_directories == ["/x"]


def test_attribute_fields_are_params_env_fields_are_context() -> None:
    """Config-derived attributes are params; per-request runtime values are context."""
    catalog = get_catalog()
    expected = {
        registry.CODE_SKILL_USE: {"skill_mode": "params"},
        registry.PERMISSION_INTERRUPT: {
            "permissions_config": "params",
            "model_name": "params",
        },
        registry.CODE_CODING_MEMORY: {
            "embed_config": "params",
            "project_dir": "context",
            "workspace_root": "context",
        },
        registry.SEND_FILE: {"channels_config": "params", "channel_id": "context"},
        registry.CONTEXT_PROCESSOR: {
            "context_engine_enabled": "params",
            "context_engine_config": "params",
        },
        registry.TEAM_SKILL_EVOLUTION: {
            "evolution_model_config": "params",
            "review_trigger": "params",
            "team_skills_dir": "context",
        },
        registry.TEAM_SKILL_CREATE: {"skill_create": "params"},
    }
    for name, fields in expected.items():
        props = catalog[name].input_schema["properties"]
        for field, want in fields.items():
            assert props[field]["source"] == want, f"{name}.{field}"


def test_config_specs_bakes_attribute_params() -> None:
    """config_specs projects config-derived attributes into spec ``params``."""
    from openjiuwen.harness.rails import SkillUseRail

    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs

    config = {
        "react": {"skill_mode": SkillUseRail.SKILL_MODE_AUTO_LIST},
        "permissions": {"enabled": True},
        "models": {"default": {"model_client_config": {"model_name": "gpt-4o"}}},
    }
    # PERMISSION_INTERRUPT is excluded from code-profile rails;
    # when enable_permissions=True the leader gets TEAM_PERMISSION_POLICY instead.
    rails_no_perm, _ = build_member_capability_specs(config, "code.team", "leader")
    by_type_no_perm = {spec.type: spec.params for spec in rails_no_perm}
    assert registry.PERMISSION_INTERRUPT not in by_type_no_perm

    rails, _ = build_member_capability_specs(config, "code.team", "leader", enable_permissions=True)
    by_type = {spec.type: spec.params for spec in rails}

    assert (
        by_type[registry.CODE_SKILL_USE]["skill_mode"]
        == SkillUseRail.SKILL_MODE_AUTO_LIST
    )
    assert registry.PERMISSION_INTERRUPT not in by_type
    assert by_type[registry.TEAM_PERMISSION_POLICY]["permissions_config"] == {
        "enabled": True
    }
    assert (
        by_type[registry.TEAM_SKILL_EVOLUTION]["evolution_model_config"]["model_name"]
        == "gpt-4o"
    )


def test_chat_team_permissions_mount_user_interrupt_only_on_leader() -> None:
    """Chat-team leaders ask the user; teammates continue asking the leader."""
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs

    config = {
        "permissions": {"enabled": True},
        "models": {"default": {"model_client_config": {"model_name": "gpt-4o"}}},
    }
    leader_rails, _ = build_member_capability_specs(
        config, "team", "leader", enable_permissions=True
    )
    teammate_rails, _ = build_member_capability_specs(
        config, "team", "teammate", enable_permissions=True
    )
    leader_by_type = {spec.type: spec.params for spec in leader_rails}
    teammate_by_type = {spec.type: spec.params for spec in teammate_rails}

    assert registry.TEAM_PERMISSION_POLICY in leader_by_type
    assert registry.PERMISSION_INTERRUPT in leader_by_type
    assert registry.TEAM_PERMISSION not in leader_by_type
    assert leader_by_type[registry.PERMISSION_INTERRUPT] == {
        "permissions_config": {"enabled": True},
        "model_name": "gpt-4o",
    }

    assert registry.TEAM_PERMISSION in teammate_by_type
    assert registry.PERMISSION_INTERRUPT not in teammate_by_type
    assert registry.TEAM_PERMISSION_POLICY not in teammate_by_type


def test_config_specs_maps_review_trigger_for_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leader uses review_trigger; teammate uses passive signal_trigger only."""
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs

    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
    config = {"evolution": {"review_trigger": True}}
    leader_rails, _ = build_member_capability_specs(config, "team", "leader")
    member_rails, _ = build_member_capability_specs(config, "team", "teammate")
    leader_params = {spec.type: spec.params for spec in leader_rails}
    member_params = {spec.type: spec.params for spec in member_rails}

    assert leader_params[registry.TEAM_SKILL_EVOLUTION]["review_trigger"] is True
    assert "signal_trigger" not in leader_params[registry.TEAM_SKILL_EVOLUTION]
    assert member_params[registry.MEMBER_SKILL_EVOLUTION]["signal_trigger"] is True
    assert member_params[registry.MEMBER_SKILL_EVOLUTION]["review_trigger"] is False


def test_config_specs_keeps_explicit_review_trigger_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leader review_trigger is configurable; teammate review_trigger stays off."""
    from jiuwenswarm.agents.swarm.config_specs import build_member_capability_specs

    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
    config = {
        "evolution": {
            "review_trigger": False,
        }
    }
    leader_rails, _ = build_member_capability_specs(config, "team", "leader")
    member_rails, _ = build_member_capability_specs(config, "team", "teammate")
    leader_params = {spec.type: spec.params for spec in leader_rails}
    member_params = {spec.type: spec.params for spec in member_rails}

    assert leader_params[registry.TEAM_SKILL_EVOLUTION]["review_trigger"] is False
    assert "signal_trigger" not in leader_params[registry.TEAM_SKILL_EVOLUTION]
    assert member_params[registry.MEMBER_SKILL_EVOLUTION]["signal_trigger"] is True
    assert member_params[registry.MEMBER_SKILL_EVOLUTION]["review_trigger"] is False


def test_descriptor_json_round_trip() -> None:
    """Every descriptor round-trips through JSON and the full list is serializable."""
    for descriptor in _swarm_catalog().values():
        restored = HarnessElementDescriptor.model_validate_json(
            descriptor.model_dump_json()
        )
        assert restored == descriptor

    payload = json.dumps(list_elements())
    assert payload
    logger.info("list_elements serialized to %d bytes", len(payload))


def test_register_from_catalog_idempotent() -> None:
    """Re-running registration leaves the ``swarm.*`` registry keys unchanged."""
    register_from_catalog()
    rail_before = _swarm_keys(das._RAIL_PROVIDER_REGISTRY)
    tool_before = _swarm_keys(das._TOOL_PROVIDER_REGISTRY)
    sub_before = _swarm_keys(das._SUBAGENT_PROVIDER_REGISTRY)

    register_from_catalog()

    assert rail_before == _swarm_keys(das._RAIL_PROVIDER_REGISTRY)
    assert tool_before == _swarm_keys(das._TOOL_PROVIDER_REGISTRY)
    assert sub_before == _swarm_keys(das._SUBAGENT_PROVIDER_REGISTRY)


def test_rail_interface_methods_introspected() -> None:
    """A rail descriptor exposes the AgentRail lifecycle hooks, minus internals."""
    descriptor = get_catalog()[registry.CODE_CONFIRM_INTERRUPT]
    names = {method.name for method in descriptor.interface_methods}

    assert "before_model_call" in names
    assert "before_tool_call" in names
    assert "get_callbacks" not in names


def test_tool_interface_methods_triad() -> None:
    """A tool descriptor exposes the invoke/stream/card surface."""
    descriptor = get_catalog()[registry.SKILL_TOOLKIT]
    by_name = {method.name: method for method in descriptor.interface_methods}

    assert {"invoke", "stream", "card"} <= set(by_name)
    assert by_name["invoke"].is_async


def test_subagent_has_no_interface_methods() -> None:
    """A sub-agent builds a SubAgentConfig spec, so it exposes no interface object."""
    descriptor = get_catalog()[registry.EXPLORE_AGENT]
    assert descriptor.interface_methods == []


def test_catalog_covers_all_kinds_without_swarm_rail_types() -> None:
    """All three kinds appear and swarm rails live in the rail-provider registry."""
    register_swarm_providers()
    kinds = {descriptor.kind for descriptor in _swarm_catalog().values()}
    assert kinds == {ElementKind.TOOL, ElementKind.RAIL, ElementKind.SUBAGENT}

    # The legacy class-type registry is gone in openjiuwen; swarm class rails are
    # unified into the rail-provider registry.
    assert not hasattr(das, "_RAIL_TYPE_REGISTRY")
    swarm_rails = {
        descriptor.name
        for descriptor in _swarm_catalog().values()
        if descriptor.kind is ElementKind.RAIL
    }
    assert swarm_rails <= _swarm_keys(das._RAIL_PROVIDER_REGISTRY)


@pytest.mark.asyncio
async def test_response_prompt_rail_builds_via_railspec_with_channel(monkeypatch) -> None:
    """The response rail provider should carry context channel into A2UI gating."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.a2ui.config.get_current_a2ui_config",
        lambda: A2UIConfig(enabled=True),
    )
    register_swarm_providers()
    ctx = SwarmBuildContext(language="cn", channel="web")

    rail = RailSpec(type=registry.RESPONSE_PROMPT).build(language="cn", context=ctx)

    assert rail is not None
    assert type(rail).__name__ == "ResponsePromptRail"
    builder = _FakePromptBuilder()
    rail.init(type("Agent", (), {"system_prompt_builder": builder})())

    await rail.before_model_call(type("Ctx", (), {"inputs": type("Inputs", (), {})()})())

    assert LocalSectionName.A2UI in builder.sections
