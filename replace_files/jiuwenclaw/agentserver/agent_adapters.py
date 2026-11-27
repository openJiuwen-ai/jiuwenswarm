# Copyright (c) Huawei Technologies, Co., Ltd. 2025. All rights reserved.

"""Unified adapter protocol for JiuWenClaw SDK backends.

Defines the minimal interface every SDK adapter must implement so that
the Facade (interface.py) can drive any backend without knowing its
internal structure.
"""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from jiuwenclaw.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
logger = logging.getLogger(__name__)

_SDK_ENV_VAR = "JIUWENCLAW_AGENT_SDK"
_DEFAULT_SDK = "harness"


@runtime_checkable
class AgentAdapter(Protocol):
    """Minimal capability set every SDK adapter must satisfy.

    The Facade (JiuWenClaw) depends only on this interface; individual
    adapter modules implement it without any coupling to each other.
    """

    async def create_instance(self, config: dict[str, Any] | None = None, *, mode: str = "claw") -> None:
        """Initialise the underlying SDK agent from config.

        Called once on startup and again after skill install/uninstall.
        """
        ...

    async def reload_agent_config(
        self,
        config_base: dict[str, Any] | None = None,
        env_overrides: dict[str, Any] | None = None,
    ) -> None:
        """Hot-reload configuration without restarting the process.

        Args:
            config_base: Optional complete config snapshot; if provided, use it instead of reading local config.yaml.
            env_overrides: Optional environment variable overrides; only override keys present in the request.
        """
        ...

    async def process_message_impl(
            self, request: AgentRequest, inputs: dict[str, Any]
    ) -> AgentResponse:
        """Execute a single non-streaming request and return the response.

        Args:
            request: AgentRequest object.
            inputs: Pre-built inputs dict with conversation_id and query.
        """
        ...

    async def process_message_stream_impl(
            self, request: AgentRequest, inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        """Execute a streaming request; yield response chunks.

        Args:
            request: AgentRequest object.
            inputs: Pre-built inputs dict with conversation_id and query.
        """
        ...

    async def process_interrupt(self, request: AgentRequest) -> AgentResponse:
        """Handle interrupt requests (pause/resume/cancel/supplement)."""
        ...

    async def handle_user_answer(self, request: AgentRequest) -> AgentResponse:
        """Handle user answer for evolution approval or permission approval."""
        ...

    async def handle_heartbeat(self, request: AgentRequest) -> AgentResponse:
        """Handle heartbeat requests."""

    def is_working(self, session_tasks: dict, session_queues: dict) -> bool:
        """Return whether the agent is currently working.

        Args:
            session_tasks: Dict of session_id -> asyncio.Task
            session_queues: Dict of session_id -> asyncio.PriorityQueue

        Returns:
            bool: True if agent is working, False otherwise.
        """
        ...


def resolve_sdk_choice() -> str:
    """Resolve SDK choice from environment variable.

    Returns:
        SDK name: 'harness', or 'pi' (reserved).

    Behavior:
        - If env var is unset or empty: return 'harness' (default).
        - If env var is 'pi': return 'pi' (not yet implemented).
        - If env var is unknown: log warning and fallback to 'harness'.
    """
    raw = os.getenv(_SDK_ENV_VAR, "").strip().lower()
    if not raw:
        logger.debug("[SDK] %s not set, using default: %s", _SDK_ENV_VAR, _DEFAULT_SDK)
        return _DEFAULT_SDK

    valid_sdks = {"harness", "pi", "skilldev"}
    if raw in valid_sdks:
        logger.info("[SDK] Resolved SDK: %s", raw)
        return raw

    logger.warning(
        "[SDK] Unknown SDK value '%s', fallback to %s",
        raw,
        _DEFAULT_SDK,
    )
    return _DEFAULT_SDK


async def create_adapter(
    sdk: str | None = None,
    workspace_dir: str | None = None,
    agent_id: str | None = None,
    service_id: str | None = None,
) -> AgentAdapter:
    """Factory function to create SDK adapter instance.

    Args:
        sdk: SDK name, if None will resolve from environment.
        workspace_dir: Workspace directory path to pass to adapter.
        agent_id: Agent ID for multi-tenant isolation.
        service_id: Service ID for multi-tenant isolation.

    Returns:
        AgentAdapter instance for the specified SDK.

    Raises:
        NotImplementedError: If SDK is 'pi' (not yet implemented).
        RuntimeError: If SDK is unknown.
    """
    sdk_name = sdk or resolve_sdk_choice()

    if sdk_name == "harness":
        import asyncio

        def import_and_create():
            from jiuwenclaw.agentserver.deep_agent.interface_deep import JiuWenClawDeepAdapter
            return JiuWenClawDeepAdapter(
                workspace_dir=workspace_dir,
                agent_id=agent_id,
                service_id=service_id,
            )

        return await asyncio.get_event_loop().run_in_executor(None, import_and_create)

    if sdk_name == "skilldev":
        import asyncio

        def import_and_create():
            from jiuwenclaw.agentserver.skilldev_agent.adapter import SkillDevDeepAdapter
            return SkillDevDeepAdapter(
                workspace_dir=workspace_dir,
                agent_id=agent_id,
                service_id=service_id,
            )

        return await asyncio.get_event_loop().run_in_executor(None, import_and_create)

    if sdk_name == "pi":
        raise NotImplementedError(
            f"SDK '{sdk_name}' is not yet implemented. "
            f"Currently supported: harness, skilldev"
        )

    raise RuntimeError(
        f"Unknown SDK '{sdk_name}'. Supported: harness, skilldev, pi (reserved)"
    )
