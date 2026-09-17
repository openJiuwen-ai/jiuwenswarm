# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Output rendering for CLI chat: human-readable, JSON, and JSONL modes."""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from jiuwenswarm.cli._terminal import write_stderr, write_stdout

_logger = logging.getLogger(__name__)

_FORWARD = [" ", "·", "✢", "✳", "✶", "✻", "✽"]
_SPINNER_FRAMES: list[str] = _FORWARD + list(reversed(_FORWARD[1:-1]))

_STALL_THRESHOLD_S = 3.0
_VERB_ROTATE_INTERVAL_S = 5.0
_GAP_THRESHOLD_S = 0.5

_VERBS = [
    "analyzing", "thinking", "planning", "exploring", "searching",
    "reading", "computing", "processing", "generating", "understanding",
    "writing", "compiling", "checking", "optimizing", "learning",
]


class HumanRenderer:
    def __init__(
        self,
        *,
        show_reasoning: bool = False,
        show_tools: bool = False,
        status_writer: Callable[[str], None] | None = None,
        content_writer: Callable[[str], None] | None = None,
    ) -> None:
        self._show_reasoning = show_reasoning
        self._show_tools = show_tools
        self._streamed_text = ""
        self._printed_final = False
        self._loading = False
        self._spinner_idx = 0
        self._start_time = 0.0
        self._last_token_time = 0.0
        self._verb = ""
        self._last_verb_time = 0.0

        self._status_writer = status_writer or self._default_status_writer
        self._content_writer = content_writer or self._default_content_writer

    @staticmethod
    def _default_status_writer(text: str) -> None:
        write_stderr(text)

    @staticmethod
    def _default_content_writer(text: str) -> None:
        write_stdout(text)

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def streamed_text(self) -> str:
        return self._streamed_text

    @property
    def start_time(self) -> float:
        return self._start_time

    @property
    def verb(self) -> str:
        return self._verb

    @property
    def spinner_idx(self) -> int:
        return self._spinner_idx

    def reset_streamed_text(self) -> None:
        """Reset the accumulated streamed text for a new response turn."""
        self._streamed_text = ""

    def clear_loading(self) -> None:
        if not self._loading:
            return
        self._status_writer("\r\033[K")
        self._loading = False

    def ensure_loading(self) -> None:
        if self._loading:
            return
        self._loading = True
        now = time.monotonic()
        self._start_time = now
        self._last_token_time = now
        self._last_verb_time = now
        self._verb = random.choice(_VERBS)

    def tick_spinner(self) -> None:
        if not self._loading:
            return
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)

        now = time.monotonic()
        elapsed = now - self._start_time
        idle_time = now - self._last_token_time

        if now - self._last_verb_time >= _VERB_ROTATE_INTERVAL_S:
            candidates = [v for v in _VERBS if v != self._verb]
            self._verb = random.choice(candidates) if candidates else self._verb
            self._last_verb_time = now

        glyph = _SPINNER_FRAMES[self._spinner_idx]
        if idle_time > _STALL_THRESHOLD_S:
            glyph = f"\033[31m{glyph}\033[0m"

        timer = f"({elapsed:.0f}s)" if elapsed >= 1.0 else ""
        parts = [glyph, self._verb, timer]
        line = " ".join(p for p in parts if p)

        self._status_writer(f"\r{line}\033[K")

    def handle_delta(self, payload: dict[str, Any]) -> None:
        text = payload.get("content", "")
        if isinstance(text, str) and text:
            if self._streamed_text and not self._streamed_text.endswith("\n"):
                gap = time.monotonic() - self._last_token_time
                if gap > _GAP_THRESHOLD_S:
                    self._content_writer("\n\n")
                    self._streamed_text += "\n\n"
            self._last_token_time = time.monotonic()
            self.clear_loading()
            self._content_writer(text)
            self._streamed_text += text

    def handle_reasoning(self, payload: dict[str, Any]) -> None:
        if not self._show_reasoning:
            return
        self._last_token_time = time.monotonic()
        text = payload.get("content", "")
        if isinstance(text, str) and text:
            _logger.info("%s", text)

    def handle_tool_call(self, payload: dict[str, Any]) -> None:
        if not self._show_tools:
            return
        self._last_token_time = time.monotonic()
        name = payload.get("tool_name") or payload.get("name", "?")
        args = payload.get("arguments") or payload.get("input", {})
        arg_str = json.dumps(args, ensure_ascii=False, default=str)
        if len(arg_str) > 120:
            arg_str = arg_str[:117] + "..."
        _logger.info("[tool] %s: %s", name, arg_str)

    def handle_tool_result(self, payload: dict[str, Any]) -> None:
        if not self._show_tools:
            return
        self._last_token_time = time.monotonic()
        name = payload.get("tool_name") or payload.get("name", "?")
        status = payload.get("status", "done")
        _logger.info("[tool] %s -> %s", name, status)

    def handle_final(self, payload: dict[str, Any]) -> None:
        if self._printed_final:
            return
        self._printed_final = True
        self.clear_loading()
        final_text = payload.get("content", "")
        if not isinstance(final_text, str) or not final_text:
            return
        if not self._streamed_text:
            self._content_writer(final_text)
            self._streamed_text = final_text
            return
        # chooseFinalAssistantContent logic (same as TUI frontend):
        # 1) Same or extends streamed → print suffix only
        # 2) Final is subset of streamed → already displayed, keep streamed
        # 3) Completely different → keep the longer one internally;
        #    don't reprint from scratch (terminal can't undo streamed text)
        if final_text == self._streamed_text:
            return
        if final_text.startswith(self._streamed_text):
            suffix = final_text[len(self._streamed_text):]
            self._content_writer(suffix)
            self._streamed_text = final_text
        elif final_text in self._streamed_text:
            # Final is a subset of what was already streamed
            pass
        else:
            # Different format — keep the longer version as canonical
            if len(final_text) >= len(self._streamed_text):
                self._streamed_text = final_text

    def handle_error(self, payload: dict[str, Any]) -> None:
        self.clear_loading()
        error = payload.get("error") or payload.get("message", "unknown error")
        _logger.error("%s", error)


