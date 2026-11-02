"""轨迹可观测性：mode 归一与 iteration 解析。

职责：
- ``_resolve_observability_mode``：把请求 params 写成轨迹 span 用的规范 mode
- ``_extract_iteration_from_*``：从 chunk / payload 尽力解析 iteration

供 LLM IO trace patch 与 stream 路径共用；无 adapter 状态。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.mode_matrix import (
    canonicalize_mode_text,
    compose_web_mode,
    deprecate_mode,
    normalize_work_mode,
)


def _resolve_observability_mode(params: dict[str, Any]) -> str:
    """Return the canonical mode written into trajectory span attributes."""
    raw_mode = params.get("mode", "agent")
    normalized_mode = canonicalize_mode_text(raw_mode)
    work_mode = normalize_work_mode(params.get("work_mode"))
    if work_mode is not None:
        composed_mode = compose_web_mode(normalized_mode, work_mode)
        if composed_mode is not None:
            return str(deprecate_mode(composed_mode[2]))
    return str(deprecate_mode(raw_mode))


def _extract_iteration_from_obj(value: Any) -> int | None:
    """Best-effort parse iteration from chunk/payload/dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("iteration", "iter", "step", "round"):
            if key in value:
                parsed = _extract_iteration_from_obj(value.get(key))
                if parsed is not None:
                    return parsed
        for key in ("metadata", "meta", "extra", "context"):
            nested = value.get(key)
            if isinstance(nested, dict):
                parsed = _extract_iteration_from_obj(nested)
                if parsed is not None:
                    return parsed
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
        return None
    return None


def _extract_iteration_from_chunk(chunk: Any) -> int | None:
    """Extract iteration from stream chunk object."""
    for attr in ("iteration", "iter", "step"):
        if hasattr(chunk, attr):
            parsed = _extract_iteration_from_obj(getattr(chunk, attr, None))
            if parsed is not None:
                return parsed
    payload = getattr(chunk, "payload", None)
    return _extract_iteration_from_obj(payload)
