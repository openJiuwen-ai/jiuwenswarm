"""``BudgetPort``: model-call admission and usage settlement backed by ``BudgetRepository``.

core asks for admission before every model call and skips the call unless it is
admitted; it reports measured or unknown usage afterwards. Storage failures are
raised: on ``admit`` core pauses the Execution with an error, on ``report`` it
only logs, and the reservation stays until reconciled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from jiuwenswarm.server.jiuwenmate.budget_repository import (
    BudgetAccountRef,
    BudgetRepository,
    receipt_ref,
    reservation_ref,
)
from jiuwenswarm.server.jiuwenmate.execution_models import PersistentContractError
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from openjiuwen.harness.persistent_agent import (
    BudgetAdmission,
    BudgetReport,
    ExecutionContext,
    InvocationRef,
    ModelUsage,
)

# Placeholder until real calls are measured (JM-07).
DEFAULT_RESERVATION_TOKENS = 4096


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HostBudgetPort:
    """agent-core ``BudgetPort``; group Sessions share their group's account."""

    def __init__(
        self,
        repository: BudgetRepository,
        sessions: ExecutionRepository,
        *,
        reservation_tokens: int = DEFAULT_RESERVATION_TOKENS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if isinstance(reservation_tokens, bool) or not isinstance(reservation_tokens, int) or reservation_tokens <= 0:
            raise ValueError("reservation_tokens must be a positive integer")
        self._repository = repository
        self._sessions = sessions
        self._reservation_tokens = reservation_tokens
        self._clock = clock

    def _account(self, execution: ExecutionContext) -> BudgetAccountRef:
        session = self._sessions.get_session(execution.namespace_id, execution.session_id)
        if session.agent_id != execution.agent_id:
            raise PersistentContractError("Session belongs to another Agent")
        if session.conversation.kind == "group":
            return BudgetAccountRef("group", session.conversation.key)
        return BudgetAccountRef("agent", execution.agent_id)

    def _admit(self, execution: ExecutionContext, model_call_id: str) -> BudgetAdmission:
        ref = InvocationRef.from_execution(execution)
        decision = self._repository.reserve(
            ref, model_call_id, self._account(execution), self._reservation_tokens, now=self._clock()
        )
        admitted = decision.status == "admitted"
        return BudgetAdmission(
            **ref.model_dump(),
            model_call_id=model_call_id,
            status=decision.status,
            reservation_ref=reservation_ref(model_call_id) if admitted else None,
            reason=decision.reason,
        )

    def _report(self, execution: ExecutionContext, model_call_id: str, usage: ModelUsage) -> BudgetReport:
        ref = InvocationRef.from_execution(execution)
        decision = self._repository.settle(ref, model_call_id, ModelUsage.model_validate(usage), now=self._clock())
        confirmed = decision.status == "confirmed"
        return BudgetReport(
            **ref.model_dump(),
            model_call_id=model_call_id,
            status=decision.status,
            receipt_ref=receipt_ref(model_call_id) if confirmed else None,
            reason=decision.reason,
        )

    async def admit(self, execution: ExecutionContext, model_call_id: str) -> BudgetAdmission:
        return await asyncio.to_thread(self._admit, execution, model_call_id)

    async def report(self, execution: ExecutionContext, model_call_id: str, usage: ModelUsage) -> BudgetReport:
        return await asyncio.to_thread(self._report, execution, model_call_id, usage)
