import pytest

from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec

from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec


def _build_member(*, enabled: bool, mode: str = "team", role: str = "leader") -> DeepAgentSpec:
    config = {
        "models": {
            "defaults": [{
                "is_default": True,
                "model_client_config": {"client_provider": "AscendAffinity"},
            }]
        },
        "react": {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": enabled,
                "enable_kv_cache_release": False,
            }
        }
    }
    return build_member_deep_agent_spec(
        config,
        mode,
        role,
        DeepAgentSpec(),
    )


def test_team_member_affinity_is_disabled_by_default() -> None:
    spec = build_member_deep_agent_spec({}, "team", "leader", DeepAgentSpec())

    assert spec.kv_cache_affinity_config is not None
    assert spec.kv_cache_affinity_config.enable_kv_cache_affinity is False


@pytest.mark.parametrize(
    ("mode", "role"),
    [
        ("team", "leader"),
        ("team", "teammate"),
        ("team.plan", "leader"),
        ("team.plan", "teammate"),
    ],
)
def test_team_member_receives_enabled_affinity_config(mode: str, role: str) -> None:
    spec = _build_member(enabled=True, mode=mode, role=role)

    assert spec.kv_cache_affinity_config is not None
    assert spec.kv_cache_affinity_config.enable_kv_cache_affinity is True


def test_team_member_receives_disabled_affinity_config() -> None:
    spec = _build_member(enabled=False)

    assert spec.kv_cache_affinity_config is not None
    assert spec.kv_cache_affinity_config.enable_kv_cache_affinity is False


def test_team_member_affinity_fails_closed_for_non_ascend_provider() -> None:
    config = {
        "models": {
            "defaults": [{
                "is_default": True,
                "model_client_config": {"client_provider": "OpenAI"},
            }]
        },
        "react": {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": True,
                "enable_kv_cache_release": False,
            }
        },
    }

    spec = build_member_deep_agent_spec(config, "team", "leader", DeepAgentSpec())

    assert spec.kv_cache_affinity_config.enable_kv_cache_affinity is False
