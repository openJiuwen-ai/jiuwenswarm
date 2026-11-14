# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import sys
import types
from types import SimpleNamespace

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import _role_evolution_rails, _ttse_rail_params
from jiuwenswarm.agents.swarm.providers import evolution_rails


def _baked_params(
    *,
    evolve_enabled: bool = True,
    inject_enabled: bool = True,
    dream_enabled: bool = True,
    model_name: str = "dummy",
    leader_bank: str = r"D:\tenant\.ttse\leader\bank.json",
    member_bank: str = r"D:\tenant\.ttse\member\bank.json",
    embedding_config: dict | None = None,
    evolution_model_config: dict | None = None,
    dream_interval: int = 50,
    dream_min_hours: float = 24.0,
    dream_ttl_days: int = 90,
    consult_top_k: int = 8,
    consult_retrieve_mode: str = "hybrid",
    consult_rrf_k: int = 60,
    trajectory_export_enabled: bool = False,
    trajectory_export_path: str = "",
) -> dict:
    return {
        "evolution_model_config": evolution_model_config
        or {
            "model_client_config": {"model_name": model_name},
            "model_config_obj": {},
            "model_name": model_name,
        },
        "evolve_enabled": evolve_enabled,
        "inject_enabled": inject_enabled,
        "dream_enabled": dream_enabled,
        "leader_bank_path": leader_bank,
        "member_bank_path": member_bank,
        "embedding_config": embedding_config or {},
        "dream_interval": dream_interval,
        "dream_min_hours": dream_min_hours,
        "dream_ttl_days": dream_ttl_days,
        "consult_top_k": consult_top_k,
        "consult_retrieve_mode": consult_retrieve_mode,
        "consult_rrf_k": consult_rrf_k,
        "trajectory_export_enabled": trajectory_export_enabled,
        "trajectory_export_path": trajectory_export_path,
    }


def test_ttse_specs_are_added_beside_skill_rails() -> None:
    config = {
        "react": {
            "ttse": {"enabled": False, "team_enabled": True},
            "evolution": {"skill_evolution": False},
        }
    }
    leader = [rail.type for rail in _role_evolution_rails(config, "leader")]
    teammate = [rail.type for rail in _role_evolution_rails(config, "teammate")]
    assert registry.TEAM_TTSE in leader
    assert registry.TEAM_SKILL_EVOLUTION in leader
    assert registry.MEMBER_TTSE in teammate
    assert registry.MEMBER_SKILL_EVOLUTION in teammate


def test_single_agent_ttse_switch_does_not_mount_team_rails() -> None:
    config = {
        "react": {
            "ttse": {"enabled": True},
            "evolution": {"skill_evolution": False},
        }
    }
    leader = [rail.type for rail in _role_evolution_rails(config, "leader")]
    teammate = [rail.type for rail in _role_evolution_rails(config, "teammate")]
    assert registry.TEAM_TTSE not in leader
    assert registry.MEMBER_TTSE not in teammate


def test_ttse_off_adds_no_ttse_rail() -> None:
    config = {"react": {"evolution": {"skill_evolution": False}}}
    leader = [rail.type for rail in _role_evolution_rails(config, "leader")]
    teammate = [rail.type for rail in _role_evolution_rails(config, "teammate")]
    assert registry.TEAM_TTSE not in leader
    assert registry.MEMBER_TTSE not in teammate
    assert registry.TEAM_SKILL_EVOLUTION in leader
    assert registry.MEMBER_SKILL_EVOLUTION in teammate


def test_shared_ttse_bank_path_joins_agents_under_one_service(tmp_path) -> None:
    from jiuwenswarm.common.config import shared_ttse_bank_path

    service = tmp_path / "service_default"
    office = service / "agent_office" / "agent" / "jiuwenclaw_workspace"
    team = service / "agent_agentteam" / "agent" / "jiuwenclaw_workspace"
    office.mkdir(parents=True)
    team.mkdir(parents=True)
    bank = service / ".ttse" / "bank.json"
    assert shared_ttse_bank_path(office) == bank
    assert shared_ttse_bank_path(team) == bank
    assert shared_ttse_bank_path(tmp_path / "agent_ws") == tmp_path / "agent_ws" / ".ttse" / "bank.json"


