# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hold a no-todo model reply only while it can still be the canned refusal.

``llm_output`` is written before ``after_model_call``. With an open todo the
close-nudge hold already drops that text. With no todo the chunks are forwarded
immediately, so the refusal sentence and the later notice both land in the
bubble. This scrub keeps only a still-matching short prefix, and lets every
other reply stream at once.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.core.session.stream import OutputSchema

from jiuwenswarm.common.utils import logger

REFUSAL_SCRUB_HOLD = "refusal_scrub_hold"
REFUSAL_SCRUB_DROP = "refusal_scrub_drop"
REFUSAL_SCRUB_RELEASE = "refusal_scrub_release"

CANNED_REFUSAL_MARK = "作为一个人工智能语言模型，我还没学习如何回答这个问题"
CANNED_REFUSAL_MAX_CHARS = 80


def is_refusal_scrub_control(chunk_type: str) -> bool:
    return chunk_type in (
        REFUSAL_SCRUB_HOLD,
        REFUSAL_SCRUB_DROP,
        REFUSAL_SCRUB_RELEASE,
    )


def compact_refusal_text(content: Any) -> str:
    return "".join(str(content or "").split())


def is_canned_refusal_text(content: Any) -> bool:
    compact = compact_refusal_text(content)
    if not compact:
        return False
    if len(compact) > CANNED_REFUSAL_MAX_CHARS:
        return False
    return compact.startswith(CANNED_REFUSAL_MARK)


def could_still_be_canned_refusal(content: Any) -> bool:
    """True while streamed text can still grow into the short canned sentence."""
    compact = compact_refusal_text(content)
    if not compact:
        return True
    if len(compact) > CANNED_REFUSAL_MAX_CHARS:
        return False
    if CANNED_REFUSAL_MARK.startswith(compact):
        return True
    return compact.startswith(CANNED_REFUSAL_MARK)


class RefusalTextScrub:
    """Buffer outer chat deltas that still look like the canned refusal."""

    def __init__(self) -> None:
        self.armed = False
        self._payloads: list[dict[str, Any]] = []
        self._text = ""

    def decide(self, chunk_type: str, *, foreign: bool = False, content: str = "") -> str:
        control = self.on_control(chunk_type)
        if control in ("arm", "drop"):
            return "skip"
        if control == "release":
            return "flush_skip"
        if chunk_type != "llm_output" or foreign or not self.armed:
            return "pass"
        candidate = self._text + content
        if could_still_be_canned_refusal(candidate):
            return "buffer"
        self.armed = False
        return "flush_pass"

    def on_control(self, chunk_type: str) -> str:
        if chunk_type == REFUSAL_SCRUB_HOLD:
            self._clear()
            self.armed = True
            return "arm"
        if chunk_type == REFUSAL_SCRUB_DROP:
            self._clear()
            self.armed = False
            return "drop"
        if chunk_type == REFUSAL_SCRUB_RELEASE:
            self.armed = False
            return "release"
        return ""

    def push(self, payload: dict[str, Any]) -> None:
        self._text += str(payload.get("content") or "")
        self._payloads.append(payload)

    def take(self) -> list[dict[str, Any]]:
        payloads = self._payloads
        self._payloads = []
        self._text = ""
        return payloads

    def _clear(self) -> None:
        self._payloads.clear()
        self._text = ""


class RefusalScrubGate:
    """Write hold / drop / release around one model call that has no todo hold."""

    def __init__(self) -> None:
        self._hold_pending = False
        self._undelivered: str | None = None

    def reset(self) -> None:
        self._hold_pending = False
        self._undelivered = None

    async def arm(self, session: Any) -> None:
        if session is None:
            return
        await self._retry_undelivered(session)
        if self._undelivered or self._hold_pending:
            return
        if await self._write(session, REFUSAL_SCRUB_HOLD):
            self._hold_pending = True

    async def finish(self, session: Any, *, dropped: bool) -> None:
        if session is None:
            self.reset()
            return
        if dropped or self._undelivered == "drop":
            if not self._hold_pending and self._undelivered != "drop":
                return
            wrote = await self._write(session, REFUSAL_SCRUB_DROP)
            self._mark("drop", wrote)
            return
        if not self._hold_pending and self._undelivered != "release":
            return
        wrote = await self._write(session, REFUSAL_SCRUB_RELEASE)
        self._mark("release", wrote)

    async def _retry_undelivered(self, session: Any) -> None:
        kind = self._undelivered
        if kind is None and self._hold_pending:
            kind = "release"
        if kind not in ("drop", "release"):
            return
        stream_type = REFUSAL_SCRUB_DROP if kind == "drop" else REFUSAL_SCRUB_RELEASE
        if await self._write(session, stream_type):
            self.reset()
            return
        self._undelivered = kind

    def _mark(self, kind: str, wrote: bool) -> None:
        if wrote:
            self.reset()
            return
        self._undelivered = kind

    @staticmethod
    async def _write(session: Any, stream_type: str) -> bool:
        try:
            await session.write_stream(
                OutputSchema(type=stream_type, index=0, payload={"result_type": "control"})
            )
        except Exception:  # noqa: BLE001
            logger.warning("[RefusalScrubGate] %s failed", stream_type, exc_info=True)
            return False
        return True
