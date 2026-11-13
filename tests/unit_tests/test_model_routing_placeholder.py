"""Regression tests: placeholder model entries must never become the default.

The factory template ``resources/config.yaml`` marks the placeholder entry
(``mdl_default_primary``) with ``is_default: true``. Adding a real model
without changing the default, or deleting the default model, must never make
``your-model-name`` / ``https://example.com/compatible-mode/v1`` reachable.
"""

from copy import deepcopy

import pytest
from agent_gateway.common.model_selection import ModelSelection

from jiuwenswarm.common.config import _infer_is_default
from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_errors import ModelSelectionError
from jiuwenswarm.common.model_routing_registry import (
    ModelExecutionContext,
    ModelSelectionResolver,
)

PLACEHOLDER = {  # factory-template shape: placeholder marked as default
    "model_id": "mdl_default_primary",
    "model_client_config": {
        "model_name": "your-model-name",
        "api_base": "https://example.com/compatible-mode/v1",
        "api_key": "sk-xxxxxxxxx",
        "client_provider": "OpenAI",
    },
    "is_default": True,
}

REAL = {  # a freshly added model that the user has not marked as default
    "model_id": "mdl_real",
    "model_client_config": {
        "model_name": "glm-5.3",
        "api_base": "http://127.0.0.1:8317/v1",
        "api_key": "local-key",
        "client_provider": "OpenAI",
    },
}


def _config(defaults, groups=None):
    return {"models": {"defaults": defaults, "agentos": [], "groups": groups or []}}


def test_infer_is_default_ignores_explicit_placeholder_default():
    inferred = _infer_is_default([PLACEHOLDER, REAL])
    assert inferred[0]["is_default"] is False
    assert inferred[1]["is_default"] is True


def test_infer_is_default_keeps_per_name_group_semantics():
    alpha = {
        **REAL,
        "model_id": "mdl_alpha",
        "model_client_config": {**REAL["model_client_config"], "model_name": "alpha"},
    }
    beta = {
        **REAL,
        "model_id": "mdl_beta",
        "model_client_config": {**REAL["model_client_config"], "model_name": "beta"},
    }
    inferred = _infer_is_default([alpha, beta])
    assert inferred[0]["is_default"] is True
    assert inferred[1]["is_default"] is True


def test_infer_is_default_same_name_group_keeps_first_entry():
    first = {**REAL, "model_id": "mdl_first", "is_default": False}
    second = {**REAL, "model_id": "mdl_second"}
    second.pop("is_default", None)
    inferred = _infer_is_default([first, second])
    assert inferred[0]["is_default"] is True
    assert inferred[1]["is_default"] is False


def test_infer_is_default_detects_api_key_placeholder():
    only_key = {
        **REAL,
        "model_id": "mdl_key",
        "model_client_config": {**REAL["model_client_config"], "api_key": "sk-xxxxxxxxx"},
    }
    inferred = _infer_is_default([only_key])
    assert inferred[0]["is_default"] is False


def test_choose_ignores_explicit_placeholder_default():
    catalog = ModelCatalog(_config([PLACEHOLDER, REAL]))
    selection = ModelSelectionResolver(catalog).choose(None, ModelExecutionContext())
    assert selection.id == "mdl_real"


def test_choose_rejects_explicit_placeholder_selection():
    catalog = ModelCatalog(_config([PLACEHOLDER, REAL]))
    with pytest.raises(ModelSelectionError):
        ModelSelectionResolver(catalog).choose(
            ModelSelection(type="model", id="mdl_default_primary"),
            ModelExecutionContext(),
        )


def test_restore_placeholder_selection_falls_back():
    catalog = ModelCatalog(_config([PLACEHOLDER, REAL]))
    restored = ModelSelectionResolver(catalog).restore(
        ModelSelection(type="model", id="mdl_default_primary")
    )
    assert restored.id == "mdl_real"


def test_restore_logs_warning_when_selection_was_deleted(caplog):
    import logging

    catalog = ModelCatalog(_config([REAL]))
    with caplog.at_level(logging.WARNING):
        restored = ModelSelectionResolver(catalog).restore(
            ModelSelection(type="model", id="mdl_deleted")
        )
    assert any("was deleted" in record.getMessage() for record in caplog.records)
    assert restored.id == "mdl_real"


