"""宿主目录、产品缺省值和会话上下文到 Core 统一配置的唯一解析入口。"""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path, PurePath
from typing import Any

from openjiuwen.symphony import OrchestrationConfig, SymphonyConfig
from pydantic import ValidationError
from jiuwenswarm.common.utils import get_agent_workspace_dir

DEFAULT_SYMPHONY_ENABLED = False
DEFAULT_EVOLUTION_ENABLED = OrchestrationConfig().experience.enabled

# Ordered aliases: the later, explicit YAML tree always wins over these values.
_LEGACY_SYMPHONY_FIELDS = {
    "enabled": "orchestration.enabled",
    "paths.skills_root": "capability.sources",
    "paths.graph_dir": "storage.artifact_root",
    "fingerprint.scan.max_depth": "capability.scan.max_depth",
    "fingerprint.extraction.workers": "capability.fingerprint.extraction.max_concurrency",
    "fingerprint.extraction.batch_size": "capability.fingerprint.extraction.batch_size",
    "fingerprint.extraction.body_limit": "capability.fingerprint.extraction.max_body_chars",
    "build.workers": "orchestration.build.max_concurrency",
    "build.batch_size": "orchestration.build.matching.batch_size",
    "build.max_candidates_per_skill_relation": "orchestration.build.matching.max_candidates_per_capability_relation",
    "build.require_consensus": "orchestration.build.matching.require_consensus",
    "build.min_edge_confidence": "orchestration.min_edge_confidence",
    "evolution.flow.enabled": "orchestration.experience.enabled",
    "evolution.enabled": "orchestration.experience.enabled",
    "evolution.flow.min_successes": "orchestration.experience.acceptance.min_successes",
    "evolution.flow.min_pack_success_rate": "orchestration.experience.acceptance.min_pack_success_rate",
    "orchestration.mode": "orchestration.plan.mode",
    "orchestration.top_k": "orchestration.plan.top_k",
    "orchestration.max_depth": "orchestration.plan.max_depth",
    "skill_retrieval.enabled": "retrieval.enabled",
    "skill_retrieval.artifact_root": "storage.artifact_root",
    "skill_retrieval.discovery.use_existing_index": "retrieval.use_existing_index",
    "skill_retrieval.discovery.candidate_budget_ratio": "retrieval.prompt.candidate_budget_ratio",
    "skill_retrieval.discovery.context_window_tokens": "retrieval.prompt.context_window_tokens",
    "skill_retrieval.discovery.prompt_preferred_skills": "retrieval.prompt.preferred_capability_ids",
    "skill_retrieval.discovery.max_output_chars": "retrieval.output.max_chars",
    "skill_retrieval.discovery.max_results": "retrieval.output.max_results",
    "skill_retrieval.discovery.max_list_entries": "retrieval.output.max_list_entries",
    "skill_retrieval.discovery.incremental_notice_max_chars": "retrieval.output.incremental_notice_max_chars",
    "skill_retrieval.build.max_depth": "retrieval.index_build.structure.max_depth",
    "skill_retrieval.build.branching_factor": "retrieval.index_build.structure.branching_factor",
    "skill_retrieval.build.root_categories": "retrieval.index_build.structure.root_categories",
    "skill_retrieval.build.max_workers": "retrieval.index_build.request.max_concurrency",
    "skill_retrieval.build.max_retries": "retrieval.index_build.request.max_retries",
    "skill_retrieval.build.request_timeout_seconds": "retrieval.index_build.request.timeout_seconds",
    "skill_retrieval.build.context_window": "retrieval.index_build.request.context_window_tokens",
    "skill_retrieval.build.max_output_tokens": "retrieval.index_build.request.max_output_tokens",
    "skill_retrieval.build.classification_batch_limit": "retrieval.index_build.request.classification_batch_limit",
    "skill_retrieval.build.total_timeout_seconds": "retrieval.index_build.task.total_timeout_seconds",
    "skill_retrieval.build.preserve_previous_index_on_failure": (
        "retrieval.index_build.task.preserve_previous_index_on_failure"
    ),
    "skill_retrieval.build.strict_failure": "retrieval.index_build.task.allow_fallback_tree",
    "skill_retrieval.build.discovery_seed": "retrieval.index_build.prompt.discovery_seed",
    "skill_retrieval.build.deterministic_prompts": "retrieval.index_build.prompt.deterministic",
    "skill_retrieval.build.postprocess_enabled": "retrieval.index_build.postprocess.enabled",
    "skill_retrieval.build.postprocess_max_passes": "retrieval.index_build.postprocess.max_passes",
    "skill_retrieval.build.postprocess_min_skills": "retrieval.index_build.postprocess.min_capabilities",
    "skill_retrieval.build.equivalence_enabled": "retrieval.index_build.equivalence.enabled",
    "skill_retrieval.build.equivalence_max_groups_per_parent": (
        "retrieval.index_build.equivalence.max_groups_per_parent"
    ),
    "skill_retrieval.build.equivalence_allow_singleton_groups": (
        "retrieval.index_build.equivalence.allow_singleton_groups"
    ),
    "skill_retrieval.build.equivalence_min_lexical_similarity": (
        "retrieval.index_build.equivalence.min_lexical_similarity"
    ),
    "skill_retrieval.build.skill_profiles_enabled": "retrieval.index_build.profiles.enabled",
    "skill_retrieval.build.skill_profile_select_rules_enabled": "retrieval.index_build.profiles.select_rules_enabled",
    "skill_retrieval.build.skill_profile_batch_size": "retrieval.index_build.profiles.batch_size",
    "skill_retrieval.build.skill_profile_description_limit": "retrieval.index_build.profiles.max_description_chars",
    "skill_retrieval.build.skill_profile_rule_limit": "retrieval.index_build.profiles.max_rule_chars",
    "skill_retrieval.build.incremental_max_change_ratio": "retrieval.index_build.incremental.max_change_ratio",
    "skill_retrieval.build.incremental_min_add_confidence": "retrieval.index_build.incremental.min_add_confidence",
    "skill_retrieval.build.incremental_min_add_confidence_margin": (
        "retrieval.index_build.incremental.min_add_confidence_margin"
    ),
    "skill_retrieval.build.incremental_branch_imbalance_ratio": (
        "retrieval.index_build.incremental.branch_imbalance_ratio"
    ),
    "skill_retrieval.build.generate_tree_html": "retrieval.index_build.output.generate_tree_html",
    "skill_retrieval.build.cache_observability": "retrieval.index_build.output.cache_metrics_enabled",
}


