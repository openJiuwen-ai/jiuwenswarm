"""Runtime configuration for the Symphony prompt optimizer.

Loaded from ``config.yaml`` under ``symphony.optimization``. Kept independent of
:class:`jiuwenswarm.symphony.config.SymphonyConfig` (which is imported widely and
frozen) so the optimizer can evolve its own settings without touching the shared
score-build config — the same pattern ``symphony.skill_retrieval`` follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jiuwenswarm.common.utils import get_agent_workspace_dir

DEFAULT_ENABLED = False
DEFAULT_CANDIDATE_PROMPTS = 5
DEFAULT_MAX_ITERATIONS = 6
DEFAULT_PARALLEL_EXECUTION = True
DEFAULT_CONVERGENCE_THRESHOLD = 0.01
DEFAULT_CONVERGENCE_WINDOW = 3
DEFAULT_DRIFT_PENALTY = 0.5
DEFAULT_MIN_CORRECTNESS = 0.5
DEFAULT_MEMORY_ENABLED = True
DEFAULT_POLICY_TEMPERATURE = 0.9

DEFAULT_REWARD_WEIGHTS: dict[str, float] = {
    "correctness": 1.0,
    "completeness": 0.3,
    "latency": 0.1,
    "token_usage": 0.1,
    "cost": 0.0,
    "structured_validation": 0.0,
}


@dataclass(frozen=True)
class OptimizationModelsConfig:
    """Model overrides for each optimizer role.

    An empty string means "use the JiuwenSwarm default model" (see
    :func:`jiuwenswarm.symphony.optimization.llm_support.resolve_llm_config`).
    """

    policy_model: str = ""
    environment_model: str = ""
    judge_model: str = ""


@dataclass(frozen=True)
class OptimizationEmbeddingConfig:
    """Embedding backend for prompt memory (reuses ``EmbeddingClient``)."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    model_name: str = ""
    dimension: int | None = None


@dataclass(frozen=True)
class OptimizationConfig:
    """Resolved ``symphony.optimization`` settings."""

    enabled: bool = DEFAULT_ENABLED
    candidate_prompts: int = DEFAULT_CANDIDATE_PROMPTS
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    parallel_execution: bool = DEFAULT_PARALLEL_EXECUTION
    convergence_threshold: float = DEFAULT_CONVERGENCE_THRESHOLD
    convergence_window: int = DEFAULT_CONVERGENCE_WINDOW
    drift_penalty: float = DEFAULT_DRIFT_PENALTY
    min_correctness: float = DEFAULT_MIN_CORRECTNESS
    memory_enabled: bool = DEFAULT_MEMORY_ENABLED
    memory_dir: Path | None = None
    policy_temperature: float = DEFAULT_POLICY_TEMPERATURE
    reward_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_REWARD_WEIGHTS)
    )
    models: OptimizationModelsConfig = field(default_factory=OptimizationModelsConfig)
    embedding: OptimizationEmbeddingConfig = field(
        default_factory=OptimizationEmbeddingConfig
    )

    @property
    def resolved_memory_dir(self) -> Path:
        if self.memory_dir is not None:
            return self.memory_dir
        return (
            get_agent_workspace_dir() / "symphony" / "optimization" / "prompt_kb"
        ).resolve()


def load_optimization_config(config: dict[str, Any] | None = None) -> OptimizationConfig:
    """Load and normalize ``symphony.optimization`` settings from ``config.yaml``."""

    if config is None:
        from jiuwenswarm.common.config import get_config

        config = get_config() or {}
    symphony = config.get("symphony") if isinstance(config, dict) else {}
    raw = symphony.get("optimization") if isinstance(symphony, dict) else {}
    return optimization_config_from_dict(raw if isinstance(raw, dict) else {})


def default_optimization_config() -> OptimizationConfig:
    return optimization_config_from_dict({})


def optimization_config_from_dict(raw: dict[str, Any] | None) -> OptimizationConfig:
    data = raw if isinstance(raw, dict) else {}
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    embedding = data.get("embedding") if isinstance(data.get("embedding"), dict) else {}

    return OptimizationConfig(
        enabled=_bool(data.get("enabled"), DEFAULT_ENABLED),
        candidate_prompts=_positive_int(
            data.get("candidate_prompts"), DEFAULT_CANDIDATE_PROMPTS
        ),
        max_iterations=_positive_int(data.get("max_iterations"), DEFAULT_MAX_ITERATIONS),
        parallel_execution=_bool(
            data.get("parallel_execution"), DEFAULT_PARALLEL_EXECUTION
        ),
        convergence_threshold=_non_negative_float(
            data.get("convergence_threshold"), DEFAULT_CONVERGENCE_THRESHOLD
        ),
        convergence_window=_positive_int(
            data.get("convergence_window"), DEFAULT_CONVERGENCE_WINDOW
        ),
        drift_penalty=_non_negative_float(data.get("drift_penalty"), DEFAULT_DRIFT_PENALTY),
        min_correctness=_clamped_float(data.get("min_correctness"), DEFAULT_MIN_CORRECTNESS),
        memory_enabled=_bool(data.get("memory_enabled"), DEFAULT_MEMORY_ENABLED),
        memory_dir=_optional_path(data.get("memory_dir")),
        policy_temperature=_non_negative_float(
            data.get("policy_temperature"), DEFAULT_POLICY_TEMPERATURE
        ),
        reward_weights=_reward_weights(data.get("reward_weights")),
        models=OptimizationModelsConfig(
            policy_model=_text(models.get("policy_model")),
            environment_model=_text(models.get("environment_model")),
            judge_model=_text(models.get("judge_model")),
        ),
        embedding=OptimizationEmbeddingConfig(
            base_url=_text(embedding.get("base_url")),
            api_key=_text(embedding.get("api_key")),
            model=_text(embedding.get("model")),
            model_name=_text(embedding.get("model_name")),
            dimension=_optional_positive_int(embedding.get("dimension"), None),
        ),
    )


# --------------------------------------------------------------------------
# Normalization helpers (mirrors jiuwenswarm.symphony.config)
# --------------------------------------------------------------------------


def _reward_weights(value: Any) -> dict[str, float]:
    weights = dict(DEFAULT_REWARD_WEIGHTS)
    if isinstance(value, dict):
        for key, raw in value.items():
            try:
                weights[str(key)] = max(0.0, float(raw))
            except (TypeError, ValueError):
                continue
    return weights


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_path(value: Any) -> Path | None:
    text = _text(value)
    if not text:
        return None
    return Path(text).expanduser().resolve()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _optional_positive_int(value: Any, default: int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _clamped_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


__all__ = [
    "OptimizationConfig",
    "OptimizationModelsConfig",
    "OptimizationEmbeddingConfig",
    "load_optimization_config",
    "default_optimization_config",
    "optimization_config_from_dict",
    "DEFAULT_REWARD_WEIGHTS",
]
