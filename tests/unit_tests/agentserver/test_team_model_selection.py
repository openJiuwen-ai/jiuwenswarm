# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team-only tests for stable model selection and compiler integration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import (
    CompiledModelSelection,
    ProviderType,
)
from openjiuwen.core.foundation.llm.routing.compiler import compile_model_selection
from openjiuwen.core.foundation.llm.routing.schema import (
    ResolvedModel,
    ResolvedModelGroup,
    ResolvedRoute,
)

from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.common.model_errors import (
    MODEL_GROUP_INVALID,
    MODEL_SELECTION_FORBIDDEN,
    MODEL_SELECTION_DISABLED,
    TEAM_MODEL_SELECTION_STALE,
    ModelSelectionError,
)
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.common.model_selection import (
    ResolvedModel as SwarmResolvedModel,
    ResolvedModelGroup as SwarmResolvedModelGroup,
    ResolvedRoute as SwarmResolvedRoute,
)
from jiuwenswarm.server.runtime.model_routing_registry import (
    ModelExecutionContext,
    ModelSelectionResolver,
)


def _team_config() -> dict:
    return {
        "team_name": "configured-team",
        "agents": {"leader": {}, "teammate": {}},
    }


def _compiled_model_group(
    *,
    selected_id: str = "group-1",
) -> CompiledModelSelection:
    primary = ResolvedModel(
        model_id="model-primary",
        model_name="primary-model",
        provider="OpenAI",
        api_key="primary-key",
        api_base="https://primary.example",
    )
    fallback = ResolvedModel(
        model_id="model-fallback",
        model_name="fallback-model",
        provider="DeepSeek",
        api_key="fallback-key",
        api_base="https://fallback.example",
    )
    return compile_model_selection(
        ResolvedModelGroup(
            model_group_id=selected_id,
            routes=[
                ResolvedRoute(route_id="primary", model=primary),
                ResolvedRoute(route_id="fallback", model=fallback),
            ],
            routing={"strategy": "ordered-failover", "num_retries": 1},
        )
    )


def _patch_team_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.agents.harness.team.team_manager as manager_module

    monkeypatch.setattr(manager_module, "get_config", lambda: {})
    monkeypatch.setattr(
        manager_module,
        "load_team_spec_dict",
        lambda **_kwargs: _team_config(),
    )


def test_team_group_selection_calls_resolver_and_materializes_one_logical_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_team_loader(monkeypatch)
    calls: list[object] = []
    compiled = _compiled_model_group()

    class _Resolver:
        def resolve(self, selection):
            calls.append(selection)
            return SimpleNamespace()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: _Resolver(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_compiler_adapter.compile_model_selection",
        lambda resolved: compiled,
    )

    spec = TeamManager._load_team_spec(
        "session-1",
        model_selection={"type": "model_group", "id": "group-1"},
    )

    assert len(calls) == 1
    assert calls[0].type == "model_group"
    assert calls[0].id == "group-1"
    assert spec.model_pool_strategy == "intelli_router"
    assert [entry.model_name for entry in spec.model_pool] == ["*"]
    assert spec.leader.model_name == "*"
    deployments = spec.model_pool[0].metadata["client"]["intelli_router"]["deployments"]
    assert [deployment["route_id"] for deployment in deployments] == [
        "primary",
        "fallback",
    ]


def test_team_materializes_model_group_without_routing_as_routed_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_team_loader(monkeypatch)
    resolved = SwarmResolvedModelGroup(
        model_group_id="group-single",
        routes=[
            SwarmResolvedRoute(
                route_id="primary",
                model=SwarmResolvedModel(
                    model_id="model-primary",
                    model_name="primary-model",
                    provider="OpenAI",
                    api_key="primary-key",
                    api_base="https://primary.example",
                    source="defaults",
                ),
            ),
        ],
        request_config={"max_tokens": 128},
        routing={},
    )

    class _Resolver:
        def resolve(self, _selection):
            return resolved

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: _Resolver(),
    )

    spec = TeamManager._load_team_spec(
        "session-single-group",
        model_selection={"type": "model_group", "id": "group-single"},
    )

    # Team defaults a strategy-less group to ordered failover, so it still
    # materializes as the single logical IntelliRouter entry.
    assert spec.model_pool_strategy == "intelli_router"
    assert len(spec.model_pool) == 1
    assert spec.model_pool[0].model_name == "*"
    assert spec.model_pool[0].api_provider == "intelli_router"
    assert spec.leader.model_name == "*"
    router = spec.model_pool[0].metadata["client"]["intelli_router"]
    assert router["strategy"] == "ordered-failover"
    assert [item["model_name"] for item in router["deployments"]] == ["primary-model"]


