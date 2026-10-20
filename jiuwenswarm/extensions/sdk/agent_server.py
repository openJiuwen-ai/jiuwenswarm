"""AgentServer requests available to Gateway extensions."""

from typing import Any


def shares_gateway_directory(agent_client: Any) -> bool:
    from jiuwenswarm.gateway.routing.e2a_proxy import is_legacy_shared_directory_client

    return is_legacy_shared_directory_client(agent_client)


async def request_agent_unary(
    *,
    agent_client: Any,
    method: str,
    params: dict[str, Any] | None,
    channel_id: str,
    session_id: str | None = None,
    user_id: str | None = None,
    label: str = "extension",
) -> tuple[bool, dict[str, Any]]:
    from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary

    return await fetch_agent_unary(
        agent_client=agent_client,
        req_method=method,
        params=params,
        session_id=session_id,
        user_id=user_id,
        channel_id=channel_id,
        label=label,
    )
