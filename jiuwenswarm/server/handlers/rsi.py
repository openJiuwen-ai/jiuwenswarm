"""传输无关的 RSI 入口，保持配置、装配和执行异常的错误码语义。"""

import logging

from jiuwenswarm.agents.harness.common.rsi.errors import RsiError
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire

logger = logging.getLogger(__name__)


async def handle_rsi(ctx):
    try:
        config = get_config() or {}
        enabled = (config.get("rsi") or {}).get("enabled", False) is True
        method = ctx.request.req_method.value
        if method == "rsi.capabilities":
            result = {"ok": True, "payload": {
                "enabled": enabled, "scenarios": ["HARNESS", "ARTIFACT"],
                "artifact_types": ["PAPER", "PROGRAM"], "supports_pause": False,
                "supports_resume": True, "supports_terminate": True,
            }}
        elif not enabled:
            result = {"ok": False, "code": "RSI_DISABLED", "error": "RSI is disabled"}
        else:
            result = await ctx.services.get_rsi_handlers().handle_async(ctx.request)
    except RsiError as exc:
        result = {"ok": False, "code": exc.code, "error": exc.message}
    except Exception as exc:
        logger.exception("RSI 配置、装配或执行失败")
        result = {"ok": False, "code": "INTERNAL_ERROR", "error": str(exc)}
    response = AgentResponse(
        request_id=ctx.request_id, channel_id=ctx.channel_id, ok=result.get("ok", False),
        payload=result.get("payload") if result.get("ok") else {
            "code": result.get("code", "INTERNAL_ERROR"), "error": result.get("error", "RSI request failed"),
        },
    )
    await ctx.sink.send_wire(encode_agent_response_for_wire(response, response_id=ctx.request_id))
