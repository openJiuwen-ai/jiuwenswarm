# jiuwenswarm/server/runtime/team_snapshot_refresh.py
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reconcile frozen per-session/per-team template snapshots with the live template."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.team.config_loader import (
    TeamTemplateNotFoundError,
    get_team_template_snapshot,
)
from jiuwenswarm.server.runtime.session.session_metadata import (
    write_session_team_template_snapshot,
)
from jiuwenswarm.server.runtime.team_entity_store import (
    TeamEntityStoreError,
    get_team_entity_store,
    normalize_team_entity_snapshot,
)

logger = logging.getLogger(__name__)


def _canonical(snapshot: dict[str, Any]) -> str:
    return json.dumps(
        snapshot,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def reconcile_session_team_snapshot(
    *,
    session_id: str,
    team_name: str,
    template_id: str,
    frozen_snapshot: dict[str, Any],
    config_base: dict[str, Any] | None,
    sessions_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the snapshot this session's rebuild should use; refresh both
    frozen copies when the live template has drifted.

    Fail-open by design: any error returns ``frozen_snapshot`` and logs, never
    blocking chat. A live template that cannot be resolved (deleted) keeps the
    frozen snapshot, preserving the existing ``preserve`` semantics.
    """
    try:
        try:
            live_raw = get_team_template_snapshot(
                config_base, template_id=template_id
            )
        except TeamTemplateNotFoundError:
            logger.info(
                "team_snapshot_refresh: live template not found (deleted?) "
                "team=%s template_id=%s — keeping frozen snapshot",
                team_name,
                template_id,
            )
            return frozen_snapshot

        try:
            live_snapshot = normalize_team_entity_snapshot(live_raw, config_base)
        except TeamEntityStoreError as exc:
            logger.warning(
                "team_snapshot_refresh: live template invalid team=%s "
                "template_id=%s error=%s — keeping frozen snapshot",
                team_name,
                template_id,
                exc,
            )
            return frozen_snapshot

        if _canonical(live_snapshot) == _canonical(frozen_snapshot):
            return frozen_snapshot

        try:
            get_team_entity_store().write(
                team_name=team_name,
                template_id=template_id,
                template_snapshot=live_snapshot,
            )
            write_session_team_template_snapshot(
                session_id, live_snapshot, sessions_root=sessions_root
            )
        except Exception as exc:  # noqa: BLE001 - fail-open: never block chat
            logger.warning(
                "team_snapshot_refresh: write failed team=%s template_id=%s "
                "session=%s error=%s — keeping frozen snapshot (retry next rebuild)",
                team_name,
                template_id,
                session_id,
                exc,
            )
            return frozen_snapshot

        logger.info(
            "team_snapshot_refresh: drift detected team=%s template_id=%s "
            "session=%s — refreshed both frozen copies",
            team_name,
            template_id,
            session_id,
        )
        return live_snapshot
    except Exception as exc:  # noqa: BLE001 - 铁律：任何异常都 fail-open
        logger.warning(
            "team_snapshot_refresh: unexpected failure team=%s template_id=%s "
            "session=%s error=%s — keeping frozen snapshot",
            team_name,
            template_id,
            session_id,
            exc,
        )
        return frozen_snapshot


def resolve_dissolve_keep_members(
    *,
    session_id: str,
    team_name: str,
    template_id: str,
    config_base: dict[str, Any] | None,
    sessions_root: str | Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> set[str] | None:
    """Return the member-name set a dissolve reset should keep; ``None`` on any failure.

    Resolves the live-aligned template snapshot for this session and extracts
    the member names that must survive a roster prune: the leader plus each
    predefined member. ``None`` means "could not determine the keep set" — the
    caller skips pruning entirely (fail-open, preserving today's behavior).

    The frozen snapshot is read three ways (session file -> metadata inline
    -> team entity store) mirroring ``_lookup_bound_team_identity``, but the
    entity-store level uses a pure read (``get``) rather than the upsert
    ``ensure_*`` helpers — a dissolve must not write team entities. When a
    frozen snapshot is available it is passed through
    ``reconcile_session_team_snapshot`` so the keep set reflects the live
    template (the frozen copy is still stale when dissolve arrives, before the
    next chat.send) and both frozen copies are refreshed as a side effect.
    When no frozen copy exists anywhere, the live template is taken directly.

    ``metadata`` is the session metadata the caller already fetched (the
    dissolve handler reads it once for its own routing). Passing it avoids a
    redundant ``get_session_metadata`` re-read here; when omitted the function
    reads it itself (cache-busted) so direct callers still work.
    """
    if not (team_name and template_id and session_id):
        return None
    try:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
            get_session_team_template_snapshot,
        )

        if metadata is None:
            metadata = get_session_metadata(
                session_id, cache_bust=True, sessions_root=sessions_root
            )
        snapshot: dict[str, Any] | None = get_session_team_template_snapshot(
            session_id, sessions_root=sessions_root
        )
        if snapshot is None and isinstance(metadata.get("team_template_snapshot"), dict):
            snapshot = copy.deepcopy(metadata["team_template_snapshot"])
        if snapshot is None:
            entity = get_team_entity_store().get(team_name)
            if entity is not None:
                snapshot = copy.deepcopy(entity.template_snapshot)

        if snapshot is not None:
            snapshot = reconcile_session_team_snapshot(
                session_id=session_id,
                team_name=team_name,
                template_id=template_id,
                frozen_snapshot=snapshot,
                config_base=config_base,
                sessions_root=sessions_root,
            )
        else:
            # No frozen copy anywhere: take the live template directly.
            if not isinstance(config_base, dict):
                return None
            live_raw = get_team_template_snapshot(config_base, template_id=template_id)
            snapshot = normalize_team_entity_snapshot(live_raw, config_base)

        keep: set[str] = set()
        leader = snapshot.get("leader") if isinstance(snapshot, dict) else None
        if isinstance(leader, dict):
            leader_member = str(leader.get("member_name") or "").strip()
            if leader_member:
                keep.add(leader_member)
        for item in snapshot.get("predefined_members") or []:
            if not isinstance(item, dict):
                continue
            member_name = str(item.get("member_name") or "").strip()
            if member_name:
                keep.add(member_name)
        return keep or None
    except TeamTemplateNotFoundError:
        logger.info(
            "resolve_dissolve_keep_members: live template not found team=%s "
            "template_id=%s — skipping prune (fail-open)",
            team_name,
            template_id,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - fail-open: never block dissolve
        logger.warning(
            "resolve_dissolve_keep_members: failed team=%s template_id=%s "
            "session=%s error=%s — skipping prune (fail-open)",
            team_name,
            template_id,
            session_id,
            exc,
        )
        return None
