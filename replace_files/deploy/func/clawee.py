import asyncio
import json
from dataclasses import asdict
from typing import Any

from jiuwenclaw.schema import AgentRequest, AgentResponseChunk, AgentResponse
from jiuwenclaw.schema.message import ReqMethod
from jiuwenclaw.utils import logger
from jiuwenclaw.agentserver.tenant_agent_pool import TenantAgentPool


def payload_to_request(request: dict[str, Any]) -> AgentRequest:
    req_method = request.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)

    return AgentRequest(
        request_id=request.get("request_id"),
        channel_id=request.get("channel_id", ""),
        session_id=request.get("session_id"),
        req_method=req_method,
        params=request.get("params", {}),
        is_stream=request.get("is_stream", False),
        timestamp=request.get("timestamp", 0.0),
        metadata=request.get("metadata"),
        service_id=request.get("service_id"),
        agent_id=request.get("agent_id"),
    )


def to_json(msg: Any) -> str:
    if msg:
        return json.dumps(asdict(msg), ensure_ascii=False)
    return ""


def chunk_to_payload(chunk: AgentResponseChunk) -> str:
    return to_json(chunk)


def response_to_payload(resp: AgentResponse) -> str:
    return to_json(resp)


def init(context):
    try:
        TenantAgentPool.get_instance()
    except Exception as e:
        logger.exception("Failed to initialize TenantAgentPool")


async def ahandler(event, context=None):
    try:
        request = payload_to_request(event)
    except Exception as e:
        logger.exception("Failed to parse event")
        return None

    pool = TenantAgentPool.get_instance()

    try:
        if request.is_stream:
            async for chunk in pool.process_message_stream(request):
                payload = chunk_to_payload(chunk)
                if context is not None:
                    context.get_stream().write(payload)
        else:
            resp = await pool.process_message(request)
            return response_to_payload(resp)

    except Exception as e:
        logger.exception("Error during message processing")
        return to_json(AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": str(e)},
        ))
    return None


def handler(event, context=None):
    return asyncio.run(ahandler(event, context))


def pre_stop():
    pass