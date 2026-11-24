# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""请求级监控汇总日志（issue #6745）。

每个请求在 pipeline 出口输出**一行** JSON 汇总，按 ``request_id`` 聚合本次请求
的结局（ok/cancelled/failed/client_gone）、阶段耗时、LLM/工具/token 统计等，
供线上运维 grep / 日志平台聚合排障使用。

- 文本日志模式：message 为压缩单行 JSON 字符串（``jq`` 可直接解析）。
- JSON 日志模式：所有字段经 ``extra=`` 平铺到 JSON 顶层（格式化器会把未知
  extra 键合并到顶层），字段名与文本模式一致。

开关优先级：环境变量 ``JIUWENSWARM_LOG_REQ_SUMMARY`` > ``config.yaml`` 的
``logging.request_summary.enabled`` > 默认开启。级别：``JIUWENSWARM_LOG_REQ_
SUMMARY_LEVEL`` / ``logging.request_summary.level``，默认 INFO。开关/级别的
解析结果按 5 秒 TTL 缓存（同步配置读取内部要临时线程跑异步存储，逐条解析
太贵），配置热更最多延迟一个 TTL 生效。

所有路径 fail-open：汇总日志的任何异常都不允许影响请求主流程。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from openjiuwen.core.common.logging import server_logger

logger = logging.getLogger("jiuwenswarm.server.request_summary")

_SUMMARY_VERSION = 1

_ENV_ENABLED = "JIUWENSWARM_LOG_REQ_SUMMARY"
_ENV_LEVEL = "JIUWENSWARM_LOG_REQ_SUMMARY_LEVEL"
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_LEVEL_NAMES = {"DEBUG", "INFO", "WARNING", "ERROR"}

_TOGGLE_CACHE_TTL_S = 5.0
_toggle_cache: dict[str, tuple[float, Any]] = {}


def _cached_toggle(key: str, resolver: Any) -> Any:
    now = time.monotonic()
    cached = _toggle_cache.get(key)
    if cached is not None and now - cached[0] < _TOGGLE_CACHE_TTL_S:
        return cached[1]
    value = resolver()
    _toggle_cache[key] = (now, value)
    return value


def reset_request_summary_cache() -> None:
    """Clear the enable/level TTL cache（配置热更与单测用）。"""
    _toggle_cache.clear()

# 结局枚举（pipeline 平面，请求级终态）
STATUS_OK = "ok"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_CLIENT_GONE = "client_gone"

_ERROR_CLASSES = frozenset(
    {"none", "timeout", "llm", "tool", "policy", "auth", "internal", "unknown"}
)

# 线上排障最关心的时间单位换算：perf summary 全部存秒，行内统一毫秒。
_S_MS = 1000.0

# logging.LogRecord 的保留属性名：extra= 撞上会 KeyError，直接跳过这些键。
_LOGRECORD_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


def _summary_enabled() -> bool:
    """Env override first, then ``logging.request_summary`` config, default on."""

    def _resolve() -> bool:
        env_value = os.environ.get(_ENV_ENABLED)
        if env_value is not None:
            return env_value.strip().lower() not in _FALSE_VALUES
        try:
            from jiuwenswarm.gateway.config.logging.access import (
                get_logging_config_section_sync,
            )

            logging_config = get_logging_config_section_sync()
        except Exception:
            return True
        if not isinstance(logging_config, dict):
            return True
        section = logging_config.get("request_summary")
        if isinstance(section, dict):
            return bool(section.get("enabled", True))
        return bool(logging_config.get("request_summary", True))

    return _cached_toggle("enabled", _resolve)


