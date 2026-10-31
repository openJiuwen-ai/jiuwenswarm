# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team-aware routing for the enterprise ``RuntimeRoutedAgentClient``.

Responsibilities (phase 1 of the enterprise Team plan):

* **Binding lookup** — given an envelope, find the ``TeamRouteBinding`` for
  ``params.team_id`` or the session, so the request can be sent straight to
  the AgentServer hosting the leader runtime (``binding.endpoint``) and kept
  alive via ``touch(team_session_id, route_request_id)``.
* **Owner identity** — a bound team carries its owner's routing triple. Other
  members' requests must derive ``service_id / agent_id / workspace_key`` from
  the *owner* (the team's data lives in the owner's workspace), while the
  envelope ``user_id`` stays the caller's so the AgentServer can validate the
  membership.
* **Binding capture** — after a successful ``route()`` for a team-mode session
  the pod is recorded; the ``team.runtime_ready`` stream event back-fills
  ``team_id`` / owner.

agent-runtime's own ``session_id`` affinity stays authoritative: any HTTP
failure against ``binding.endpoint`` falls back to ``route()`` and refreshes
the binding (``route_version`` bumps).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.e2a.models import E2AEnvelope
from gateway.common.mode_matrix import is_team_mode
from jiuwenswarm.enterprise.team.route_binding_store import (
    ROUTE_STATUS_ACTIVE,
    ROUTE_STATUS_UNHEALTHY,
    TeamRouteBinding,
    TeamRouteBindingStore,
    get_team_route_binding_store,
)

logger = logging.getLogger(__name__)

#: Methods whose envelope may legitimately target a team by ``team_id`` only.
TEAM_CONTROL_METHODS = frozenset(
    {
        "team.get",
        "team.stop",
        "team.member.join",
        "team.member.leave",
        "team.members.list",
    }
)

#: Catalog reads are not commands against a live leader. They always use
#: runtime ``route()``, even when the caller happens to be inside a team session.
_CATALOG_METHODS = frozenset({"team.list", "team.quota.get"})

_SHARED_ROUTE_SESSION_PREFIX = "shared_user_"


@dataclass(frozen=True)
class TeamRouteDecision:
    binding: TeamRouteBinding
    base_url: str
    route_id: str
    team_session_id: str
    owner_triple: tuple[str, str, str] | None


def _params(envelope: E2AEnvelope) -> dict[str, Any]:
    return envelope.params if isinstance(envelope.params, dict) else {}


def is_shared_route_session(session_id: str) -> bool:
    """True for the per-user slot that only conserves a runtime placement.

    ``shared_user_*`` is not a team session. It must not be stored as, or used
    to find, the Pod that hosts a leader.
    """
    return str(session_id or "").strip().startswith(_SHARED_ROUTE_SESSION_PREFIX)


def envelope_is_team_scoped(envelope: E2AEnvelope) -> bool:
    """True when the request names a team whose leader Pod should be reused.

    Naming means ``params.team_id``, or an envelope session that is the team's
    own session. A ``team.*`` method on the shared routing session does not.
    Catalog reads never pin.
    """
    params = _params(envelope)
    method = str(envelope.method or "")
    if method in _CATALOG_METHODS:
        return False
    if str(params.get("team_id") or "").strip():
        return True
    sid = _session_id(envelope)
    if not sid or is_shared_route_session(sid):
        return False
    if method in TEAM_CONTROL_METHODS or method.startswith("team."):
        return True
    mode = str(params.get("mode") or "").strip()
    return bool(mode) and is_team_mode(mode)


def _session_id(envelope: E2AEnvelope) -> str:
    return str(envelope.session_id or _params(envelope).get("session_id") or "").strip()