def test_choose_falls_back_to_first_model_when_all_entries_are_placeholders():
    other_placeholder = {
        **PLACEHOLDER,
        "model_id": "mdl_placeholder_2",
        "model_client_config": {**PLACEHOLDER["model_client_config"], "model_name": "your-model-name-2"},
    }
    catalog = ModelCatalog(_config([PLACEHOLDER, other_placeholder]))
    selection = ModelSelectionResolver(catalog).choose(None, ModelExecutionContext())
    assert selection.id in {"mdl_default_primary", "mdl_placeholder_2"}


def test_team_pool_drops_placeholder_and_ignores_placeholder_request():
    from jiuwenswarm.agents.harness.team.config_loader import (
        get_effective_team_model_entries,
    )

    entries = get_effective_team_model_entries(
        _config([PLACEHOLDER, REAL]),
        requested_model_name="your-model-name",
    )
    assert entries
    assert all(
        entry["model_client_config"]["model_name"] != "your-model-name"
        for entry in entries
    )


def test_team_pool_is_empty_for_unconfigured_install():
    from jiuwenswarm.agents.harness.team.config_loader import (
        get_effective_team_model_entries,
    )

    entries = get_effective_team_model_entries(_config([PLACEHOLDER]))
    assert entries == []


@pytest.mark.parametrize("selected_id", ["mdl_real", "mdl_second", "mdl_default_primary"])
def test_team_model_id_selection_preserves_placeholder_filtering(
    monkeypatch: pytest.MonkeyPatch,
    selected_id: str,
) -> None:
    from jiuwenswarm.agents.harness.team import team_manager as manager_module
    from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection

    second = deepcopy(REAL)
    second["model_id"] = "mdl_second"
    second["is_default"] = True
    second["model_client_config"]["api_base"] = "http://127.0.0.1:8320/v1"
    second["model_client_config"]["api_key"] = "second-key"
    config = _config([deepcopy(PLACEHOLDER), deepcopy(REAL), second])
    config["modes"] = {"team": {"demo": {
        "team_name": "demo", "agents": {"leader": {}, "teammate": {}},
    }}}
    resolver = ModelSelectionResolver(ModelCatalog(config))
    monkeypatch.setattr(manager_module, "get_config", lambda: config)
    monkeypatch.setattr(
        manager_module.TeamManager,
        "_resolve_and_compile_team_selection",
        staticmethod(lambda selection: compile_model_selection(resolver.resolve(selection))),
    )
    selection = ModelSelection(type="model", id=selected_id)
    if selected_id == "mdl_default_primary":
        with pytest.raises(ModelSelectionError) as caught:
            manager_module.TeamManager._load_team_spec("selected-model-id", model_selection=selection)
        assert caught.value.code == "MODEL_SELECTION_DISABLED"
        return

    spec = manager_module.TeamManager._load_team_spec("selected-model-id", model_selection=selection)

    expected = second if selected_id == "mdl_second" else REAL
    for member in ("leader", "teammate"):
        client = spec.agents[member].model.model_client_config
        assert client.api_base == expected["model_client_config"]["api_base"]
        assert client.api_key == expected["model_client_config"]["api_key"]
    assert spec.model_pool[0].api_base_url == expected["model_client_config"]["api_base"]
    assert len(spec.model_pool) == 2


@pytest.mark.parametrize("defaults", [[], [PLACEHOLDER]])
def test_team_member_models_work_without_shared_pool(monkeypatch, defaults):
    from jiuwenswarm.agents.harness.team import team_manager as manager_module

    config = _config(deepcopy(defaults))
    config["modes"] = {"team": {"demo": {
        "team_name": "demo",
        "agents": {
            "leader": {"model": deepcopy(REAL)},
            "teammate": {"model": deepcopy(REAL)},
        },
    }}}
    monkeypatch.setattr(manager_module, "get_config", lambda: config)

    spec = manager_module.TeamManager._load_team_spec("member-models")

    assert not spec.model_pool
    for member in ("leader", "teammate"):
        assert spec.agents[member].model.model_request_config.model_name == "glm-5.3"
        assert spec.agents[member].model.model_client_config.api_key == "local-key"


@pytest.mark.parametrize("defaults", [[], [PLACEHOLDER]])
@pytest.mark.parametrize("missing_member", ["leader", "teammate", "both"])
def test_team_without_pool_rejects_missing_member_fallback(monkeypatch, defaults, missing_member):
    from jiuwenswarm.agents.harness.team import team_manager as manager_module

    for name in ("API_BASE", "API_KEY", "MODEL_NAME", "MODEL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    agents = {
        "leader": {"model": deepcopy(REAL)},
        "teammate": {"model": deepcopy(REAL)},
    }
    for member in agents:
        if missing_member in (member, "both"):
            agents[member] = {}
    config = _config(deepcopy(defaults))
    config["modes"] = {"team": {"demo": {"team_name": "demo", "agents": agents}}}
    monkeypatch.setattr(manager_module, "get_config", lambda: config)

    with pytest.raises(ModelSelectionError) as caught:
        manager_module.TeamManager._load_team_spec("missing-member-model")

    assert caught.value.code == "MODEL_SELECTION_DISABLED"


