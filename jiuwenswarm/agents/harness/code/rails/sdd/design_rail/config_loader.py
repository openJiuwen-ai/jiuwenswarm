# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""config_loader — YAML load + one-shot validation for design_rail/config.yaml.

Validation (BC-005/FR-015/FR-016): on failure the DesignRail builder MUST
refuse to mount and fall back to plain Code mode — this module never degrades
to a default config. Validation covers required fields (DC-001~005), core
stage presence (DC-008), and a forward path from ``init`` to ``done``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


__all__ = ["ConfigLoadError", "ValidationResult", "load", "validate"]

_CORE_STAGES = ("analysis", "analysis_review", "design", "design_review")


class ConfigLoadError(Exception):
    """Raised when config.yaml cannot be loaded or parsed."""


@dataclass
class ValidationResult:
    """Structured outcome of config validation (RDS §6.2 IF-R03)."""

    ok: bool
    errors: List[str] = field(default_factory=list)


def load(path: Path) -> dict:
    """Load and parse ``config.yaml``.

    Raises:
        ConfigLoadError: if the file is missing or contains invalid YAML.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigLoadError(f"config file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigLoadError(f"config file unreadable: {p}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigLoadError(
            f"config root must be a mapping, got {type(data).__name__}: {p}"
        )
    return data


def validate(
    cfg: dict, rail_pkg_dir: Optional[Path] = None
) -> ValidationResult:
    """Validate a parsed config dict against RDS §4.2 (5 rules) + Rule 6.

    Never raises; returns a :class:`ValidationResult` with all detected
    errors so the caller (builder) can decide to refuse mounting (BC-005).

    Rule 6 (skill-file existence) runs only when ``rail_pkg_dir`` is provided:
    verifies each core stage's declared ``skill`` resolves to an existing
    ``SKILL.md`` under ``rail_pkg_dir / skills_dir``, catching typos like
    ``aet-req-analisys`` at mount time rather than as a silent runtime
    fall-back to bootstrap.
    """
    errors: List[str] = []
    if not isinstance(cfg, dict):
        return ValidationResult(ok=False, errors=["config root must be a mapping"])

    stages = cfg.get("stages")
    if not isinstance(stages, dict) or not stages:
        errors.append("missing_or_empty_stages")
        # Cannot meaningfully run the remaining stage-level rules; fall through
        # to init/done presence + reachability checks which will also flag.
        stages = {}

    stage_names = set(stages.keys())

    # Rule 1: core stages must be present (DC-008).
    missing_core = [s for s in _CORE_STAGES if s not in stage_names]
    if missing_core:
        errors.append("missing_core_stages: " + ",".join(missing_core))

    # Rule 2 + 4: per-stage required fields and next-type check.
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            errors.append(f"{name}.stage must be a mapping")
            continue
        if name in _CORE_STAGES:
            skill = stage.get("skill")
            if not (isinstance(skill, str) and skill.strip()):
                errors.append(f"{name}.missing_skill")
            # artifacts is OPTIONAL: review stages (analysis_review,
            # design_review) don't produce files — their result is the
            # ask_user approve/reject, not a file artifact. Only validate
            # artifacts if the stage declares them.
            artifacts = stage.get("artifacts")
            if artifacts is not None:
                if not (isinstance(artifacts, list) and artifacts):
                    errors.append(f"{name}.artifacts_must_be_nonempty_list")
        nxt = stage.get("next")
        if not isinstance(nxt, list):
            errors.append(f"{name}.next must be list")

    # Rule 5: 'done' must be reachable from 'init' via 'next' edges (no cycles
    # that trap the machine before 'done').
    if "init" not in stage_names:
        errors.append("missing_init_stage")
    if "done" not in stage_names:
        errors.append("missing_done_stage")
    if "init" in stage_names and "done" in stage_names:
        if not _reachable(stages, "init", "done"):
            errors.append("no_path_init_to_done")

    # Rule 6: each declared skill's SKILL.md must exist (mount-time catch for
    # typo'd skill names that would otherwise silently fall back to bootstrap).
    if rail_pkg_dir is not None:
        skills_dir = cfg.get("skills_dir") or "skills/"
        skills_base = Path(rail_pkg_dir) / skills_dir
        for name in _CORE_STAGES:
            stage = stages.get(name)
            if not isinstance(stage, dict):
                continue
            skill = stage.get("skill")
            if isinstance(skill, str) and skill.strip():
                if not (skills_base / skill / "SKILL.md").exists():
                    errors.append(f"{name}.skill_file_not_found:{skill}")

    return ValidationResult(ok=(len(errors) == 0), errors=errors)


def _reachable(stages: dict, start: str, target: str) -> bool:
    """DFS from ``start`` over ``stages[name].next`` edges; True if ``target`` reached."""
    seen: set[str] = set()
    stack: List[str] = [start]
    while stack:
        name = stack.pop()
        if name == target:
            return True
        if name in seen:
            continue
        seen.add(name)
        stage = stages.get(name)
        if not isinstance(stage, dict):
            continue
        nxt = stage.get("next")
        if not isinstance(nxt, list):
            continue
        for nxt_name in nxt:
            if isinstance(nxt_name, str) and nxt_name not in seen:
                stack.append(nxt_name)
    return False