class TeamRouteResolver:
    def __init__(self, store: TeamRouteBindingStore | None = None) -> None:
        self._store = store or get_team_route_binding_store()

    @property
    def store(self) -> TeamRouteBindingStore:
        return self._store

    # -- lookup -------------------------------------------------------------

    def resolve(self, envelope: E2AEnvelope) -> TeamRouteDecision | None:
        """Return a binding-backed route for the envelope, or ``None``."""
        params = _params(envelope)
        binding: TeamRouteBinding | None = None
        team_id = str(params.get("team_id") or "").strip()
        if team_id:
            binding = self._store.get_by_team_id(team_id)
        if binding is None:
            sid = _session_id(envelope)
            if sid and not is_shared_route_session(sid):
                binding = self._store.get_by_session(sid)
        if binding is None or binding.status != ROUTE_STATUS_ACTIVE or not binding.endpoint:
            return None
        return TeamRouteDecision(
            binding=binding,
            base_url=binding.endpoint,
            route_id=binding.route_request_id,
            team_session_id=binding.team_session_id,
            owner_triple=binding.owner_triple() if binding.has_owner else None,
        )

    # -- capture --------------------------------------------------------------

    def record_route(
        self,
        envelope: E2AEnvelope,
        *,
        base_url: str,
        pod_id: str,
        route_id: str,
        caller_triple: tuple[str, str, str],
    ) -> TeamRouteBinding | None:
        """After ``route()`` on a team-scoped request, remember the pod.

        The caller triple is recorded as owner only when the binding is new —
        a later member hitting a fresh route must not overwrite the owner.
        """
        if not envelope_is_team_scoped(envelope):
            return None
        sid = _session_id(envelope)
        # A panel request is scoped by team_id but still carries the shared
        # routing session. That slot is not the leader's session.
        if not sid or is_shared_route_session(sid) or not base_url:
            return None
        existing = self._store.get_by_session(sid)
        owner = None if (existing is not None and existing.has_owner) else caller_triple
        try:
            binding = self._store.upsert(
                team_session_id=sid,
                endpoint=base_url,
                agent_service_id=pod_id,
                route_request_id=route_id,
                owner=owner,
                status=ROUTE_STATUS_ACTIVE,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[TeamRoute] record_route failed: session=%s", sid, exc_info=True)
            return None
        logger.info(
            "[TeamRoute] bound: session=%s pod=%s endpoint=%s version=%s owner=%s",
            sid, pod_id, base_url, binding.route_version, owner is not None,
        )
        return binding

    def record_runtime_ready(self, session_id: str, chunk_payload: dict[str, Any]) -> None:
        """Back-fill ``team_id`` / owner from a ``team.runtime_ready`` event."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        team_id = str(chunk_payload.get("team_id") or "").strip()
        owner_raw = chunk_payload.get("owner")
        owner: tuple[str, str, str] | None = None
        if isinstance(owner_raw, dict) and str(owner_raw.get("user_id") or "").strip():
            owner = (
                str(owner_raw.get("group_id") or "").strip(),
                str(owner_raw.get("bot_id") or "").strip(),
                str(owner_raw.get("user_id") or "").strip(),
            )
        if not team_id and owner is None:
            return
        existing = self._store.get_by_session(sid)
        if existing is None:
            return
        try:
            self._store.upsert(
                team_session_id=sid,
                endpoint=existing.endpoint,
                agent_service_id=existing.agent_service_id,
                route_request_id=existing.route_request_id,
                team_id=team_id or None,
                owner=owner if (owner and not existing.has_owner) else None,
                status=ROUTE_STATUS_ACTIVE,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[TeamRoute] runtime_ready back-fill failed", exc_info=True)

    def mark_unhealthy(self, team_session_id: str) -> None:
        try:
            self._store.mark_status(team_session_id, ROUTE_STATUS_UNHEALTHY)
        except Exception:  # noqa: BLE001
            logger.debug("[TeamRoute] mark_unhealthy failed", exc_info=True)

    def touch(self, team_session_id: str) -> None:
        try:
            self._store.touch(team_session_id)
        except Exception:  # noqa: BLE001
            pass


def extract_runtime_ready_payload(chunk: Any) -> dict[str, Any] | None:
    """Return the payload dict when ``chunk`` is a ``team.runtime_ready`` frame."""
    payload = getattr(chunk, "payload", None)
    if not isinstance(payload, dict):
        return None
    if payload.get("event_type") == "team.runtime_ready":
        return payload
    inner = payload.get("payload")
    if isinstance(inner, dict) and inner.get("event_type") == "team.runtime_ready":
        return inner
    return None


__all__ = [
    "TEAM_CONTROL_METHODS",
    "TeamRouteDecision",
    "TeamRouteResolver",
    "envelope_is_team_scoped",
    "extract_runtime_ready_payload",
    "is_shared_route_session",
]