@pytest.mark.parametrize("member_model", [
    PLACEHOLDER,
    {**REAL, "model_request_config": {"model": "your-model-name"}},
    {**REAL, "model_client_config": {**REAL["model_client_config"], "api_key": "sk-xxxxxxxxx"}},
])
def test_team_without_pool_rejects_explicit_placeholder_member(monkeypatch, member_model):
    from jiuwenswarm.agents.harness.team import team_manager as manager_module

    config = _config([])
    config["modes"] = {"team": {"demo": {
        "team_name": "demo",
        "agents": {
            "leader": {"model": deepcopy(REAL)},
            "teammate": {"model": deepcopy(member_model)},
        },
    }}}
    monkeypatch.setattr(manager_module, "get_config", lambda: config)

    with pytest.raises(ModelSelectionError) as caught:
        manager_module.TeamManager._load_team_spec("placeholder-member-model")

    assert caught.value.code == "MODEL_SELECTION_DISABLED"


def test_team_without_pool_checks_auto_added_teammate(monkeypatch):
    from jiuwenswarm.agents.harness.team import team_manager as manager_module

    config = _config([])
    config["modes"] = {"team": {"demo": {
        "team_name": "demo",
        "agents": {"leader": {"model": deepcopy(REAL)}},
    }}}
    monkeypatch.setattr(manager_module, "get_config", lambda: config)

    with pytest.raises(ModelSelectionError) as caught:
        manager_module.TeamManager._load_team_spec("auto-added-teammate")

    assert caught.value.code == "MODEL_SELECTION_DISABLED"


def test_model_group_keeps_disabled_placeholder_route_without_failing():
    from jiuwenswarm.server.runtime.model_compiler_adapter import compile_model_selection

    group = {
        "model_group_id": "mgp_mixed",
        "enabled": True,
        "routing": {"strategy": "ordered-failover"},
        "routes": [
            {"route_id": "rte_placeholder", "model_id": "mdl_default_primary", "enabled": False},
            {"route_id": "rte_real", "model_id": "mdl_real", "enabled": True},
        ],
    }
    catalog = ModelCatalog(_config([PLACEHOLDER, REAL], groups=[group]))
    resolved = ModelSelectionResolver(catalog).resolve(
        ModelSelection(type="model_group", id="mgp_mixed")
    )
    assert [route.route_id for route in resolved.routes] == ["rte_placeholder", "rte_real"]
    assert resolved.routes[0].enabled is False
    assert resolved.routes[1].enabled is True
    compiled = compile_model_selection(resolved)
    deployments = compiled.model_client_config.intelli_router.deployments
    assert [deployment.route_id for deployment in deployments] == ["rte_real"]


def test_model_group_with_only_disabled_routes_is_rejected():
    group = {
        "model_group_id": "mgp_disabled",
        "enabled": True,
        "routes": [
            {"route_id": "rte_one", "model_id": "mdl_real", "enabled": False},
        ],
    }
    catalog = ModelCatalog(_config([PLACEHOLDER, REAL], groups=[group]))
    with pytest.raises(ModelSelectionError):
        ModelSelectionResolver(catalog).resolve(
            ModelSelection(type="model_group", id="mgp_disabled")
        )


def test_agentos_same_name_index_keeps_configured_order(monkeypatch):
    from jiuwenswarm.agents.harness.team import config_loader as config_loader_module

    glm_a = {
        **REAL,
        "model_id": "mdl_glm_a",
        "model_client_config": {**REAL["model_client_config"], "api_base": "http://127.0.0.1:8317/v1"},
    }
    glm_b = {
        **REAL,
        "model_id": "mdl_glm_b",
        "model_client_config": {**REAL["model_client_config"], "api_base": "http://127.0.0.1:8320/v1"},
    }
    other = {
        **REAL,
        "model_id": "mdl_other",
        "model_client_config": {**REAL["model_client_config"], "model_name": "other"},
    }
    monkeypatch.setattr(config_loader_module, "is_agentos_runtime", lambda: True)
    entries = config_loader_module.get_effective_team_model_entries(
        _config([glm_a, glm_b, other]),
        requested_model_name="glm-5.3#1",
    )
    assert [entry["model_id"] for entry in entries][:2] == ["mdl_glm_a", "mdl_glm_b"]


