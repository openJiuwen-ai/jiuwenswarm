"""Event-driven SDK grants with injected publication and playback operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from openjiuwen.agent_teams.meeting import MeetingCoordinator
from openjiuwen.agent_teams.schema.meeting import (
    MeetingScope,
    SpeechInterruption,
    SpeechRequest,
    SpeechRequestStatus,
)

logger = logging.getLogger(__name__)


class MeetingPlaybackConsumer(Protocol):
    """Trusted host boundary. Returning from speech_ready is NOT completion.

    The consumer must stop all matching work before stop returns. It must not
    wait for the driver to shut down from inside one of its own callbacks.
    """

    async def speech_ready(self, request: SpeechRequest) -> None: ...

    async def speech_error(self, request: SpeechRequest | None, error: str) -> None: ...

    async def stop(self) -> None: ...

    async def interrupt(self, interruption: SpeechInterruption) -> None: ...

    def abandon_transport(self) -> None: ...


@dataclass(frozen=True)
class GrantDriverState:
    phase: Literal[
        "idle", "publishing", "awaiting_playback", "publication_failed",
        "consumer_failed", "playback_failed", "coordination_failed", "closing", "closed",
        "interrupting", "interruption_failed",
    ] = "idle"
    request: SpeechRequest | None = None
    error: str | None = None


class MeetingGrantDriver:
    """One serialized worker per meeting, woken by admission and explicit acks.

    SDK methods choose the next speaker; the host explicitly publishes full text. No timer,
    synthesis result, or consumer callback return can release a speaking lease.
    Publication failures and explicit audio stops hold coordination. Confirmed
    failed audio revokes its lease and advances. Interruption waits
    for audio stop and human input acceptance, then resumes the SDK queue.
    """

    def __init__(
        self, coordinator: MeetingCoordinator, consumer: MeetingPlaybackConsumer | None = None, *,
        is_current: Callable[[], Awaitable[bool]],
        publish_granted: Callable[[str, str | None, bool, bool], Awaitable[SpeechRequest | None]],
    ) -> None:
        if consumer is not None and any(not callable(getattr(consumer, name, None)) for name in ("speech_ready", "speech_error", "stop", "interrupt")):
            raise ValueError("A playback consumer with ready, error, and stop callbacks is required")
        self.coordinator = coordinator
        self.consumer = consumer
        self._is_current = is_current
        self._publish_granted = publish_granted
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._stopped = False
        self._retry_requested = False
        self._state = GrantDriverState()
        self._interruption: SpeechInterruption | None = None
        self._audio_stopped = False
        self._input_admitted = False
        self._finished_interruptions: set[str] = set()
        self._audio_enabled = True
        self._detaching = False
        self._attachment_lock = asyncio.Lock()
        self._shutdown_phase: str | None = None
        self._recovering = False
        self._transport_lost = False

    def set_audio_enabled(self, enabled: bool) -> None:
        if self._audio_enabled != enabled:
            self._audio_enabled = enabled
            if self._task is not None:
                self.wake()

    def recover_publication(self, request: SpeechRequest) -> None:
        if not request.lease_token or self._task is not None:
            raise ValueError("Recovery requires an unstarted worker and a claimed request")
        self._state = GrantDriverState("publication_failed", request)
        self._retry_requested = True
        self._recovering = True

    async def attach_consumer(self, consumer: MeetingPlaybackConsumer) -> None:
        if any(not callable(getattr(consumer, name, None)) for name in ("speech_ready", "speech_error", "stop", "interrupt")):
            raise ValueError("A playback consumer with ready, error, stop, and interrupt callbacks is required")
        async with self._attachment_lock, self._lock:
            if self._closed or self._detaching or (self.consumer is not None and self.consumer is not consumer):
                raise ValueError("Meeting queue already has a playback consumer or is stopping")
            self.consumer = consumer
            self._stopped = False
            self._transport_lost = False
            if self._task is not None:
                self.wake()

    def abandon_transport(self, consumer: MeetingPlaybackConsumer) -> None:
        if self.consumer is not consumer:
            raise ValueError("Playback consumer is no longer current")
        consumer.abandon_transport()
        self._transport_lost = True
        self._detaching = True

    async def _release_abandoned_speech(self) -> None:
        state = await self.coordinator.get_state()
        if state is not None and not state.closed and state.lease_token:
            request = await self.coordinator.get_speaker()
            current = self._state.request
            if (
                request is None or current is None or request.request_id != current.request_id
                or request.lease_token != current.lease_token or request.lease_token != state.lease_token
            ):
                raise RuntimeError("Abandoned speech no longer matches the current claim")
            if request.source_message_id is None and request.content != "":
                # Detaching audio must not discard text whose publication failed.
                published = await self._publish_granted(request.request_id, request.lease_token, False, True)
                if published is not None and (published.delivery_error or published.message_error):
                    self._recovering = True
                    self._state = GrantDriverState(
                        "publication_failed", published,
                        published.delivery_error or published.message_error,
                    )
                    return
                state = await self.coordinator.get_state()
                if state is not None and state.lease_token:
                    raise RuntimeError("Abandoned publication could not be finished silently")
        if state is not None and not state.closed and (state.lease_token or state.interruption_id):
            released = await self.coordinator.force_release(
                expected_token=state.lease_token, expected_interruption_id=state.interruption_id,
            )
            if not released:
                latest = await self.coordinator.get_state()
                if latest is not None and not latest.closed and (latest.lease_token or latest.interruption_id):
                    raise RuntimeError("Abandoned speech ownership changed during recovery")
        self._interruption = None
        self._audio_stopped = False
        self._input_admitted = False
        self._state = GrantDriverState()
        self._recovering = False

    async def detach_consumer(self) -> None:
        """Explicit transport teardown; meeting disable only changes the audio gate."""
        self._detaching = True
        async with self._attachment_lock:
            consumer = self.consumer
            if consumer is None:
                self._detaching = False
                return
            await consumer.stop()
            async with self._lock:
                request = self._state.request
                if self._transport_lost:
                    await self._release_abandoned_speech()
                    request = None
                elif self._interruption is not None:
                    if not await self.coordinator.resume_after_interruption(self._interruption.interruption_id):
                        raise RuntimeError("Stopped interruption barrier could not be released")
                    self._interruption = None
                    self._state = GrantDriverState()
                    request = None
                if request is not None and self._state.phase != "publication_failed" and request.lease_token:
                    interruption = await self.coordinator.interrupt_speech(expected_token=request.lease_token)
                    if interruption is not None:
                        if not await self.coordinator.resume_after_interruption(interruption.interruption_id):
                            raise RuntimeError("Stopped speech interruption could not be released")
                    self._state = GrantDriverState()
                    self._interruption = None
                self.consumer = None
                self._detaching = False
                if self._task is not None:
                    self.wake()

    @property
    def state(self) -> GrantDriverState:
        return self._state

    @property
    def started(self) -> bool:
        return self._task is not None

    @property
    def audio_enabled(self) -> bool:
        return self._audio_enabled

    @property
    def pending_interruption(self) -> SpeechInterruption | None:
        return self._interruption

    def start(self) -> None:
        if self._closed:
            raise ValueError("This grant driver has ended")
        self.wake()

    def wake(self) -> None:
        if not self._closed:
            if self._task is None:
                self._task = asyncio.create_task(self._run(), name=f"meeting-grants-{self.coordinator.scope.meeting_id}")
            if self._state.phase == "publication_failed":
                self._retry_requested = True
            self._wake.set()

    def close(self) -> None:
        """Revoke immediately; aclose drains work before the manager closes the DB scope."""
        if self._closed:
            return
        self._shutdown_phase = self._state.phase
        self._closed = True
        self._state = GrantDriverState("closing", self._state.request)
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()

    async def aclose(self) -> None:
        self.close()
        if self._task is not None and self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)
        # Drain a completion already in progress before stopping playback and
        # letting the manager close the coordinator/database resources.
        async with self._lock:
            if self.consumer is not None and not self._stopped:
                await self.consumer.stop()
                self._stopped = True
            request = self._state.request
            if self._transport_lost and self.consumer is not None:
                await self._release_abandoned_speech()
            elif request is not None and request.lease_token and self._shutdown_phase not in {"publication_failed", "publishing"}:
                interruption = await self.coordinator.interrupt_speech(expected_token=request.lease_token)
                if interruption is not None:
                    await self.coordinator.resume_after_interruption(interruption.interruption_id)
            if self._interruption is not None:
                await self.coordinator.resume_after_interruption(self._interruption.interruption_id)
                self._interruption = None
            self.consumer = None
            self._state = GrantDriverState("closed", self._state.request, "meeting_closed")

    def _matches(self, scope: MeetingScope, request_id: str, lease_token: str) -> bool:
        request = self._state.request
        return bool(
            not self._closed and not self._transport_lost and scope == self.coordinator.scope and request is not None
            and request.request_id == request_id and lease_token and request.lease_token == lease_token
        )

    async def playback_is_current(self, scope: MeetingScope, request_id: str, lease_token: str) -> bool:
        """Authorize audio against both local playback and the SDK's current lease."""
        if not await self._is_current():
            return False
        speaker = await self.coordinator.get_speaker()
        # Recheck local state after the SDK read: interruption/teardown may have
        # completed while it was in flight. Reading never grants or republishes.
        return bool(
            not self._detaching and self._state.phase == "awaiting_playback" and self._matches(scope, request_id, lease_token)
            and speaker is not None and speaker.scope == scope
            and speaker.request_id == request_id and speaker.lease_token == lease_token
        )

    async def complete_playback(self, scope: MeetingScope, request_id: str, lease_token: str) -> bool:
        if not await self._is_current():
            return False
        async with self._lock:
            if not self._matches(scope, request_id, lease_token) or self._state.phase != "awaiting_playback":
                return False
            if not await self.coordinator.release(lease_token):
                return False
            if not self._closed:
                self._state = GrantDriverState()
                self.wake()
            return True

    async def fail_playback(
        self, scope: MeetingScope, request_id: str, lease_token: str, *, advance: bool = False,
    ) -> bool:
        if not await self._is_current():
            return False
        async with self._lock:
            if not self._matches(scope, request_id, lease_token) or self._state.phase != "awaiting_playback":
                return False
            if advance or self._detaching or not self._audio_enabled:
                # The owner reported failed audio and Gateway drained synthesis.
                # Revoke as interrupted, never falsely acknowledge it as played.
                interruption = await self.coordinator.interrupt_speech(expected_token=lease_token)
                if interruption is None:
                    return False
                if not await self.coordinator.resume_after_interruption(interruption.interruption_id):
                    raise RuntimeError("Failed speech interruption could not be released")
                logger.warning("Skipping failed meeting audio request=%s", request_id)
                self._state = GrantDriverState()
                self.wake()
                return True
            self._state = GrantDriverState("playback_failed", self._state.request, "playback_failed")
        # No complete_playback call: failed/stopped audio is never reported as played.
        await self._report_error()
        return True

    async def retry_publication(self, scope: MeetingScope, request_id: str, lease_token: str) -> bool:
        if not await self._is_current():
            return False
        async with self._lock:
            if not self._matches(scope, request_id, lease_token) or self._state.phase != "publication_failed":
                return False
            self._retry_requested = True
            self.wake()
            return True

    async def interrupt(
        self, expected_token: str, *, leader: str | None = None, human: bool = False,
    ) -> SpeechInterruption | None:
        """Revoke exactly the observed lease; grants wait for real audio/input acks."""
        if not await self._is_current():
            return None
        async with self._lock:
            request = self._state.request
            if (
                self._closed or self._transport_lost or self.consumer is None or request is None or request.lease_token != expected_token
                or self._interruption is not None or self._state.phase not in {"awaiting_playback", "publication_failed"}
            ):
                return None
            interruption = (
                await self.coordinator.interrupt_from_leader(leader, expected_token=expected_token)
                if leader is not None else
                await self.coordinator.interrupt_speech(expected_token=expected_token)
            )
            if interruption is None:
                return None
            self._interruption = interruption
            self._audio_stopped = False
            self._input_admitted = not human
            self._state = GrantDriverState("interrupting", request)
        # Stop acknowledgements are handled independently of the driver lock.
        # The SDK's durable interruption barrier blocks all new grants meanwhile.
        try:
            await self.consumer.interrupt(interruption)
        except Exception:
            async with self._lock:
                if self._transport_lost or self._interruption is not interruption:
                    return interruption
                if not self._closed:
                    self._state = GrantDriverState("interruption_failed", request, "interruption_stop_failed")
            logger.exception("Meeting speech stop was not confirmed")
            await self._report_error()
            raise RuntimeError("Meeting speech stop was not confirmed; end the meeting to recover") from None
        async with self._lock:
            if self._transport_lost or self._interruption is not interruption:
                return interruption
            self._audio_stopped = True
            await self._resume_interruption()
        return interruption

    async def input_admitted(self, interruption_id: str) -> bool:
        if not await self._is_current():
            return False
        async with self._lock:
            if self._closed or self._transport_lost:
                return False
            if interruption_id in self._finished_interruptions:
                return True
            if self._interruption is None or self._interruption.interruption_id != interruption_id:
                return False
            self._input_admitted = True
            await self._resume_interruption()
            return True

    async def _resume_interruption(self) -> None:
        if self._closed or self._transport_lost or self._interruption is None or not self._audio_stopped or not self._input_admitted:
            return
        interruption_id = self._interruption.interruption_id
        if not await self.coordinator.resume_after_interruption(interruption_id):
            raise RuntimeError("Meeting interruption barrier is no longer current")
        self._finished_interruptions.add(interruption_id)
        self._interruption = None
        self._state = GrantDriverState()
        self.wake()

    async def _report_error(self) -> None:
        if self._closed or self._transport_lost or self.consumer is None:
            return
        try:
            await self.consumer.speech_error(self._state.request, self._state.error or "coordination_failed")
        except Exception:
            logger.exception("Meeting consumer could not receive error for %s", self.coordinator.scope)

    async def _step(self) -> None:
        if not await self._is_current():
            return
        async with self._lock:
            if self._closed or self._detaching:
                return
            current = self._state
            retry = current.phase == "publication_failed" and self._retry_requested
            if current.phase != "idle" and not retry:
                return
            self._retry_requested = False
            self._state = GrantDriverState("publishing", current.request)
            playback_ready = self.consumer is not None and self._audio_enabled and not self._detaching and not self._recovering
            if retry:
                assert current.request is not None and current.request.lease_token
                request = await self.coordinator.get_speaker()
                if request is not None and request.lease_token != current.request.lease_token:
                    request = None
            else:
                request = await self.coordinator.grant_next(playback_ready=playback_ready)
            if request is not None:
                request = await self._publish_granted(
                    request.request_id, request.lease_token, playback_ready, retry,
                )
            if self._closed:
                return
            if request is None:
                # A revoked retry is not a fresh admission. Do not pick another
                # speaker until its lifecycle/interruption owner resolves it.
                self._state = (
                    GrantDriverState("coordination_failed", current.request, "lease_unavailable")
                    if retry else GrantDriverState()
                )
            elif (
                request.scope != self.coordinator.scope
                or request.status not in {SpeechRequestStatus.GRANTED, SpeechRequestStatus.PUBLISHING, SpeechRequestStatus.TEXT_ONLY}
                or (retry and (
                    request.request_id != current.request.request_id
                    or (request.status != SpeechRequestStatus.TEXT_ONLY and request.lease_token != current.request.lease_token)
                ))
            ):
                self._state = GrantDriverState("coordination_failed", current.request, "invalid_speech_grant")
            elif (
                request.status == SpeechRequestStatus.TEXT_ONLY and request.lease_token is None
                and (request.source_message_id is not None or request.content == "")
                and not request.delivery_error and not request.message_error
            ):
                # The host finished a dequeue with audio disabled, publishing
                # text only when present. There is no speech to replay later.
                self._state = GrantDriverState()
                self._recovering = False
                self.wake()
            elif not request.lease_token:
                self._state = GrantDriverState("coordination_failed", request, "invalid_speech_grant")
            elif (
                request.delivery_error or request.message_error
                or (request.source_message_id is None and request.content != "")
            ):
                self._state = GrantDriverState(
                    "publication_failed", request,
                    request.delivery_error or request.message_error or "source_message_unavailable",
                )
            elif request.status != SpeechRequestStatus.GRANTED or not playback_ready:
                self._state = GrantDriverState("coordination_failed", request, "invalid_speech_grant")
            else:
                self._state = GrantDriverState("awaiting_playback", request)
        if self._state.error:
            await self._report_error()
        elif self._state.phase == "awaiting_playback" and request is not None and not self._closed and await self._is_current():
            consumer = self.consumer
            if consumer is None or self._state.phase != "awaiting_playback" or not self._matches(
                request.scope, request.request_id, request.lease_token,
            ):
                return
            try:
                # Do not hold the lock across consumer code: a controlled consumer
                # may acknowledge actual completion within this callback.
                await consumer.speech_ready(request)
            except Exception:
                logger.exception("Meeting playback delivery failed for %s", request.request_id)
                failed = False
                async with self._lock:
                    if (
                        self._matches(request.scope, request.request_id, request.lease_token)
                        and self._state.phase == "awaiting_playback"
                    ):
                        self._state = GrantDriverState("consumer_failed", request, "consumer_delivery_failed")
                        failed = True
                if failed:
                    await self._report_error()

    async def _run(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self._step()
            except Exception:
                logger.exception("Meeting grant coordination failed for %s", self.coordinator.scope)
                if not self._closed:
                    self._state = GrantDriverState("coordination_failed", self._state.request, "coordination_failed")
                    await self._report_error()
