"""JiuwenSwarm adapters for the public :mod:`openjiuwen.symphony` API."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from typing import Any

from openjiuwen.symphony import (
    ArtifactSpec,
    CapabilityFingerprint,
    OrchestrationConfig,
    ParameterSpec,
)

from jiuwenswarm.symphony.config import SymphonyConfig
from jiuwenswarm.symphony.llm import LLMConfig, create_model_response_observer


def capability_from_skill(value: Any) -> CapabilityFingerprint:
    """Map an extracted JiuwenSwarm Skill fingerprint to a public capability."""

    payload = value.to_dict() if hasattr(value, "to_dict") else dict(value)
    capability_id = str(payload.get("capability_id") or payload.get("id") or "").strip()
    if not capability_id:
        raise ValueError("Skill fingerprint requires an id.")
    capability_type = str(
        payload.get("capability_type") or payload.get("type") or "skill"
    ).strip()
    return CapabilityFingerprint(
        capability_id=capability_id,
        capability_type=capability_type,
        name=str(payload.get("name") or capability_id),
        description=str(payload.get("description") or ""),
        version=str(payload.get("version") or "1.0.0"),
        inputs=[
            item
            if isinstance(item, ParameterSpec)
            else ParameterSpec(
                name=str(_field(item, "name") or "input"),
                type=str(_field(item, "type") or "unknown"),
                required=bool(_field(item, "required", True)),
                description=str(_field(item, "description") or ""),
                default=_field(item, "default"),
            )
            for item in payload.get("inputs", [])
        ],
        outputs=[
            item
            if isinstance(item, ArtifactSpec)
            else ArtifactSpec(
                name=str(_field(item, "name") or "result"),
                type=str(_field(item, "type") or "unknown"),
                description=str(_field(item, "description") or ""),
            )
            for item in payload.get("outputs", [])
        ],
        static_data=dict(payload.get("static_data") or {}),
    )


def capabilities_from_skills(values: Iterable[Any]) -> list[CapabilityFingerprint]:
    return [capability_from_skill(value) for value in values]


def candidate_ids_from_skill_ids(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        candidate_id = str(item or "").strip()
        if candidate_id and candidate_id not in seen:
            output.append(candidate_id)
            seen.add(candidate_id)
    return output


def orchestration_config_from_swarm(
    config: SymphonyConfig,
    *,
    mode: str | None = None,
) -> OrchestrationConfig:
    orchestration = config.orchestration
    return OrchestrationConfig(
        mode=mode or orchestration.mode,
        top_k=orchestration.top_k,
        max_depth=orchestration.max_depth,
        min_edge_confidence=orchestration.min_edge_confidence,
        dynamic_graph_enabled=config.evolution.enabled,
    )


def graph_build_orchestration_config_from_swarm(
    config: SymphonyConfig,
) -> OrchestrationConfig:
    """Use build-time relation thresholds without changing planning policy."""

    orchestration = config.orchestration
    return OrchestrationConfig(
        mode=orchestration.mode,
        top_k=orchestration.top_k,
        max_depth=orchestration.max_depth,
        min_edge_confidence=config.build.min_edge_confidence,
        dynamic_graph_enabled=config.evolution.enabled,
    )


def graph_config_from_swarm(config: SymphonyConfig) -> dict[str, Any]:
    build = config.build
    if is_dataclass(build) and not isinstance(build, type):
        return asdict(build)
    graph_config: dict[str, Any] = {}
    for key in (
        "workers",
        "batch_size",
        "max_candidates_per_skill_relation",
        "require_consensus",
        "min_edge_confidence",
    ):
        graph_config[key] = getattr(build, key)
    return graph_config


def llm_config_signature(config: LLMConfig) -> str:
    """Hash all client-affecting settings without retaining sensitive text."""

    return config.identity_digest()


def model_from_config(config: LLMConfig):
    return config.create_model()


def model_response_observer_from_config(config: LLMConfig):
    return create_model_response_observer(config)


def swarm_plan_from_public(value: Any) -> Any:
    """Restore the Agent-tool field vocabulary while core stays capability-first."""

    key_mapping = {
        "capability_id": "skill_id",
        "capability_ids": "skill_ids",
        "candidate_ids": "candidate_skill_ids",
        "candidate_count": "candidate_skill_count",
    }
    if isinstance(value, dict):
        return {
            key_mapping.get(key, key): swarm_plan_from_public(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [swarm_plan_from_public(item) for item in value]
    if isinstance(value, tuple):
        return [swarm_plan_from_public(item) for item in value]
    return value


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)
