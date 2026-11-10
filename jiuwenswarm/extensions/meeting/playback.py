"""AgentServer-owned speech records and browser playback completion boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

EVENT = "meeting.playback"
STOP_TIMEOUT_SECONDS = 10
IDENTITY_FIELDS = ("session_id", "meeting_id", "request_id", "lease_token", "source_message_id", "speaker_id", "playback_id")


def speech_chunks(text: str, limit: int = 800) -> tuple[str, ...]:
    if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
        raise ValueError("Speech summary must contain 1-16000 characters")
    rest, chunks = text.strip(), []
    while len(rest) > limit:
        prefix = rest[:limit]
        boundary = max(prefix.rfind(mark) for mark in ("。", ". ", "\n", "！", "？", "! ", "? "))
        end = boundary + 1 if boundary >= limit // 2 else limit
        chunks.append(rest[:end])
        rest = rest[end:]
    if rest:
        chunks.append(rest)
    return tuple(chunks)


@dataclass(frozen=True)
class SpeechPlayback:
    session_id: str
    meeting_id: str
    request_id: str
    lease_token: str
    source_message_id: str | None
    speaker_id: str
    playback_id: str
    voice: str
    model: str
    catalog_version: str
    config_revision: str
    chunks: tuple[str, ...]

    def identity(self) -> dict:
        return {key: getattr(self, key) for key in IDENTITY_FIELDS}

    def matches(self, params: dict) -> bool:
        return all(key in params and params[key] == value for key, value in self.identity().items())


class BrowserPlaybackConsumer:
    def __init__(self, scope, voices, *, owner_id, route_id, config_revision, emit, stop_timeout=STOP_TIMEOUT_SECONDS):
        self.scope, self.voices = scope, voices
        self.owner_id, self.route_id = owner_id, route_id
        self.config_revision, self.emit = config_revision, emit
        self.driver = None
        self._records: dict[str, SpeechPlayback] = {}
        self._request_ids: dict[str, str] = {}
        self._receipts: dict[str, tuple[str, int]] = {}
        self._current: str | None = None
        self._closing = False
        self._transport_lost = False
        self._result_lock = asyncio.Lock()
        self._stop_id: str | None = None
        self._stop_waiter: asyncio.Future | None = None
        self._stop_timeout = stop_timeout
        self._interrupted: set[str] = set()
        self._speech_stops: dict[str, asyncio.Future] = {}

    async def _push(self, event: str, payload: dict) -> None:
        if not await self.emit({
            "event_type": EVENT, "event": event, "route_id": self.route_id,
            "owner_id": self.owner_id, "session_id": self.scope.session_id,
            "meeting_id": self.scope.meeting_id, **payload,
        }):
            raise RuntimeError("Meeting playback could not reach its owning browser")

    async def addressed_members_changed(self, names: list[str]) -> None:
        if not self._closing:
            await self._push("meeting.addressed_members", {"addressed_members": names})

    async def speech_ready(self, request) -> None:
        if (
            self._closing or request.scope != self.scope or not request.lease_token
            or (request.source_message_id is None and request.content != "")
        ):
            raise ValueError("A matching granted speech request is required")
        if not await self.driver.playback_is_current(request.scope, request.request_id, request.lease_token):
            return
        playback_id = self._request_ids.get(request.request_id)
        if playback_id is None:
            voice = self.voices.get_voice(request.member_name)
            playback_id = uuid4().hex
            record = SpeechPlayback(
                self.scope.session_id, self.scope.meeting_id, request.request_id,
                request.lease_token, request.source_message_id, request.member_name,
                playback_id, voice.provider_voice_id, voice.model, voice.catalog_version,
                self.config_revision, speech_chunks(request.speech_text),
            )
            self._records[playback_id] = record
            self._request_ids[request.request_id] = playback_id
        record = self._records[playback_id]
        self._current = playback_id
        await self._push("meeting.speech.ready", {
            **record.identity(), "voice": record.voice, "model": record.model,
            "catalog_version": record.catalog_version, "chunks": list(record.chunks),
            "chunk_count": len(record.chunks),
        })

    async def speech_error(self, request, error) -> None:
        await self._push("meeting.speech.error", {"error": error})

    def _record(self, params: dict) -> SpeechPlayback:
        record = self._records.get(params.get("playback_id"))
        if record is None or not record.matches(params):
            raise ValueError("Speech identity is stale or does not belong to this meeting")
        return record

    async def resolve_chunk(self, params: dict) -> dict:
        record = self._record(params)
        if self._closing or self._current != record.playback_id or record.playback_id in self._receipts or record.playback_id in self._interrupted:
            raise ValueError("Speech playback is no longer active")
        if not await self.driver.playback_is_current(self.scope, record.request_id, record.lease_token):
            raise ValueError("Speech lease is no longer active")
        index = params.get("chunk_index")
        if type(index) is not int or not 0 <= index < len(record.chunks):
            raise ValueError("Invalid speech chunk index")
        return {**record.identity(), "chunk_index": index, "text": record.chunks[index],
                "voice": record.voice, "model": record.model, "config_revision": record.config_revision}

    async def result(self, params: dict) -> dict:
        record = self._record(params)
        outcome, count = params.get("outcome"), params.get("completed_chunks")
        if outcome not in {"completed", "stopped", "failed"} or type(count) is not int or not 0 <= count <= len(record.chunks):
            raise ValueError("Invalid playback outcome")
        if outcome == "completed" and count != len(record.chunks):
            raise ValueError("All speech chunks must naturally finish before completion")
        async with self._result_lock:
            previous = self._receipts.get(record.playback_id)
            if previous is not None:
                if previous != (outcome, count):
                    raise ValueError("Conflicting playback result")
                return {"accepted": True}
            if self._closing or self._current != record.playback_id:
                raise ValueError("Playback result is no longer current")
            if outcome == "completed":
                accepted = await self.driver.complete_playback(self.scope, record.request_id, record.lease_token)
            else:
                accepted = await self.driver.fail_playback(
                    self.scope, record.request_id, record.lease_token, advance=outcome == "failed",
                )
            if not accepted:
                raise ValueError("Playback lease is no longer current")
            self._receipts[record.playback_id] = (outcome, count)
        return {"accepted": True}

    def confirm_stop(self, stop_id: str) -> dict:
        # Deliberately independent from driver/result locks: aclose holds the
        # driver lock while waiting for this receipt, including cleanup retries.
        if self._transport_lost:
            raise ValueError("Playback transport has been retired")
        speech_waiter = self._speech_stops.get(stop_id)
        if speech_waiter is not None:
            if not speech_waiter.done():
                speech_waiter.set_result(True)
            return {"accepted": True}
        if not self._stop_id or stop_id != self._stop_id or self._stop_waiter is None:
            raise ValueError("Unknown playback stop acknowledgement")
        if not self._stop_waiter.done():
            self._stop_waiter.set_result(True)
        return {"accepted": True}

    def abandon_transport(self) -> None:
        self._transport_lost = True
        self._closing = True
        self._current = None
        self._interrupted.update(self._records)
        # False retires waiters without fabricating a browser acknowledgement.
        for waiter in [self._stop_waiter, *self._speech_stops.values()]:
            if waiter is not None and not waiter.done():
                waiter.set_result(False)

    async def interrupt(self, interruption) -> None:
        playback_id = self._request_ids.get(interruption.request_id)
        if playback_id is None:
            # Publication failed before any audio was offered to the browser.
            return
        record = self._records[playback_id]
        if record.lease_token != interruption.lease_token or self._closing:
            raise ValueError("Speech interruption is no longer current")
        self._interrupted.add(playback_id)
        if self._current == playback_id:
            self._current = None
        waiter = self._speech_stops.setdefault(
            interruption.interruption_id, asyncio.get_running_loop().create_future(),
        )
        await self._push("meeting.speech.stop", {**record.identity(), "stop_id": interruption.interruption_id})
        try:
            if not await asyncio.wait_for(asyncio.shield(waiter), self._stop_timeout):
                raise RuntimeError("Meeting playback transport was lost")
        except TimeoutError:
            raise RuntimeError("Interrupted speech stop was not confirmed") from None

    async def stop(self) -> None:
        self._closing = True
        if self._transport_lost:
            return
        if self._stop_waiter is None:
            self._stop_id = uuid4().hex
            self._stop_waiter = asyncio.get_running_loop().create_future()
        if self._stop_waiter.done():
            return
        # Gateway drained matching synthesis before forwarding these receipts.
        receipt = self._receipts.get(self._current)
        if receipt is not None and receipt[0] in {"stopped", "failed"}:
            self._stop_waiter.set_result(True)
            return
        await self._push("meeting.playback.stop", {"stop_id": self._stop_id})
        try:
            await asyncio.wait_for(asyncio.shield(self._stop_waiter), self._stop_timeout)
        except TimeoutError:
            raise RuntimeError("Browser playback stop was not confirmed; retry End meeting") from None
