# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime scope key for multi-tenant isolation of process-level managers.

Isolation dimension matches tenant env bags: ``(service_id, agent_id)``,
optionally including ``session_id`` for session-scoped registries (Team, Ask).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _norm_id(value: str | None, *, default: str = "default") -> str:
    text = default if value is None else str(value).strip() or default
    return text


@dataclass(frozen=True)
class RuntimeScopeKey:
    """Immutable scope for runtime registries (Team / Rail / Ask / DeepResearch)."""

    service_id: str = "default"
    agent_id: str = "default"
    workspace_key: str = "default"
    session_id: str = ""

    def tenant(self) -> tuple[str, str, str]:
        return (self.service_id, self.agent_id, self.workspace_key)

    def session_key(self) -> tuple[str, str, str, str]:
        return (self.service_id, self.agent_id, self.workspace_key, self.session_id)

    def with_session(self, session_id: str | None) -> RuntimeScopeKey:
        return RuntimeScopeKey(
            service_id=self.service_id,
            agent_id=self.agent_id,
            workspace_key=self.workspace_key,
            session_id=_norm_id(session_id, default="") if session_id is not None else "",
        )

    @classmethod
    def from_ids(
        cls,
        service_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        workspace_key: str | None = None,
    ) -> RuntimeScopeKey:
        return cls(
            service_id=_norm_id(service_id),
            agent_id=_norm_id(agent_id),
            workspace_key=_norm_id(workspace_key),
            session_id=_norm_id(session_id, default="") if session_id is not None else "",
        )

    @classmethod
    def from_request(
        cls,
        request: Any,
        *,
        include_session: bool = False,
    ) -> RuntimeScopeKey:
        # Align with TenantAgentPool.extract_ids (ACP / officeclaw / normalize).
        from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

        agent_id, service_id, workspace_key = TenantAgentPool.extract_ids(request)
        sess = getattr(request, "session_id", None) if include_session else None
        return cls.from_ids(
            service_id,
            agent_id,
            sess if include_session else None,
            workspace_key=workspace_key,
        )

    @classmethod
    def from_adapter(
        cls,
        adapter: Any,
        *,
        session_id: str | None = None,
    ) -> RuntimeScopeKey:
        sid = (
            getattr(adapter, "_env_service_id", None)
            or getattr(adapter, "_service_id", None)
            or "default"
        )
        aid = (
            getattr(adapter, "_env_agent_id", None)
            or getattr(adapter, "_agent_id", None)
            or "default"
        )
        wk = getattr(adapter, "_workspace_key", None) or "default"
        return cls.from_ids(sid, aid, session_id, workspace_key=wk)
