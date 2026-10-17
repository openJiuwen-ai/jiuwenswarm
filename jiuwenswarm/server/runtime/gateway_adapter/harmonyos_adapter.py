"""HarmonyOSAdapter: TUI HarmonyOS DevEco bootstrap executed in AgentServer.

HarmonyOS project inspection and DevEco CLI / Skill bootstrap run in the
target AgentServer. The Gateway keeps the TUI entry protocol and long-operation
cancellation; it forwards these RPCs here so that project context
(``agent/workspace/harmonyos-projects``), skills and MCP state are written to
this process's injected ``.jiuwenswarm``.

``user_id`` carried by the E2A envelope is never used for directory selection.
"""

from __future__ import annotations

import logging

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)
from jiuwenswarm.server.runtime.harmonyos.harmonyos_dev import (
    run_harmonyos_dev_init,
    run_harmonyos_project_init,
)
from jiuwenswarm.server.runtime.harmonyos.harmonyos_project import (
    HarmonyOSProjectError,
)

logger = logging.getLogger(__name__)


class HarmonyOSAdapter(GatewayAdapter):
    """Adapter for the TUI ``harmonyos.*`` bootstrap methods."""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.HARMONYOS_PROJECT_INIT.value,
            ReqMethod.HARMONYOS_DEV_INIT.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        try:
            if request.req_method == ReqMethod.HARMONYOS_PROJECT_INIT:
                payload = await run_harmonyos_project_init(dict(params))
            elif request.req_method == ReqMethod.HARMONYOS_DEV_INIT:
                payload = await run_harmonyos_dev_init(dict(params))
            else:
                return build_error_response(
                    request, f"unsupported method: {request.req_method}", code="BAD_REQUEST"
                )
        except HarmonyOSProjectError as exc:
            return build_error_response(request, str(exc), code="BAD_REQUEST")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HarmonyOSAdapter] %s failed: %s", request.req_method, exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload if isinstance(payload, dict) else {"result": payload},
            metadata=request.metadata,
        )
