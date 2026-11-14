# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional sample expert-team templates shipped outside config.yaml."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import load_yaml_dict

logger = logging.getLogger(__name__)

_SAMPLES_ENV = "JIUWENSWARM_TEAM_SAMPLES"


def sample_team_templates_enabled() -> bool:
    """Samples default on; set ``JIUWENSWARM_TEAM_SAMPLES=0`` to disable."""
    raw = str(os.environ.get(_SAMPLES_ENV, "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def resolve_sample_team_templates_dir() -> Path:
    """``jiuwenswarm/resources/team_templates/samples``."""
    # team/ -> harness/ -> agents/ -> jiuwenswarm/
    return Path(__file__).resolve().parents[3] / "resources" / "team_templates" / "samples"


def _normalize_sample_document(
    raw: dict[str, Any],
    fallback_id: str,
) -> tuple[str, dict[str, Any]] | None:
    doc = dict(raw)
    nested = doc.get("modes")
    if isinstance(nested, dict):
        team_map = nested.get("team")
        if isinstance(team_map, dict) and len(team_map) == 1:
            only_id, only_raw = next(iter(team_map.items()))
            if isinstance(only_raw, dict):
                template_id = str(only_id or "").strip() or fallback_id
                body = dict(only_raw)
                body.setdefault("sample", True)
                body.setdefault("expose_in_catalog", True)
                return template_id, body

    template_id = (
        str(doc.pop("template_id", "") or "").strip()
        or str(doc.get("team_name") or "").strip()
        or fallback_id
    )
    if not template_id:
        return None
    doc.setdefault("sample", True)
    doc.setdefault("expose_in_catalog", True)
    if not str(doc.get("team_name") or "").strip():
        doc["team_name"] = template_id
    return template_id, doc


def load_sample_team_templates() -> dict[str, dict[str, Any]]:
    """Load ``*.yaml`` sample teams; empty when disabled or directory missing."""
    if not sample_team_templates_enabled():
        return {}
    samples_dir = resolve_sample_team_templates_dir()
    if not samples_dir.is_dir():
        return {}
    loaded: dict[str, dict[str, Any]] = {}
    paths = sorted(
        [*samples_dir.glob("*.yaml"), *samples_dir.glob("*.yml")],
        key=lambda path: path.name.lower(),
    )
    for path in paths:
        try:
            raw = load_yaml_dict(path)
        except Exception as exc:  # noqa: BLE001 - sample files must not break startup
            logger.warning("[TeamSamples] failed to read %s: %s", path, exc)
            continue
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_sample_document(raw, fallback_id=path.stem)
        if normalized is None:
            continue
        template_id, body = normalized
        if template_id in loaded:
            logger.warning(
                "[TeamSamples] duplicate template_id=%s from %s; keeping first",
                template_id,
                path,
            )
            continue
        loaded[template_id] = body
    return loaded


def merge_sample_team_templates(teams_raw: dict[str, Any]) -> dict[str, Any]:
    """Merge samples into ``modes.team`` without overriding existing keys."""
    merged = dict(teams_raw or {})
    for template_id, body in load_sample_team_templates().items():
        if template_id in merged:
            continue
        merged[template_id] = body
    return merged


def lookup_sample_team_template(template_id: str) -> dict[str, Any] | None:
    """Return a copy of a shipped sample template body, or ``None``."""
    wanted = str(template_id or "").strip()
    if not wanted:
        return None
    body = load_sample_team_templates().get(wanted)
    if not isinstance(body, dict):
        return None
    return dict(body)


def is_catalog_expert_template(
    team_raw: dict[str, Any] | None,
    *,
    template_id: str = "",
) -> bool:
    """Whether a template should appear in the expert-team summon catalog."""
    if not isinstance(team_raw, dict):
        return False
    tid = (
        str(template_id or "").strip()
        or str(team_raw.get("team_name") or "").strip()
        or str(team_raw.get("template_id") or "").strip()
    ).lower()
    if tid in {"jiuwen_team", "jiuwen-team"}:
        return False

    flag = team_raw.get("expose_in_catalog")
    if flag is True:
        return True
    if flag is False:
        return False

    # Ace / OfficeAce preset ids after sync (oc_team_preset-research-insight, …).
    if "preset-" in tid or tid.startswith("oc_team_preset"):
        return True
    if str(team_raw.get("creation_source") or team_raw.get("creationSource") or "").strip().lower() == "preset":
        return True
    if team_raw.get("sample") is True:
        return True

    members = team_raw.get("predefined_members")
    if not isinstance(members, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("member_name") or "").strip()
        for item in members
    )


def pick_default_auto_bind_template(
    templates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Prefer the engine shell ``jiuwen_team``, never a sample, for auto-bind."""
    preferred: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    for item in templates:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("template_id") or "").strip()
        if tid == "jiuwen_team":
            preferred = item
            break
        if item.get("sample"):
            continue
        if fallback is None:
            fallback = item
    return preferred or fallback