def test_team_group_keeps_external_cli_deployment_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_team_loader(monkeypatch)
    compiled = _compiled_model_group()

    class _Resolver:
        def resolve(self, _selection):
            return SimpleNamespace()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: _Resolver(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_compiler_adapter.compile_model_selection",
        lambda resolved: compiled,
    )

    def _team_with_cli(**_kwargs):
        return {
            **_team_config(),
            "predefined_members": [
                {
                    "role_type": "external_cli",
                    "member_name": "reviewer",
                    "display_name": "Reviewer",
                    "model_name": "qwen-plus",
                    "external_cli": {"cli_agent": "codex"},
                }
            ],
        }

    import jiuwenswarm.agents.harness.team.team_manager as manager_module

    monkeypatch.setattr(manager_module, "load_team_spec_dict", _team_with_cli)
    spec = TeamManager._load_team_spec(
        "session-cli-group",
        model_selection={"type": "model_group", "id": "group-1"},
    )

    assert spec.predefined_members[0].model_name == "qwen-plus"


def test_team_single_model_selection_keeps_every_configured_model_in_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jiuwenswarm.agents.harness.team.team_manager as manager_module

    captured: dict = {}

    def fake_load_team_spec_dict(**kwargs):
        captured["requested_model_name"] = kwargs.get("requested_model_name")
        return _team_config()

    def fake_effective_entries(_config_base, **kwargs):
        captured["effective_requested_model_name"] = kwargs.get("requested_model_name")
        return [
            {
                "model_client_config": {
                    "model_name": name,
                    "api_key": f"{name}-key",
                    "api_base": f"https://{name}.example",
                    "client_provider": "OpenAI",
                },
            }
            for name in ("deepseek-v4-flash", "GLM-5.3-Flash", "GLM-5.3")
        ]

    monkeypatch.setattr(manager_module, "get_config", lambda: {})
    monkeypatch.setattr(manager_module, "load_team_spec_dict", fake_load_team_spec_dict)
    monkeypatch.setattr(manager_module, "get_effective_team_model_entries", fake_effective_entries)
    monkeypatch.setattr(
        TeamManager,
        "_resolve_and_compile_team_selection",
        staticmethod(
            lambda _selection: compile_model_selection(
                ResolvedModel(
                    model_id="model-ds",
                    model_name="deepseek-v4-flash",
                    provider="OpenAI",
                    api_key="deepseek-v4-flash-key",
                    api_base="https://deepseek-v4-flash.example",
                )
            )
        ),
    )

    spec = TeamManager._load_team_spec(
        "session-single-model",
        model_selection={"type": "model", "id": "model-ds"},
    )

    # The selection names the default model; the pool still offers every
    # configured model so members can be assigned any of them.
    assert captured["requested_model_name"] == "deepseek-v4-flash"
    assert captured["effective_requested_model_name"] == "deepseek-v4-flash"
    assert spec.model_pool_strategy == "by_model_name"
    assert [entry.model_name for entry in spec.model_pool] == [
        "deepseek-v4-flash",
        "GLM-5.3-Flash",
        "GLM-5.3",
    ]


def test_team_route_pinned_group_selection_reaches_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_team_loader(monkeypatch)
    compiled = _compiled_model_group()
    captured: dict = {}

    class _Resolver:
        def resolve(self, selection):
            captured["selection"] = selection
            return SimpleNamespace()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: _Resolver(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_compiler_adapter.compile_model_selection",
        lambda resolved: compiled,
    )

    TeamManager._load_team_spec(
        "session-route-pin",
        model_selection={"type": "model_group", "id": "group-1", "route_id": "primary"},
    )

    assert captured["selection"].type == "model_group"
    assert captured["selection"].id == "group-1"
    assert captured["selection"].route_id == "primary"