def _summary_level() -> int:
    """Resolve the emit level: env > config > INFO; unknown names fall back."""

    def _resolve() -> int:
        name = (os.environ.get(_ENV_LEVEL) or "").strip().upper()
        if not name:
            try:
                from jiuwenswarm.gateway.config.logging.access import (
                    get_logging_config_section_sync,
                )

                logging_config = get_logging_config_section_sync()
                if isinstance(logging_config, dict):
                    section = logging_config.get("request_summary")
                    if isinstance(section, dict):
                        name = str(section.get("level", "") or "").strip().upper()
            except Exception:
                name = ""
        if name not in _LEVEL_NAMES:
            name = "INFO"
        return getattr(logging, name, logging.INFO)

    return _cached_toggle("level", _resolve)


def classify_error_class(exc: BaseException | None) -> str:
    """Map a pipeline-level exception to a coarse error class.

    String matching on class/module names keeps this decoupled from concrete
    exception types; perf-collected ``errors[0].kind`` stays the fallback in
    the renderer for adapter-internal failures.
    """
    if exc is None:
        return "none"
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    parts = f"{type(exc).__module__}.{type(exc).__name__}".lower()
    if "cancelled" in parts:
        return "none"
    if "policy" in parts or "binding" in parts or "permission" in parts:
        return "policy"
    if "auth" in parts or "token" in parts or "credential" in parts:
        return "auth"
    if "timeout" in parts:
        return "timeout"
    if "tool" in parts:
        return "tool"
    if any(marker in parts for marker in ("llm", "model", "openai", "maas", "sse")):
        return "llm"
    if "websocket" in parts or "connection" in parts or "closed" in parts:
        return "unknown"
    return "internal"


def resolve_status(pipeline_status: str, perf_status: str | None) -> str:
    """Merge pipeline-plane and adapter-plane outcomes (more severe wins).

    The pipeline sees cancel/connection-close/policy failure; the adapter
    additionally sees chat.error emitted without a pipeline exception.
    """
    if pipeline_status == STATUS_FAILED:
        return STATUS_FAILED
    if pipeline_status == STATUS_CLIENT_GONE:
        return STATUS_CLIENT_GONE
    if pipeline_status == STATUS_CANCELLED:
        return STATUS_CANCELLED
    if perf_status == "error":
        return STATUS_FAILED
    if perf_status == "cancelled":
        return STATUS_CANCELLED
    return STATUS_OK


def _round_ms(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 1)


def _stage_offset_ms(
    marks: dict[str, Any],
    stage: str,
    t0_monotonic: float,
    t_end_monotonic: float,
) -> float | None:
    """Convert one first-mark-wins stage mark into an ms offset from recv."""
    raw = marks.get(stage)
    if not isinstance(raw, (int, float)):
        return None
    offset = (float(raw) - t0_monotonic) * _S_MS
    if offset < 0.0:
        return None
    return round(min(offset, (t_end_monotonic - t0_monotonic) * _S_MS), 1)


def _error_class_from_perf(perf: dict[str, Any] | None) -> str | None:
    if not perf:
        return None
    errors = perf.get("errors")
    if isinstance(errors, list) and errors:
        kind = str(errors[0].get("kind") or "").strip().lower()
        if kind in _ERROR_CLASSES:
            return kind
        return "unknown"
    # errors[] is config-gated; fail counters are always present.
    stats = (perf.get("summary") or {}).get("stats") or {}
    if int((stats.get("tool") or {}).get("fail_count") or 0) > 0:
        return "tool"
    if int((stats.get("llm") or {}).get("fail_count") or 0) > 0:
        return "llm"
    return None


