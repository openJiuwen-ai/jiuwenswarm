# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parent-owned live turn layout for the interactive Process CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import StyleAndTextTuples

_MAX_OUTPUT_CHARS = 256 * 1024
FORWARDED_RECEIPT_PREFIX = "JIUWEN_PROCESS_CLI_RECEIPT "


@dataclass(slots=True)
class _Supplement:
    text: str
    status: Literal["pending", "accepted", "rejected", "unknown"] = "pending"


class LiveTurnLayout:
    """Keep turn inputs, model output and the editor in one prompt application."""

    def __init__(self, request_text: str) -> None:
        self.request_text = request_text.strip() or "（空请求）"
        self.supplements: list[_Supplement] = []
        self.output_text = ""
        self._session: PromptSession[str] | None = None

    def bind(self, session: PromptSession[str]) -> None:
        self._session = session

    def add_supplement(self, text: str) -> None:
        self.supplements.append(_Supplement(text=text))
        self.invalidate()

    def apply_receipt(self, status: str) -> None:
        for supplement in self.supplements:
            if supplement.status == "pending":
                supplement.status = (
                    status
                    if status in {"accepted", "rejected", "unknown"}
                    else "unknown"
                )
                self.invalidate()
                return

    def append_output(self, text: str) -> None:
        if not text:
            return
        self.output_text += text.replace("\r", "")
        if len(self.output_text) > _MAX_OUTPUT_CHARS:
            self.output_text = self.output_text[-_MAX_OUTPUT_CHARS:]
        self.invalidate()

    def invalidate(self) -> None:
        session = self._session
        if session is not None and session.app.is_running:
            session.app.invalidate()

    def message(self) -> StyleAndTextTuples:
        parts: StyleAndTextTuples = []
        self._append_multiline(parts, "class:user-prefix", "› ", self.request_text)
        for supplement in self.supplements:
            marker = {
                "pending": "↳",
                "accepted": "✓",
                "rejected": "×",
                "unknown": "!",
            }[supplement.status]
            style = {
                "pending": "class:supplement",
                "accepted": "class:accepted",
                "rejected": "class:rejected",
                "unknown": "class:unknown",
            }[supplement.status]
            self._append_multiline(parts, style, f"  {marker} ", supplement.text)
        parts.append(("", "\n"))
        if self.output_text:
            parts.append(("class:assistant", "• JiuwenSwarm\n"))
            parts.append(("", self.output_text))
            if not self.output_text.endswith("\n"):
                parts.append(("", "\n"))
        else:
            parts.append(("class:processing", "• 正在处理…\n"))
        parts.append(("", "\n"))
        parts.append(("class:input-prefix", "› "))
        return parts

    def final_text(self) -> str:
        """Return the complete turn once, after leaving the live screen."""

        parts: list[str] = [f"› {self.request_text}\n"]
        for supplement in self.supplements:
            marker = {
                "pending": "↳",
                "accepted": "✓",
                "rejected": "×",
                "unknown": "!",
            }[supplement.status]
            parts.append(f"  {marker} {supplement.text}\n")
        parts.append("\n")
        if self.output_text:
            parts.extend(("• JiuwenSwarm\n", self.output_text))
            if not self.output_text.endswith("\n"):
                parts.append("\n")
        return "".join(parts)

    @staticmethod
    def _append_multiline(
        parts: StyleAndTextTuples,
        style: str,
        prefix: str,
        text: str,
    ) -> None:
        lines = text.splitlines() or [""]
        parts.append((style, prefix))
        parts.append(("", lines[0]))
        continuation = " " * len(prefix)
        for line in lines[1:]:
            parts.append(("", f"\n{continuation}{line}"))
        parts.append(("", "\n"))


__all__ = ["FORWARDED_RECEIPT_PREFIX", "LiveTurnLayout"]
