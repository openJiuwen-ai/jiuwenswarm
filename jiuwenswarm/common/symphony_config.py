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


def symphony_config_from_dict(
    raw: dict[str, Any] | None,
    *,
    context_window_tokens: int | None = None,
) -> SymphonyConfig:
    """显式 YAML 值优先；不识别旧字段或静默丢弃未知配置。"""
    workspace = get_agent_workspace_dir()
    defaults = {
        "capability": {
            "sources": [{"type": "skill", "root": str(workspace / "skills")}],
            "fingerprint": {"extraction": {
                "llm_enabled": True, "max_concurrency": 1, "batch_size": 1,
                "max_output_tokens": None,
            }},
        },
        "storage": {"artifact_root": str(workspace / "symphony" / "runtime")},
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
    data = _merge(defaults, resolve_env_vars(raw or {}))
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
            if isinstance(source, dict) and isinstance(source.get("root"), (str, PurePath)):
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
