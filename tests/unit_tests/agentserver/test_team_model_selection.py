# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team-only tests for stable model selection and compiler integration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import (
    CompiledModelSelection,
    ModelClientConfig,
    ModelRequestConfig,
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


def _compiled_model(*, selected_id: str = "model-1") -> CompiledModelSelection:
    return CompiledModelSelection(
        model_client_config=ModelClientConfig(
            client_provider=ProviderType.OpenAI,
            api_key="model-key",
            api_base="https://model.example/v1",
        ),
        model_request_config=ModelRequestConfig(model="compiled-model"),
        selected_type="model",
        selected_id=selected_id,
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
    manager_module = __import__(
        "jiuwenswarm.agents.harness.team.team_manager",
        fromlist=["TeamManager"],
    )
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


def test_team_consumes_trusted_compiled_selection_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_team_loader(monkeypatch)
    resolver = SimpleNamespace(resolve=lambda _selection: pytest.fail("resolver called"))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        lambda: resolver,
    )

    spec = TeamManager._load_team_spec(
        "session-2",
        model_selection={"type": "model", "id": "model-1"},
        compiled_model_selection=_compiled_model(),
    )

    assert spec.model_pool_strategy == "by_model_name"
    assert len(spec.model_pool) == 1
    assert spec.model_pool[0].model_name == "compiled-model"
    assert spec.leader.model_name == "compiled-model"


def test_cold_restore_reads_stable_selection_and_forwards_it_to_team_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TeamManager()
    captured: dict = {}

    monkeypatch.setattr(
        manager,
        "_lookup_bound_team_identity",
        lambda _session_id: (None, None, None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.team_manager.get_session_metadata",
        lambda _session_id, cache_bust=False: {
            "model_selection": {"type": "model_group", "id": "group-restore"},
        },
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

    def __init__(self, *, groups=None, models=None):
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


def test_resolver_returns_no_enabled_routes_for_all_disabled_group_for_compiler_to_reject():
    group = _catalog_group()
    group["routes"][0]["enabled"] = False
    resolver = ModelSelectionResolver(_Catalog(groups=[group], models=[{"model_id": "m1", "model_name": "m1", "provider": "OpenAI"}]))
    resolved = resolver.resolve(ModelSelection(type="model_group", id="g1"))
    assert resolved.routes and all(not route.enabled for route in resolved.routes)


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
