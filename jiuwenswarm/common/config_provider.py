"""Config source SPI with the existing YAML and enterprise paths as defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from jiuwenswarm.edition import is_enterprise

logger = logging.getLogger(__name__)


class ConfigSource(str, Enum):
    YAML = "yaml"
    ENTERPRISE = "enterprise"


def resolve_config_source() -> ConfigSource:
    env_raw = os.getenv("JIUWENSWARM_CONFIG_SOURCE")
    yaml_raw = None
    raw = env_raw
    if raw is not None and not str(raw).strip():
        raw = None
    if raw is None:
        try:
            from jiuwenswarm.common.config import get_config

            config = get_config()
            yaml_raw = (
                (config.get("config") or {}).get("source")
                if isinstance(config, dict)
                else None
            )
            raw = yaml_raw
        except Exception:  # noqa: BLE001 - invalid config must fall back to edition default
            raw = None
    chosen = ConfigSource.ENTERPRISE if is_enterprise() else ConfigSource.YAML
    if raw is not None:
        value = str(raw).strip().lower()
        if value == ConfigSource.ENTERPRISE.value:
            if is_enterprise():
                chosen = ConfigSource.ENTERPRISE
            else:
                chosen = ConfigSource.YAML
                logger.warning(
                    "enterprise config source requested in community edition; using yaml"
                )
        elif value == ConfigSource.YAML.value:
            chosen = ConfigSource.YAML
        else:
            logger.warning("unknown config source %r; using edition default", raw)
    return chosen


@dataclass(frozen=True)
class AgentConfigContext:
    service_id: str | None = None
    agent_id: str | None = None
    workspace_key: str | None = None
    request: Any = None
    routing: dict | None = None
    cached_policy: Any = None


@dataclass
class AgentConfigResult:
    config: dict
    policy: Any | None = None


@runtime_checkable
class ConfigProvider(Protocol):
    name: str

    def get_process_config(self) -> dict | None:
        return None

    async def load_process_config(self) -> dict | None:
        return None

    async def load_agent_config(
        self, ctx: AgentConfigContext, *, base: dict
    ) -> AgentConfigResult | None:
        return None

    async def refresh(self) -> None:
        return None


_provider: ConfigProvider | None = None


def register_config_provider(provider: ConfigProvider) -> None:
    global _provider
    if _provider is not None:
        logger.warning("Replacing registered config provider")
    _provider = provider


def get_config_provider() -> ConfigProvider | None:
    return _provider


def reset_config_provider() -> None:
    global _provider
    _provider = None


def apply_enterprise_mcp(base: dict, loaded: Any) -> tuple[dict, bool]:
    """Apply the enterprise MCP table, preserving the existing fail-closed rule."""
    from jiuwenswarm.server.runtime.enterprise_config.apply_mcp import (
        apply_enterprise_mcp_to_config,
        clear_local_mcp_servers,
    )

    if not is_enterprise():
        return base, False
    if loaded is None:
        return clear_local_mcp_servers(base), False
    return apply_enterprise_mcp_to_config(base, loaded)


def apply_enterprise_models_and_mcp(
    base: dict,
    loaded: Any,
) -> tuple[dict, bool, bool]:
    """Apply enterprise model, embedding and MCP slots to a config snapshot."""
    from jiuwenswarm.agents.harness.common.memory.config import (
        clear_embed_config_db_cache,
        set_embed_config_db_cache,
    )
    from jiuwenswarm.server.runtime.enterprise_config.apply_models import (
        apply_enterprise_models_to_config,
    )

    if loaded is None:
        clear_embed_config_db_cache()
        merged, mcp_applied = apply_enterprise_mcp(base, loaded)
        return merged, False, mcp_applied
    merged, models_applied = apply_enterprise_models_to_config(base, loaded)
    set_embed_config_db_cache(getattr(loaded, "embedding", None))
    merged, mcp_applied = apply_enterprise_mcp(merged, loaded)
    return merged, models_applied, mcp_applied


def _apply_enterprise_policy(base: dict, loaded: Any) -> dict:
    from jiuwenswarm.agents.harness.common.memory.config import (
        merge_memory_config_into_config,
    )

    merged, _, _ = apply_enterprise_models_and_mcp(base, loaded)
    return merge_memory_config_into_config(merged)


class DefaultConfigProvider:
    name = "default"

    @staticmethod
    def get_process_config() -> dict | None:
        return None

    async def load_process_config(self) -> dict | None:
        return None

    async def load_agent_config(
        self, ctx: AgentConfigContext, *, base: dict
    ) -> AgentConfigResult | None:
        source = resolve_config_source()
        if source is ConfigSource.YAML:
            return None
        from jiuwenswarm.server.runtime.enterprise_config import (
            DEFAULT_AGENT_LOAD_SLOTS,
            load_effective_enterprise_config,
        )

        loaded = (
            await load_effective_enterprise_config(
                ctx.request, DEFAULT_AGENT_LOAD_SLOTS
            )
            if ctx.request is not None
            else ctx.cached_policy
        )
        return AgentConfigResult(
            config=_apply_enterprise_policy(base, loaded), policy=loaded
        )

    async def refresh(self) -> None:
        source = resolve_config_source()
        if source is ConfigSource.ENTERPRISE and is_enterprise():
            from jiuwenswarm.server.runtime.enterprise_config import (
                invalidate_enterprise_config_caches,
            )
            from jiuwenswarm.agents.harness.common.memory.config import (
                reload_memory_config_from_gateway_db,
            )

            invalidate_enterprise_config_caches()
            await reload_memory_config_from_gateway_db()
            return


_DEFAULT_PROVIDER = DefaultConfigProvider()


async def resolve_agent_config(
    base: dict,
    *,
    request: Any = None,
    service_id: str | None = None,
    agent_id: str | None = None,
    workspace_key: str | None = None,
    routing: dict | None = None,
    cached_policy: Any = None,
) -> AgentConfigResult:
    if routing is None and request is not None:
        from jiuwenswarm.common.request_identity import web_routing_identity

        metadata = getattr(request, "metadata", None)
        routing = (
            web_routing_identity(metadata if isinstance(metadata, dict) else None)
            or None
        )
    ctx = AgentConfigContext(
        service_id=service_id,
        agent_id=agent_id,
        workspace_key=workspace_key,
        request=request,
        routing=routing,
        cached_policy=cached_policy,
    )
    provider = get_config_provider()
    if provider is not None:
        try:
            result = await provider.load_agent_config(ctx, base=base)
            if result is not None:
                return result
        except Exception:
            logger.warning("config provider failed, fallback to default", exc_info=True)
    result = await _DEFAULT_PROVIDER.load_agent_config(ctx, base=base)
    resolved = result or AgentConfigResult(config=base, policy=None)
    return resolved


async def refresh_config_source() -> None:
    provider = get_config_provider()
    refresh = getattr(provider, "refresh", None) if provider is not None else None
    if (
        callable(refresh)
        and getattr(refresh, "__func__", refresh) is not ConfigProvider.refresh
    ):
        try:
            await refresh()
            logger.info(
                "config provider refresh completed",
                extra={"provider": getattr(provider, "name", type(provider).__name__)},
            )
            return
        except Exception:
            logger.warning(
                "config provider refresh failed, fallback to default", exc_info=True
            )
    elif provider is not None:
        logger.info(
            "config provider has no refresh override, fallback to default",
            extra={"provider": getattr(provider, "name", type(provider).__name__)},
        )
    await _DEFAULT_PROVIDER.refresh()
