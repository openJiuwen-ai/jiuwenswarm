# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host SummaryTeamFactory: SummaryTeamSpec -> TeamAgentSpec -> activate (+pause).

The framework's on-demand Summary Team is built from its own frozen preset
(:class:`~openjiuwen.agent_teams.organization.summary.SummaryTeamSpec`), not
from an AgentGroup package.  This adapter translates that preset into a base
``TeamAgentSpec`` (via the same config-driven base the expert launcher uses),
activates it so it lands in the team runtime pool, parks it PAUSED so the
framework's summary-turn drain can resume it, and returns a
``LaunchedSummaryTeam`` for ``_complete_summary_provision`` to bind.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

_ORG_SUMMARY_CAPABILITY = "summary"


def _summary_team_id_for_task(summary_task_id: str) -> str:
    """Derive a deterministic Summary Team id for a given Summary Task.

    ``recover`` (§8) runs only when the execution never bound a
    ``summary_team_id``, so the running pool is the only place the team could
    survive.  Making the id a pure function of the task lets a cold re-launch
    converge on the same team name, and lets ``activate`` re-adopt any paused
    in-session instance for that name.
    """
    digest = hashlib.sha256(str(summary_task_id).encode("utf-8")).hexdigest()[:12]
    return f"org-summary-{digest}"


async def _leader_id_from_agent(agent: Any, team_id: str) -> str:
    """Resolve the team's leader member name for the LaunchedSummaryTeam binding.

    Prefer ``TeamBackend.resolve_leader_member_name()``: it answers "who leads
    this team" for both the leader's own backend and a plain member's backend,
    and falls back to the ``team_info`` row when the name was not handed in at
    construction.  It may legitimately return ``""`` (team row absent), so the
    deterministic ``leader-<team_id>`` remains as the non-empty floor.
    """
    backend = getattr(agent, "team_backend", None)
    if backend is not None:
        resolve = getattr(backend, "resolve_leader_member_name", None)
        if callable(resolve):
            resolved = await resolve()
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()
    return f"leader-{team_id}"


def _align_spec_storage(spec: Any, donor_db: Any) -> None:
    """Point spec.storage at the donor DB so build uses the same TeamDatabase instance."""
    config = getattr(donor_db, "config", None)
    if config is None:
        raise ValueError("donor TeamDatabase has no config")

    from openjiuwen.agent_teams.schema.blueprint import StorageSpec

    db_type = str(getattr(config, "db_type", "") or "sqlite").strip() or "sqlite"
    params: dict[str, Any] = {
        "connection_string": str(getattr(config, "connection_string", "") or ""),
    }
    for key in ("db_timeout", "db_enable_wal"):
        if hasattr(config, key):
            params[key] = getattr(config, key)

    spec.storage = StorageSpec(type=db_type, params=params)


def _verify_shared_database(agent: Any, donor_backend: Any) -> None:
    backend = getattr(agent, "team_backend", None)
    if backend is None:
        raise ValueError("summary team has no team_backend")
    donor_db = getattr(donor_backend, "db", None)
    if donor_db is None:
        raise ValueError("donor team has no TeamDatabase")
    if backend.db is not donor_db:
        raise ValueError(
            "summary team must use the owner's shared TeamDatabase instance"
        )


