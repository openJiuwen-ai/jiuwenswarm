# pylint: disable=line-too-long
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntFlag
from pathlib import Path
from typing import Any, Dict, List, Sequence

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional in tests
    OpenAI = Any  # type: ignore[misc,assignment]

from ..catalog.records import CatalogRecord


class BuildMethod(IntFlag):
    TREE = 4


@dataclass(frozen=True)
class BuildConfig:
    method: BuildMethod = BuildMethod.TREE
    llm_openai_client: OpenAI | None = None
    llm_model: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_seed: int | None = None
    tree_branching_factor: int = 128
    tree_max_depth: int = 6
    tree_root_categories: list[str | dict[str, object]] | None = None
    tree_max_workers: int = 2
    tree_caching: bool = False
    tree_num_retries: int = 2
    tree_timeout_seconds: float = 420.0
    tree_classify_batch_cap: int = 32
    tree_context_window: int = 0
    tree_max_output_tokens: int = 0
    tree_postprocess_enabled: bool = True
    tree_postprocess_max_passes: int = 1
    tree_postprocess_min_skills: int = 6
    tree_equiv_grouping_enabled: bool = True
    tree_equiv_max_groups_per_parent: int = 6
    tree_equiv_allow_singleton_groups: bool = True
    tree_equiv_min_lexical_similarity: float = 0.12
    tree_deterministic_prompts: bool = True
    tree_discovery_seed: int = 42
    tree_prompt_fingerprint_version: str = "v1"
    tree_cache_observability: bool = True
    tree_skill_profiles_enabled: bool = False
    tree_skill_profile_select_rules_enabled: bool = True
    tree_skill_profile_batch_size: int = 48
    tree_skill_profile_description_limit: int = 140
    tree_skill_profile_rule_limit: int = 120
    generate_tree_html: bool = False
    allow_fallback_tree: bool = True


# 向后兼容别名：IndexBuildRuntimeConfig 已合并到 BuildConfig
IndexBuildRuntimeConfig = BuildConfig


@dataclass(frozen=True)
class ResolvedBuildConfig:
    method: BuildMethod
    llm_openai_client: Any | None
    llm_model: str
    llm_seed: int | None
    tree_llm_api_key: str
    tree_llm_base_url: str
    tree_branching_factor: int
    tree_max_depth: int
    tree_root_categories: list[str | dict[str, object]] | None
    tree_max_workers: int
    tree_caching: bool
    tree_num_retries: int
    tree_timeout_seconds: float
    tree_classify_batch_cap: int
    tree_context_window: int
    tree_max_output_tokens: int
    tree_postprocess_enabled: bool
    tree_postprocess_max_passes: int
    tree_postprocess_min_skills: int
    tree_equiv_grouping_enabled: bool
    tree_equiv_max_groups_per_parent: int
    tree_equiv_allow_singleton_groups: bool
    tree_equiv_min_lexical_similarity: float
    tree_deterministic_prompts: bool
    tree_discovery_seed: int
    tree_prompt_fingerprint_version: str
    tree_cache_observability: bool
    tree_skill_profiles_enabled: bool
    tree_skill_profile_select_rules_enabled: bool
    tree_skill_profile_batch_size: int
    tree_skill_profile_description_limit: int
    tree_skill_profile_rule_limit: int
    generate_tree_html: bool
    allow_fallback_tree: bool


def resolve_build_config(
    *,
    config: BuildConfig | None = None,
    runtime_config: BuildConfig | None = None,  # 向后兼容：现在也接受BuildConfig
) -> ResolvedBuildConfig:
    if config is not None and runtime_config is not None:
        raise ValueError("Specify either config or runtime_config, not both")
    cfg = config or runtime_config or BuildConfig()
    return ResolvedBuildConfig(
        method=BuildMethod(cfg.method),
        llm_openai_client=cfg.llm_openai_client,
        llm_model=str(cfg.llm_model or "").strip(),
        llm_seed=cfg.llm_seed,
        tree_llm_api_key=str(cfg.llm_api_key or "").strip(),
        tree_llm_base_url=str(cfg.llm_base_url or "").strip(),
        tree_branching_factor=max(1, int(cfg.tree_branching_factor or 8)),
        tree_max_depth=max(1, int(cfg.tree_max_depth or 6)),
        tree_root_categories=list(cfg.tree_root_categories) if cfg.tree_root_categories else None,
        tree_max_workers=max(1, int(cfg.tree_max_workers or 1)),
        tree_caching=bool(cfg.tree_caching),
        tree_num_retries=max(0, int(cfg.tree_num_retries or 0)),
        tree_timeout_seconds=float(cfg.tree_timeout_seconds),
        tree_classify_batch_cap=max(1, int(cfg.tree_classify_batch_cap or 1)),
        tree_context_window=max(0, int(cfg.tree_context_window or 0)),
        tree_max_output_tokens=max(0, int(cfg.tree_max_output_tokens or 0)),
        tree_postprocess_enabled=bool(cfg.tree_postprocess_enabled),
        tree_postprocess_max_passes=max(0, int(cfg.tree_postprocess_max_passes or 0)),
        tree_postprocess_min_skills=max(2, int(cfg.tree_postprocess_min_skills or 2)),
        tree_equiv_grouping_enabled=bool(cfg.tree_equiv_grouping_enabled),
        tree_equiv_max_groups_per_parent=max(2, int(cfg.tree_equiv_max_groups_per_parent or 2)),
        tree_equiv_allow_singleton_groups=bool(cfg.tree_equiv_allow_singleton_groups),
        tree_equiv_min_lexical_similarity=max(0.0, min(1.0, float(cfg.tree_equiv_min_lexical_similarity))),
        tree_deterministic_prompts=bool(cfg.tree_deterministic_prompts),
        tree_discovery_seed=int(cfg.tree_discovery_seed),
        tree_prompt_fingerprint_version=str(cfg.tree_prompt_fingerprint_version or "v1"),
        tree_cache_observability=bool(cfg.tree_cache_observability),
        tree_skill_profiles_enabled=bool(cfg.tree_skill_profiles_enabled),
        tree_skill_profile_select_rules_enabled=bool(cfg.tree_skill_profile_select_rules_enabled),
        tree_skill_profile_batch_size=max(1, int(cfg.tree_skill_profile_batch_size or 1)),
        tree_skill_profile_description_limit=max(40, int(cfg.tree_skill_profile_description_limit or 40)),
        tree_skill_profile_rule_limit=max(40, int(cfg.tree_skill_profile_rule_limit or 40)),
        generate_tree_html=bool(cfg.generate_tree_html),
        allow_fallback_tree=bool(cfg.allow_fallback_tree),
    )


