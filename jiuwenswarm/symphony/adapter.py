"""JiuwenSwarm adapters for the public :mod:`openjiuwen.symphony` API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from openjiuwen.symphony import (
    CapabilityDescriptor,
    FingerprintSettings,
    OrchestrationConfig,
    ScanResult,
    SourceSnapshot,
)

from jiuwenswarm.symphony.config import SymphonyConfig
from jiuwenswarm.symphony.llm import LLMConfig, create_model_response_observer


@dataclass(frozen=True)
class ScanResultCapabilityProvider:
    """Expose one immutable core scanner result through CapabilityProvider."""

    result: ScanResult

    async def capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        return self.result.capabilities

    async def source_snapshot(self) -> SourceSnapshot:
        return self.result.source_snapshot

    async def inventory_snapshot(
        self,
    ) -> tuple[SourceSnapshot, tuple[CapabilityDescriptor, ...]]:
        return self.result.source_snapshot, self.result.capabilities


class FingerprintLLMAdapter:
    """Tag core fingerprint model calls for JiuwenSwarm usage accounting."""

    def __init__(self, config: LLMConfig) -> None:
        self._model = model_from_config(config)
        self._observe = model_response_observer_from_config(config)
        self.cache_signature = llm_config_signature(config)

    async def invoke(self, messages: Any, **kwargs: Any) -> object:
        response = await self._model.invoke(messages, **kwargs)
        self._observe(
            response,
            "fingerprint_extraction",
            "capability_extraction",
        )
        return response


def fingerprint_settings_from_swarm(
    config: SymphonyConfig,
    llm_config: LLMConfig | None,
) -> FingerprintSettings:
    """Translate app configuration into the core fingerprint contract."""

    defaults = FingerprintSettings()
    extraction = config.fingerprint.extraction
    return FingerprintSettings(
        enable_llm_extraction=llm_config is not None,
        enable_llm_evaluation=False,
        batch_size=extraction.batch_size,
        max_concurrency=extraction.workers,
        body_limit=(
            defaults.body_limit
            if extraction.body_limit is None
            else extraction.body_limit
        ),
        llm_max_tokens=defaults.llm_max_tokens,
        llm_timeout=defaults.llm_timeout,
        evidence_text_limit=defaults.evidence_text_limit,
        cache_enabled=True,
        extraction_protocol_version=defaults.extraction_protocol_version,
        evaluation_protocol_version=defaults.evaluation_protocol_version,
        configuration_signature=(
            llm_config_signature(llm_config) if llm_config is not None else ""
        ),
    )


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
