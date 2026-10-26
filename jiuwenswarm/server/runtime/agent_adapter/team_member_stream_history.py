# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-member durable buffers for team-mode streaming history.

Team 流里 leader 与各成员的 ``chat.delta`` / ``chat.reasoning`` 交错到达同一条
流。facade 的 durable 缓冲原本只有一份（给 leader 用），成员帧混进去后会出现：

- 成员的分段正文被任意一方的 ``chat.tool_call`` 冲刷成无 ``member_name`` 的
  ``role=assistant`` chat.final，刷新后显示成 leader 气泡；
- leader 的 reasoning 被附到成员正文上（反之亦然）；
- 成员最后一段正文先被 leader 的 tool_call 冲成 assistant 记录，随后成员自己的
  chat.final 又落一条 teammate 记录，形成重复。

这里按成员分桶：成员帧只进自己的桶，只被自己的 tool_call / final 冲刷，落盘时
带上成员身份（``role=teammate`` + ``member_name`` + team 标识）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 成员落盘记录需要保留的身份字段，与成员 chat.tool_call / chat.final 记录一致。
_MEMBER_IDENTITY_KEYS = ("role", "member_name", "team_id", "team_name", "rid")

# 结束成员当前输出段的事件（与 leader 的冲刷边界一致）。
MEMBER_SEGMENT_BOUNDARY_EVENTS = frozenset({
    "chat.tool_call",
    "chat.ask_user_question",
    "harness.activate_interaction",
    "plan.approval_required",
})


def team_member_stream_key(payload: Any) -> str | None:
    """Return the member key of a teammate-originated stream payload, else ``None``."""
    if not isinstance(payload, dict):
        return None
    role = payload.get("role")
    if not isinstance(role, str) or role.strip().lower() != "teammate":
        return None
    member_name = payload.get("member_name")
    if isinstance(member_name, str) and member_name.strip():
        return member_name.strip()
    return "teammate"


@dataclass
class _MemberSegment:
    identity: dict[str, Any] = field(default_factory=dict)
    text_chunks: list[str] = field(default_factory=list)
    text_started_at: float | None = None
    text_order: Any = None
    reasoning_chunks: list[str] = field(default_factory=list)
    reasoning_started_at_ms: float | None = None
    reasoning_updated_at_ms: float | None = None
    reasoning_order: Any = None
    persisted_text: str = ""


@dataclass
class PendingMemberFinal:
    """A teammate text segment ready to be persisted as a ``chat.final`` record."""

    content: str
    started_at: float | None
    extra: dict[str, Any]


class TeamMemberStreamHistory:
    """Buffers teammate deltas/reasoning per member until that member's own boundary."""

    def __init__(self) -> None:
        self._segments: dict[str, _MemberSegment] = {}

    def _segment(self, member_key: str, payload: dict[str, Any]) -> _MemberSegment:
        segment = self._segments.get(member_key)
        if segment is None:
            segment = _MemberSegment()
            self._segments[member_key] = segment
        for key in _MEMBER_IDENTITY_KEYS:
            value = payload.get(key)
            if value not in (None, ""):
                segment.identity[key] = value
        return segment

    def note_delta(self, member_key: str, content: str, payload: dict[str, Any]) -> None:
        """Buffer a teammate ``chat.delta``."""
        segment = self._segment(member_key, payload)
        if segment.text_started_at is None:
            segment.text_started_at = time.time()
            segment.text_order = payload.get("output_order")
        segment.text_chunks.append(content)

    def note_reasoning(self, member_key: str, content: str, payload: dict[str, Any]) -> None:
        """Buffer a teammate ``chat.reasoning`` frame."""
        segment = self._segment(member_key, payload)
        now_ms = time.time() * 1000
        if segment.reasoning_started_at_ms is None:
            segment.reasoning_started_at_ms = now_ms
            segment.reasoning_order = payload.get("output_order")
        segment.reasoning_updated_at_ms = now_ms
        segment.reasoning_chunks.append(content)

    def text_segment_start(self, member_key: str) -> tuple[float | None, Any]:
        """Return ``(started_at, output_order)`` of the member's pending text segment."""
        segment = self._segments.get(member_key)
        if segment is None:
            return None, None
        return segment.text_started_at, segment.text_order

    def attach_reasoning(
        self,
        member_key: str,
        extra_fields: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Move the member's buffered reasoning onto ``extra_fields`` (consumes it)."""
        segment = self._segments.get(member_key)
        if segment is None:
            return extra_fields
        reasoning_text = "".join(segment.reasoning_chunks)
        started_at = segment.reasoning_started_at_ms
        updated_at = segment.reasoning_updated_at_ms
        order = segment.reasoning_order
        segment.reasoning_chunks = []
        segment.reasoning_started_at_ms = None
        segment.reasoning_updated_at_ms = None
        segment.reasoning_order = None
        if not reasoning_text.strip():
            return extra_fields
        merged = dict(extra_fields) if isinstance(extra_fields, dict) else {}
        merged["reasoning_content"] = reasoning_text
        if order:
            merged["reasoning_output_order"] = order
        merged["reasoning_updated_at"] = updated_at or started_at or (time.time() * 1000)
        return merged

    def discard_text(self, member_key: str, *, final_content: str = "") -> None:
        """Drop the pending text; a non-empty member ``chat.final`` supersedes it."""
        segment = self._segments.get(member_key)
        if segment is None:
            return
        segment.text_chunks = []
        segment.text_started_at = None
        segment.text_order = None
        if final_content:
            segment.persisted_text = final_content

    def take_pending_reasoning(self, member_key: str) -> tuple[float | None, dict[str, Any]] | None:
        """Pop standalone reasoning with its first-frame time (ms) and member identity."""
        segment = self._segments.get(member_key)
        if segment is None:
            return None
        started_at = segment.reasoning_started_at_ms
        extra = self.attach_reasoning(member_key, segment.identity) or {}
        if not extra.get("reasoning_content"):
            return None
        return started_at, extra

    def take_pending_final(
        self,
        member_key: str,
        *,
        completed_at: float | None = None,
    ) -> PendingMemberFinal | None:
        """Pop the member's pending text as a teammate ``chat.final`` with its own reasoning."""
        segment = self._segments.get(member_key)
        if segment is None:
            return None
        text = "".join(segment.text_chunks)
        started_at = segment.text_started_at
        order = segment.text_order
        self.discard_text(member_key)
        if not text or text == segment.persisted_text:
            return None
        segment.persisted_text = text
        extra: dict[str, Any] = dict(segment.identity)
        extra.setdefault("role", "teammate")
        if order:
            extra["output_order"] = order
        if completed_at is not None:
            extra["completed_at"] = completed_at
        extra = self.attach_reasoning(member_key, extra) or extra
        return PendingMemberFinal(content=text, started_at=started_at, extra=extra)

    def take_all_pending_finals(self, *, completed_at: float | None = None) -> list[PendingMemberFinal]:
        """Pop every member's pending text (used at user-input boundaries)."""
        pending = []
        for member_key in list(self._segments):
            item = self.take_pending_final(member_key, completed_at=completed_at)
            if item is not None:
                pending.append(item)
        return pending