def test_cold_restore_reads_stable_selection_and_forwards_it_to_team_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    captured: dict = {}
    monkeypatch.setattr(TeamManager, "_default_if_deleted", staticmethod(lambda selection: selection))

    monkeypatch.setattr(
        manager,
        "_lookup_bound_team_identity",
        lambda _session_id: (None, None, None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection",
        lambda _session_id: ModelSelection(type="model_group", id="group-restore"),
    )

    def fake_load(_session_id: str, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(team_name="team")

    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(fake_load))

    spec, has_binding = manager._load_session_team_spec("session-restore")

    assert has_binding is False
    assert spec.team_name == "team"
    assert captured["model_selection"] == {
        "type": "model_group",
        "id": "group-restore",
    }
    assert "compiled_model_selection" not in captured


def test_cold_restore_prefers_team_binding_selection_over_session_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    captured: dict = {}
    monkeypatch.setattr(TeamManager, "_default_if_deleted", staticmethod(lambda selection: selection))
    monkeypatch.setattr(
        manager,
        "_lookup_bound_team_identity",
        lambda _session_id: ("shared-team", None, None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {
            "team_name": "shared-team",
            "model_selection": {"type": "model", "id": "legacy-session"},
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(
            get_team_selection=lambda _name: {"type": "model_group", "id": "team-group"},
        ),
    )

    def fake_load(_session_id: str, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(team_name="team")

    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(fake_load))
    manager._load_session_team_spec("session-restore")
    assert captured["model_selection"] == {
        "type": "model_group",
        "id": "team-group",
    }


@pytest.mark.parametrize("source", ["binding", "session"])
def test_cold_restore_deleted_selection_uses_default(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    manager = TeamManager()
    captured: dict = {}
    monkeypatch.setattr("jiuwenswarm.common.model_catalog.get_config", lambda: {"models": {
        "defaults": [{"model_id": "current", "is_default": True,
                      "model_client_config": {"model_name": "current"}}],
    }})
    monkeypatch.setattr(
        manager, "_lookup_bound_team_identity",
        lambda _session_id: ("shared-team" if source == "binding" else None, None, None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {"team_name": "shared-team"} if source == "binding" else {},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(get_team_selection=lambda _name: {"type": "model", "id": "deleted"}),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection",
        lambda _session_id: ModelSelection(type="model", id="deleted"),
    )

    def fake_load(_session_id: str, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(team_name="team")

    monkeypatch.setattr(TeamManager, "_load_team_spec", staticmethod(fake_load))
    manager._load_session_team_spec("session-restore")
    assert captured["model_selection"] == ModelSelection(type="model", id="current")


def test_cold_restore_preserves_resolver_business_error_without_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_team_loader(monkeypatch)

    class _Resolver:
        def resolve(self, _selection):
            raise ModelSelectionError(
                MODEL_SELECTION_DISABLED,
                "selected group is disabled",
            )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: _Resolver(),
    )

    with pytest.raises(ModelSelectionError) as error:
        TeamManager._load_team_spec(
            "session-stale",
            model_selection={"type": "model_group", "id": "disabled-group"},
        )

    assert error.value.code == MODEL_SELECTION_DISABLED


def test_cold_restore_wraps_non_business_failures_as_stale_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_team_loader(monkeypatch)

    class _Resolver:
        def resolve(self, _selection):
            raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: _Resolver(),
    )

    with pytest.raises(ModelSelectionError) as error:
        TeamManager._load_team_spec(
            "session-stale",
            model_selection={"type": "model_group", "id": "group-stale"},
        )

    assert error.value.code == TEAM_MODEL_SELECTION_STALE


class _Catalog:
    """Minimal catalog double for resolver boundary tests."""

    def __init__(self, *, groups=None, models=None, config=None):
        self.config = config or {}
        self.snapshot = {"groups": groups or []}
        self._groups = {g["model_group_id"]: g for g in self.snapshot["groups"]}
        self._models = {m["model_id"]: {"entry": {"model_client_config": {
            "model_name": m["model_name"], "client_provider": m["provider"],
            "api_base": m.get("api_base", ""), "api_key": m.get("api_key", ""),
        }, "model_config_obj": {}}, "source": "defaults"} for m in (models or [])}

    def get_group(self, group_id):
        return self._groups[group_id]

    def get_model(self, model_id):
        return self._models[model_id]

    def list_public_models(self):
        return [
            {"model_id": key, "model_name": value["entry"]["model_client_config"]["model_name"], "alias": "", "is_default": False}
            for key, value in self._models.items()
        ]


def _catalog_group(*, enabled=True, route_enabled=True):
    return {
        "model_group_id": "g1", "enabled": enabled, "is_default": True,
        "routes": [{"route_id": "r1", "model_id": "m1", "enabled": route_enabled}],
        "request_config": {"max_tokens": 128},
        "routing": {"strategy": "ordered-failover"},
    }


def test_resolver_precedence_is_explicit_then_session_then_spec_then_default():
    resolver = ModelSelectionResolver(_Catalog(groups=[_catalog_group()], models=[{"model_id": "m1", "model_name": "m1", "provider": "OpenAI"}]))
    explicit = ModelSelection(type="model", id="m1")
    context = ModelExecutionContext(
        session_selection=ModelSelection(type="model", id="session"),
        spec_selection=ModelSelection(type="model", id="spec"),
    )
    assert resolver.choose(explicit, context) == explicit
    assert resolver.choose(None, context).id == "session"
    assert resolver.choose(None, ModelExecutionContext(spec_selection=ModelSelection(type="model", id="spec"))).id == "spec"
    assert resolver.choose(None, ModelExecutionContext()).id == "g1"


def test_resolver_rejects_disabled_group(group=_catalog_group(enabled=False)):
    resolver = ModelSelectionResolver(_Catalog(groups=[group], models=[{"model_id": "m1", "model_name": "m1", "provider": "OpenAI"}]))
    with pytest.raises(ModelSelectionError) as error:
        resolver.resolve(ModelSelection(type="model_group", id="g1"))
    assert error.value.code in {MODEL_SELECTION_DISABLED, MODEL_GROUP_INVALID}


def test_resolver_keeps_enabled_routes_when_another_route_is_disabled():
    group = _catalog_group()
    group["routes"].append({"route_id": "disabled", "model_id": "m1", "enabled": False})
    resolver = ModelSelectionResolver(_Catalog(groups=[group], models=[{"model_id": "m1", "model_name": "m1", "provider": "OpenAI"}]))
    resolved = resolver.resolve(ModelSelection(type="model_group", id="g1"))
    assert [route.route_id for route in resolved.routes] == ["r1", "disabled"]
    assert any(route.enabled for route in resolved.routes)


def test_resolver_rejects_all_disabled_group_with_business_error():
    group = _catalog_group()
    group["routes"][0]["enabled"] = False
    resolver = ModelSelectionResolver(_Catalog(groups=[group], models=[{"model_id": "m1", "model_name": "m1", "provider": "OpenAI"}]))
    with pytest.raises(ModelSelectionError) as error:
        resolver.resolve(ModelSelection(type="model_group", id="g1"))
    assert error.value.code == MODEL_GROUP_INVALID


def test_resolver_applies_access_check_to_group_and_route_models():
    resolver = ModelSelectionResolver(_Catalog(groups=[_catalog_group()], models=[{"model_id": "m1", "model_name": "m1", "provider": "OpenAI"}]))
    with pytest.raises(ModelSelectionError) as error:
        resolver.resolve(ModelSelection(type="model_group", id="g1"), ModelExecutionContext(can_access=lambda scope, _id: scope != "model_group"))
    assert error.value.code == MODEL_SELECTION_FORBIDDEN
    with pytest.raises(ModelSelectionError) as error:
        resolver.resolve(ModelSelection(type="model_group", id="g1"), ModelExecutionContext(can_access=lambda scope, _id: scope != "model"))
    assert error.value.code == MODEL_SELECTION_FORBIDDEN


def test_resolver_rejects_route_with_empty_model_name_as_disabled():
    catalog = _Catalog(groups=[_catalog_group()], models=[{"model_id": "m1", "model_name": "", "provider": "OpenAI"}])
    resolver = ModelSelectionResolver(catalog)
    with pytest.raises(ModelSelectionError) as error:
        resolver.resolve(ModelSelection(type="model_group", id="g1"))
    assert error.value.code == MODEL_SELECTION_DISABLED


def test_swarm_compiler_adapter_preserves_group_identity_and_route_fields():
    resolver = ModelSelectionResolver(_Catalog(groups=[_catalog_group()], models=[{"model_id": "m1", "model_name": "m1", "provider": "OpenAI", "api_key": "k", "api_base": "https://a"}]))
    resolved = resolver.resolve(ModelSelection(type="model_group", id="g1"))
    compiled = __import__(
        "jiuwenswarm.server.runtime.model_compiler_adapter",
        fromlist=["compile_model_selection"],
    ).compile_model_selection(resolved)
    assert compiled.selected_type == "model_group"
    assert compiled.selected_id == "g1"
    assert compiled.model_client_config.client_provider == ProviderType.IntelliRouter.value
    assert [route.route_id for route in compiled.model_client_config.intelli_router.deployments] == ["r1"]
    assert compiled.model_client_config.intelli_router.deployments[0].request_defaults == {"max_tokens": 128}


def _two_route_resolver(monkeypatch: pytest.MonkeyPatch, routing: dict | None, *, config=None) -> None:
    """Route TeamManager through the real resolver over a two-model group."""
    group = {
        "model_group_id": "g1", "enabled": True, "is_default": True,
        "routes": [
            {"route_id": "r1", "model_id": "m1"},
            {"route_id": "r2", "model_id": "m2"},
        ],
    }
    if routing is not None:
        group["routing"] = routing
    catalog = _Catalog(groups=[group], models=[
        {"model_id": "m1", "model_name": "model-one", "provider": "OpenAI",
         "api_base": "https://one.test", "api_key": "mock-key-one"},
        {"model_id": "m2", "model_name": "model-two", "provider": "OpenAI",
         "api_base": "https://two.test", "api_key": "mock-key-two"},
    ], config=config)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: ModelSelectionResolver(catalog),
    )


def test_team_group_without_routing_defaults_to_ordered_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Groups saved from the UI carry no routing; Team must still route the whole group."""
    _two_route_resolver(monkeypatch, routing=None)

    compiled = TeamManager._resolve_and_compile_team_selection(ModelSelection(type="model_group", id="g1"))

    router = compiled.model_client_config.intelli_router
    assert router is not None
    assert router.strategy == "ordered-failover"
    assert router.timeout == 360
    assert [item.model_name for item in router.deployments] == ["model-one", "model-two"]


def test_team_group_keeps_its_own_routing_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_route_resolver(monkeypatch, routing={"strategy": "tag-filtered", "num_retries": 1, "timeout_seconds": 90})

    compiled = TeamManager._resolve_and_compile_team_selection(ModelSelection(type="model_group", id="g1"))

    router = compiled.model_client_config.intelli_router
    assert router.strategy == "tag-filtered"
    assert router.num_retries == 1
    assert router.timeout == 90


def test_team_group_timeout_uses_config_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_route_resolver(
        monkeypatch, routing=None,
        config={"models": {"group_routing_timeout_seconds": "420"}},
    )

    compiled = TeamManager._resolve_and_compile_team_selection(ModelSelection(type="model_group", id="g1"))

    assert compiled.model_client_config.intelli_router.timeout == 420


def test_team_group_timeout_override_is_independent_of_global_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_route_resolver(
        monkeypatch,
        routing={"timeout_seconds": 75, "stream_first_chunk_timeout": 55, "stream_idle_timeout": 20},
        config={"models": {"group_routing_timeout_seconds": 420}},
    )

    compiled = TeamManager._resolve_and_compile_team_selection(ModelSelection(type="model_group", id="g1"))

    assert compiled.model_client_config.intelli_router.timeout == 75


@pytest.mark.parametrize("value", ["5m", "", None, 0, -1, "nan", "inf"])
def test_team_group_invalid_config_timeout_uses_default(
    monkeypatch: pytest.MonkeyPatch, value: object,
) -> None:
    _two_route_resolver(
        monkeypatch, routing=None,
        config={"models": {"group_routing_timeout_seconds": value}},
    )

    compiled = TeamManager._resolve_and_compile_team_selection(ModelSelection(type="model_group", id="g1"))

    assert compiled.model_client_config.intelli_router.timeout == 360


def test_team_manual_route_without_routing_stays_a_single_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _two_route_resolver(monkeypatch, routing=None)

    compiled = TeamManager._resolve_and_compile_team_selection(
        ModelSelection(type="model_group", id="g1", route_id="r2")
    )

    assert compiled.model_client_config.intelli_router is None
    assert compiled.model_request_config.model_name == "model-two"
