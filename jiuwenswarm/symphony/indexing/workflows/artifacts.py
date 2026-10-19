from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntFlag
from pathlib import Path
from typing import Any, Dict, List, Sequence

try:
    from openai import OpenAI
except Exception:
    OpenAI = Any

from indexing.catalog.records import CatalogRecord
from indexing.tree.root_categories import RootCategoryInput, resolve_tree_root_categories
from shared.tags import normalize_tags


class BuildMethod(IntFlag):
    """Compatibility enum for callers that still import build methods."""

    TREE = 4


@dataclass(frozen=True)
class BuildLLMConfig:
    """LLM settings for offline tree construction.

    Attributes:
        model: Model name sent to the chat-completions compatible client.
        client: Optional prebuilt OpenAI-compatible client. When omitted,
            `api_key` and `base_url` are used by the tree builder.
        api_key: API key used when `client` is not supplied.
        base_url: Optional OpenAI-compatible base URL.
        seed: Optional deterministic seed forwarded through `extra_body`.
    """

    # Model identity is required for real tree construction. An empty value is
    # allowed at config construction time so default construction stays cheap;
    # the build workflow fails before running without a usable LLM config.
    model: str = ""
    client: OpenAI | None = None
    api_key: str = ""
    base_url: str = ""

    # Seed controls deterministic sampling for providers that support it.
    seed: int | None = None


@dataclass(frozen=True)
class TaxonomyBuildConfig:
    """Tree shape and taxonomy refinement settings.

    Attributes:
        branching_factor: Base scale for split thresholds and tree shape controls.
        max_depth: Maximum tree depth used by recursive construction.
        root_categories: Optional predefined root taxonomy. When omitted,
            `FIXED_ROOT_CATEGORIES` is used by the tree schema resolver.
        postprocess_enabled: Whether to run tree repair after construction.
        postprocess_max_passes: Maximum repair pass count.
        postprocess_min_skills: Minimum skill count for repair candidates.
        equivalence_enabled: Whether to add one equivalence-group layer under
            sibling leaf sets after normal tree construction.
    """

    branching_factor: int = 128
    max_depth: int = 6
    root_categories: RootCategoryInput = None

    postprocess_enabled: bool = True
    postprocess_max_passes: int = 1
    postprocess_min_skills: int = 6

    equivalence_enabled: bool = True


@dataclass(frozen=True)
class BuildExecutionConfig:
    """Execution controls for the offline build.

    Attributes:
        max_workers: Maximum concurrent LLM calls.
        max_retries: Retry count for transient LLM failures.
        request_timeout_seconds: Per-request timeout for LLM calls.
        classification_batch_limit: Upper bound for one classification prompt.
        discovery_seed: Seed used for deterministic discovery sampling.
    """

    max_workers: int = 2
    max_retries: int = 2
    request_timeout_seconds: float = 420.0

    classification_batch_limit: int = 32

    discovery_seed: int = 42


@dataclass(frozen=True)
class BuildOutputConfig:
    """Output artifact settings.

    Attributes:
        generate_html: Whether to emit the optional HTML tree visualization.
    """

    generate_html: bool = False


@dataclass(frozen=True)
class BuildConfig:
    """Public offline build configuration.

    The build path is intentionally fixed to LLM-driven TREE construction. This
    config only exposes settings that still affect that path.

    Attributes:
        llm_config: OpenAI-compatible LLM client configuration.
        taxonomy_config: Tree shape, postprocess, and equivalence settings.
        execution_config: Concurrency, retry, timeout, and deterministic seed.
        output_config: Optional output artifact controls.
    """

    llm_config: BuildLLMConfig = field(default_factory=BuildLLMConfig)
    taxonomy_config: TaxonomyBuildConfig = field(default_factory=TaxonomyBuildConfig)
    execution_config: BuildExecutionConfig = field(default_factory=BuildExecutionConfig)
    output_config: BuildOutputConfig = field(default_factory=BuildOutputConfig)


