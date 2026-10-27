"""Durable result delivery for the JiuwenMate runtime: messages and handed-off files."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from openjiuwen.harness.persistent_agent import AgentEvent, EventType

from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.group_collaboration import resolve_group_mentions

logger = logging.getLogger(__name__)

GatewaySend = Callable[[dict[str, object]], Awaitable[Mapping[str, Any] | None]]
MentionRosterSource = Callable[[dict[str, object]], Sequence[Mapping[str, Any]]]
# A file send uploads up to the published share_file limit before the message is sent.
FILE_SEND_TIMEOUT = 180.0
# How long AgentServer waits for Gateway's ACK; shorter than the service timeouts above.
GATEWAY_ACK_TIMEOUT = 25.0
GATEWAY_FILE_ACK_TIMEOUT = 170.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResultDeliveryService:
    """Claim owned IM outputs handed off by a final or paused event, request a Gateway send, and persist its ACK."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        namespace_id: str,
        send_delivery: GatewaySend,
        channels: frozenset[str] = frozenset({"feishu"}),
        clock: Callable[[], datetime] = _utc_now,
        poll_interval: float = 1.0,
        send_timeout: float = 30.0,
        archive_delivery: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        file_send_timeout: float = FILE_SEND_TIMEOUT,
        mention_roster: MentionRosterSource | None = None,
        handoff_handler: Callable[[dict[str, object], Sequence[Mapping[str, Any]], str | None], Awaitable[None]]
        | None = None,
    ) -> None:
        if poll_interval <= 0 or send_timeout <= 0 or file_send_timeout <= 0:
            raise ValueError("delivery intervals must be positive")
        self._file_send_timeout = file_send_timeout
        self._mention_roster = mention_roster
        self._handoff_handler = handoff_handler
        self._repository = repository
        self._namespace_id = namespace_id
        self._channels = channels
        self._send_delivery = send_delivery
        self._archive_delivery = archive_delivery
        self._archive_after: tuple[str, str] | None = None
        self._welink_handoff_after = ""
        self._welink_reconcile_after = ""
        self._next_welink_reconcile: datetime | None = None
        self._clock = clock
        self._poll_interval = poll_interval
        self._send_timeout = send_timeout
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._welink_task: asyncio.Task[None] | None = None

    def set_mention_roster(self, resolver: MentionRosterSource) -> None:
        """Attach the host's current-group membership resolver after runtime assembly."""
        self._mention_roster = resolver

    def set_handoff_handler(
        self,
        handler: Callable[[dict[str, object], Sequence[Mapping[str, Any]], str | None], Awaitable[None]],
    ) -> None:
        """Start the next colleague after a public handoff has a platform receipt."""
        self._handoff_handler = handler

    async def start(self) -> None:
        """Mark interrupted sends unknown and resume only definitely pending rows."""
        if self._task is not None:
            return
        await asyncio.to_thread(
            self._repository.recover_sending_result_deliveries,
            self._namespace_id,
            channels=self._channels,
            now=self._clock(),
        )
        self._task = asyncio.create_task(self._run(), name="jiuwenmate-result-delivery")
        self._welink_task = asyncio.create_task(
            self._run_welink_reconciliation(), name="jiuwenmate-welink-reconciliation",
        )
        self._wake.set()

    async def aclose(self) -> None:
        tasks = [task for task in (self._task, self._welink_task) if task is not None]
        self._task = None
        self._welink_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def observe(self, event: AgentEvent) -> None:
        """Wake the worker only after a critical final or paused event has been persisted."""
        if event.namespace_id == self._namespace_id and event.event_type in (
            EventType.EXECUTION_FINAL,
            EventType.EXECUTION_PAUSED,
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
                channels=self._channels,
                limit=1,
                exclude_delivery_ids=attempted_delivery_ids,
                now=self._clock(),
            )
            if not jobs:
                break
            job = jobs[0]
            delivery_id = str(job["delivery_id"])
            attempted_delivery_ids.append(delivery_id)
            text = job.get("text")
            if isinstance(text, str) and "[[agent:" in text:
                roster = []
                if self._mention_roster is not None and job.get("is_direct_message") is not True:
                    try:
                        roster = await asyncio.to_thread(self._mention_roster, dict(job))
                    except Exception as exc:
                        logger.warning(
                            "[JiuwenMate] handoff roster unavailable: request=%s error_type=%s",
                            job.get("request_id"),
                            type(exc).__name__,
                        )
                cleaned_text, mentions = resolve_group_mentions(
                    text,
                    roster,
                    sender_agent_id=str(job.get("agent_id") or ""),
                )
                if job.get("channel") == "welink":
                    for target in mentions:
                        cleaned_text = cleaned_text.replace(f"[[agent:{target['agent_id']}]]", target["name"])
                job["text"] = cleaned_text
                if mentions:
                    job["mention_targets"] = mentions
            if job.get("channel") == "welink":
                await asyncio.to_thread(self._repository.save_result_delivery_job, self._namespace_id, job)
            timeout = self._file_send_timeout if "file" in job else self._send_timeout
            try:
                gateway_job = dict(job)
                for field in ("namespace_id", "agent_id", "session_id", "request_kind"):
                    gateway_job.pop(field, None)
                ack = await asyncio.wait_for(self._send_delivery(gateway_job), timeout=timeout)
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
                file_key=self._file_key(job, ack),
            )
            dispatched += 1
            if (outcome == "delivered" and job.get("channel") != "welink"
                    and job.get("mention_targets") and self._handoff_handler is not None):
                try:
                    await self._handoff_handler(
                        job,
                        job["mention_targets"],
                        external_message_id,
                    )
                except Exception:
                    logger.warning(
                        "[JiuwenMate] group handoff was not accepted: delivery_id=%s",
                        delivery_id,
                    )
        await self.reconcile_archives()
        return dispatched

    async def reconcile_welink_deliveries(self) -> None:
        """Ask Gateway for history evidence. Unknown rows remain fenced from dispatch."""
        now = self._clock()
        if self._next_welink_reconcile is not None and now < self._next_welink_reconcile:
            return
        self._next_welink_reconcile = now + timedelta(seconds=10)
        jobs = await asyncio.to_thread(
            self._repository.unknown_welink_result_deliveries, self._namespace_id,
            after=self._welink_reconcile_after, limit=1,
        )
        if not jobs and self._welink_reconcile_after:
            self._welink_reconcile_after = ""
            jobs = await asyncio.to_thread(self._repository.unknown_welink_result_deliveries, self._namespace_id, limit=1)
        for job in jobs:
            self._welink_reconcile_after = str(job["delivery_id"])
            try:
                ack = await asyncio.wait_for(self._send_delivery(dict(job)), timeout=self._send_timeout)
                outcome, message_id, error_code = self._classify_ack(job, ack)
                if outcome not in {"delivered", "failed"}:
                    continue
                await asyncio.to_thread(
                    self._repository.update_result_delivery, self._namespace_id, job["delivery_id"],
                    outcome=outcome, external_message_id=message_id, error_code=error_code, now=self._clock(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[JiuwenMate] WeLink delivery still unconfirmed: delivery_id=%s", job["delivery_id"])

        await self._reconcile_welink_handoffs()

    async def _reconcile_welink_handoffs(self) -> None:
        if self._handoff_handler is None:
            return
        jobs = await asyncio.to_thread(
            self._repository.pending_welink_handoffs, self._namespace_id, after=self._welink_handoff_after,
        )
        if not jobs and self._welink_handoff_after:
            self._welink_handoff_after = ""
            jobs = await asyncio.to_thread(self._repository.pending_welink_handoffs, self._namespace_id)
        for job in jobs:
            self._welink_handoff_after = str(job["delivery_id"])
            try:
                validation = await asyncio.wait_for(
                    self._send_delivery(dict(job, operation="validate_handoff")), timeout=self._send_timeout,
                )
                if (not isinstance(validation, Mapping) or validation.get("delivery_id") != job["delivery_id"]
                        or validation.get("stage") != "send"):
                    continue
                valid = validation.get("handoff_target_valid")
                if valid is True:
                    await self._handoff_handler(job, job["mention_targets"], job["external_message_id"])
                elif valid is not False:
                    continue
                else:
                    logger.info("[JiuwenMate] WeLink handoff target unavailable: delivery_id=%s", job["delivery_id"])
                await asyncio.to_thread(self._repository.mark_welink_handoff_done, self._namespace_id, job["delivery_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[JiuwenMate] WeLink confirmed handoff pending: delivery_id=%s", job["delivery_id"])

    async def reconcile_archives(self) -> None:
        """Retry only public-record persistence after a durable send ACK; never resend the message."""
        if self._archive_delivery is None:
            return
        jobs = await asyncio.to_thread(self._repository.pending_delivery_archives, self._namespace_id,
                                       after=self._archive_after)
        if not jobs and self._archive_after is not None:
            self._archive_after = None
            jobs = await asyncio.to_thread(self._repository.pending_delivery_archives, self._namespace_id)
        if jobs:
            self._archive_after = (jobs[-1]["updated_at"], jobs[-1]["delivery_id"])
        for job in jobs:
            try:
                await self._archive_delivery(job)
                await asyncio.to_thread(self._repository.mark_delivery_archived, self._namespace_id, job["delivery_id"])
            except Exception:
                logger.warning("[JM-11] confirmed delivery archive pending: delivery_id=%s", job["delivery_id"])

    async def apply_ack(self, ack: Mapping[str, Any]) -> dict[str, object]:
        """Persist an ACK that arrived after the request waiter timed out or disconnected."""
        delivery_id = ack.get("delivery_id")
        if (
            not isinstance(delivery_id, str)
            or not delivery_id.strip()
            or ack.get("stage") != "send"
        ):
            raise ValueError("invalid result delivery ACK")
        if ack.get("operation") == "validate_handoff":
            return {"delivery_id": delivery_id, "status": "validation"}
        outcome = ack.get("outcome")
        external_message_id = ack.get("external_message_id")
        error_code = ack.get("error_code")
        if outcome not in {"delivered", "failed", "unknown", "pending"}:
            raise ValueError("invalid result delivery ACK outcome")
        saved = await asyncio.to_thread(
            self._repository.update_result_delivery,
            self._namespace_id,
            delivery_id,
            outcome=outcome,
            external_message_id=external_message_id,
            error_code=error_code,
            now=self._clock(),
            file_key=ack.get("file_key") if isinstance(ack.get("file_key"), str) else None,
        )
        await self.reconcile_archives()
        return saved

    @staticmethod
    def _file_key(job: Mapping[str, object], ack: Mapping[str, Any] | None) -> str | None:
        """The upload's ``file_key`` from an ACK that answers this delivery."""
        if ack is None or ack.get("delivery_id") != job.get("delivery_id"):
            return None
        file_key = ack.get("file_key")
        return file_key if isinstance(file_key, str) and file_key.strip() else None

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

    async def _run_welink_reconciliation(self) -> None:
        """Keep slow WeLink history requests and scan failures off the delivery worker."""
        while True:
            try:
                await self.reconcile_welink_deliveries()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[JiuwenMate] WeLink reconciliation scan failed")
            await asyncio.sleep(self._poll_interval)
