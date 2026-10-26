"""AgentServer-only helpers so CronSchedulerService can run without Gateway.

Used by relay-claw / ``app_agentserver`` sidecars that never start ``app_gateway``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, AsyncIterator, ClassVar, TypeVar, cast

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk

logger = logging.getLogger(__name__)

T = TypeVar("T")


class NopCronMessageHandler:
    """Drop channel pushes when MessageHandler / ChannelManager are not in-process."""

    @staticmethod
    async def publish_robot_messages(msg: Any) -> None:
        channel = getattr(msg, "channel_id", None)
        logger.debug(
            "[NopCronMessageHandler] skip publish channel=%s (no Gateway MessageHandler)",
            channel,
        )


class InProcessAgentServerClient:
    """Invoke ``TenantAgentPool.process_message`` without a WebSocket hop.

    Satisfies the ``send_request`` surface used by ``CronSchedulerService._on_wake``.
    """

    def __init__(self, agent_manager: Any | None = None) -> None:
        self._agent_manager = agent_manager

    def _resolve_pool(self) -> Any:
        if self._agent_manager is not None:
            return self._agent_manager
        try:
            from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

            server = AgentWebSocketServer.get_instance()
            pool = getattr(server, "_agent_manager", None)
            if pool is not None:
                return pool
        except Exception:
            logger.debug(
                "[InProcessAgentServerClient] resolve pool from AgentWebSocketServer failed",
                exc_info=True,
            )
        from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

        return TenantAgentPool.get_instance()

    @staticmethod
    async def connect(uri: str) -> None:
        return None

    @staticmethod
    async def disconnect() -> None:
        return None

    @staticmethod
    def set_or_update_server_config(
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        return None

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        request = e2a_to_agent_request(envelope)
        pool = self._resolve_pool()
        return await pool.process_message(request)

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        request = e2a_to_agent_request(envelope)
        request.is_stream = True
        pool = self._resolve_pool()
        async for chunk in pool.process_message_stream(request):
            yield chunk


def resolve_agent_side_cron_deps(
    *,
    agent_client: Any | None = None,
    message_handler: Any | None = None,
) -> tuple[Any, Any]:
    """Resolve wake client + push handler for Agent-side CronSchedulerService."""
    client = agent_client
    if client is None:
        client = InProcessAgentServerClient()
        logger.info("[CronLocal] using InProcessAgentServerClient for Agent-side scheduler")

    handler = message_handler
    if handler is None:
        try:
            from jiuwenswarm.gateway.message_handler import MessageHandler

            handler = MessageHandler.get_instance()
        except Exception:
            handler = None
    if handler is None:
        handler = NopCronMessageHandler()
        logger.info("[CronLocal] using NopCronMessageHandler (no Gateway MessageHandler)")

    return client, handler


class AgentCronRegistry:
    """Process-level registry of per-tenant Agent-side ``CronTools`` (+ scheduler)."""

    _lock = threading.Lock()
    _tools: ClassVar[dict[tuple[str, str], Any]] = {}

    @staticmethod
    def _key(service_id: str, agent_id: str) -> tuple[str, str]:
        return (
            (str(service_id or "default").strip() or "default"),
            (str(agent_id or "default").strip() or "default"),
        )

    @classmethod
    def get_or_create(
        cls,
        service_id: str,
        agent_id: str,
        *,
        factory: Callable[[], T],
    ) -> T:
        """Return shared CronTools for ``(service_id, agent_id)``, creating once."""
        key = cls._key(service_id, agent_id)
        with cls._lock:
            existing = cls._tools.get(key)
            if existing is not None:
                return cast(T, existing)
            tools = factory()
            cls._tools[key] = tools
            return tools

    @classmethod
    def register(cls, service_id: str, agent_id: str, tools: Any) -> None:
        """Idempotent put (e.g. after scheduler restart on an existing instance)."""
        key = cls._key(service_id, agent_id)
        with cls._lock:
            cls._tools[key] = tools

    @classmethod
    def is_current(cls, service_id: str, agent_id: str, tools: Any) -> bool:
        """True iff ``tools`` is the live registry entry for the tenant."""
        key = cls._key(service_id, agent_id)
        with cls._lock:
            return cls._tools.get(key) is tools

    @classmethod
    async def remove(cls, service_id: str, agent_id: str) -> bool:
        """Stop Agent-side scheduler for the tenant and drop the registry entry."""
        key = cls._key(service_id, agent_id)
        with cls._lock:
            tools = cls._tools.pop(key, None)
        if tools is None:
            return False
        stop = getattr(tools, "stop_scheduler", None)
        if stop is not None:
            try:
                await stop()
            except Exception:
                logger.warning(
                    "[AgentCronRegistry] stop_scheduler failed tenant=(%s, %s)",
                    key[0],
                    key[1],
                    exc_info=True,
                )
        logger.info(
            "[AgentCronRegistry] removed tenant cron tools service_id=%s agent_id=%s",
            key[0],
            key[1],
        )
        return True

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._tools.clear()
