"""Runtime configuration for Symphony orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_workspace_dir


DEFAULT_FINGERPRINT_EXTRACTION_WORKERS = 1
DEFAULT_FINGERPRINT_EXTRACTION_BATCH_SIZE = 1
DEFAULT_FINGERPRINT_EXTRACTION_BODY_LIMIT = None
DEFAULT_FINGERPRINT_SCAN_MAX_DEPTH = None

DEFAULT_FINGERPRINT_NORMALIZATION_WORKERS = 1
DEFAULT_FINGERPRINT_NORMALIZATION_BATCH_SIZE = 1
DEFAULT_FINGERPRINT_NORMALIZATION_DUPLICATE_THRESHOLD = 0.86
DEFAULT_FINGERPRINT_NORMALIZATION_MAX_VOCAB_SIZE = None

DEFAULT_BUILD_WORKERS = 1
DEFAULT_BUILD_MATCHER_BATCH_SIZE = 12
DEFAULT_BUILD_MAX_CANDIDATES_PER_SKILL_RELATION = 32
DEFAULT_BUILD_REQUIRE_CONSENSUS = False
DEFAULT_BUILD_MIN_EDGE_CONFIDENCE = 0.5

DEFAULT_SYMPHONY_ENABLED = False
DEFAULT_EVOLUTION_ENABLED = False

DEFAULT_ORCHESTRATION_MODE = "fast"
DEFAULT_ORCHESTRATION_TOP_K = 3
DEFAULT_ORCHESTRATION_MAX_DEPTH = 4
DEFAULT_ORCHESTRATION_MIN_EDGE_CONFIDENCE = 0.5


@dataclass(frozen=True)
class SymphonyPathsConfig:
    skills_root: Path
    score_dir: Path


@dataclass(frozen=True)
class FingerprintScanConfig:
    max_depth: int | None = DEFAULT_FINGERPRINT_SCAN_MAX_DEPTH


@dataclass(frozen=True)
class FingerprintExtractionConfig:
    workers: int = DEFAULT_FINGERPRINT_EXTRACTION_WORKERS
    batch_size: int = DEFAULT_FINGERPRINT_EXTRACTION_BATCH_SIZE
    body_limit: int | None = DEFAULT_FINGERPRINT_EXTRACTION_BODY_LIMIT


@dataclass(frozen=True)
class FingerprintNormalizationConfig:
    workers: int = DEFAULT_FINGERPRINT_NORMALIZATION_WORKERS
    batch_size: int = DEFAULT_FINGERPRINT_NORMALIZATION_BATCH_SIZE
    duplicate_name_similarity_threshold: float = (
        DEFAULT_FINGERPRINT_NORMALIZATION_DUPLICATE_THRESHOLD
    )
    max_vocab_size: int | None = DEFAULT_FINGERPRINT_NORMALIZATION_MAX_VOCAB_SIZE


@dataclass(frozen=True)
class SymphonyFingerprintConfig:
    scan: FingerprintScanConfig
    extraction: FingerprintExtractionConfig
    normalization: FingerprintNormalizationConfig


@dataclass(frozen=True)
class SymphonyBuildConfig:
    workers: int = DEFAULT_BUILD_WORKERS
    batch_size: int = DEFAULT_BUILD_MATCHER_BATCH_SIZE
    max_candidates_per_skill_relation: int = (
        DEFAULT_BUILD_MAX_CANDIDATES_PER_SKILL_RELATION
    )
    require_consensus: bool = DEFAULT_BUILD_REQUIRE_CONSENSUS
    min_edge_confidence: float = DEFAULT_BUILD_MIN_EDGE_CONFIDENCE


@dataclass(frozen=True)
class SymphonyEvolutionConfig:
    enabled: bool = DEFAULT_EVOLUTION_ENABLED


@dataclass(frozen=True)
class SymphonyOrchestrationConfig:
    mode: str = DEFAULT_ORCHESTRATION_MODE
    top_k: int = DEFAULT_ORCHESTRATION_TOP_K
    max_depth: int = DEFAULT_ORCHESTRATION_MAX_DEPTH
    min_edge_confidence: float = DEFAULT_ORCHESTRATION_MIN_EDGE_CONFIDENCE


@dataclass(frozen=True)
class SymphonyConfig:
    enabled: bool
    paths: SymphonyPathsConfig
    fingerprint: SymphonyFingerprintConfig
    build: SymphonyBuildConfig
    evolution: SymphonyEvolutionConfig
    orchestration: SymphonyOrchestrationConfig


def load_symphony_config(config: dict[str, Any] | None = None) -> SymphonyConfig:
    """Load and normalize ``config.yaml`` Symphony settings."""

    if config is None:
        from jiuwenswarm.common.config import get_config

        config = get_config() or {}
    raw = config.get("symphony") if isinstance(config, dict) else {}
    return symphony_config_from_dict(raw if isinstance(raw, dict) else {})


def default_symphony_config() -> SymphonyConfig:
    return symphony_config_from_dict({})


def symphony_config_from_dict(raw: dict[str, Any] | None) -> SymphonyConfig:
    data = raw if isinstance(raw, dict) else {}
    paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    fingerprint = (
        data.get("fingerprint") if isinstance(data.get("fingerprint"), dict) else {}
    )
    scan = fingerprint.get("scan") if isinstance(fingerprint.get("scan"), dict) else {}
    extraction = (
        fingerprint.get("extraction")
        if isinstance(fingerprint.get("extraction"), dict)
        else {}
    )
    normalization = (
        fingerprint.get("normalization")
        if isinstance(fingerprint.get("normalization"), dict)
        else {}
    )
    build = data.get("build") if isinstance(data.get("build"), dict) else {}
    evolution = (
        data.get("evolution") if isinstance(data.get("evolution"), dict) else {}
    )
    orchestration = (
        data.get("orchestration") if isinstance(data.get("orchestration"), dict) else {}
    )

    return SymphonyConfig(
        enabled=_bool(data.get("enabled"), DEFAULT_SYMPHONY_ENABLED),
        paths=SymphonyPathsConfig(
            skills_root=_resolve_path(
                paths.get("skills_root"),
                get_agent_workspace_dir() / "skills",
            ),
            score_dir=_resolve_path(
                paths.get("score_dir"),
                get_agent_workspace_dir() / "symphony" / "score",
            ),
        ),
        fingerprint=SymphonyFingerprintConfig(
            scan=FingerprintScanConfig(
                max_depth=_optional_non_negative_int(
                    scan.get("max_depth"),
                    DEFAULT_FINGERPRINT_SCAN_MAX_DEPTH,
                ),
            ),
            extraction=FingerprintExtractionConfig(
                workers=_positive_int(
                    extraction.get("workers"),
                    DEFAULT_FINGERPRINT_EXTRACTION_WORKERS,
                ),
                batch_size=_positive_int(
                    extraction.get("batch_size"),
                    DEFAULT_FINGERPRINT_EXTRACTION_BATCH_SIZE,
                ),
                body_limit=_optional_body_limit(
                    extraction.get("body_limit"),
                    DEFAULT_FINGERPRINT_EXTRACTION_BODY_LIMIT,
                ),
            ),
            normalization=FingerprintNormalizationConfig(
                workers=_positive_int(
                    normalization.get("workers"),
                    DEFAULT_FINGERPRINT_NORMALIZATION_WORKERS,
                ),
                batch_size=_positive_int(
                    normalization.get("batch_size"),
                    DEFAULT_FINGERPRINT_NORMALIZATION_BATCH_SIZE,
                ),
                duplicate_name_similarity_threshold=_clamped_float(
                    normalization.get("duplicate_name_similarity_threshold"),
                    DEFAULT_FINGERPRINT_NORMALIZATION_DUPLICATE_THRESHOLD,
                ),
                max_vocab_size=_optional_positive_int(
                    normalization.get("max_vocab_size"),
                    DEFAULT_FINGERPRINT_NORMALIZATION_MAX_VOCAB_SIZE,
                ),
            ),
        ),
        build=SymphonyBuildConfig(
            workers=_positive_int(build.get("workers"), DEFAULT_BUILD_WORKERS),
            batch_size=_positive_int(
                build.get("batch_size"),
                DEFAULT_BUILD_MATCHER_BATCH_SIZE,
            ),
            max_candidates_per_skill_relation=_positive_int(
                build.get("max_candidates_per_skill_relation"),
                DEFAULT_BUILD_MAX_CANDIDATES_PER_SKILL_RELATION,
            ),
            require_consensus=_bool(
                build.get("require_consensus"),
                DEFAULT_BUILD_REQUIRE_CONSENSUS,
            ),
            min_edge_confidence=_clamped_float(
                build.get("min_edge_confidence"),
                DEFAULT_BUILD_MIN_EDGE_CONFIDENCE,
            ),
        ),
        evolution=SymphonyEvolutionConfig(
            enabled=_bool(
                evolution.get("enabled"),
                DEFAULT_EVOLUTION_ENABLED,
            ),
        ),
        orchestration=SymphonyOrchestrationConfig(
            mode=_orchestration_mode(
                orchestration.get("mode"),
                DEFAULT_ORCHESTRATION_MODE,
            ),
            top_k=_positive_int(
                orchestration.get("top_k"),
                DEFAULT_ORCHESTRATION_TOP_K,
            ),
            max_depth=_positive_int(
                orchestration.get("max_depth"),
                DEFAULT_ORCHESTRATION_MAX_DEPTH,
            ),
            min_edge_confidence=_clamped_float(
                orchestration.get("min_edge_confidence"),
                DEFAULT_ORCHESTRATION_MIN_EDGE_CONFIDENCE,
            ),
        ),
    )


def _resolve_path(value: Any, default: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return default.resolve()
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
    return _positive_int(value, default or 1)


def _optional_non_negative_int(value: Any, default: int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _optional_body_limit(value: Any, default: int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else None


def _clamped_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


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


def _orchestration_mode(value: Any, default: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"fast", "beam"}:
        return text
    raise ValueError(f"Unsupported Symphony orchestration mode: {value}")
