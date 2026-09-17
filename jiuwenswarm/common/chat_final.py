# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""chat.final 落地协议字段。

前端优先读 final_mode，避免用字符串 includes 启发式折叠整轮：
- patch_segment：覆写当前/匹配分段（工具打断后的默认路径）
- replace_turn：用 final 替换本轮全部助手气泡（需显式声明）
- append：追加新气泡
"""
from __future__ import annotations

from typing import Any, Mapping, MutableMapping

FINAL_MODE_PATCH_SEGMENT = "patch_segment"
FINAL_MODE_REPLACE_TURN = "replace_turn"
FINAL_MODE_APPEND = "append"


def annotate_chat_final(
    payload: Mapping[str, Any] | None,
    *,
    final_mode: str = FINAL_MODE_PATCH_SEGMENT,
) -> dict[str, Any]:
    """确保 chat.final payload 带 final_mode；已有显式值时不覆盖。"""
    out: dict[str, Any] = dict(payload or {})
    if out.get("event_type") and str(out.get("event_type")).strip() != "chat.final":
        return out
    existing = out.get("final_mode")
    if isinstance(existing, str) and existing.strip():
        return out
    out["final_mode"] = final_mode
    out.setdefault("event_type", "chat.final")
    return out


def ensure_final_mode_inplace(
    payload: MutableMapping[str, Any],
    *,
    final_mode: str = FINAL_MODE_PATCH_SEGMENT,
) -> None:
    """就地写入 final_mode（仅当缺失时）。"""
    if str(payload.get("event_type") or "").strip() not in ("", "chat.final"):
        return
    existing = payload.get("final_mode")
    if isinstance(existing, str) and existing.strip():
        return
    payload["final_mode"] = final_mode