@dataclass(frozen=True)
class ResolvedTaxonomyBuildConfig:
    """Normalized taxonomy build settings used inside workflows.

    Attributes:
        branching_factor: Normalized split-threshold/tree-shape scale.
        max_depth: Normalized maximum tree depth.
        root_categories: Root categories after applying schema defaults.
        postprocess_enabled: Whether repair passes run.
        postprocess_max_passes: Normalized repair pass limit.
        postprocess_min_skills: Normalized repair minimum size.
        equivalence_enabled: Whether equivalence-group refinement runs.
    """

    branching_factor: int
    max_depth: int
    root_categories: list[str | dict[str, object]] | None

    postprocess_enabled: bool
    postprocess_max_passes: int
    postprocess_min_skills: int

    equivalence_enabled: bool


@dataclass(frozen=True)
class ResolvedBuildConfig:
    """Normalized build configuration used by workflow internals.

    Attributes:
        llm_config: Normalized LLM config.
        taxonomy_config: Normalized taxonomy config.
        execution_config: Normalized execution config.
        output_config: Normalized output config.
    """

    llm_config: BuildLLMConfig
    taxonomy_config: ResolvedTaxonomyBuildConfig
    execution_config: BuildExecutionConfig
    output_config: BuildOutputConfig


def resolve_build_config(*, config: BuildConfig | None = None) -> ResolvedBuildConfig:
    """Normalize public build config for workflow execution.

    Args:
        config: Optional public build configuration.

    Returns:
        A resolved config with stripped strings and bounded numeric values.
    """

    cfg = config or BuildConfig()
    llm_cfg = cfg.llm_config
    taxonomy_cfg = cfg.taxonomy_config
    execution_cfg = cfg.execution_config
    output_cfg = cfg.output_config
    return ResolvedBuildConfig(
        llm_config=BuildLLMConfig(
            model=str(llm_cfg.model or "").strip(),
            client=llm_cfg.client,
            api_key=str(llm_cfg.api_key or "").strip(),
            base_url=str(llm_cfg.base_url or "").strip(),
            seed=llm_cfg.seed,
        ),
        taxonomy_config=ResolvedTaxonomyBuildConfig(
            branching_factor=max(1, int(taxonomy_cfg.branching_factor or 8)),
            max_depth=max(1, int(taxonomy_cfg.max_depth or 6)),
            root_categories=resolve_tree_root_categories(taxonomy_cfg.root_categories),
            postprocess_enabled=bool(taxonomy_cfg.postprocess_enabled),
            postprocess_max_passes=max(0, int(taxonomy_cfg.postprocess_max_passes or 0)),
            postprocess_min_skills=max(2, int(taxonomy_cfg.postprocess_min_skills or 2)),
            equivalence_enabled=bool(taxonomy_cfg.equivalence_enabled),
        ),
        execution_config=BuildExecutionConfig(
            max_workers=max(1, int(execution_cfg.max_workers or 1)),
            max_retries=max(0, int(execution_cfg.max_retries or 0)),
            request_timeout_seconds=float(execution_cfg.request_timeout_seconds),
            classification_batch_limit=max(1, int(execution_cfg.classification_batch_limit or 1)),
            discovery_seed=int(execution_cfg.discovery_seed),
        ),
        output_config=BuildOutputConfig(generate_html=bool(output_cfg.generate_html)),
    )


