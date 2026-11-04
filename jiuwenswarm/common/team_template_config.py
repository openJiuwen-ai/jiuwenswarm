# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read team template dicts from config without loading the team runtime."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)


class TeamTemplateNotFoundError(ValueError):
    """Raised when a bound team references a template that no longer exists."""


def _get_modes_team(config_base: dict[str, Any]) -> dict[str, Any]:
    modes_raw = config_base.get("modes", {})
    if not isinstance(modes_raw, dict):
        return {}

    teams_raw = modes_raw.get("team", {})
    if not isinstance(teams_raw, dict):
        return {}
    return teams_raw


def _resolve_legacy_team_template(config_base: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    legacy_team = config_base.get("team", {})
    if isinstance(legacy_team, dict) and legacy_team:
        template_id = (
            str(legacy_team.get("team_name") or "").strip()
            or str(legacy_team.get("name") or "").strip()
            or "default"
        )
        return template_id, legacy_team

    if any(key in config_base for key in ("team_name", "leader", "agents", "storage", "predefined_members")):
        template_id = (
            str(config_base.get("team_name") or "").strip()
            or str(config_base.get("name") or "").strip()
            or "default"
        )
        return template_id, config_base

    return "", {}


def _select_modes_team(
    config_base: dict[str, Any],
    template_id: str | None = None,
    *,
    strict_template: bool = False,
) -> tuple[str, dict[str, Any]]:
    teams_raw = _get_modes_team(config_base)
    legacy_template_id, legacy_team = _resolve_legacy_team_template(config_base)
    requested_template_id = str(template_id or "").strip()
    if requested_template_id:
        candidate = teams_raw.get(requested_template_id)
        if isinstance(candidate, dict):
            logger.debug("[TeamConfigLoader] selected team template: %s", requested_template_id)
            return requested_template_id, candidate
        if legacy_team and requested_template_id == legacy_template_id:
            logger.debug("[TeamConfigLoader] selected legacy team template: %s", requested_template_id)
            return legacy_template_id, legacy_team
        if strict_template:
            raise TeamTemplateNotFoundError(f"team template not found: {requested_template_id}")
        logger.warning("[TeamConfigLoader] requested team template not found: %s", requested_template_id)

    for team_name, team_raw in teams_raw.items():
        if isinstance(team_raw, dict):
            logger.debug("[TeamConfigLoader] selected team from modes.team: %s", team_name)
            return str(team_name), team_raw

    if legacy_team:
        logger.debug("[TeamConfigLoader] selected legacy team template: %s", legacy_template_id)
        return legacy_template_id, legacy_team

    return "", {}


def list_team_template_summaries(config_base: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return configured team templates from ``modes.team``."""
    if config_base is None:
        from jiuwenswarm.common.config import get_config

        config_base = get_config()
    templates: list[dict[str, Any]] = []
    for template_id, team_raw in _get_modes_team(config_base).items():
        if not isinstance(team_raw, dict):
            continue
        display_name = (
            str(team_raw.get("display_name") or "").strip()
            or str(team_raw.get("name") or "").strip()
            or str(team_raw.get("team_name") or "").strip()
            or str(template_id)
        )
        templates.append(
            {
                "template_id": str(template_id),
                "display_name": display_name,
                "available": True,
                "source": f"modes.team.{template_id}",
                "team_name": str(team_raw.get("team_name") or "").strip(),
            }
        )
    if templates:
        return templates

    template_id, legacy_team = _resolve_legacy_team_template(config_base)
    if legacy_team:
        display_name = (
            str(legacy_team.get("display_name") or "").strip()
            or str(legacy_team.get("name") or "").strip()
            or str(legacy_team.get("team_name") or "").strip()
            or template_id
        )
        templates.append(
            {
                "template_id": template_id,
                "display_name": display_name,
                "available": True,
                "source": "team",
                "team_name": str(legacy_team.get("team_name") or "").strip(),
            }
        )
    return templates


def get_team_template_snapshot(
    config_base: dict[str, Any] | None = None,
    *,
    template_id: str,
) -> dict[str, Any]:
    """Return a copy of the selected raw team template for team entity persistence."""
    if config_base is None:
        from jiuwenswarm.common.config import get_config

        config_base = get_config()
    resolved_template_id, team_raw = _select_modes_team(
        config_base,
        template_id=template_id,
        strict_template=True,
    )
    snapshot = deepcopy(team_raw)
    if resolved_template_id and not str(snapshot.get("team_name") or "").strip():
        snapshot["team_name"] = resolved_template_id
    return snapshot
