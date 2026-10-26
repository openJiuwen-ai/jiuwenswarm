# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""JiuWenSwarm adapter: load react.concurrency config and register core hook."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

_LOG_PREFIX = "[tool_concurrency]"
_logger = logging.getLogger("jiuwenswarm.app")


@dataclass(frozen=True)
class ToolConcurrencyRule:
    limit: int


@dataclass(frozen=True)
class ConcurrencyPolicy:
    enabled: bool = True
    tools: dict[str, ToolConcurrencyRule] = field(default_factory=dict)

    def as_log_text(self) -> str:
        if not self.tools:
            return "{}"
        parts = [f"{name}={rule.limit}" for name, rule in sorted(self.tools.items())]
        return "{" + ", ".join(parts) + "}"


_controller = None


def _normalize_tool_name(name: str | None) -> str:
    return str(name or "").strip().lower()


def resolve_concurrency_policy(
    config_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> ConcurrencyPolicy:
    if config_provider is not None:
        return _load_policy_from_mapping(config_provider())
    return _load_policy_from_config()


def _parse_limit(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return max(1, raw)
    if isinstance(raw, float):
        if raw != int(raw):
            _logger.warning(
                "%s truncating non-integer tool_limits value %r",
                _LOG_PREFIX,
                raw,
            )
        return max(1, int(raw))
    if isinstance(raw, Mapping):
        limit_raw = raw.get("limit", raw.get("max"))
        if limit_raw is None:
            return None
        return _parse_limit(limit_raw)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def _load_policy_from_mapping(config: Mapping[str, Any] | None) -> ConcurrencyPolicy:
    react = (config or {}).get("react", {}) or {}
    concurrency = react.get("concurrency") or {}
    enabled = True
    tools: dict[str, ToolConcurrencyRule] = {}

    if isinstance(concurrency, Mapping):
        enabled = concurrency.get("enabled", True) is not False
        raw_tools = concurrency.get("tool_limits") or {}
        if isinstance(raw_tools, Mapping):
            for name, value in raw_tools.items():
                tool_name = _normalize_tool_name(str(name))
                if not tool_name:
                    continue
                limit = _parse_limit(value)
                if limit is None:
                    _logger.warning(
                        "%s ignore invalid tool_limits entry tool=%r value=%r",
                        _LOG_PREFIX,
                        tool_name,
                        value,
                    )
                    continue
                tools[tool_name] = ToolConcurrencyRule(limit=limit)

    if not enabled:
        return ConcurrencyPolicy(enabled=False, tools={})
    return ConcurrencyPolicy(enabled=True, tools=tools)


def _load_policy_from_config() -> ConcurrencyPolicy:
    try:
        from jiuwenswarm.common.config import get_config

        return _load_policy_from_mapping(get_config() or {})
    except (ImportError, OSError, TypeError, ValueError, AttributeError) as exc:
        _logger.warning(
            "%s config load failed, concurrency limits disabled: %s",
            _LOG_PREFIX,
            exc,
        )
    except Exception:
        _logger.exception(
            "%s unexpected policy load error, concurrency limits disabled",
            _LOG_PREFIX,
        )
    return ConcurrencyPolicy(enabled=False, tools={})


def _to_core_policy(policy: ConcurrencyPolicy):
    from openjiuwen.core.single_agent.tool_batch_concurrency import (
        ToolBatchConcurrencyPolicy,
        ToolConcurrencyRule as core_tool_concurrency_rule_cls,
    )

    return ToolBatchConcurrencyPolicy(
        enabled=policy.enabled,
        tools={
            name: core_tool_concurrency_rule_cls(limit=rule.limit)
            for name, rule in policy.tools.items()
        },
    )


def _get_controller():
    """Return the process-wide controller singleton (asyncio event loop only)."""
    global _controller
    if _controller is None:
        from openjiuwen.core.single_agent.tool_batch_concurrency import (
            ToolBatchConcurrencyController,
        )

        _controller = ToolBatchConcurrencyController(
            lambda: _to_core_policy(resolve_concurrency_policy())
        )
    return _controller


def register_tool_batch_concurrency() -> None:
    """Wire jiuwenswarm config into AbilityManager via openjiuwen core hook.

    Always register the controller (even when policy is empty/disabled) so that
    later config hot-reload / migrate that adds ``react.concurrency.tool_limits``
    takes effect without restarting AgentServer. An empty policy is a no-op in
    ``batch_scope`` / ``run_with_slot``.
    """
    from openjiuwen.core.single_agent.ability_manager import AbilityManager

    policy = resolve_concurrency_policy()
    AbilityManager.configure_tool_batch_concurrency(_get_controller())
    if not policy.enabled or not policy.tools:
        _logger.info(
            "[tool_concurrency] AbilityManager core hook registered "
            "(empty/disabled policy; limits inactive until tool_limits set)"
        )
        return

    _logger.info(
        "[tool_concurrency] AbilityManager core hook registered policy=%s",
        policy.as_log_text(),
    )


def apply_tool_concurrency_limit() -> None:
    """Register batch tool concurrency via openjiuwen AbilityManager core hook."""
    try:
        register_tool_batch_concurrency()
    except Exception as exc:
        _logger.warning(
            "AbilityManager tool concurrency registration failed; limits disabled: %s",
            exc,
            exc_info=True,
        )