def build_catalog_records_from_nodes(
    *,
    nodes: Sequence[object],
    scanned_skills: Dict[str, dict],
    restrict_worker_ids: set[str] | None = None,
) -> List[CatalogRecord]:
    """Build catalog records from tree leaf nodes.

    Args:
        nodes: Raw tree preset nodes.
        scanned_skills: Scanned item metadata keyed by worker id.
        restrict_worker_ids: Optional worker-id allowlist for incremental builds.

    Returns:
        Catalog records sorted by CID.
    """

    records: List[CatalogRecord] = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        worker_id = str(raw_node.get("worker_id") or "").strip()
        if not worker_id:
            continue
        if restrict_worker_ids is not None and worker_id not in restrict_worker_ids:
            continue
        scanned = scanned_skills.get(worker_id) or {}
        cid = str(raw_node.get("cid") or "")
        name = str(scanned.get("name") or worker_id)
        has_tree_profile = any(
            str(raw_node.get(key) or "").strip() for key in ("source_description", "select_when", "dont_select_when")
        )
        description = (
            str(raw_node.get("description") or scanned.get("description") or "").strip()
            if has_tree_profile
            else str(scanned.get("description") or raw_node.get("description") or "").strip()
        )
        content = str(scanned.get("content") or "").strip()
        source_description = str(raw_node.get("source_description") or scanned.get("description") or "").strip()
        select_when = str(raw_node.get("select_when") or "").strip()
        dont_select_when = str(raw_node.get("dont_select_when") or "").strip()
        skill_path = str(scanned.get("path") or "")
        # Scanner outputs expose one canonical, normalized tag field. In
        # particular, the JSONL scanner has already merged top-level and
        # contentExtendParam tag/device fields into this value.
        tags = normalize_tags(scanned.get("tags"))
        records.append(
            CatalogRecord(
                worker_id=worker_id,
                cid=cid,
                name=name,
                description=description,
                skill_path=skill_path,
                branch_path=tuple(cid.split(".")[:-1]),
                category=".".join(cid.split(".")[:-1]),
                retrieval_text=build_retrieval_text(
                    skill_id=worker_id,
                    name=name,
                    description=description,
                    content=content,
                    cid=cid,
                ),
                metadata={
                    "content": content,
                    "source_description": source_description,
                    "select_when": select_when,
                    "dont_select_when": dont_select_when,
                    "tags": list(tags),
                },
                tags=tags,
            )
        )
    return sorted(records, key=lambda item: item.cid)


def write_catalog(records: Sequence[CatalogRecord], path: Path) -> None:
    """Write catalog records as JSONL.

    Args:
        records: Catalog records to serialize.
        path: Destination JSONL path.
    """

    lines = [
        json.dumps(
            {
                "worker_id": record.worker_id,
                "cid": record.cid,
                "name": record.name,
                "description": record.description,
                "skill_path": record.skill_path,
                "branch_path": list(record.branch_path),
                "category": record.category,
                "retrieval_text": record.retrieval_text,
                "metadata": record.metadata,
                "tags": list(normalize_tags(record.tags, record.metadata.get("tags"))),
            },
            ensure_ascii=False,
        )
        for record in records
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def build_retrieval_text(*, skill_id: str, name: str, description: str, content: str, cid: str) -> str:
    """Build text used by non-tree retrieval artifacts.

    Args:
        skill_id: Skill id.
        name: Skill display name.
        description: Skill description.
        content: Source skill content.
        cid: Tree CID assigned to the skill.

    Returns:
        A compact newline-joined retrieval text string.
    """

    parts = [
        compact_text(name, limit=200),
        compact_text(description, limit=400),
        compact_text(content, limit=1200),
        compact_text(skill_id, limit=120),
        compact_text(cid, limit=200),
    ]
    return "\n".join(part for part in parts if part)


def compact_text(text: str, *, limit: int) -> str:
    """Compact text to a single line with a fixed character limit.

    Args:
        text: Input text.
        limit: Maximum number of characters.

    Returns:
        Normalized text, truncated with an ellipsis when necessary.
    """

    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:max(0, limit - 1)].rstrip() + "..."


def can_build_tree_with_llm(config: BuildConfig | ResolvedBuildConfig | None = None) -> bool:
    """Check whether the LLM tree build path can run.

    Args:
        config: Public or resolved build config.

    Returns:
        True when both a model and either a client or API key are configured.
    """

    llm_config = (
        config.llm_config if isinstance(config, ResolvedBuildConfig) else resolve_build_config(config=config).llm_config
    )
    return bool(str(llm_config.model or "").strip()) and (
        llm_config.client is not None or bool(str(llm_config.api_key or "").strip())
    )


__all__ = [
    "BuildConfig",
    "BuildExecutionConfig",
    "BuildLLMConfig",
    "BuildMethod",
    "BuildOutputConfig",
    "ResolvedBuildConfig",
    "ResolvedTaxonomyBuildConfig",
    "TaxonomyBuildConfig",
    "build_catalog_records_from_nodes",
    "build_retrieval_text",
    "can_build_tree_with_llm",
    "compact_text",
    "resolve_build_config",
    "write_catalog",
]