def test_runtime_catalog_skips_placeholder_for_current_selection():
    from jiuwenswarm.runtime.model_catalog import build_model_catalog

    catalog = build_model_catalog([PLACEHOLDER, REAL])
    assert catalog.current_selection == "glm-5.3#1"


def test_model_group_route_to_placeholder_is_rejected():
    group = {
        "model_group_id": "mgp_placeholder",
        "enabled": True,
        "routes": [{"route_id": "rte_placeholder", "model_id": "mdl_default_primary"}],
    }
    catalog = ModelCatalog(_config([PLACEHOLDER, REAL], groups=[group]))
    with pytest.raises(ModelSelectionError):
        ModelSelectionResolver(catalog).resolve(
            ModelSelection(type="model_group", id="mgp_placeholder")
        )


def test_probe_models_skip_placeholder_entries():
    from jiuwenswarm.server.runtime.image_modality_warmup import _enumerate_probe_targets

    targets = _enumerate_probe_targets(_config([PLACEHOLDER, REAL]))
    api_bases = {
        target.model_client_config.get("api_base", "") for target in targets.values()
    }
    assert all("example.com" not in base for base in api_bases)
    assert any("127.0.0.1:8317" in base for base in api_bases)


def test_choose_resolves_legacy_name_ignoring_placeholder():
    catalog = ModelCatalog(_config([PLACEHOLDER, REAL]))
    selection = ModelSelectionResolver(catalog).choose(
        None,
        ModelExecutionContext(legacy_model_name="glm-5.3"),
    )
    assert selection.id == "mdl_real"


def test_choose_prefers_explicit_default_over_list_order():
    workint = {
        **REAL,
        "model_id": "mdl_workint",
        "model_client_config": {**REAL["model_client_config"], "model_name": "WorkInt-4.1"},
    }
    glm = {**REAL, "model_id": "mdl_glm", "is_default": True}
    catalog = ModelCatalog(_config([PLACEHOLDER, workint, glm]))
    selection = ModelSelectionResolver(catalog).choose(None, ModelExecutionContext())
    assert selection.id == "mdl_glm"


def test_runtime_catalog_prefers_explicit_default():
    from jiuwenswarm.runtime.model_catalog import build_model_catalog

    workint = {
        **REAL,
        "model_id": "mdl_workint",
        "model_client_config": {**REAL["model_client_config"], "model_name": "WorkInt-4.1"},
    }
    glm = {**REAL, "model_id": "mdl_glm", "is_default": True}
    catalog = build_model_catalog([PLACEHOLDER, workint, glm])
    assert catalog.current_selection == "glm-5.3#2"


def test_team_default_ignores_placeholder_model_id():
    """A placeholder model_id must not become the member fallback entry."""
    from jiuwenswarm.agents.harness.team.config_loader import (
        _select_default_model_config,
    )
    from jiuwenswarm.common.config import get_default_models

    entries = get_default_models(_config([PLACEHOLDER, REAL]))
    selected = _select_default_model_config(entries, model_id="mdl_default_primary")
    assert selected["model_id"] == "mdl_real"
    assert selected["model_client_config"]["api_base"] != "https://example.com/compatible-mode/v1"


def test_team_default_keeps_valid_model_id():
    """A non-placeholder model_id still wins, so duplicate names stay addressable."""
    from jiuwenswarm.agents.harness.team.config_loader import (
        _select_default_model_config,
    )
    from jiuwenswarm.common.config import get_default_models

    other = {
        **REAL,
        "model_id": "mdl_other",
        "model_client_config": {**REAL["model_client_config"], "api_base": "http://127.0.0.1:8320/v1"},
    }
    entries = get_default_models(_config([PLACEHOLDER, REAL, other]))
    assert _select_default_model_config(entries, model_id="mdl_other")["model_id"] == "mdl_other"


def test_team_default_prefers_explicit_default():
    from jiuwenswarm.agents.harness.team.config_loader import (
        _select_default_model_config,
    )
    from jiuwenswarm.common.config import get_default_models

    workint = {
        **REAL,
        "model_id": "mdl_workint",
        "model_client_config": {**REAL["model_client_config"], "model_name": "WorkInt-4.1"},
    }
    glm = {**REAL, "model_id": "mdl_glm", "is_default": True}
    selected = _select_default_model_config(get_default_models(_config([PLACEHOLDER, workint, glm])))
    assert selected["model_client_config"]["model_name"] == "glm-5.3"
