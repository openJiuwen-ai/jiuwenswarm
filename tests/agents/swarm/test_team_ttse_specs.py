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
    }


def test_ttse_specs_are_added_beside_skill_rails() -> None:
    config = {
        "react": {
            "ttse": {"enabled": True},
            "evolution": {"skill_evolution": False},
        }
    }
    leader = [rail.type for rail in _role_evolution_rails(config, "leader")]
    teammate = [rail.type for rail in _role_evolution_rails(config, "teammate")]
    assert registry.TEAM_TTSE in leader
    assert registry.TEAM_SKILL_EVOLUTION in leader
    assert registry.MEMBER_TTSE in teammate
    assert registry.MEMBER_SKILL_EVOLUTION in teammate


def test_ttse_off_adds_no_ttse_rail() -> None:
    config = {"react": {"evolution": {"skill_evolution": False}}}
    leader = [rail.type for rail in _role_evolution_rails(config, "leader")]
    teammate = [rail.type for rail in _role_evolution_rails(config, "teammate")]
    assert registry.TEAM_TTSE not in leader
    assert registry.MEMBER_TTSE not in teammate
    assert registry.TEAM_SKILL_EVOLUTION in leader
    assert registry.MEMBER_SKILL_EVOLUTION in teammate


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
    assert params["leader_bank_path"] == str(tmp_path / "agent_ws" / ".ttse" / "leader" / "bank.json")
    assert params["member_bank_path"] == str(tmp_path / "agent_ws" / ".ttse" / "member" / "bank.json")
    assert params["evolution_model_config"]["model_name"] == "deepseek-flash"
    assert params["evolution_model_config"]["model_client_config"]["api_base"] == "https://api.deepseek.com"
    assert params["embedding_config"] == {
        "api_key": "k",
        "base_url": "https://emb.example",
        "model": "emb-1",
    }


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
            **kwargs,
        ):
            del llm, model, trajectory_span_processor, embedding, kwargs
            captured["ttse_config"] = ttse_config

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
    )
    ctx = SimpleNamespace(role="teammate", config={"react": {"ttse": {"enabled": False}}})
    rails = evolution_rails.build_member_ttse_rail(params, ctx)
    assert len(rails) == 1
    config = captured["ttse_config"]
    assert config.evolve_enabled is False
    assert config.dream_enabled is False
    assert config.inject_enabled is True
    assert config.store_path == params["member_bank_path"]


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
    )
    # Receiving process config is off / has no tenant CV: builders must still use baked paths.
    ctx = SimpleNamespace(role="leader", config={"react": {"ttse": {"enabled": False}}})
    rails = evolution_rails.build_team_ttse_rail(params, ctx)
    assert len(rails) == 1
    assert captured["ttse_config"].store_path == params["leader_bank_path"]
    assert captured["ttse_config"].evolve_enabled is True
    assert captured["ttse_config"].dream_enabled is False
    assert captured["member_store_path"] == params["member_bank_path"]
