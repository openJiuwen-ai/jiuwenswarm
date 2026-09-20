# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm launcher for the reusable organization Summary Team."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.agents.harness.team.expert_org.launcher import (
    _align_spec_storage,
    _leader_id_from_agent,
    _resolve_launch_channel_id,
)

logger = logging.getLogger(__name__)


class JiuwenSummaryTeamLauncher:
    """Build one stable, generic Summary Team from JiuwenSwarm's base Team template."""

    def __init__(self, *, runtime_manager: Any | None = None) -> None:
        """Keep an optional runtime for tests; production resolves the shared runner runtime lazily."""
        self._runtime_manager = runtime_manager

    def _get_runtime(self) -> Any:
        """Return the JiuwenSwarm Team runtime used to activate the Summary Team."""
        if self._runtime_manager is not None:
            return self._runtime_manager
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER
        from jiuwenswarm.agents.harness.team.team_manager import (
            _runner_team_runtime_manager,
        )

        return _runner_team_runtime_manager(GLOBAL_RUNNER)

    @staticmethod
    def _verify_shared_database(agent: Any, donor_backend: Any) -> None:
        """Verify every Summary Team manager uses the root team's TeamDatabase instance."""
        backend = getattr(agent, "team_backend", None)
        donor_db = getattr(donor_backend, "db", None)
        if backend is None or donor_db is None:
            raise ValueError(
                "Summary Team and root team must both expose a TeamDatabase"
            )
        if backend.db is not donor_db:
            raise ValueError(
                "Summary Team must use the root team's shared TeamDatabase instance"
            )
        for name in ("task_manager", "message_manager"):
            manager = getattr(backend, name, None)
            if manager is not None and getattr(manager, "db", None) is not donor_db:
                raise ValueError(
                    f"Summary Team {name} must use the root team's shared TeamDatabase instance"
                )

    async def launch(
        self,
        *,
        organization_id: str,
        session_id: str,
        share_db_from_team_id: str,
    ) -> Any:
        """Activate the stable Summary Team and make it share the root team's database."""
        if not organization_id or not session_id or not share_db_from_team_id:
            raise ValueError(
                "organization_id, session_id and share_db_from_team_id are required"
            )
        team_id = f"org-summary-{organization_id}"
        runtime = self._get_runtime()
        donor_entry = await runtime.pool.get(share_db_from_team_id)
        donor_backend = getattr(
            getattr(donor_entry, "agent", None), "team_backend", None
        )
        donor_db = getattr(donor_backend, "db", None)
        if donor_db is None:
            raise ValueError(
                "root team must have a TeamDatabase before Summary Team launch"
            )
        existing = await runtime.pool.get(team_id)
        if existing is not None and existing.current_session_id == session_id:
            agent = existing.agent
            from openjiuwen.agent_teams.organization.summary_team import (
                LaunchedSummaryTeam,
            )

            return LaunchedSummaryTeam(
                team_id=team_id, leader_id=_leader_id_from_agent(agent, team_id)
            )
        from jiuwenswarm.agents.harness.team.summary_org.spec import (
            build_summary_team_spec,
        )

        spec = build_summary_team_spec(
            team_id=team_id,
            organization_id=organization_id,
            session_id=session_id,
            channel_id=_resolve_launch_channel_id(
                (getattr(getattr(donor_entry.agent, "spec", None), "metadata", None) or {}).get("channel_id"),
                session_id,
            ),
        )
        _align_spec_storage(spec, donor_db)
        try:
            activation = await runtime.activate(spec, session_id)
            agent = getattr(activation, "agent", None)
            backend = getattr(agent, "team_backend", None)
            if agent is None or backend is None:
                raise ValueError("Summary Team activation returned no team backend")
            await backend.build_team(
                display_name="Organization Summary Team",
                desc="Consolidates accepted source-task outputs for one organization root task.",
                leader_display_name="summary-leader",
                leader_desc="Creates the final result from accepted source-task outputs.",
            )
            self._verify_shared_database(agent, donor_backend)
            pause = getattr(runtime, "pause", None)
            if not callable(pause):
                raise ValueError("Summary Team runtime must support pause")
            if not await pause(team_name=team_id, session_id=session_id):
                raise RuntimeError(
                    "Summary Team could not enter the paused scheduling state"
                )
            from openjiuwen.agent_teams.organization.summary_team import (
                LaunchedSummaryTeam,
            )

            return LaunchedSummaryTeam(
                team_id=team_id, leader_id=_leader_id_from_agent(agent, team_id)
            )
        except Exception:
            await self._stop_with_runtime(
                runtime, team_id=team_id, session_id=session_id
            )
            raise

    async def stop(self, *, team_id: str, session_id: str) -> None:
        """Stop the Summary Team during launch rollback or organization teardown."""
        await self._stop_with_runtime(
            self._get_runtime(), team_id=team_id, session_id=session_id
        )

    async def _stop_with_runtime(
        self, runtime: Any, *, team_id: str, session_id: str
    ) -> None:
        """Best-effort stop using a runtime already resolved by the caller."""
        name = str(team_id or "").strip()
        if not name:
            return
        stop_team = getattr(runtime, "stop_team", None)
        if callable(stop_team):
            try:
                await stop_team(team_name=name, session_id=session_id)
            except Exception as exc:  # pragma: no cover - teardown is best effort
                logger.warning(
                    "failed to stop Summary Team team=%s session=%s",
                    name,
                    session_id,
                    exc_info=exc,
                )


__all__ = ["JiuwenSummaryTeamLauncher"]