class JsonRenderer:
    def __init__(
        self,
        *,
        content_writer: Callable[[str], None] | None = None,
    ) -> None:
        self._events: list[dict[str, Any]] = []
        self._content_writer = content_writer or self._default_content_writer

    @staticmethod
    def _default_content_writer(text: str) -> None:
        write_stdout(text)

    def handle_event(self, _event_type: str, payload: dict[str, Any]) -> None:
        self._events.append(payload)

    def output(self) -> None:
        final_content = ""
        for evt in self._events:
            if isinstance(evt.get("content"), str) and evt["content"]:
                final_content = evt["content"]
        result: dict[str, Any] = {"ok": True, "content": final_content}
        errors = [e for e in self._events if e.get("error")]
        if errors:
            result["ok"] = False
            result["error"] = errors[-1].get("error", "unknown error")
        self._content_writer(json.dumps(result, ensure_ascii=False, default=str) + "\n")

    def handle_error(self, payload: dict[str, Any]) -> None:
        self._events.append(payload)


class JsonlRenderer:
    def __init__(
        self,
        *,
        content_writer: Callable[[str], None] | None = None,
    ) -> None:
        self._content_writer = content_writer or self._default_content_writer

    @staticmethod
    def _default_content_writer(text: str) -> None:
        write_stdout(text)

    def handle_event(self, event_type: str, payload: dict[str, Any]) -> None:
        frame: dict[str, Any] = {"type": "event", "event": event_type, "payload": payload}
        self._content_writer(json.dumps(frame, ensure_ascii=False, default=str) + "\n")