def test_resolve_shared_ttse_bank_moves_legacy_workspace_bank(tmp_path) -> None:
    from jiuwenswarm.common.config import resolve_shared_ttse_bank

    service = tmp_path / "service_default"
    office = service / "agent_office" / "agent" / "jiuwenclaw_workspace"
    legacy = office / ".ttse"
    legacy.mkdir(parents=True)
    payload = '{"facts":[{"text":"keep"}],"tips":[]}'
    (legacy / "bank.json").write_text(payload, encoding="utf-8")
    (legacy / "chroma").mkdir()
    (legacy / "chroma" / "fingerprint.json").write_text("{}", encoding="utf-8")
    (legacy / "dream-state.json").write_text("{}", encoding="utf-8")

    resolved = resolve_shared_ttse_bank(office)

    new_bank = service / ".ttse" / "bank.json"
    assert resolved == new_bank
    assert new_bank.read_text(encoding="utf-8") == payload
    assert not (legacy / "bank.json").exists()
    assert (service / ".ttse" / "chroma" / "fingerprint.json").is_file()
    assert (service / ".ttse" / "dream-state.json").is_file()


def test_resolve_shared_ttse_bank_keeps_existing_service_bank(tmp_path) -> None:
    from jiuwenswarm.common.config import resolve_shared_ttse_bank

    service = tmp_path / "service_default"
    office = service / "agent_office" / "agent" / "jiuwenclaw_workspace"
    legacy = office / ".ttse"
    legacy.mkdir(parents=True)
    (legacy / "bank.json").write_text('{"facts":[{"text":"old"}]}', encoding="utf-8")
    new_dir = service / ".ttse"
    new_dir.mkdir(parents=True)
    (new_dir / "bank.json").write_text('{"facts":[{"text":"current"}]}', encoding="utf-8")

    resolved = resolve_shared_ttse_bank(office)

    assert resolved == new_dir / "bank.json"
    assert (new_dir / "bank.json").read_text(encoding="utf-8") == '{"facts":[{"text":"current"}]}'
    assert (legacy / "bank.json").read_text(encoding="utf-8") == '{"facts":[{"text":"old"}]}'


def test_resolve_shared_ttse_bank_keeps_legacy_when_move_fails(tmp_path) -> None:
    from jiuwenswarm.common.config import resolve_shared_ttse_bank

    service = tmp_path / "service_default"
    office = service / "agent_office" / "agent" / "jiuwenclaw_workspace"
    legacy = office / ".ttse"
    legacy.mkdir(parents=True)
    (legacy / "bank.json").write_text('{"facts":[{"text":"old"}]}', encoding="utf-8")
    (service / ".ttse").write_text("not-a-directory", encoding="utf-8")

    resolved = resolve_shared_ttse_bank(office)

    assert resolved == legacy / "bank.json"
    assert resolved.read_text(encoding="utf-8") == '{"facts":[{"text":"old"}]}'


def test_explicit_service_workspace_does_not_fall_back_to_service_default(tmp_path, monkeypatch) -> None:
    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import resolve_process_agent_workspace
    from jiuwenswarm.agents.swarm.config_specs import _ttse_bank_path
    from jiuwenswarm.server.runtime.tenant_context import (
        bind_jiuwenclaw_workspace,
        get_bound_jiuwenclaw_workspace,
        reset_tenant_workspace_dirs,
    )

    monkeypatch.setattr("jiuwenswarm.common.utils.is_enterprise", lambda: False)
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    workspace = resolve_process_agent_workspace(service_id="billing", agent_id="office")
    assert "service_default" not in workspace
    assert workspace.endswith(str(tmp_path / "service_billing" / "agent_office" / "agent" / "jiuwenclaw_workspace"))

    tokens = bind_jiuwenclaw_workspace(workspace)
    try:
        bank = _ttse_bank_path("leader")
    finally:
        reset_tenant_workspace_dirs(tokens)

    assert bank == str(tmp_path / "service_billing" / ".ttse" / "bank.json")
    assert get_bound_jiuwenclaw_workspace() is None