def build_catalog_records_from_nodes(
    *,
    nodes: Sequence[object],
    scanned_skills: Dict[str, dict],
    restrict_worker_ids: set[str] | None = None,
) -> List[CatalogRecord]:
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
            str(raw_node.get(key) or "").strip()
            for key in ("source_description", "select_when", "dont_select_when")
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
        records.append(
            CatalogRecord(
                skill_id=worker_id,
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
                },
            )
        )
    return sorted(records, key=lambda item: item.cid)


def write_catalog(records: Sequence[CatalogRecord], path: Path) -> None:
    lines = [
        json.dumps(
            {
                "skill_id": record.skill_id,
                "worker_id": record.worker_id,
                "cid": record.cid,
                "name": record.name,
                "description": record.description,
                "skill_path": record.skill_path,
                "branch_path": list(record.branch_path),
                "category": record.category,
                "retrieval_text": record.retrieval_text,
                "metadata": record.metadata,
            },
            ensure_ascii=False,
        )
        for record in records
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def build_fallback_tree_index(*, aggregate_dir: Path, output_path: Path) -> None:
    nodes = [{"cid": "Skills", "type": "branch", "description": "Fallback skill index built without LLM tree generation."}]
    for skill_dir in sorted(path for path in aggregate_dir.iterdir() if path.is_dir()):
        worker_id = skill_dir.name
        nodes.append(
            {
                "cid": f"Skills.{worker_id}",
                "type": "leaf",
                "description": worker_id,
                "worker_id": worker_id,
            }
        )
    from indexing.io.tree import write_tree_preset

    write_tree_preset({"nodes": nodes}, output_path)


def build_retrieval_text(*, skill_id: str, name: str, description: str, content: str, cid: str) -> str:
    parts = [
        compact_text(name, limit=200),
        compact_text(description, limit=400),
        compact_text(content, limit=1200),
        compact_text(skill_id, limit=120),
        compact_text(cid, limit=200),
    ]
    return "\n".join(part for part in parts if part)


def compact_text(text: str, *, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "..."


def can_build_tree_with_llm(runtime_config: BuildConfig | ResolvedBuildConfig | None = None) -> bool:
    """检查是否可以用LLM构建树索引。
    
    Args:
        runtime_config: BuildConfig 或 ResolvedBuildConfig（向后兼容也接受IndexBuildRuntimeConfig）
    
    Returns:
        如果配置了LLM模型和API密钥/客户端，返回True
    """
    if isinstance(runtime_config, ResolvedBuildConfig):
        return bool(str(runtime_config.llm_model or "").strip()) and (
            runtime_config.llm_openai_client is not None or bool(str(runtime_config.tree_llm_api_key or "").strip())
        )
    # BuildConfig 或向后兼容的 IndexBuildRuntimeConfig
    if runtime_config is None:
        runtime_config = BuildConfig()
    # BuildConfig 使用 llm_model 和 llm_api_key/llm_base_url
    if hasattr(runtime_config, 'llm_model'):
        return bool(str(runtime_config.llm_model or "").strip()) and (
            runtime_config.llm_openai_client is not None or bool(str(runtime_config.llm_api_key or "").strip())
        )
    # 向后兼容：IndexBuildRuntimeConfig 使用 tree_llm_model 和 tree_llm_api_key
    return bool(str(runtime_config.tree_llm_model or "").strip()) and bool(str(runtime_config.tree_llm_api_key or "").strip())


__all__ = [
    "BuildConfig",
    "BuildMethod",
    "IndexBuildRuntimeConfig",
    "ResolvedBuildConfig",
    "build_catalog_records_from_nodes",
    "build_fallback_tree_index",
    "build_retrieval_text",
    "can_build_tree_with_llm",
    "compact_text",
    "resolve_build_config",
    "write_catalog",
]
