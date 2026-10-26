# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for reading and filtering enterprise extension_config in AgentServer rails."""

from __future__ import annotations

from typing import Any


def filter_agent_extension_config(
    records: list[Any] | None,
) -> list[dict[str, Any]]:
    """Keep enabled records targeted at agent_server (or missing component).

    Gateway-only hooks (``component=gateway``) and disabled rows are dropped so
    Agent rails only see configs meant for them.
    """
    if not records:
        return []
    filtered: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("enabled") is False:
            continue
        component = record.get("component")
        if component in (None, "", "agent_server"):
            filtered.append(record)
    return filtered


def _extension_config_from_run_context(run_context: Any) -> list[dict[str, Any]] | None:
    """Return ``extension_config`` list from a RunContext-like object, if present."""
    if run_context is None:
        return None
    extra = getattr(run_context, "extra", None)
    if not isinstance(extra, dict):
        return None
    ext_config = extra.get("extension_config")
    if isinstance(ext_config, list):
        return ext_config
    return None


def get_extension_config_from_ctx(ctx: Any) -> list[dict[str, Any]] | None:
    """Extract extension_config from AgentCallbackContext.

    Supports:
    1) InvokeInputs / dataclass with ``run_context.extra`` (DeepAgent outer path)
    2) ``ctx.extra["run_context"].extra`` (ReActAgent bridged path)
    3) Raw dict inputs with top-level ``extension_config``
    """
    inputs = getattr(ctx, "inputs", None)
    if inputs is None:
        return None

    # Path 1: ctx.inputs.run_context.extra (DeepAgent BEFORE/AFTER_INVOKE)
    found = _extension_config_from_run_context(getattr(inputs, "run_context", None))
    if found is not None:
        return found

    # Path 2: ctx.extra["run_context"].extra
    # ReActAgent._inner_invoke keeps InvokeInputs.run_context=None and puts the
    # object on ctx.extra; bridged rails (before_tool_call / before_model_call)
    # therefore only see this location.
    ctx_extra = getattr(ctx, "extra", None)
    if isinstance(ctx_extra, dict):
        found = _extension_config_from_run_context(ctx_extra.get("run_context"))
        if found is not None:
            return found

    # Path 3: raw dict inputs (top-level passthrough)
    if isinstance(inputs, dict):
        ext_config = inputs.get("extension_config")
        if isinstance(ext_config, list):
            return ext_config

    return None


def summarize_extension_config_for_log(
    records: list[Any] | None,
) -> list[dict[str, Any]]:
    """Return a redacted summary safe for INFO logs (no hook_config / params)."""
    summary: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        summary.append(
            {
                "template_id": record.get("template_id") or record.get("id"),
                "template_name": record.get("template_name") or record.get("name"),
                "component": record.get("component"),
                "hook_type": record.get("hook_type"),
                "enabled": record.get("enabled", True),
            }
        )
    return summary


def is_extension_config_debug_rail_enabled() -> bool:
    """Whether ``AGENT_EXTENSION_CONFIG_DEBUG_RAIL`` opts into the debug rail."""
    import os

    flag = os.getenv("AGENT_EXTENSION_CONFIG_DEBUG_RAIL", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def write_extension_config_into_inputs(
    inputs: dict[str, Any],
    enterprise_extension_config: list[Any] | None,
) -> list[dict[str, Any]] | None:
    """Filter and align ``extension_config`` on ``inputs`` and ``run.context.extra``.

    Prefer an existing top-level list on ``inputs`` (e.g. upstream passthrough);
    otherwise use ``enterprise_extension_config``. Always write the filtered
    result to both places so Rails reading ``run_context.extra`` stay in sync.

    Returns the filtered list written, or ``None`` when nothing was written.
    """
    existing = inputs.get("extension_config")
    had_existing = isinstance(existing, list)
    if had_existing:
        raw_ext_config: list[Any] | None = existing
    elif isinstance(enterprise_extension_config, list):
        raw_ext_config = enterprise_extension_config
    else:
        return None

    ext_config = filter_agent_extension_config(raw_ext_config)
    # 无上游透传且过滤后为空：不污染 inputs
    if not ext_config and not had_existing:
        return None

    inputs["extension_config"] = ext_config
    run_payload = inputs.get("run")
    if not isinstance(run_payload, dict):
        run_payload = {}
        inputs["run"] = run_payload
    context = run_payload.get("context")
    if not isinstance(context, dict):
        context = {}
        run_payload["context"] = context
    extra = context.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        context["extra"] = extra
    extra["extension_config"] = ext_config
    return ext_config