def test_ttse_params_bake_banks_switches_and_model(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.agents.swarm.config_specs.get_agent_workspace_dir",
        lambda: tmp_path / "agent_ws",
    )
    params = _ttse_rail_params(
        {
            "react": {
                "ttse": {
                    "enabled": True,
                    "evolve_enabled": False,
                    "inject_enabled": True,
                    "dream_enabled": False,
                    "model_name": "deepseek-flash",
                    "embedding": {
                        "api_key": "k",
                        "base_url": "https://emb.example",
                        "model": "emb-1",
                    },
                }
            },
            "models": {
                "defaults": [
                    {
                        "model_client_config": {
                            "model_name": "deepseek-flash",
                            "api_base": "https://api.deepseek.com",
                        },
                        "model_config_obj": {"context_window": 262144},
                    }
                ]
            },
        }
    )
    assert params["evolve_enabled"] is False
    assert params["inject_enabled"] is True
    assert params["dream_enabled"] is False
    shared = str(tmp_path / "agent_ws" / ".ttse" / "bank.json")
    assert params["leader_bank_path"] == shared
    assert params["member_bank_path"] == shared
    assert params["evolution_model_config"]["model_name"] == "deepseek-flash"
    assert params["evolution_model_config"]["model_client_config"]["api_base"] == "https://api.deepseek.com"
    assert params["embedding_config"] == {
        "api_key": "k",
        "base_url": "https://emb.example",
        "model": "emb-1",
    }
    assert params["dream_interval"] == 50
    assert params["dream_min_hours"] == 24.0
    assert params["dream_ttl_days"] == 90
    assert params["consult_top_k"] == 8
    assert params["consult_retrieve_mode"] == "hybrid"
    assert params["consult_rrf_k"] == 60
    assert params["trajectory_export_enabled"] is False
    assert params["trajectory_export_path"] == ""
    overridden = _ttse_rail_params(
        {
            "react": {
                "ttse": {
                    "dream_interval": 80,
                    "dream_min_hours": 12,
                    "dream_ttl_days": 30,
                    "consult_top_k": 4,
                    "consult_retrieve_mode": "embed",
                    "consult_rrf_k": 20,
                    "trajectory_export_enabled": True,
                    "trajectory_export_path": "traj.json",
                }
            }
        }
    )
    assert overridden["dream_interval"] == 80
    assert overridden["dream_min_hours"] == 12.0
    assert overridden["dream_ttl_days"] == 30
    assert overridden["consult_top_k"] == 4
    assert overridden["consult_retrieve_mode"] == "embed"
    assert overridden["consult_rrf_k"] == 20
    assert overridden["trajectory_export_enabled"] is True
    assert overridden["trajectory_export_path"] == "traj.json"


def test_member_builder_forces_evolve_and_dream_off(monkeypatch) -> None:
    captured: dict = {}

    class FakeTTSERail:
        def __init__(
            self,
            *,
            llm,
            model,
            ttse_config,
            trajectory_span_processor,
            embedding=None,
            catalog_omit_categories=(),
            **kwargs,
        ):
            del llm, model, trajectory_span_processor, embedding, kwargs
            captured["ttse_config"] = ttse_config
            captured["catalog_omit_categories"] = catalog_omit_categories

    monkeypatch.setattr(evolution_rails, "_build_evolution_llm_from", lambda config: (object(), "dummy"))
    monkeypatch.setattr(evolution_rails, "get_trajectory_span_processor", lambda: object())
    monkeypatch.setattr(
        "openjiuwen.harness.rails.evolution.ttse_rail.TTSERail",
        FakeTTSERail,
    )
    params = _baked_params(
        evolve_enabled=True,
        dream_enabled=True,
        inject_enabled=True,
        member_bank=r"D:\tenant\.ttse\member\bank.json",
        dream_interval=50,
        consult_top_k=3,
        consult_retrieve_mode="bm25",
        consult_rrf_k=40,
        trajectory_export_enabled=True,
        trajectory_export_path=r"D:\tenant\traj.json",
    )
    ctx = SimpleNamespace(role="teammate", config={"react": {"ttse": {"enabled": False}}})
    rails = evolution_rails.build_member_ttse_rail(params, ctx)
    assert len(rails) == 1
    config = captured["ttse_config"]
    assert config.evolve_enabled is False
    assert config.dream_enabled is False
    assert config.inject_enabled is True
    assert config.store_path == params["member_bank_path"]
    assert config.dream_interval == 50
    assert config.consult_top_k == 3
    assert config.consult_retrieve_mode == "bm25"
    assert config.consult_rrf_k == 40
    assert config.trajectory_export_enabled is True
    assert config.trajectory_export_path == r"D:\tenant\traj.json"
    assert captured["catalog_omit_categories"] == ("team-planning-and-acceptance",)


