"""Config source SPI with the existing YAML and enterprise paths as defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from jiuwenswarm.enterprise import is_enterprise

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
    # 产品形态闸门（edition）；与平面是否组装无关。
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
    from jiuwenswarm.enterprise.config.effective.apply_mcp import (
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
    from jiuwenswarm.enterprise.config.effective.apply_models import (
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


def _merge_swarmflow_policy(config_base: dict, policy: dict) -> dict:
    """把 Swarmflow 企业策略写入 ``modes.team.jiuwen_team.swarmflow``。

    深拷贝后写入，保留本地 ``config.yaml`` 已配置而企业策略未覆盖的键。
    """
    import copy

    merged = copy.deepcopy(config_base)
    modes = merged.get("modes")
    modes_cfg = dict(modes) if isinstance(modes, dict) else {}
    team = modes_cfg.get("team")
    team_cfg = dict(team) if isinstance(team, dict) else {}
    jiuwen_team = team_cfg.get("jiuwen_team")
    jiuwen_team_cfg = dict(jiuwen_team) if isinstance(jiuwen_team, dict) else {}
    existing = jiuwen_team_cfg.get("swarmflow")
    swarmflow_cfg = dict(existing) if isinstance(existing, dict) else {}
    swarmflow_cfg.update(policy)
    jiuwen_team_cfg["swarmflow"] = swarmflow_cfg
    team_cfg["jiuwen_team"] = jiuwen_team_cfg
    modes_cfg["team"] = team_cfg
    merged["modes"] = modes_cfg
    return merged


def _apply_enterprise_policy(base: dict, loaded: Any, *, bind_request_credentials: bool = True) -> dict:
    from jiuwenswarm.agents.harness.common.memory.config import (
        merge_memory_config_into_config,
    )
    from jiuwenswarm.common.mcp_dynamic_token_patch import apply_dispatched_credentials
    from jiuwenswarm.enterprise.config.effective.apply_advanced_config import (
        apply_enterprise_advanced_config_to_config,
    )
    from jiuwenswarm.enterprise.config.effective.apply_permissions import (
        apply_enterprise_permissions_to_config,
    )
    from jiuwenswarm.enterprise.config.effective.apply_execution_guard import (
        apply_enterprise_execution_guard_to_config,
    )
    from jiuwenswarm.enterprise.config.effective.apply_react import (
        apply_enterprise_react_to_config,
    )

    merged, _, _ = apply_enterprise_models_and_mcp(base, loaded)
    # 执行护栏在模型之后整段替换，避免模型 retry_count 旧逻辑盖掉护栏开关和重试次数。
    merged, _ = apply_enterprise_execution_guard_to_config(merged, loaded)
    merged = merge_memory_config_into_config(merged)
    merged, _ = apply_enterprise_permissions_to_config(merged, loaded)
    merged, _ = apply_enterprise_react_to_config(merged, loaded)
    # advanced_config（含 a4p_enabled）→ config.a4p.enabled 等
    merged, _ = apply_enterprise_advanced_config_to_config(merged, loaded)
    if bind_request_credentials:
        apply_dispatched_credentials(loaded)
    # expert_package_template.swarmflow → config.modes.team.jiuwen_team.swarmflow.*
    swarmflow_policy = getattr(loaded, "swarmflow", None)
    if isinstance(swarmflow_policy, dict) and swarmflow_policy:
        merged = _merge_swarmflow_policy(merged, swarmflow_policy)
    elif getattr(loaded, "swarmflow_load_failed", False):
        # Fail closed: 策略加载失败 ≠ 策略缺省。门禁语义下宁可错关不可错开，
        # 置 allowed=False 并告警，避免瞬时 DB 故障把企业门禁静默放行。
        logger.warning(
            "[enterprise_config] swarmflow policy load failed; failing closed "
            "(allowed=False) for this resolve",
        )
        merged = _merge_swarmflow_policy(merged, {"allowed": False})
    return merged


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
        from jiuwenswarm.enterprise.config.effective import (
            DEFAULT_AGENT_LOAD_SLOTS,
            load_effective_enterprise_config,
        )

        # Execution owns memory caches. Read persisted values here so a fresh
        # worker (or one without Runtime refresh notifications) does not depend
        # on Gateway startup or a shared Python process to populate them.
        try:
            await self._refresh_memory_configs()
        except Exception:
            logger.warning(
                "memory configuration refresh failed; keeping last applied values",
                exc_info=True,
            )
        loaded = (
            await load_effective_enterprise_config(
                ctx.request, DEFAULT_AGENT_LOAD_SLOTS
            )
            if ctx.request is not None
            else ctx.cached_policy
        )
        return AgentConfigResult(
            config=_apply_enterprise_policy(base, loaded, bind_request_credentials=ctx.request is not None),
            policy=loaded,
        )

    async def refresh(self) -> None:
        source = resolve_config_source()
        if source is ConfigSource.ENTERPRISE and is_enterprise():
            from jiuwenswarm.enterprise.config.effective import (
                invalidate_enterprise_config_caches,
            )
            invalidate_enterprise_config_caches()
            await self._refresh_memory_configs()

    @staticmethod
    async def _refresh_memory_configs() -> None:
        from jiuwenswarm.agents.harness.common.memory.config import (
            reload_memory_config_from_gateway_db,
            reload_task_memory_config_from_gateway_db,
        )

        await reload_memory_config_from_gateway_db()
        await reload_task_memory_config_from_gateway_db()


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
    if request is not None:
        from jiuwenswarm.common.mcp_dynamic_token_patch import apply_dispatched_credentials

        # A failed or skipped lookup must not inherit the caller's previous registration.
        apply_dispatched_credentials(None)
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
