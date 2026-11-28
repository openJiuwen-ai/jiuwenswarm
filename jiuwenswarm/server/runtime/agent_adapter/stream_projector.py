# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-owned stream projection state, independent from Runtime execution."""

from __future__ import annotations

from typing import Any, Callable


class StreamProjector:
    """Keep response text and A2UI buffering isolated for one consumer stream."""

    def __init__(
        self, *, block_buffer: Any, split_probe: Callable, extend_probe: Callable
    ) -> None:
        self.final_answer_content = ""
        self.final_answer_chunks: list[str] = []
        self.suppress_a2ui_stream = False
        self.a2ui_pending_render_sent = False
        self.a2ui_stream_probe = ""
        self.team_a2ui_blocks = block_buffer
        self.team_a2ui_tasks: dict[tuple[str, str], Any] = {}
        self.team_a2ui_pending_finals: dict[tuple[str, str], dict[str, Any]] = {}
        self._split_probe = split_probe
        self._extend_probe = extend_probe

    @property
    def assistant_message(self) -> str:
        return self.final_answer_content or "".join(self.final_answer_chunks)

    def probe(self, content: str):
        split = self._split_probe(self.a2ui_stream_probe, content)
        self.a2ui_stream_probe = self._extend_probe(self.a2ui_stream_probe, content)
        return split

    def process_team_payload(
        self,
        payload: dict[str, Any],
        schedule: Callable,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Buffer only the member awaiting finalization; let other members pass."""
        event_type = str(payload.get("event_type") or "")
        content = str(payload.get("content") or "")
        key = self.team_a2ui_blocks.key_for(payload)
        if event_type == "chat.final" and key in self.team_a2ui_tasks:
            self.team_a2ui_pending_finals[key] = payload
            return [], None
        decision = self.team_a2ui_blocks.consume(payload, event_type, content)
        if decision is None:
            return [], payload
        direct_payloads = []
        if decision.passthrough:
            direct_payloads.append({**payload, "content": decision.passthrough})
        if decision.replacement is not None:
            return direct_payloads, {**payload, "content": decision.replacement}
        if decision.raw_block:
            schedule(payload, decision)
        return direct_payloads, None if decision.suppress else payload
