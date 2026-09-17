from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.symphony.skill_retrieval.taxonomy_config import coerce_root_categories_value


@dataclass(frozen=True)
class LLMSettings:
    model: str
    api_key: str
    base_url: str
    seed: int | None = None


@dataclass(frozen=True)
class BuildSettings:
    branching_factor: int = 128
    max_depth: int = 6
    root_categories: Any = None
    max_workers: int = 2
    max_retries: int = 2
    request_timeout_seconds: float = 420.0
    total_timeout_seconds: float = 0.0
    classification_batch_limit: int = 32
    discovery_seed: int = 42
    postprocess_enabled: bool = True
    postprocess_max_passes: int = 1
    postprocess_min_skills: int = 6
    equivalence_enabled: bool = True


@dataclass(frozen=True)
class RetrieveSettings:
    top_k: int = 10
    compact_codes_enabled: bool = False
    flatten_tree: bool = False
    max_exposure_depth: int = 1
    max_branch_choices: int = 2
    max_parallel_branches: int = 2
    max_tokens: int = 96
    request_timeout_seconds: float = 120.0


@dataclass(frozen=True)
class SkillRetrievalSettings:
    enabled: bool
    artifact_root: Path
    llm: LLMSettings
    build: BuildSettings
    retrieve: RetrieveSettings


def load_settings() -> SkillRetrievalSettings:
    config = get_config() or {}
    section = _dict_get(config, "symphony", "skill_retrieval")
    artifact_root = _artifact_root(section)
    return SkillRetrievalSettings(
        enabled=_enabled(section),
        artifact_root=artifact_root,
        llm=_load_llm(section, config),
        build=_load_build(section.get("build") if isinstance(section.get("build"), dict) else {}),
        retrieve=_load_retrieve(section.get("retrieve") if isinstance(section.get("retrieve"), dict) else {}),
    )


def _dict_get(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _artifact_root(section: dict[str, Any]) -> Path:
    raw = str(section.get("artifact_root") or os.getenv("SYMPHONY_SKILL_RETRIEVAL_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return get_agent_workspace_dir() / "symphony" / "skill_retrieval"


def _enabled(section: dict[str, Any]) -> bool:
    env_value = os.getenv("SYMPHONY_SKILL_RETRIEVAL_ENABLED")
    if env_value is not None and env_value.strip() != "":
        return _as_bool(env_value, False)
    return _as_bool(section.get("enabled"), False)


def _load_llm(section: dict[str, Any], config: dict[str, Any]) -> LLMSettings:
    override = section.get("llm") if isinstance(section.get("llm"), dict) else {}
    model_cfg = _default_model_client_config(config)
    model = str(
        override.get("model")
        or override.get("model_name")
        or model_cfg.get("model")
        or model_cfg.get("model_name")
        or os.getenv("MODEL_NAME")
        or ""
    ).strip()
    api_key = str(override.get("api_key") or model_cfg.get("api_key") or os.getenv("API_KEY") or "").strip()
    base_url = str(
        override.get("base_url")
        or override.get("api_base")
        or model_cfg.get("base_url")
        or model_cfg.get("api_base")
        or os.getenv("API_BASE")
        or ""
    ).strip()
    seed = _as_optional_int(override.get("seed"))
    return LLMSettings(model=model, api_key=api_key, base_url=base_url, seed=seed)


def _default_model_client_config(config: dict[str, Any]) -> dict[str, Any]:
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    defaults = models.get("defaults")
    if isinstance(defaults, list) and defaults:
        first = defaults[0]
        if isinstance(first, dict):
            mcc = first.get("model_client_config")
            if isinstance(mcc, dict):
                return mcc
    default = models.get("default")
    if isinstance(default, dict):
        mcc = default.get("model_client_config")
        if isinstance(mcc, dict):
            return mcc
    return {}


def _load_build(raw: dict[str, Any]) -> BuildSettings:
    return BuildSettings(
        branching_factor=_as_int(raw.get("branching_factor"), 128),
        max_depth=_as_int(raw.get("max_depth"), 6),
        root_categories=_as_optional_root_categories(raw.get("root_categories")),
        max_workers=_as_int(raw.get("max_workers"), 2),
        max_retries=_as_non_negative_int(raw.get("max_retries"), 2),
        request_timeout_seconds=_as_float(raw.get("request_timeout_seconds"), 420.0),
        total_timeout_seconds=_as_float(raw.get("total_timeout_seconds"), 0.0),
        classification_batch_limit=_as_int(raw.get("classification_batch_limit"), 32),
        discovery_seed=_as_raw_int(raw.get("discovery_seed"), 42),
        postprocess_enabled=_as_bool(raw.get("postprocess_enabled"), True),
        postprocess_max_passes=_as_non_negative_int(raw.get("postprocess_max_passes"), 1),
        postprocess_min_skills=_as_int(raw.get("postprocess_min_skills"), 6),
        equivalence_enabled=_as_bool(raw.get("equivalence_enabled"), True),
    )


def _load_retrieve(raw: dict[str, Any]) -> RetrieveSettings:
    return RetrieveSettings(
        top_k=_as_int(raw.get("top_k"), 10),
        compact_codes_enabled=_as_bool(raw.get("compact_codes_enabled"), False),
        flatten_tree=_as_bool(raw.get("flatten_tree"), False),
        max_exposure_depth=_as_int(raw.get("max_exposure_depth"), 1),
    )


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "enabled"}


def _as_optional_root_categories(value: Any) -> Any:
    return coerce_root_categories_value(value, allow_path=True)


def _as_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _as_non_negative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _as_raw_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
