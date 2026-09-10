"""Enterprise projection over Manager-owned A2A templates and local state."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from jiuwenswarm.gateway.config.enterprise.repository import (
    EnterpriseRecordRepository,
)
from jiuwenswarm.gateway.storage.protocols.persistent import PersistentStore

from .errors import A2AOutboundError, A2AOutboundErrorCode, safe_error_summary
from .models import A2AOutboundAgent, A2AOutboundAvailability, A2AOutboundDispatch
from .repository import A2AOutboundRepository, JsonA2AOutboundRecordCodec

logger = logging.getLogger(__name__)

_DISPATCH_DATETIME_FIELDS = (
    "accepted_at",
    "finished_at",
    "last_polled_at",
    "created_at",
    "updated_at",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _to_timestamp_text(value: Any) -> Any:
    if not isinstance(value, datetime):
        return value
    normalized = (
        value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    )
    return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _EnterpriseA2AOutboundRecordCodec(JsonA2AOutboundRecordCodec):
    """Adapt the shared dispatch domain record to enterprise datetime columns."""

    @staticmethod
    def dispatch_to_record(dispatch: A2AOutboundDispatch) -> dict[str, Any]:
        record = dispatch.to_record()
        for field in _DISPATCH_DATETIME_FIELDS:
            if field in record:
                record[field] = _to_datetime(record[field])
        return record

    @staticmethod
    def dispatch_from_record(record: Any) -> A2AOutboundDispatch:
        values = dict(record)
        for field in _DISPATCH_DATETIME_FIELDS:
            if field in values:
                values[field] = _to_timestamp_text(values[field])
        return A2AOutboundDispatch.from_record(values)


@dataclass(frozen=True)
class EnterpriseA2AAgentView:
    agent: A2AOutboundAgent
    manager_enabled: bool
    user_enabled: bool

    @property
    def effective_enabled(self) -> bool:
        return self.manager_enabled and self.user_enabled

    def public_dict(self) -> dict[str, Any]:
        return {
            **self.agent.public_dict(),
            "manager_enabled": self.manager_enabled,
            "user_enabled": self.user_enabled,
            "effective_enabled": self.effective_enabled,
        }


class EnterpriseA2AProjection(A2AOutboundRepository):
    """Project enterprise rows into the existing outbound domain model."""

    manager_owned = True

    def __init__(
        self,
        store: PersistentStore,
        *,
        templates: EnterpriseRecordRepository,
        user_states: EnterpriseRecordRepository,
        runtime_states: EnterpriseRecordRepository,
        policies: EnterpriseRecordRepository | None = None,
        agent_templates: EnterpriseRecordRepository | None = None,
        instance_resources: EnterpriseRecordRepository | None = None,
    ) -> None:
        super().__init__(store, _EnterpriseA2AOutboundRecordCodec())
        self._templates = templates
        self._user_states = user_states
        self._runtime_states = runtime_states
        self._policies = policies
        self._agent_templates = agent_templates
        self._instance_resources = instance_resources

    async def resolve_effective_a2a_agent_ids(
        self, resource_id: str
    ) -> frozenset[str]:
        """Resolve the current policy/manager/user intersection for one resource."""
        allowed, projected = await self._resolve_policy_scope(resource_id)
        effective_ids = []
        for item in projected:
            if item.agent.agent_id not in allowed:
                continue
            if not item.manager_enabled:
                continue
            if not item.user_enabled:
                continue
            effective_ids.append(item.agent.agent_id)
        return frozenset(effective_ids)

    async def resolve_authorized_a2a_agent_ids(
        self, resource_id: str
    ) -> frozenset[str]:
        """Resolve the policy-authorized catalog before local enablement state."""
        allowed, _ = await self._resolve_policy_scope(resource_id)
        return allowed

    async def _resolve_policy_scope(
        self, resource_id: str
    ) -> tuple[frozenset[str], list[EnterpriseA2AAgentView]]:
        if not all((self._policies, self._agent_templates, self._instance_resources)):
            return frozenset(), []
        resource = await self._instance_resources.get(
            resource_id=str(resource_id or "").strip()
        )
        if resource is None or not bool(resource.get("enabled")):
            return frozenset(), []
        expires_at = _to_datetime(resource.get("expires_at"))
        if expires_at is not None and expires_at <= _utc_now():
            return frozenset(), []

        template_id = str(resource.get("ref_template_id") or "").strip()
        agent_template = await self._agent_templates.get(template_id=template_id)
        if agent_template is None or not bool(agent_template.get("enabled")):
            return frozenset(), []
        template_ref = agent_template.get("template_ref")
        refs = (
            template_ref.get("a2a_access_policy")
            if isinstance(template_ref, dict)
            else None
        )
        if not isinstance(refs, list) or len(refs) != 1:
            return frozenset(), []
        policy_id = str(refs[0] or "").strip()
        if not policy_id or policy_id.startswith("${") or " or " in policy_id.lower():
            return frozenset(), []
        policy = await self._policies.get(policy_id=policy_id)
        if policy is None or not bool(policy.get("enabled")):
            return frozenset(), []

        members = {
            str(item).strip()
            for item in (policy.get("member_template_ids") or [])
            if str(item).strip()
        }
        projected = await self.list_projected_agents()
        mode = str(policy.get("mode") or "").strip().lower()
        if mode == "allowlist":
            policy_allowed = members
        elif mode == "denylist":
            policy_allowed = {item.agent.agent_id for item in projected} - members
        else:
            return frozenset(), projected
        return (
            frozenset(
                item.agent.agent_id
                for item in projected
                if item.agent.agent_id in policy_allowed
            ),
            projected,
        )

    @staticmethod
    def _project(
        template: dict[str, Any],
        user_state: dict[str, Any] | None,
        runtime_state: dict[str, Any] | None,
    ) -> EnterpriseA2AAgentView:
        manager_enabled = bool(template.get("enabled"))
        user_enabled = (
            True if user_state is None else bool(user_state.get("user_enabled"))
        )
        runtime = runtime_state or {}
        network_policy = (template.get("data") or {}).get("network_policy")
        invalid_policy = False
        if network_policy is None:
            network_policy = {}
        elif not isinstance(network_policy, dict):
            invalid_policy = True
        else:
            for value in network_policy.values():
                if not isinstance(value, bool):
                    invalid_policy = True
                    break
        if invalid_policy:
            logger.warning(
                "Invalid A2A network_policy template_id=%s; using strict defaults",
                template.get("template_id"),
            )
            network_policy = {}
        availability = str(
            runtime.get("availability") or A2AOutboundAvailability.AVAILABLE.value
        )
        agent = A2AOutboundAgent.from_record(
            {
                "agent_id": template.get("template_id"),
                "display_name": template.get("template_name"),
                "source_url": template.get("source_url"),
                "card_path": template.get("card_path"),
                "card_fingerprint": template.get("card_fingerprint"),
                "card_revision": template.get("card_revision"),
                "agent_card": template.get("agent_card"),
                "network_policy": network_policy,
                "selected_interface": template.get("selected_interface"),
                "enabled": manager_enabled and user_enabled,
                "availability": availability,
                "credential_ref": template.get("credential_ref"),
                "connect_timeout_seconds": template.get("connect_timeout_seconds"),
                "sync_wait_seconds": template.get("sync_wait_seconds"),
                "last_checked_at": _to_timestamp_text(runtime.get("last_checked_at")),
                "last_success_at": _to_timestamp_text(runtime.get("last_success_at")),
                "last_error_code": runtime.get("last_error_code"),
                "last_error_summary": runtime.get("last_error_summary"),
                "created_at": _to_timestamp_text(template.get("created_at")),
                "updated_at": _to_timestamp_text(template.get("updated_at")),
            }
        )
        return EnterpriseA2AAgentView(
            agent=agent,
            manager_enabled=manager_enabled,
            user_enabled=user_enabled,
        )

    async def get_projected_agent(
        self, template_id: str
    ) -> EnterpriseA2AAgentView | None:
        normalized = str(template_id or "").strip()
        template = await self._templates.get(template_id=normalized)
        if template is None:
            return None
        user_state, runtime_state = await asyncio.gather(
            self._user_states.get(template_id=normalized),
            self._runtime_states.get(template_id=normalized),
        )
        return self._project(template, user_state, runtime_state)

    async def list_projected_agents(self) -> list[EnterpriseA2AAgentView]:
        templates, user_rows, runtime_rows = await asyncio.gather(
            self._templates.list(order_by="updated_at DESC"),
            self._user_states.list(),
            self._runtime_states.list(),
        )
        user_states = {row["template_id"]: row for row in user_rows}
        runtime_states = {row["template_id"]: row for row in runtime_rows}
        return [
            self._project(
                template,
                user_states.get(template.get("template_id")),
                runtime_states.get(template.get("template_id")),
            )
            for template in templates
        ]

    async def get_agent(self, agent_id: str) -> A2AOutboundAgent | None:
        projected = await self.get_projected_agent(agent_id)
        return None if projected is None else projected.agent

    async def list_agents(self) -> list[A2AOutboundAgent]:
        return [item.agent for item in await self.list_projected_agents()]

    async def set_user_enabled(
        self, template_id: str, user_enabled: bool
    ) -> EnterpriseA2AAgentView:
        if not isinstance(user_enabled, bool):
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)
        normalized = str(template_id or "").strip()
        if await self._templates.get(template_id=normalized) is None:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        await self._user_states.upsert(
            {
                "template_id": normalized,
                "user_enabled": user_enabled,
                "updated_at": _utc_now(),
            }
        )
        projected = await self.get_projected_agent(normalized)
        if projected is None:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        return projected

    async def update_runtime_state(
        self,
        template_id: str,
        availability: A2AOutboundAvailability | str,
        *,
        error_code: str | None = None,
    ) -> None:
        normalized = str(template_id or "").strip()
        if await self._templates.get(template_id=normalized) is None:
            raise A2AOutboundError(A2AOutboundErrorCode.AGENT_NOT_REGISTERED)
        try:
            state = A2AOutboundAvailability(availability)
        except ValueError as exc:
            raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID) from exc
        now = _utc_now()
        existing = await self._runtime_states.get(template_id=normalized)
        values = {
            "template_id": normalized,
            "availability": state.value,
            "last_checked_at": now,
            "last_success_at": (
                now
                if state is A2AOutboundAvailability.AVAILABLE
                else _to_datetime((existing or {}).get("last_success_at"))
            ),
            "last_error_code": error_code,
            "last_error_summary": (
                safe_error_summary(error_code) if error_code else None
            ),
            "updated_at": now,
        }
        await self._runtime_states.upsert(values)

    async def clear_agent_state(self, template_id: str) -> None:
        key = {"template_id": str(template_id or "").strip()}
        await asyncio.gather(
            self._user_states.delete(key),
            self._runtime_states.delete(key),
        )

    async def create_agent(self, agent: A2AOutboundAgent) -> A2AOutboundAgent:
        del agent
        raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)

    async def update_agent(
        self,
        agent_id: str,
        updater: Callable[[A2AOutboundAgent], A2AOutboundAgent],
    ) -> A2AOutboundAgent | None:
        del agent_id, updater
        raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)

    async def delete_agent(self, agent_id: str) -> bool:
        del agent_id
        raise A2AOutboundError(A2AOutboundErrorCode.STORE_INVALID)


__all__ = ["EnterpriseA2AAgentView", "EnterpriseA2AProjection"]
