# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm Connector Hub extension entry.

Registers the ``openjiuwen-hub`` Provider factory into ExtensionRegistry.
Process ``ConnectorSourceManager`` binds ``default_source_config()`` on first
``get_connector_source_manager()`` (same idea as skill hub builtins).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from jiuwenswarm.extensions.sdk.connector_source import (
    ConnectorSourceConfig,
    ConnectorSourceExtension,
    ConnectorSourceProvider,
)
from jiuwenswarm.server.runtime.marketplace.hub_client import DEFAULT_HUB_BASE_URL

from .constants import OPENJIUWEN_HUB_PROVIDER_TYPE, OPENJIUWEN_HUB_SOURCE_ID
from .hub_verify import HubTrustPolicy
from .provider import SwarmConnectorHubProvider

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_ENV_REF_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_ALLOWED_DOWNLOAD_HOSTS: tuple[str, ...] = (
    "openjiuwen-market.obs.*.myhuaweicloud.com",
    "127.0.0.1",
    "localhost",
)
_DEFAULTS: dict[str, Any] = {
    "endpoint": DEFAULT_HUB_BASE_URL,
    "timeout": _DEFAULT_TIMEOUT,
    "verification": "off",
    "hmac_key_id": "default",
    "hmac_secret_ref": "env://SKILL_DOWNLOAD_HMAC_SECRET",
    "allowed_download_hosts": list(_DEFAULT_ALLOWED_DOWNLOAD_HOSTS),
}


def _load_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        logger.warning(
            "读取 swarm_connector_hub config.yaml 失败，使用内置默认配置",
            exc_info=True,
        )
        return {}


def _config_value(key: str) -> Any:
    cfg = _load_config()
    return cfg[key] if key in cfg else _DEFAULTS.get(key)


def _resolve_endpoint_ref(reference: str | None) -> str | None:
    value = str(reference or "").strip()
    if not value.startswith("env://"):
        return None
    variable = value[6:]
    if not _ENV_REF_RE.fullmatch(variable):
        return None
    resolved = str(os.getenv(variable) or "").strip()
    return resolved or None


def trust_policy_from_config() -> HubTrustPolicy:
    key_id = str(_config_value("hmac_key_id") or "default").strip() or "default"
    secret_ref = str(
        _config_value("hmac_secret_ref") or "env://SKILL_DOWNLOAD_HMAC_SECRET"
    ).strip()
    verification = str(_config_value("verification") or "off").strip().lower()
    return HubTrustPolicy(
        verification=verification,
        hmac_key_id=key_id,
        hmac_secret_ref=secret_ref,
    )


def default_download_allowed_hosts(endpoint: str) -> tuple[str, ...]:
    hosts: list[str] = []
    endpoint_host = (urlparse(str(endpoint or "").strip()).hostname or "").strip().lower()
    if endpoint_host:
        hosts.append(endpoint_host)
    configured = _config_value("allowed_download_hosts")
    if isinstance(configured, (list, tuple)):
        extra = [str(h).strip().lower() for h in configured if str(h).strip()]
    else:
        extra = list(_DEFAULT_ALLOWED_DOWNLOAD_HOSTS)
    for host in extra:
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def default_source_config_from_package() -> ConnectorSourceConfig:
    endpoint = str(_config_value("endpoint") or DEFAULT_HUB_BASE_URL).strip()
    return ConnectorSourceConfig(
        source_id=OPENJIUWEN_HUB_SOURCE_ID,
        provider_type=OPENJIUWEN_HUB_PROVIDER_TYPE,
        enabled=True,
        priority=100,
        endpoint_ref=endpoint,
        capabilities=frozenset({"search", "get_artifact", "get_detail", "check_updates"}),
    )


class SwarmConnectorHubSourceExtension(ConnectorSourceExtension):
    """Factory creating OpenJiuwen Hub MCP catalogue Provider instances."""

    provider_type = OPENJIUWEN_HUB_PROVIDER_TYPE

    def default_source_config(self) -> ConnectorSourceConfig | None:
        return default_source_config_from_package()

    async def initialize(self, config) -> None:
        del config
        return None

    async def shutdown(self) -> None:
        return None

    def create_provider(self, config: ConnectorSourceConfig) -> ConnectorSourceProvider:
        endpoint = _resolve_endpoint_ref(config.endpoint_ref)
        if not endpoint:
            endpoint = str(config.endpoint_ref or "").strip() or DEFAULT_HUB_BASE_URL
        try:
            timeout = float(_config_value("timeout") or _DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            logger.warning("Invalid timeout; using %.1f", _DEFAULT_TIMEOUT)
            timeout = _DEFAULT_TIMEOUT

        from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient

        # Only this Connector path forwards SourceConfig.verify_ssl into Hub TLS
        # verification. Other HubClient callers keep the transport default (True).
        hub = HubClient(
            base_url=endpoint,
            timeout=timeout,
            verify=config.verify_ssl,
        )
        display_name = str(config.options.get("display_name") or "").strip() or None
        return SwarmConnectorHubProvider(
            source_id=config.source_id or None,
            display_name=display_name,
            hub_port=hub,
            verify_ssl=config.verify_ssl,
            download_allowed_hosts=default_download_allowed_hosts(endpoint),
            trust_policy=trust_policy_from_config(),
        )


async def register_extensions(registry):
    extension = SwarmConnectorHubSourceExtension()
    registry.register_connector_source_extension(extension)
    return [extension]
