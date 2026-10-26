# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Effective debug-trace settings resolution.

Merges the request-level ``/debug`` flag with the ``debug_trace`` config block:

    debug_enabled = request_debug OR debug_trace.<mode>.enabled
    dump_enabled  = debug_enabled AND debug_trace.<mode>.dump_enabled != false

Per-mode ``include_*`` toggles, ``limits`` and ``redaction`` are read with
sensible defaults. OTel is still off (third phase). Config reading is
best-effort: any failure falls back to request-level-only behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DebugTraceSettings:
    """Resolved debug-trace behaviour for one run."""

    mode: str
    enabled: bool
    dump_enabled: bool
    otel_enabled: bool
    # Include toggles.
    include_model_output: bool = True
    include_reasoning: bool = True
    include_tool_args: bool = True
    include_tool_result: bool = True
    # When False, subagent streams fall back to plain invoke() (no subagent
    # section in the dump). See invoke_subagent_with_trace.
    include_subagent_flow: bool = True
    # Payload caps (chars).
    tool_args_max_chars: int = 2000
    tool_result_max_chars: int = 8000
    generic_payload_max_chars: int = 4000
    max_model_output_chars: int | None = None  # None = never cap model text
    # Redaction (secret-key masking is always on regardless).
    redact_prompts: bool = False
    redact_completions: bool = False


def _load_debug_trace_config() -> dict[str, Any]:
    """Best-effort read of the ``debug_trace`` config block."""
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config().get("debug_trace", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _mode_key(mode: str) -> str:
    return "code" if (mode or "").startswith("code") else "agent"


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_debug_trace_settings(*, mode: str, request_debug: bool) -> DebugTraceSettings:
    """Resolve effective settings for *mode* given the request-level flag.

    Merges request-level ``/debug`` with the ``debug_trace`` config block.
    """
    cfg = _load_debug_trace_config()
    mode_cfg = cfg.get(_mode_key(mode), {})
    if not isinstance(mode_cfg, dict):
        mode_cfg = {}
    limits = cfg.get("limits", {})
    if not isinstance(limits, dict):
        limits = {}
    redaction = cfg.get("redaction", {})
    if not isinstance(redaction, dict):
        redaction = {}

    debug_enabled = bool(
        request_debug
        or mode_cfg.get("enabled")
    )
    dump_enabled = debug_enabled and (mode_cfg.get("dump_enabled", True) is not False)
    # /debug-driven OTel: only force-enable agent_observability when this run is
    # debugged AND the mode opted into OTel. (When agent_observability.enabled is
    # already true, OTel runs regardless — handled by sync_agent_observability.)
    otel_enabled = debug_enabled and bool(mode_cfg.get("otel_enabled", False))

    # max_model_output_chars: empty/None -> no cap.
    mmo_raw = limits.get("max_model_output_chars")
    max_model_output_chars: int | None = None
    if mmo_raw not in (None, ""):
        try:
            max_model_output_chars = int(mmo_raw)
        except (TypeError, ValueError):
            max_model_output_chars = None

    return DebugTraceSettings(
        mode=mode,
        enabled=debug_enabled,
        dump_enabled=dump_enabled,
        otel_enabled=otel_enabled,
        include_model_output=bool(mode_cfg.get("include_model_output", True)),
        include_reasoning=bool(mode_cfg.get("include_reasoning", True)),
        include_tool_args=bool(mode_cfg.get("include_tool_args", True)),
        include_tool_result=bool(mode_cfg.get("include_tool_result", True)),
        include_subagent_flow=bool(mode_cfg.get("include_subagent_flow", True)),
        tool_args_max_chars=_as_int(limits.get("tool_args_max_chars"), 2000),
        tool_result_max_chars=_as_int(limits.get("tool_result_max_chars"), 8000),
        generic_payload_max_chars=_as_int(limits.get("generic_payload_max_chars"), 4000),
        max_model_output_chars=max_model_output_chars,
        redact_prompts=bool(redaction.get("redact_prompts", False)),
        redact_completions=bool(redaction.get("redact_completions", False)),
    )


__all__ = ["DebugTraceSettings", "resolve_debug_trace_settings"]