def test_ttse_model_name_is_taken_from_baked_params(monkeypatch) -> None:
    seen: dict = {}

    def _capture(config):
        seen.update(config)
        return object(), config["model_name"]

    class FakeTeamTTSERail:
        def __init__(
            self,
            *,
            llm,
            model,
            member_store_path,
            ttse_config,
            trajectory_span_processor,
            embedding=None,
            **kwargs,
        ):
            del llm, model, member_store_path, ttse_config, trajectory_span_processor, embedding, kwargs

    monkeypatch.setattr(evolution_rails, "_build_evolution_llm_from", _capture)
    monkeypatch.setattr(evolution_rails, "get_trajectory_span_processor", lambda: object())
    module = types.ModuleType("openjiuwen.harness.rails.evolution.team_ttse_rail")
    module.TeamTTSERail = FakeTeamTTSERail
    monkeypatch.setitem(sys.modules, "openjiuwen.harness.rails.evolution.team_ttse_rail", module)
    params = _baked_params(
        evolution_model_config={
            "model_client_config": {"model_name": "deepseek-flash", "api_base": "https://api.deepseek.com"},
            "model_config_obj": {"context_window": 262144},
            "model_name": "deepseek-flash",
        }
    )
    ctx = SimpleNamespace(role="leader", config={"react": {"ttse": {"enabled": False}}})
    assert evolution_rails.build_team_ttse_rail(params, ctx)
    assert seen["model_name"] == "deepseek-flash"
    assert seen["model_client_config"]["api_base"] == "https://api.deepseek.com"


def test_leader_builder_uses_baked_bank_paths(monkeypatch) -> None:
    captured: dict = {}

    class FakeTeamTTSERail:
        def __init__(
            self,
            *,
            llm,
            model,
            member_store_path,
            ttse_config,
            trajectory_span_processor,
            embedding=None,
            **kwargs,
        ):
            del llm, model, trajectory_span_processor, embedding, kwargs
            captured["member_store_path"] = member_store_path
            captured["ttse_config"] = ttse_config

    monkeypatch.setattr(evolution_rails, "_build_evolution_llm_from", lambda config: (object(), "dummy"))
    monkeypatch.setattr(evolution_rails, "get_trajectory_span_processor", lambda: object())
    module = types.ModuleType("openjiuwen.harness.rails.evolution.team_ttse_rail")
    module.TeamTTSERail = FakeTeamTTSERail
    monkeypatch.setitem(sys.modules, "openjiuwen.harness.rails.evolution.team_ttse_rail", module)
    params = _baked_params(
        evolve_enabled=True,
        dream_enabled=False,
        leader_bank=r"D:\tenant\.ttse\leader\bank.json",
        member_bank=r"D:\tenant\.ttse\member\bank.json",
        dream_interval=50,
        consult_retrieve_mode="hybrid",
    )
    # Receiving process config is off / has no tenant CV: builders must still use baked paths.
    ctx = SimpleNamespace(role="leader", config={"react": {"ttse": {"enabled": False}}})
    rails = evolution_rails.build_team_ttse_rail(params, ctx)
    assert len(rails) == 1
    assert captured["ttse_config"].store_path == params["leader_bank_path"]
    assert captured["ttse_config"].evolve_enabled is True
    assert captured["ttse_config"].dream_enabled is False
    assert captured["ttse_config"].dream_interval == 50
    assert captured["ttse_config"].consult_retrieve_mode == "hybrid"
    assert captured["member_store_path"] == params["member_bank_path"]