def migrate_legacy_symphony_config(
    raw: dict[str, Any],
    *,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert legacy settings without mutating the input.

    Args:
        raw: The Symphony section before template defaults are added.
        template: On disk, a complete template layout can already be present
            alongside legacy fields. Only its matching defaults yield to legacy values.

    Returns:
        The canonical section. Retired model/cache options have no Runtime
        equivalent; the file migration retains the original YAML in a backup.
        Old artifacts stay untouched; custom roots use a fresh runtime child.
    """
    from jiuwenswarm.common.config import resolve_env_vars

    explicit = deepcopy(raw)
    legacy = _pop_legacy_fields(explicit, ("enabled", "paths", "fingerprint", "build", "evolution", "skill_retrieval"))
    orchestration = explicit.get("orchestration")
    if isinstance(orchestration, dict):
        legacy["orchestration"] = _pop_legacy_fields(orchestration, ("mode", "top_k", "max_depth"))
    converted: dict[str, Any] = {}
    for old_path, new_path in _LEGACY_SYMPHONY_FIELDS.items():
        value = legacy
        for segment in old_path.split("."):
            if not isinstance(value, dict) or segment not in value:
                break
            value = value[segment]
        else:
            # 旧配置里 root_categories 常写成空字符串；新模型要求 list/null。
            # 空字符串按“未配置”处理，否则升级后 Worker 会因校验失败起不来。
            if (
                old_path == "skill_retrieval.build.root_categories"
                and (value == "" or value == [])
            ):
                value = None
            if value is None and old_path != "skill_retrieval.build.root_categories":
                continue
            if old_path == "fingerprint.extraction.body_limit":
                value = _legacy_body_limit(value)
                if value is None:
                    continue
            if new_path in ("capability.sources", "storage.artifact_root"):
                resolved = resolve_env_vars(value)
                if not str(resolved or "").strip():
                    continue
                if new_path == "capability.sources":
                    value = [{"type": "skill", "root": value}]
                else:
                    value = str(Path(str(resolved).strip()) / "runtime")
            if old_path == "skill_retrieval.build.strict_failure" and isinstance(value, bool):
                value = not value
            target = converted
            segments = new_path.split(".")
            for segment in segments[:-1]:
                target = target.setdefault(segment, {})
            target.setdefault(segments[-1], value)
    # A partial canonical layout contains explicit user settings, including false and null.
    if converted and isinstance(template, dict) and _merge(template, explicit) == explicit:
        _discard_premerged_defaults(explicit, converted, template)
    return _merge(converted, explicit)


def _legacy_body_limit(value: Any) -> int | None:
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return limit if limit > 0 else None


def _discard_premerged_defaults(
    explicit: dict[str, Any], converted: dict[str, Any], template: dict[str, Any],
) -> None:
    """Let an old setting replace a copied template default at the same destination."""
    for key in converted.keys() & template.keys() & explicit.keys():
        current = explicit[key]
        if isinstance(current, dict) and isinstance(converted[key], dict) and isinstance(template[key], dict):
            _discard_premerged_defaults(current, converted[key], template[key])
        elif current == template[key]:
            del explicit[key]


def _pop_legacy_fields(config: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    fields = {}
    for key in keys:
        if key in config:
            fields[key] = config.pop(key)
    return fields


def _merge(base: dict[str, Any], explicit: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in explicit.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def resolve_symphony_enabled(value: Any) -> bool:
    """null 继承产品缺省；非布尔值交由严格配置校验拒绝。"""
    return DEFAULT_SYMPHONY_ENABLED if value is None else value


def resolve_symphony_evolution_enabled(value: Any) -> bool:
    return DEFAULT_EVOLUTION_ENABLED if value is None else value


def load_symphony_config(
    config: dict[str, Any] | None = None,
    *,
    context_window_tokens: int | None = None,
) -> SymphonyConfig:
    if config is None:
        from jiuwenswarm.common.config import get_config
        config = get_config() or {}
    return symphony_config_from_dict(config.get("symphony", {}), context_window_tokens=context_window_tokens)


def configured_skill_roots(config: SymphonyConfig) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(Path(source.root) for source in config.capability.sources if source.type == "skill"))


def require_graph_skill_root(config: SymphonyConfig) -> Path:
    roots = configured_skill_roots(config)
    if len(roots) != 1:
        raise ValueError("Symphony orchestration requires exactly one skill capability source")
    return roots[0]


def symphony_config_from_dict(
    raw: dict[str, Any] | None,
    *,
    context_window_tokens: int | None = None,
) -> SymphonyConfig:
    """Migrate legacy fields, resolve capability sources, and validate the Core configuration."""
    from jiuwenswarm.common.paths import get_symphony_dir

    workspace = get_agent_workspace_dir()
    defaults = {
        "capability": {
            "sources": [{"type": "skill", "root": str(workspace / "skills")}],
            "fingerprint": {"extraction": {
                "llm_enabled": True, "max_concurrency": 1, "batch_size": 1,
                "max_output_tokens": None,
            }},
        },
        "storage": {"artifact_root": str(get_symphony_dir() / "runtime")},
        "retrieval": {"enabled": False, "use_existing_index": True},
        "orchestration": {
            "enabled": False, "min_edge_confidence": 0.5,
            "build": {"max_concurrency": 1, "matching": {"batch_size": 12, "require_consensus": False}},
        },
    }
    if context_window_tokens is not None:
        defaults["retrieval"]["prompt"] = {"context_window_tokens": context_window_tokens}
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("symphony must be an object")
    from jiuwenswarm.common.config import resolve_env_vars
    data = _merge(defaults, resolve_env_vars(migrate_legacy_symphony_config(raw or {})))
    for group in ("retrieval", "orchestration"):
        node = data.get(group)
        if isinstance(node, dict) and node.get("enabled") is None:
            node["enabled"] = False
    orchestration = data.get("orchestration")
    if isinstance(orchestration, dict):
        experience = orchestration.get("experience")
        if isinstance(experience, dict) and experience.get("enabled") is None:
            experience.pop("enabled", None)
        if isinstance(experience, dict) and experience.get("enabled") is True and orchestration.get("enabled") is False:
            orchestration["enabled"] = True
        plan = orchestration.get("plan")
        if isinstance(plan, dict) and plan.get("dynamic_graph_enabled") is None:
            plan.pop("dynamic_graph_enabled", None)
    capability = data.get("capability")
    sources = capability.get("sources") if isinstance(capability, dict) else None
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, dict):
                continue
            if source.get("type") == "connector":
                source["type"] = "mcp"
            if isinstance(source.get("root"), (str, PurePath)):
                root = os.path.expandvars(str(source["root"])).strip()
                source["root"] = str(Path(root).expanduser().resolve()) if root else root
    storage = data.get("storage")
    if isinstance(storage, dict):
        artifact_root = storage.get("artifact_root")
        if artifact_root in ("", None):
            artifact_root = defaults["storage"]["artifact_root"]
        if isinstance(artifact_root, str):
            storage["artifact_root"] = str(Path(os.path.expandvars(artifact_root)).expanduser().resolve())
    try:
        return SymphonyConfig.model_validate(data)
    except ValidationError as exc:
        errors = [{**error, "loc": ("symphony", *error["loc"])} for error in exc.errors(include_url=False)]
        raise ValidationError.from_exception_data("Swarm Symphony configuration", errors) from exc