def build_summary_payload(
    *,
    request: Any,
    pipeline_status: str,
    pipeline_error: BaseException | None,
    error_class: str,
    t0_monotonic: float,
    t_end_monotonic: float,
    t0_wall: float,
    perf: dict[str, Any] | None,
    hitl_wait_total_ms: float | None = None,
    hitl_wait_count: int = 0,
) -> dict[str, Any]:
    """Assemble the flat single-line payload (server field names)."""
    meta = (perf or {}).get("meta") or {}
    summ = (perf or {}).get("summary") or {}
    stats = summ.get("stats") or {}
    llm = stats.get("llm") or {}
    tool = stats.get("tool") or {}
    task = stats.get("task") or {}
    tokens = summ.get("tokens") or {}
    stream = (perf or {}).get("stream") or {}
    marks = (perf or {}).get("stage_marks") or {}
    skills = (perf or {}).get("skills") or {}
    ctx_window = (perf or {}).get("context_window") or {}

    status = resolve_status(pipeline_status, summ.get("status"))
    if error_class == "none" and status == STATUS_FAILED:
        error_class = _error_class_from_perf(perf) or "internal"

    permission = getattr(request, "permission_context", None)
    uid = ""
    for attr in ("triggering_user_id", "principal_user_id"):
        value = str(getattr(permission, attr, "") or "").strip()
        if value:
            uid = value
            break

    metadata = getattr(request, "metadata", None)
    ext = metadata.get("ext") if isinstance(metadata, dict) else None
    ext = ext if isinstance(ext, dict) else {}
    source = str(ext.get("source") or ext.get("client_type") or "").strip()

    method = getattr(getattr(request, "req_method", None), "value", None) or ""
    total_ms = round(max(0.0, (t_end_monotonic - t0_monotonic) * _S_MS), 1)

    payload: dict[str, Any] = {
        "v": _SUMMARY_VERSION,
        "rid": str(getattr(request, "request_id", "") or ""),
        "sid": str(getattr(request, "session_id", "") or ""),
        "status": status,
        "error_class": error_class,
        "method": method,
        "channel": str(getattr(request, "channel_id", "") or ""),
        "total_ms": total_ms,
        "DurationMs": total_ms,
    }
    if uid:
        payload["uid"] = uid
    if source:
        payload["source"] = source
    trace_id = str(meta.get("trace_id") or "").strip()
    if trace_id:
        payload["trace_id"] = trace_id
    mode = str(meta.get("mode") or "").strip()
    if mode:
        payload["mode"] = mode

    # --- timing (all recv-anchored unless noted) ---
    payload["prep_ms"] = _round_ms(
        (float(meta.get("started_at") or 0.0) - t0_wall) * _S_MS
    ) if meta.get("started_at") else 0.0
    for key, stage in (
        ("t_init_ms", "init"),
        ("t_mcp_ms", "mcp"),
        ("t_pre_llm_ms", "pre_llm"),
        ("t_first_out_ms", "first_out"),
    ):
        offset = _stage_offset_ms(marks, stage, t0_monotonic, t_end_monotonic)
        if offset is not None:
            payload[key] = offset
    first_byte_ms = _round_ms(float(summ.get("first_byte_latency_s") or 0.0) * _S_MS)
    first_answer_ms = _round_ms(float(summ.get("first_answer_latency_s") or 0.0) * _S_MS)
    if first_byte_ms:
        payload["first_byte_ms"] = first_byte_ms
    if first_answer_ms:
        payload["first_answer_ms"] = first_answer_ms

    # --- LLM ---
    payload["llm_calls"] = int(llm.get("count") or 0)
    payload["llm_fail"] = int(llm.get("fail_count") or 0)
    payload["llm_ms"] = _round_ms(float(llm.get("total_s") or 0.0) * _S_MS)
    if llm.get("ttft_ms") is not None:
        payload["llm_ttft_ms"] = _round_ms(llm.get("ttft_ms"))
    if llm.get("tpot_ms") is not None:
        payload["llm_tpot_ms"] = _round_ms(llm.get("tpot_ms"))
    model = str(llm.get("model") or "").strip()
    if model:
        payload["model"] = model

    # --- tokens ---
    payload["tok_in"] = int(tokens.get("input") or 0)
    payload["tok_out"] = int(tokens.get("output") or 0)
    payload["tok_total"] = int(tokens.get("total") or 0)
    payload["tok_cache"] = int(tokens.get("cache_read") or 0)
    payload["tok_reason"] = int(tokens.get("reasoning") or 0)
    limit_tokens = ctx_window.get("limit_tokens")
    occupancy = ctx_window.get("occupancy_rate")
    if limit_tokens:
        payload["ctx_window_tokens"] = int(limit_tokens)
    if occupancy is not None:
        payload["ctx_occupancy"] = occupancy

    # --- tools ---
    payload["tool_calls"] = int(tool.get("count") or 0)
    payload["tool_fail"] = int(tool.get("fail_count") or 0)
    payload["tool_ms"] = _round_ms(float(tool.get("total_s") or 0.0) * _S_MS)
    payload["tool_max_ms"] = _round_ms(float(tool.get("max_s") or 0.0) * _S_MS)
    payload["tool_rounds"] = int(tool.get("round_count") or 0)
    detail = tool.get("detail")
    if isinstance(detail, dict) and detail:
        payload["tool_stats"] = detail

    # --- stream segments ---
    payload["thinking_total_ms"] = _round_ms(stream.get("thinking_total_ms") or 0.0)
    payload["thinking_max_ms"] = _round_ms(stream.get("thinking_max_ms") or 0.0)
    segments = stream.get("thinking_segments")
    if isinstance(segments, dict) and segments:
        payload["thinking_stats"] = segments
    payload["msg_gen_total_ms"] = _round_ms(stream.get("msg_gen_total_ms") or 0.0)
    payload["msg_gen_max_ms"] = _round_ms(stream.get("msg_gen_max_ms") or 0.0)
    # 口径声明：gap_total = Duration 内不属于 LLM 调用与工具执行的其余开销
    # （框架调度、排队、流控等）。注意不能用 perf 的 unattributed_s —— 那是
    # "未归属到 task" 的时长，会把主 agent 的 LLM/工具时间也算进去。
    # gap_max = 流内相邻事件间无 LLM/工具在途的最大空窗（近似）。
    payload["gap_total_ms"] = _round_ms(
        max(0.0, total_ms - float(llm.get("total_s") or 0.0) * _S_MS
            - float(tool.get("total_s") or 0.0) * _S_MS)
    )
    gap_max = stream.get("stream_gap_max_ms")
    if gap_max:
        payload["gap_max_ms"] = _round_ms(gap_max)

    # --- HITL / skills / errors / tasks ---
    if hitl_wait_count:
        payload["hitl_wait_total_ms"] = _round_ms(hitl_wait_total_ms or 0.0)
        payload["hitl_wait_count"] = int(hitl_wait_count)
    if skills:
        payload["skill_stats"] = skills
    payload["chat_errors"] = int(stream.get("chat_errors") or 0)
    payload["tasks"] = int(task.get("count") or 0)
    payload["task_fail"] = int(task.get("fail_count") or 0)
    chunk_count = (perf or {}).get("chunk_count")
    if chunk_count:
        payload["chunks"] = int(chunk_count)

    return payload


def emit_request_summary(payload: dict[str, Any]) -> None:
    """Emit one single-line summary record through the dedicated logger.

    Text mode consumes ``record.message`` (compact JSON); JSON mode merges the
    flat payload to the top level via ``extra``. Gated by ``_summary_enabled``
    (env > config，TTL 缓存). Never raises.
    """
    if not payload or not _summary_enabled():
        return
    try:
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        extra = {k: v for k, v in payload.items() if k not in _LOGRECORD_RESERVED}
        logger.log(_summary_level(), message, extra=extra)
    except Exception:
        try:
            server_logger.warning(
                "[request_summary] emit failed: %s",
                payload.get("rid", "-"),
                exc_info=True,
            )
        except Exception as fallback_exc:
            # 兜底日志自身也失败时仍不能抛出：降级 debug 留痕后放行。
            logger.debug(
                "[request_summary] emit fallback failed: rid=%s err=%s",
                payload.get("rid", "-"),
                fallback_exc,
            )


__all__ = [
    "build_summary_payload",
    "classify_error_class",
    "emit_request_summary",
    "resolve_status",
]
