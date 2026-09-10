"""Manager-owned A2A access policy projection."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.tables.template_models import (
    A2A_ACCESS_POLICY_TEMPLATE_TABLE_DEF,
)

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.template_schemas import (
    A2AAccessPolicyTemplateCreateRequest,
    A2AAccessPolicyTemplateUpdateRequest,
)

_TABLE = A2A_ACCESS_POLICY_TEMPLATE_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _policy_id(value: Any) -> str:
    policy_id = str(value or "").strip()
    if not policy_id:
        raise ValueError("policy_id is required")
    return policy_id


class A2AAccessPolicyTemplateService:
    async def _upsert(self, policy: dict[str, Any], *, full: bool) -> str:
        policy_id = _policy_id(policy.get("policy_id"))
        request = (
            A2AAccessPolicyTemplateCreateRequest.model_validate(policy)
            if full
            else A2AAccessPolicyTemplateUpdateRequest.model_validate(
                {key: value for key, value in policy.items() if key != "policy_id"}
            )
        )
        repo = require_enterprise_repository(_TABLE)
        existing = await repo.get(policy_id=policy_id)
        values = request.model_dump(exclude_unset=True, exclude={"policy_id"})
        now = utc_now()
        values["updated_at"] = parse_iso_datetime(values.get("updated_at")) or now
        if existing is None:
            await repo.create({"policy_id": policy_id, **values, "created_at": now})
        else:
            updated = await repo.update({"policy_id": policy_id}, values)
            if updated is None:
                raise ValueError(
                    f"a2a access policy policy_id={policy_id!r} not found"
                )
        return policy_id

    async def create(self, policy: dict[str, Any]) -> dict[str, str]:
        if not isinstance(policy, dict):
            raise ValueError("a2a_access_policies.create requires policy object")
        policy_id = await self._upsert(policy, full=True)
        logger.info(
            "[ManagerConfigReceiver] a2a_access_policies upsert policy_id=%s",
            policy_id,
        )
        return {"policy_id": policy_id}

    async def update(self, policy_id: str, updates: dict[str, Any]) -> None:
        pid = _policy_id(policy_id)
        if not isinstance(updates, dict) or not updates:
            raise ValueError("a2a_access_policies.update requires non-empty updates")
        repo = require_enterprise_repository(_TABLE)
        if await repo.get(policy_id=pid) is None:
            raise ValueError(f"a2a access policy policy_id={pid!r} not found")
        await self._upsert({"policy_id": pid, **updates}, full=False)
        logger.info(
            "[ManagerConfigReceiver] a2a_access_policies update policy_id=%s",
            pid,
        )

    async def delete(self, policy_id: str) -> None:
        pid = _policy_id(policy_id)
        await require_enterprise_repository(_TABLE).delete(policy_id=pid)
        logger.info(
            "[ManagerConfigReceiver] a2a_access_policies delete policy_id=%s",
            pid,
        )
