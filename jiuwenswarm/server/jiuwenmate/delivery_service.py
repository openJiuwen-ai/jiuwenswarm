"""Durable final-result delivery for the JiuwenMate runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from openjiuwen.harness.persistent_agent import AgentEvent, EventType

from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository

logger = logging.getLogger(__name__)

GatewaySend = Callable[[dict[str, object]], Awaitable[Mapping[str, Any] | None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResultDeliveryService:
    """Claim final Feishu text outputs, request a Gateway send, and persist its ACK."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        namespace_id: str,
        send_delivery: GatewaySend,
        clock: Callable[[], datetime] = _utc_now,
        poll_interval: float = 1.0,
        send_timeout: float = 30.0,
    ) -> None:
        if poll_interval <= 0 or send_timeout <= 0:
            raise ValueError("delivery intervals must be positive")
        self._repository = repository
        self._namespace_id = namespace_id
        self._send_delivery = send_delivery
        self._clock = clock
        self._poll_interval = poll_interval
        self._send_timeout = send_timeout
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Mark interrupted sends unknown and resume only definitely pending rows."""
        if self._task is not None:
            return
        await asyncio.to_thread(
            self._repository.recover_sending_result_deliveries,
            self._namespace_id,
            now=self._clock(),
        )
        self._task = asyncio.create_task(self._run(), name="jiuwenmate-result-delivery")
        self._wake.set()

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def observe(self, event: AgentEvent) -> None:
        """Wake the worker only after the critical final event has been persisted."""
        if (
            event.namespace_id == self._namespace_id
            and event.event_type == EventType.EXECUTION_FINAL
        ):
            self._wake.set()

    async def dispatch_pending(self, *, limit: int = 100) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")

        dispatched = 0
        attempted_delivery_ids: list[str] = []
        for _ in range(limit):
            jobs = await asyncio.to_thread(
                self._repository.claim_pending_result_deliveries,
                self._namespace_id,
                limit=1,
                exclude_delivery_ids=attempted_delivery_ids,
                now=self._clock(),
            )
            if not jobs:
                break
            job = jobs[0]
            delivery_id = str(job["delivery_id"])
            attempted_delivery_ids.append(delivery_id)
            try:
                ack = await asyncio.wait_for(
                    self._send_delivery(dict(job)), timeout=self._send_timeout
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                ack = {"outcome": "unknown", "error_code": "ACK_TIMEOUT"}
            except Exception as exc:  # an exception can follow a platform-side effect
                logger.warning(
                    "[JiuwenMate] Gateway result delivery failed ambiguously: delivery_id=%s error_type=%s",
                    delivery_id,
                    type(exc).__name__,
                )
                ack = {"outcome": "unknown", "error_code": "GATEWAY_ERROR"}

            outcome, external_message_id, error_code = self._classify_ack(job, ack)
            await asyncio.to_thread(
                self._repository.update_result_delivery,
                self._namespace_id,
                delivery_id,
                outcome=outcome,
                external_message_id=external_message_id,
                error_code=error_code,
                now=self._clock(),
            )
            dispatched += 1
        return dispatched

    async def apply_ack(self, ack: Mapping[str, Any]) -> dict[str, object]:
        """Persist an ACK that arrived after the request waiter timed out or disconnected."""
        delivery_id = ack.get("delivery_id")
        if (
            not isinstance(delivery_id, str)
            or not delivery_id.strip()
            or ack.get("stage") != "send"
        ):
            raise ValueError("invalid result delivery ACK")
        outcome = ack.get("outcome")
        external_message_id = ack.get("external_message_id")
        error_code = ack.get("error_code")
        if outcome not in {"delivered", "failed", "unknown", "pending"}:
            raise ValueError("invalid result delivery ACK outcome")
        return await asyncio.to_thread(
            self._repository.update_result_delivery,
            self._namespace_id,
            delivery_id,
            outcome=outcome,
            external_message_id=external_message_id,
            error_code=error_code,
            now=self._clock(),
        )

    @staticmethod
    def _classify_ack(
        job: Mapping[str, object], ack: Mapping[str, Any] | None
    ) -> tuple[str, str | None, str | None]:
        if ack is None:
            return "pending", None, "GATEWAY_UNAVAILABLE"
        if (
            ack.get("delivery_id") != job.get("delivery_id")
            or ack.get("stage") != "send"
        ):
            return "unknown", None, "ACK_MISMATCH"
        outcome = ack.get("outcome")
        message_id = ack.get("external_message_id")
        error_code = ack.get("error_code")
        if (
            outcome == "delivered"
            and isinstance(message_id, str)
            and message_id.strip()
        ):
            return "delivered", message_id, None
        if outcome == "failed":
            return (
                "failed",
                None,
                error_code if isinstance(error_code, str) else "FEISHU_REJECTED",
            )
        if outcome == "pending":
            return (
                "pending",
                None,
                error_code if isinstance(error_code, str) else "GATEWAY_UNAVAILABLE",
            )
        return (
            "unknown",
            None,
            error_code if isinstance(error_code, str) else "ACK_UNKNOWN",
        )

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.dispatch_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[JiuwenMate] result delivery scan failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
