"""Gateway-owned browser routes and provider-independent authorized speech."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import weakref
from uuid import uuid4

from agent_gateway.common.protocol_ids import is_valid_session_id
from agent_gateway.routing.e2a_proxy import fetch_agent_unary

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import mask_sensitive
from jiuwenswarm.common.voice_matching import load_voice_catalog
from jiuwenswarm.common.voice.config import TtsConfig
from jiuwenswarm.common.voice.tts import TtsSession

from .playback import IDENTITY_FIELDS

_routes = weakref.WeakValueDictionary()
logger = logging.getLogger(__name__)


def config_revision(config: TtsConfig) -> str:
    return hashlib.sha256(json.dumps([config.provider, config.upstream_url, config.api_key, config.model, config.voice]).encode()).hexdigest()


class GatewayPlaybackRoute:
    def __init__(self, channel, ws, agent_client, session_id, meeting_id, owner_id, user_id, config, startup_id):
        self.id = uuid4().hex
        self.channel, self.ws, self.agent_client = channel, ws, agent_client
        self.session_id, self.meeting_id = session_id, meeting_id
        self.owner_id, self.user_id, self.config = owner_id, user_id, config
        self.startup_id = startup_id
        self.revision = config_revision(config)
        self.revoked = False
        self.records = {}
        self.produced = {}
        self.receipts = {}
        self.pending_receipts = {}
        self.drained_playbacks: set[str] = set()
        self.current_playback = None
        self.result_lock = asyncio.Lock()
        self._media_lock = asyncio.Lock()
        self.tasks: dict[tuple, asyncio.Task] = {}
        self.tts = TtsSession(config)
        self.stops: dict[str, asyncio.Task] = {}
        self.stop_receipts: dict[str, dict] = {}
        self.shutdown_stop_id: str | None = None
        self.interrupted: set[str] = set()
        _routes[self.id] = self

    async def fetch(self, method, params):
        ok, payload = await fetch_agent_unary(
            agent_client=self.agent_client(), req_method=method,
            params={**params, "_meeting_owner_id": self.owner_id},
            session_id=self.session_id, user_id=self.user_id,
            channel_id=self.channel.channel_id, label="meeting.playback", timeout_seconds=15,
        )
        if not ok:
            raise ValueError(payload.get("error") or "Meeting playback request failed")
        return payload

    def identity(self, params):
        record = self.records.get(params.get("playback_id"))
        if record is None or any(key not in params or params[key] != record.get(key) for key in IDENTITY_FIELDS):
            raise ValueError("Unknown or stale meeting speech identity")
        return {key: record[key] for key in IDENTITY_FIELDS}

    async def cancel_synthesis(self):
        async with self._media_lock:
            pending = list(self.tasks.items())
            tasks = [task for _, task in pending]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for key, task in pending:
                if self.tasks.get(key) is task:
                    self.tasks.pop(key, None)
            await self.tts.aclose()

    async def interrupt_synthesis(self):
        await self.cancel_synthesis()
        if not self.revoked:
            # The next speaker gets a fresh session after cancellation is drained.
            self.tts = TtsSession(self.config)

    async def stop_media(self):
        self.revoked = True
        await self.cancel_synthesis()

    async def push(self, payload):
        if (payload.get("owner_id"), payload.get("session_id"), payload.get("meeting_id")) != (
            self.owner_id, self.session_id, self.meeting_id,
        ):
            raise ValueError("Foreign meeting playback push")
        event = payload.get("event")
        if event == "meeting.speech.ready":
            if self.revoked:
                return
            record = {key: value for key, value in payload.items() if key not in {"event_type", "event", "route_id", "owner_id"}}
            previous = self.records.get(record.get("playback_id"))
            if previous is not None and previous != record:
                raise ValueError("Conflicting speech delivery")
            self.records[record["playback_id"]] = record
            self.current_playback = record["playback_id"]
            self.produced.setdefault(record["playback_id"], set())
        elif event == "meeting.playback.stop":
            self.revoked = True
            stop_id = payload.get("stop_id")
            if not isinstance(stop_id, str) or not stop_id:
                raise ValueError("Invalid playback stop identity")
            self.shutdown_stop_id = stop_id
            if stop_id not in self.stops:
                self.stops[stop_id] = asyncio.create_task(self.stop_media())
        elif event == "meeting.speech.stop":
            identity = self.identity(payload)
            stop_id = payload.get("stop_id")
            if not isinstance(stop_id, str) or not stop_id:
                raise ValueError("Invalid speech interruption identity")
            self.interrupted.add(identity["playback_id"])
            if stop_id not in self.stops:
                self.stops[stop_id] = asyncio.create_task(self.interrupt_synthesis())
        elif event == "meeting.addressed_members":
            if self.revoked:
                return
            names = payload.get("addressed_members")
            if not isinstance(names, list) or len(names) > 100 or any(
                not isinstance(name, str) or not name.strip() or len(name) > 255
                for name in names
            ):
                raise ValueError("Invalid meeting addressed members")
        elif event != "meeting.speech.error":
            raise ValueError("Unknown meeting playback event")
        await self.channel.send_event(self.ws, event, {
            key: value for key, value in payload.items() if key not in {"event_type", "event", "route_id", "owner_id"}
        })


async def handle_playback_push(payload: dict) -> None:
    route = _routes.get(payload.get("route_id"))
    if route is not None:
        await route.push(payload)


async def forget_playback_routes(ws, session_id: str, meeting_id: str | None = None, *, startup_id=None):
    routes = getattr(ws, "_meeting_playback_routes", {})
    for key, route in list(routes.items()):
        if (
            key[0] == session_id and (meeting_id is None or key[1] == meeting_id)
            and (startup_id is None or route.startup_id == startup_id)
        ):
            await route.stop_media()
            routes.pop(key, None)
            _routes.pop(route.id, None)


def _route_for(ws, params, session_id):
    target = str(params.get("session_id") or session_id or "")
    route = getattr(ws, "_meeting_playback_routes", {}).get((target, params.get("meeting_id")))
    if route is None or route.owner_id != getattr(ws, "_meeting_owner_id", None):
        raise ValueError("Meeting playback is not owned by this connection")
    return route


async def playback_ready(channel, agent_client, ws, req_id, params, session_id, user_id=None):
    route = None
    created = False
    attachment_requested = False
    try:
        params = params if isinstance(params, dict) else {}
        target = str(params.get("session_id") or session_id or "")
        meeting_id, startup_id = params.get("meeting_id"), params.get("startup_id")
        owner = getattr(ws, "_meeting_owner_id", None)
        if (
            not is_valid_session_id(target) or target == "new" or not isinstance(meeting_id, str) or not meeting_id
            or not isinstance(startup_id, str) or not startup_id or not owner
            or (target, startup_id) not in getattr(ws, "_meeting_owned_sessions", {})
        ):
            raise ValueError("A matching owned meeting startup is required")
        config = TtsConfig.from_environment()
        config.validate()
        load_voice_catalog(config.model).compatible(config.model)
        routes = getattr(ws, "_meeting_playback_routes", None)
        if routes is None:
            routes = {}
            setattr(ws, "_meeting_playback_routes", routes)
        route = routes.get((target, meeting_id))
        if route is not None and route.revoked and (
            route.shutdown_stop_id in route.stop_receipts
            or any(outcome == "stopped" for outcome, _ in route.receipts.values())
        ):
            await forget_playback_routes(ws, target, meeting_id, startup_id=route.startup_id)
            route = None
        if route is None:
            route = GatewayPlaybackRoute(channel, ws, agent_client, target, meeting_id, owner, user_id, config, startup_id)
            routes[(target, meeting_id)] = route
            created = True
        if route.revoked or route.startup_id != startup_id or route.revision != config_revision(config):
            raise ValueError("Meeting playback configuration changed or has stopped")
        # Prepare the meeting-owned provider session before grants.
        await route.tts.start()
        if route.revoked or getattr(ws, "_meeting_disconnected", False):
            raise ValueError("Meeting playback transport has stopped")
        attachment_requested = True
        payload = await route.fetch(ReqMethod.MEETING_PLAYBACK_READY, {
            "meeting_id": meeting_id, "startup_id": startup_id,
            "_meeting_route_id": route.id, "_tts_model": config.model,
            "_tts_revision": route.revision, "_tts_default_voice": config.voice,
        })
        await channel.send_response(ws, req_id, ok=True, payload=payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:

        message = mask_sensitive(str(exc).strip() or type(exc).__name__)
        logger.exception(
            "Meeting playback preparation failed: session_id=%s meeting_id=%s startup_id=%s request_id=%s route_created=%s error=%s",
            target, meeting_id, startup_id, req_id, created, message,
        )
        if created:
            await route.stop_media()
            # A lost ready response may still have installed this consumer.
            # Retain its route identity until release confirms server cleanup.
            if not attachment_requested:
                await forget_playback_routes(ws, target, meeting_id, startup_id=startup_id)
        await channel.send_response(ws, req_id, ok=False, error=message, code="MEETING_PLAYBACK_UNAVAILABLE")


async def synthesize_speech(channel, ws, req_id, params, session_id, user_id=None):
    task = None
    key = None
    route = None
    try:
        params = params if isinstance(params, dict) else {}
        if any(key in params for key in ("text", "voice", "model", "provider_voice_id", "config_revision")):
            raise ValueError("Meeting TTS text and voice are resolved by the server")
        route = _route_for(ws, params, session_id)
        identity = route.identity(params)
        index = params.get("chunk_index")
        if type(index) is not int:
            raise ValueError("Invalid speech chunk index")
        key = (identity["playback_id"], index)
        if (
            route.revoked or route.tasks or identity["playback_id"] in route.interrupted
            or identity["playback_id"] in route.pending_receipts or identity["playback_id"] in route.receipts
        ):
            raise ValueError("Meeting synthesis has stopped or is already active")
        chunk = await route.fetch(ReqMethod.MEETING_SPEECH_GET, {**identity, "chunk_index": index})
        config = TtsConfig.from_environment()
        if (
            route.revoked or route.tasks or identity["playback_id"] in route.interrupted or config_revision(config) != route.revision
            or identity["playback_id"] in route.pending_receipts or identity["playback_id"] in route.receipts
            or chunk.get("config_revision") != route.revision or chunk.get("model") != config.model
        ):
            raise ValueError("Meeting TTS configuration changed")
        task = asyncio.create_task(route.tts.synthesize(chunk["text"], chunk["voice"]))
        route.tasks[key] = task
        audio = await task
        await route.fetch(ReqMethod.MEETING_SPEECH_GET, {**identity, "chunk_index": index})
        if (
            route.revoked or identity["playback_id"] in route.interrupted
            or identity["playback_id"] in route.pending_receipts or identity["playback_id"] in route.receipts
            or config_revision(TtsConfig.from_environment()) != route.revision
        ):
            raise ValueError("Meeting playback stopped or its configuration changed")
        route.produced[identity["playback_id"]].add(index)
        await channel.send_response(ws, req_id, ok=True, payload={
            "success": True, "playback_id": identity["playback_id"], "chunk_index": index,
            "audio_base64": base64.b64encode(audio.audio).decode(), "audio_mime": audio.audio_mime,
        })
    except asyncio.CancelledError:
        await channel.send_response(ws, req_id, ok=False, error="Meeting synthesis cancelled", code="CANCELLED")
    except Exception as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code=getattr(exc, "code", "MEETING_TTS_FAILED"))
    finally:
        if route is not None and route.tasks.get(key) is task:
            route.tasks.pop(key, None)


async def playback_result(channel, ws, req_id, params, session_id, user_id=None):
    try:
        params = params if isinstance(params, dict) else {}
        route = _route_for(ws, params, session_id)
        identity = route.identity(params)
        async with route.result_lock:
            if identity["playback_id"] in route.interrupted:
                raise ValueError("Interrupted speech cannot be reported as played")
            outcome, count = params.get("outcome"), params.get("completed_chunks")
            record = route.records[identity["playback_id"]]
            if outcome not in {"completed", "stopped", "failed"} or type(count) is not int or not 0 <= count <= record["chunk_count"]:
                raise ValueError("Invalid playback outcome")
            if outcome == "completed" and count != record["chunk_count"]:
                raise ValueError("All chunks must finish before completion")
            previous = route.receipts.get(identity["playback_id"], route.pending_receipts.get(identity["playback_id"]))
            if previous is not None and previous != (outcome, count):
                raise ValueError("Conflicting playback receipt")
            if previous is None and (route.revoked or route.current_playback != identity["playback_id"]):
                raise ValueError("Speech is no longer current")
            if outcome in {"failed", "stopped"}:
                route.pending_receipts[identity["playback_id"]] = (outcome, count)
                if identity["playback_id"] not in route.drained_playbacks:
                    if outcome == "stopped":
                        route.revoked = True
                        await route.cancel_synthesis()
                    else:
                        # A failed reading must not poison later speakers.
                        # Drain first; then allow the server to release its lease.
                        await route.interrupt_synthesis()
                    route.drained_playbacks.add(identity["playback_id"])
            elif outcome == "completed":
                record = route.records[identity["playback_id"]]
                if len(route.produced[identity["playback_id"]]) != record["chunk_count"]:
                    raise ValueError("Speech chunks have not all been synthesized")
            route.pending_receipts[identity["playback_id"]] = (outcome, count)
            payload = await route.fetch(ReqMethod.MEETING_PLAYBACK_RESULT, {
                **identity, "outcome": outcome, "completed_chunks": count,
            })
            route.receipts[identity["playback_id"]] = (outcome, count)
            route.pending_receipts.pop(identity["playback_id"], None)
        await channel.send_response(ws, req_id, ok=True, payload=payload)
    except (ValueError, RuntimeError) as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MEETING_PLAYBACK_RESULT_FAILED")


async def playback_stopped(channel, ws, req_id, params, session_id, user_id=None):
    try:
        params = params if isinstance(params, dict) else {}
        route = _route_for(ws, params, session_id)
        stop_id = params.get("stop_id")
        if stop_id not in route.stops:
            raise ValueError("Unknown meeting playback stop")
        if stop_id in route.stop_receipts:
            await channel.send_response(ws, req_id, ok=True, payload=route.stop_receipts[stop_id])
            return
        await asyncio.shield(route.stops[stop_id])
        payload = await route.fetch(ReqMethod.MEETING_PLAYBACK_STOPPED, {
            "meeting_id": route.meeting_id, "stop_id": stop_id,
        })
        route.stop_receipts[stop_id] = payload
        await channel.send_response(ws, req_id, ok=True, payload=payload)
    except (ValueError, RuntimeError) as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="MEETING_PLAYBACK_STOP_FAILED")


async def close_browser_playback(ws, _sessions=None):
    for route in list(getattr(ws, "_meeting_playback_routes", {}).values()):
        await route.stop_media()


async def drain_lost_playback(ws, session_id, startup_id):
    if not getattr(ws, "_meeting_disconnected", False):
        raise ValueError("Playback transport is still connected")
    routes = [
        route for route in getattr(ws, "_meeting_playback_routes", {}).values()
        if route.session_id == session_id and route.startup_id == startup_id
    ]
    if not routes:
        return None
    if len(routes) != 1 or routes[0].owner_id != getattr(ws, "_meeting_owner_id", None):
        raise ValueError("Disconnected playback ownership is ambiguous")
    route = routes[0]
    route.revoked = True
    async with route.result_lock:
        await route.stop_media()
        if route.stops:
            await asyncio.gather(*route.stops.values())
    return {"meeting_id": route.meeting_id, "route_id": route.id}
