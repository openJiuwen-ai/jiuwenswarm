# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enforce unique AgentGroup membership in JiuwenSwarm organizations."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakValueDictionary

from openjiuwen.agent_teams.organization.runtime import OrganizationRuntimeManager
from openjiuwen.agent_teams.organization.schema import OrganizationSpec
from openjiuwen.agent_teams.runtime import TeamRuntimeManager

from jiuwenswarm.server.runtime.team_organization_identity_store import load_organization_team_identity


def install_expert_invitation_policy(
    runtime: OrganizationRuntimeManager | None, team_runtime: TeamRuntimeManager,
) -> None:
    """Wrap the public invitation entry points on the existing core manager."""
    if not isinstance(runtime, OrganizationRuntimeManager):
        return
    if isinstance(getattr(runtime.invite_team, "__self__", None), ExpertInvitationPolicy):
        return
    policy = ExpertInvitationPolicy(runtime, team_runtime)
    # Core's create flow calls invite_team on this same manager after launch.
    setattr(runtime, "create_and_invite_expert_team", policy.create_and_invite_expert_team)
    setattr(runtime, "invite_team", policy.invite_team)


class ExpertInvitationPolicy:
    """Apply the host's AgentGroup identity to core organization invitations."""

    def __init__(self, runtime: OrganizationRuntimeManager, team_runtime: TeamRuntimeManager) -> None:
        self._runtime = runtime
        self._team_runtime = team_runtime
        self._create = runtime.create_and_invite_expert_team
        self._invite = runtime.invite_team
        self._group_locks: WeakValueDictionary[tuple[str, str, str], asyncio.Lock] = WeakValueDictionary()
        self._lock_owners: dict[tuple[str, str, str], asyncio.Task[Any]] = {}

    @asynccontextmanager
    async def _serialize_group(self, session_id: str, organization_id: str, group_name: str) -> AsyncIterator[None]:
        key = (session_id, organization_id, group_name)
        task = asyncio.current_task()
        # Core creates and then invites on the same task. Only that task can reenter.
        if task is not None and self._lock_owners.get(key) is task:
            yield
            return
        lock = self._group_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if task is not None:
                self._lock_owners[key] = task
            try:
                yield
            finally:
                self._lock_owners.pop(key, None)

    async def _group_name(self, team_id: str, session_id: str) -> str:
        entry = await self._team_runtime.pool.get(team_id)
        if entry is not None and entry.current_session_id == session_id:
            spec = entry.agent.spec
            group = getattr(spec, "agent_group_name", None) or (spec.metadata or {}).get("agent_group_name")
            if group:
                return str(group).strip()
        return load_organization_team_identity(team_id).get("agent_group_name", "")

    async def _check_group(
        self, organization: OrganizationSpec, group_name: str, session_id: str,
    ) -> None:
        if group_name:
            for leader in organization.leaders:
                if await self._group_name(leader.team_id, session_id) == group_name:
                    raise ValueError(
                        f"expert group '{group_name}' is already represented in this organization by "
                        f"team '{leader.team_id}'; use the existing team"
                    )

    async def create_and_invite_expert_team(
        self,
        *,
        organization_id: str,
        owner_team_id: str,
        agent_group_name: str,
        session_id: str,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        group_name = str(agent_group_name or "").strip()
        async with self._serialize_group(session_id, organization_id, group_name):
            organization = await self._runtime.get_organization(
                organization_id=organization_id, team_id=owner_team_id, session_id=session_id,
            )
            if organization is not None and organization.owner_team_id == owner_team_id:
                await self._check_group(organization, group_name, session_id)
            return await self._create(
                organization_id=organization_id,
                owner_team_id=owner_team_id,
                agent_group_name=agent_group_name,
                session_id=session_id,
                display_name=display_name,
            )

    async def invite_team(
        self,
        *,
        organization_id: str,
        inviter_team_id: str,
        target_team_id: str,
        session_id: str,
    ) -> OrganizationSpec:
        group_name = await self._group_name(target_team_id, session_id)
        async with self._serialize_group(session_id, organization_id, group_name):
            organization = await self._runtime.get_organization(
                organization_id=organization_id, team_id=inviter_team_id, session_id=session_id,
            )
            if organization is not None and organization.owner_team_id == inviter_team_id:
                if any(leader.team_id == target_team_id for leader in organization.leaders):
                    owner = await self._team_runtime.pool.get(inviter_team_id)
                    if owner is None or owner.current_session_id != session_id:
                        raise ValueError(f"organization owner is no longer active: {inviter_team_id}")
                    entry = await self._team_runtime.pool.get(target_team_id)
                    db = owner.agent.team_backend.db
                    if entry is not None and entry.current_session_id == session_id:
                        backend = getattr(entry.agent, "team_backend", None)
                        binding = getattr(getattr(backend, "org_task_manager", None), "organization_id", None)
                        if getattr(backend, "db", None) is db and binding == organization_id:
                            return organization
                    if entry is None and target_team_id != inviter_team_id and group_name:
                        from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

                        # Activation restores organization bindings and pending work.
                        await JiuwenExpertTeamLauncher(
                            runtime_manager=self._team_runtime, organization_runtime=self._runtime,
                        ).restore(team_id=target_team_id, session_id=session_id, organization=organization, db=db)
                        return organization
                else:
                    await self._check_group(
                        organization, group_name, session_id,
                    )
            return await self._invite(
                organization_id=organization_id,
                inviter_team_id=inviter_team_id,
                target_team_id=target_team_id,
                session_id=session_id,
            )
