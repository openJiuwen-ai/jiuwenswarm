"""Select the Manager-owned Keycloak registration for a single Agent."""

from __future__ import annotations

from typing import Any

MANAGED_CLIENTS = "managed_agent_clients"


def select_agent_credentials(template: dict[str, Any], identity: dict[str, str]) -> dict[str, str] | None:
    """Match the Agent and both template IDs against the Manager's dispatch data."""
    agent_id = identity.get("agent_id")
    config_id = identity.get("agent_template_id")
    runtime_id = identity.get("runtime_template_id")
    if not agent_id or not config_id or not runtime_id:
        return None
    if config_id != template.get("template_id"):
        return None

    data = template.get("data")
    clients = data.get(MANAGED_CLIENTS) if isinstance(data, dict) else None
    client = clients.get(agent_id) if isinstance(clients, dict) else None
    if not isinstance(client, dict):
        return None
    if client.get("client_id") != agent_id or client.get("runtime_template_id") != runtime_id:
        return None

    secret = client.get("client_secret")
    if not isinstance(secret, str) or not secret.strip():
        return None
    return {"client_id": agent_id, "client_secret": secret}
