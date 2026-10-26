# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for the SkillTurbo guided-mode (interactive_ask) flag."""

from __future__ import annotations

from typing import Any, Mapping


def _raw_interactive_ask(source: Mapping[str, Any] | None) -> Any:
    if not isinstance(source, Mapping):
        return None
    return source.get("interactive_ask", source.get("interactiveAsk"))


def extract_interactive_ask(*sources: Mapping[str, Any] | None) -> bool:
    """Guided mode is explicit opt-in only.

    Missing / None means off. Never infer from ``supports_user_interaction``.
    """
    for source in sources:
        raw = _raw_interactive_ask(source)
        if raw is not None:
            return bool(raw)
    return False


def resolve_interactive_ask_from_inputs(inputs: dict[str, Any] | None) -> bool:
    """Read guided-mode flag from SkillTurbo inputs / metadata."""
    if not isinstance(inputs, dict):
        return False
    metadata = inputs.get("metadata")
    return extract_interactive_ask(
        inputs,
        metadata if isinstance(metadata, dict) else None,
    )


def apply_interactive_ask_to_inputs(
    inputs: dict[str, Any] | None,
    raw_interactive: Any,
) -> dict[str, Any]:
    """Copy inputs and stamp ``metadata.interactive_ask``.

    Omitted resume params mean guided mode is off, so a polluted saved True
    from ``supports_user_interaction`` fallback cannot leak into P5.
    """
    merged = dict(inputs or {})
    metadata = merged.get("metadata")
    meta = dict(metadata) if isinstance(metadata, dict) else {}
    meta["interactive_ask"] = bool(raw_interactive)
    merged["metadata"] = meta
    return merged


def resolve_resume_interactive_ask(
    raw_interactive: Any,
    saved_inputs: Mapping[str, Any] | None,
) -> bool:
    """解析 resume 时的 interactive_ask 值，供 ``apply_interactive_ask_to_inputs`` 使用。

    规则：
    - 入站 ``raw_interactive`` 非 None（前端显式传了）：以前端值为准（含 False），
      保持"显式 opt-in"原则。
    - 入站为 None（answers payload 一般不带此字段）：从中断点 ``saved_inputs``
      恢复中断前的引导模式状态，避免 resume 后被强制设为 False、
      让带 preview 的内容确认类 ask_user 误判 skipped，或使引导模式管线在
      resume 阶段退化为非引导。
    - 入站与 saved 都没有：返回 False，``apply_interactive_ask_to_inputs`` 落成 False。
    """
    if raw_interactive is not None:
        return bool(raw_interactive)
    if not isinstance(saved_inputs, Mapping):
        return False
    metadata = saved_inputs.get("metadata")
    return extract_interactive_ask(
        saved_inputs,
        metadata if isinstance(metadata, Mapping) else None,
    )
