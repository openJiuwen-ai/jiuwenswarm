"""Gateway-owned media leases. Task ownership outlives these short-lived leases."""

from dataclasses import dataclass
import secrets
import time

from ..qwen_omni_gateway import QwenOmniRealtimeConfig
from ..openai_realtime_gateway import OpenAIRealtimeConfig
from ..task_tools import realtime_tools
from .gateway import serve_realtime_websocket

REALTIME_PROXY_PATH = "/ws/video/realtime"
PROVIDERS = {"qwen_omni": QwenOmniRealtimeConfig, "openai": OpenAIRealtimeConfig}


@dataclass
class MediaSession:
    id: str
    owner: str
    conversation: str
    provider: str
    config: object
    reply_language: str
    ticket: str
    expires_at: float
    active: bool = False


class MediaSessions:
    def __init__(self):
        self.sessions = {}

    def create(self, owner, conversation, provider, reply_language):
        self.prune()
        if provider not in PROVIDERS:
            raise ValueError("Unsupported realtime provider")
        if len(self.sessions) >= 512 or sum(s.owner == owner for s in self.sessions.values()) >= 32:
            raise ValueError("Too many realtime sessions")
        config = PROVIDERS[provider].from_environment()
        config.validate()
        session = MediaSession(
            secrets.token_urlsafe(24), owner, conversation, provider, config,
            reply_language, secrets.token_urlsafe(32), time.monotonic() + 120,
        )
        self.sessions[session.id] = session
        return session

    def prune(self):
        now = time.monotonic()
        for key, session in list(self.sessions.items()):
            if not session.active and session.expires_at <= now:
                self.sessions.pop(key, None)

    def claim(self, session_id, ticket):
        self.prune()
        session = self.sessions.get(session_id)
        if not session or session.active or not ticket or not secrets.compare_digest(session.ticket, ticket):
            raise ValueError("Realtime connection ticket is invalid or expired")
        session.ticket = ""
        session.active = True
        return session

    def require(self, session_id, owner, conversation):
        session = self.sessions.get(session_id)
        if not session or not session.active or (session.owner, session.conversation) != (owner, conversation):
            raise ValueError("Realtime session is unavailable to this conversation")
        return session

    def close(self, session_id):
        self.sessions.pop(session_id, None)

    @staticmethod
    def payload(session):
        # The upstream URL and credential are deliberately absent.
        tools = realtime_tools()
        if session.provider == "openai":
            tools = [{"type": "function", **tool} for tool in tools]
        else:
            tools = [{"type": "function", "function": tool} for tool in tools]
        return {
            "media_session_id": session.id, "provider": session.provider,
            "url": f"{REALTIME_PROXY_PATH}?session={session.id}&ticket={session.ticket}",
            "model": session.config.model, "voice": session.config.voice,
            "reply_language": session.reply_language, "tools": tools,
            "config_version": session.id,
        }


media_sessions = MediaSessions()


async def serve_bound_realtime_websocket(websocket):
    try:
        session = media_sessions.claim(
            websocket.query_params.get("session"), websocket.query_params.get("ticket"),
        )
    except ValueError:
        await websocket.close(code=1008, reason="Invalid realtime session")
        return
    try:
        # Existing gateway authentication issued a single-use capability over the
        # control connection. Do not infer identity from Origin or query owner.
        await serve_realtime_websocket(websocket, session.config, session.provider)
    finally:
        media_sessions.close(session.id)
