# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Validated deployment settings for the ADX transport."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AdxRouterConfig:
    ingress_url: str
    tenant: str
    template: str
    version: str
    token: str = field(repr=False)
    agent_type: str = "jiuwenswarm"
    service_port: int | None = None
    connect_timeout_seconds: float = 60.0
    max_connections: int = 128
    parameters: dict[str, str] = field(default_factory=dict, repr=False)

    def binding_id(self, user: str) -> str:
        """Keep control-plane and Ingress routing identities identical."""
        identity = json.dumps(
            [self.tenant, user, self.agent_type], ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        return "jiuwen-" + hashlib.sha256(identity).hexdigest()


def adx_router_selected(config: dict) -> bool:
    gateway = config.get("gateway") or {}
    client = gateway.get("agent_client") or {}
    return str(client.get("type") or "").strip().lower() == "adx_router"


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"gateway.adx.{name} must be a nonempty identifier")
    if len(value.encode("utf-8")) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"gateway.adx.{name} is invalid")
    return value


def _secret(env_name: object) -> str:
    if not isinstance(env_name, str) or not env_name.strip():
        raise ValueError("ADX credential environment variable name is required")
    value = os.environ.get(env_name, "")
    if not value or any(char.isspace() for char in value) or not value.isascii():
        raise ValueError(f"Missing or invalid ADX credential environment variable: {env_name}")
    return value


def load_router_config(config: dict) -> AdxRouterConfig:
    """Load secrets from environment references and reject unsafe settings."""
    settings = (config.get("gateway") or {}).get("adx") or {}
    if not isinstance(settings, dict):
        raise ValueError("gateway.adx must be a mapping")
    allowed_keys = {
        "ingress_url", "tenant", "template", "version", "token_env",
        "agent_type", "service_port", "connect_timeout_seconds", "max_connections",
        "parameters",
    }
    if settings.keys() - allowed_keys:
        raise ValueError("gateway.adx contains unknown settings")
    url = settings.get("ingress_url", "")
    if not isinstance(url, str) or any(ord(char) < 33 for char in url):
        raise ValueError("gateway.adx.ingress_url must be an HTTPS origin")
    if "?" in url or "#" in url:
        raise ValueError("gateway.adx.ingress_url must be an HTTPS origin")
    parsed = urlsplit(url)
    valid_location = parsed.scheme == "https" and bool(parsed.hostname)
    has_credentials = parsed.username is not None or parsed.password is not None
    has_suffix = bool(parsed.path not in ("", "/") or parsed.query or parsed.fragment)
    if not valid_location or has_credentials or has_suffix:
        raise ValueError("gateway.adx.ingress_url must be an HTTPS origin")
    # Accessing port also validates malformed/out-of-range authorities.
    parsed.port
    token = _secret(settings.get("token_env"))
    timeout = float(settings.get("connect_timeout_seconds", 60.0))
    if not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ValueError("gateway.adx.connect_timeout_seconds must be in (0, 86400]")
    capacity = settings.get("max_connections", 128)
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
        raise ValueError("gateway.adx.max_connections must be a positive integer")
    port = settings.get("service_port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("gateway.adx.service_port must be a valid TCP port")
    parameters = settings.get("parameters", {})
    if not isinstance(parameters, dict) or len(parameters) > 63:
        raise ValueError("gateway.adx.parameters must be a bounded string mapping")
    for key, value in parameters.items():
        valid_key = isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", key) is not None
        valid_value = isinstance(value, str) and "\0" not in value and len(value.encode("utf-8")) <= 8192
        if not valid_key or not valid_value:
            raise ValueError("gateway.adx.parameters contains an invalid entry")
    if "workspace" in parameters:
        raise ValueError("workspace is derived from the Jiuwen user identity")
    return AdxRouterConfig(
        ingress_url=url.rstrip("/"),
        tenant=_identifier(settings.get("tenant"), "tenant"),
        template=_identifier(settings.get("template"), "template"),
        version=_identifier(settings.get("version"), "version"),
        token=token,
        agent_type=_identifier(settings.get("agent_type", "jiuwenswarm"), "agent_type"),
        service_port=port,
        connect_timeout_seconds=timeout,
        max_connections=capacity,
        parameters=dict(parameters),
    )