class JiuwenSummaryTeamFactory:
    """SummaryTeamFactory for JiuwenSwarm: provision / recover / release a Summary Team."""

    def __init__(self, *, runtime_manager: Any | None = None) -> None:
        self._runtime_manager = runtime_manager

    # -- framework SummaryTeamFactory protocol ----------------------------------

    def default_spec(self) -> Any:
        from openjiuwen.agent_teams.organization.summary import SummaryTeamSpec

        return SummaryTeamSpec()

    async def provision(
        self,
        *,
        organization_id: str,
        root_task_id: str,
        summary_task_id: str,
        owner_team_id: str,
        session_id: str,
    ) -> Any:
        from openjiuwen.agent_teams.organization.summary import LaunchedSummaryTeam

        spec0 = self.default_spec()
        team_id = await self._allocate_team_id()
        launched = await self._launch_team(
            spec0=spec0,
            team_id=team_id,
            organization_id=organization_id,
            root_task_id=root_task_id,
            summary_task_id=summary_task_id,
            owner_team_id=owner_team_id,
            session_id=session_id,
        )
        return LaunchedSummaryTeam(
            team_id=launched.team_id,
            leader_id=launched.leader_id,
            root_task_id=root_task_id,
            summary_task_id=summary_task_id,
            spec=spec0,
        )

    async def recover(
        self,
        *,
        execution_id: str,
        organization_id: str,
        root_task_id: str,
        summary_task_id: str,
        owner_team_id: str,
        session_id: str,
    ) -> Any:
        """Re-attach or recreate a Summary Team for an interrupted execution (§8).

        The framework calls this only for an execution that never bound a
        ``summary_team_id``.  The team id is a pure function of the Summary Task,
        so this is idempotent: a repeated recovery converges on the same team
        name, and ``activate`` re-adopts any paused in-session instance for it.
        """
        from openjiuwen.agent_teams.organization.summary import LaunchedSummaryTeam

        spec0 = self.default_spec()
        team_id = _summary_team_id_for_task(summary_task_id)
        launched = await self._launch_team(
            spec0=spec0,
            team_id=team_id,
            organization_id=organization_id,
            root_task_id=root_task_id,
            summary_task_id=summary_task_id,
            owner_team_id=owner_team_id,
            session_id=session_id,
        )
        return LaunchedSummaryTeam(
            team_id=launched.team_id,
            leader_id=launched.leader_id,
            root_task_id=root_task_id,
            summary_task_id=summary_task_id,
            spec=spec0,
        )

    async def release(
        self,
        *,
        execution_id: str,
        summary_team_id: str,
        session_id: str,
    ) -> None:
        """Stop and reclaim a previously provisioned Summary Team instance."""
        await self.stop(team_id=summary_team_id, session_id=session_id)

    # -- shared launch machinery ----------------------------------------------

    async def _allocate_team_id(self) -> str:
        return f"org-summary-{uuid.uuid4().hex[:12]}"

    def _get_runtime(self) -> Any:
        if self._runtime_manager is not None:
            return self._runtime_manager
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER
        from jiuwenswarm.agents.harness.team.team_manager import (
            _runner_team_runtime_manager,
        )

        return _runner_team_runtime_manager(GLOBAL_RUNNER)

    async def _resolve_donor_backend(
        self,
        *,
        owner_team_id: str,
        session_id: str,
    ) -> Any:
        """Return the owner team's backend, whose DB the Summary Team shares.

        The owner is resolved explicitly instead of scanning the session pool:
        the organization's storage is the owner's, and picking whichever team
        happens to sit first in the pool would silently depend on insertion
        order.  A missing (or DB-less) owner is an error rather than a fallback,
        because a Summary Team without the organization's DB cannot see its
        source tasks.
        """
        runtime = self._get_runtime()
        owner_id = str(owner_team_id or "").strip()
        if not owner_id:
            raise ValueError("summary team requires an owner_team_id to share storage with")
        entry = await runtime.pool.get(owner_id)
        donor_backend = getattr(entry.agent, "team_backend", None) if entry is not None else None
        if donor_backend is None or getattr(donor_backend, "db", None) is None:
            raise ValueError(
                "summary team cannot share storage: owner team "
                f"{owner_id!r} has no pooled TeamDatabase in session {session_id!r}"
            )
        return donor_backend

    async def _launch_team(
        self,
        *,
        spec0: Any,
        team_id: str,
        organization_id: str,
        root_task_id: str,
        summary_task_id: str,
        owner_team_id: str,
        session_id: str,
    ) -> Any:
        """Build the TeamAgentSpec, activate it, and park it PAUSED."""
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class _Launched:
            team_id: str
            leader_id: str

        runtime = self._get_runtime()
        # The summary team shares the organization's storage: borrow the owner
        # team's DB explicitly rather than scanning for the first available one.
        donor_backend = await self._resolve_donor_backend(
            owner_team_id=owner_team_id,
            session_id=session_id,
        )
        shared_db = donor_backend.db

        spec = await self._build_enriched_spec(
            spec0=spec0,
            team_id=team_id,
            session_id=session_id,
            shared_db=shared_db,
        )
        activation_attempted = False
        try:
            activation_attempted = True
            activation = await runtime.activate(spec, session_id)
            agent = getattr(activation, "agent", None)
            if agent is None:
                raise ValueError(f"activate returned no agent for team: {team_id}")

            _verify_shared_database(agent, donor_backend)
            await self._materialize_team_in_db(agent, spec0)

            # Design: Summary Team should sit PAUSED without an idle warm-up so
            # the framework's summary-turn drain can resume it on demand.
            pause = getattr(runtime, "pause", None)
            if callable(pause):
                try:
                    await pause(team_name=team_id, session_id=session_id)
                except Exception as exc:  # pragma: no cover - best effort
                    import logging

                    logging.getLogger(__name__).warning(
                        "[SummaryTeamFactory] pause after activate failed team=%s: %s",
                        team_id,
                        exc,
                    )
            return _Launched(
                team_id=team_id, leader_id=await _leader_id_from_agent(agent, team_id)
            )
        except Exception as exc:
            import logging
            import traceback

            logging.getLogger(__name__).error(
                "[SummaryTeamFactory] _launch_team FAILED team=%s session=%s "
                "activation_attempted=%s exc_type=%s exc=%r\n%s",
                team_id,
                session_id,
                activation_attempted,
                type(exc).__name__,
                exc,
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )
            if activation_attempted:
                await self.stop(team_id=team_id, session_id=session_id)
            raise

    async def _build_enriched_spec(
        self,
        *,
        spec0: Any,
        team_id: str,
        session_id: str,
        shared_db: Any | None = None,
    ) -> Any:
        """Base Team Spec from config, then a SummaryTeamSpec overlay.

        No AgentGroup overlay: ``enrich_team_spec_for_swarm`` skips
        ``_apply_agent_group`` when ``agent_group_name`` is falsy, so the
        Summary Team keeps the preset prompt instead of a group instruction.
        """
        from jiuwenswarm.agents.harness.team.team_manager import TeamManager
        from jiuwenswarm.agents.swarm.assembly import enrich_team_spec_for_swarm

        spec = TeamManager._load_team_spec(session_id)
        metadata = dict(getattr(spec, "metadata", None) or {})
        metadata["capabilities"] = list(spec0.capabilities)
        metadata["summary_team"] = True
        metadata["display_name"] = spec0.display_name
        updates: dict[str, Any] = {
            "team_name": team_id,
            "lifecycle": "persistent",
            "metadata": metadata,
        }
        spec = spec.model_copy(update=updates)

        # The Summary Team has no AgentGroup instruction; carry the preset
        # prompt onto the leader's private system prompt.
        leader = getattr(spec, "leader", None)
        if leader is not None:
            spec.leader = leader.model_copy(update={"prompt": spec0.prompt})

        enrich_team_spec_for_swarm(
            spec,
            session_id=session_id,
            mode="team",
            request_metadata={"mode": "team", "summary_team": True},
            agent_group_name=None,
            agent_group_package=None,
        )
        if shared_db is not None:
            _align_spec_storage(spec, shared_db)
        return spec

    async def _materialize_team_in_db(self, agent: Any, spec0: Any | None = None) -> None:
        backend = getattr(agent, "team_backend", None)
        if backend is None:
            raise ValueError("summary team backend has no team_backend")
        build_team = getattr(backend, "build_team", None)
        if not callable(build_team):
            raise ValueError("summary team backend has no build_team")
        # rebase-on-jiuwen: TeamBackend.build_team now requires the 4 label args.
        display = str(getattr(spec0, "display_name", "") or "").strip() or "Summary Team"
        await build_team(
            display_name=display,
            desc=display,
            leader_display_name=display,
            leader_desc=display,
        )

    async def stop(self, *, team_id: str, session_id: str) -> None:
        name = str(team_id or "").strip()
        if not name:
            return
        runtime = self._get_runtime()
        stop_team = getattr(runtime, "stop_team", None)
        if not callable(stop_team):
            return
        try:
            await stop_team(team_name=name, session_id=session_id)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "[SummaryTeamFactory] stop_team failed team=%s session=%s: %s",
                name,
                session_id,
                exc,
            )


__all__ = ["JiuwenSummaryTeamFactory"]
