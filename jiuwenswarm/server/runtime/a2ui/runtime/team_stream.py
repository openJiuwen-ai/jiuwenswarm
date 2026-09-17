# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member-scoped A2UI buffering for persistent Team streams."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_A2UI_OPEN_TAG = "<a2ui-json>"
_A2UI_CLOSE_TAG = "</a2ui-json>"
_MAX_BLOCK_CHARS = 1_000_000


@dataclass
class TeamA2UIBlockDecision:
    """One member-scoped action produced while reading a Team event."""

    key: tuple[str, str]
    passthrough: str = ""
    raw_block: str = ""
    trailing: str = ""
    finalize_whole_event: bool = False
    replacement: str | None = None
    suppress: bool = False


@dataclass
class _BlockState:
    """Incremental A2UI state for one Team member in one round."""

    pending_open_tag: str = ""
    active_block: str = ""
    replacements: list[tuple[str, str]] = field(default_factory=list)


def _stream_key(payload: dict[str, Any]) -> tuple[str, str]:
    """Return a stable round/member key without sharing state across teammates."""
    round_id = str(payload.get("rid") or "")
    member_id = str(payload.get("member_name") or payload.get("role") or "unknown")
    return round_id, member_id


def _partial_open_tag_suffix(value: str) -> str:
    """Keep a tokenizer-split opening tag from leaking to the client."""
    max_length = min(len(value), len(_A2UI_OPEN_TAG) - 1)
    for length in range(max_length, 1, -1):
        suffix = value[-length:]
        if _A2UI_OPEN_TAG.startswith(suffix):
            return suffix
    return ""


class TeamA2UIBlockBuffer:
    """Buffer only the A2UI block emitted by one Team member."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _BlockState] = {}

    def _state(self, key: tuple[str, str]) -> _BlockState:
        return self._states.setdefault(key, _BlockState())

    @staticmethod
    def key_for(payload: dict[str, Any]) -> tuple[str, str]:
        """Expose the member scope used to coordinate asynchronous repair."""
        return _stream_key(payload)

    def remember_finalized(
            self,
            key: tuple[str, str],
            raw_content: str,
            finalized_content: str,
    ) -> None:
        """Reuse a repaired block when the member repeats it in chat.final."""
        self._state(key).replacements.append((raw_content, finalized_content))

    def consume(
            self,
            payload: dict[str, Any],
            event_type: str,
            content: str,
    ) -> TeamA2UIBlockDecision | None:
        """Inspect one member event and return only member-local stream actions."""
        key = _stream_key(payload)
        state = self._state(key)
        if event_type == "chat.final":
            return self._consume_final(key, state, content)
        if event_type != "chat.delta":
            return None
        return self._consume_delta(key, state, content)

    def _consume_final(
            self,
            key: tuple[str, str],
            state: _BlockState,
            content: str,
    ) -> TeamA2UIBlockDecision | None:
        replaced = content
        unprocessed = content
        for raw_block, finalized_block in state.replacements:
            raw_index = unprocessed.find(raw_block)
            if raw_index >= 0:
                unprocessed = (
                    unprocessed[:raw_index]
                    + unprocessed[raw_index + len(raw_block):]
                )
                replaced = replaced.replace(raw_block, finalized_block, 1)
                continue

            finalized_index = unprocessed.find(finalized_block)
            if finalized_index >= 0:
                unprocessed = (
                    unprocessed[:finalized_index]
                    + unprocessed[finalized_index + len(finalized_block):]
                )

        has_unclosed_block = bool(state.active_block)
        has_new_block = _A2UI_OPEN_TAG in unprocessed
        self._states.pop(key, None)
        if has_unclosed_block or has_new_block:
            return TeamA2UIBlockDecision(
                key=key,
                raw_block=replaced,
                finalize_whole_event=True,
                suppress=True,
            )
        if replaced != content:
            return TeamA2UIBlockDecision(key=key, replacement=replaced)
        return None

    def _consume_delta(
            self,
            key: tuple[str, str],
            state: _BlockState,
            content: str,
    ) -> TeamA2UIBlockDecision | None:
        if state.active_block:
            state.active_block += content
            return self._finish_or_hold(key, state)

        previous_pending = state.pending_open_tag
        candidate = f"{previous_pending}{content}"
        state.pending_open_tag = ""
        open_index = candidate.find(_A2UI_OPEN_TAG)
        if open_index >= 0:
            state.active_block = candidate[open_index:]
            decision = self._finish_or_hold(key, state)
            decision.passthrough = candidate[:open_index]
            return decision

        pending_suffix = _partial_open_tag_suffix(candidate)
        if not pending_suffix:
            if previous_pending:
                return TeamA2UIBlockDecision(
                    key=key,
                    passthrough=candidate,
                    suppress=True,
                )
            return None
        state.pending_open_tag = pending_suffix
        return TeamA2UIBlockDecision(
            key=key,
            passthrough=candidate[:-len(pending_suffix)],
            suppress=True,
        )

    @staticmethod
    def _finish_or_hold(
            key: tuple[str, str],
            state: _BlockState,
    ) -> TeamA2UIBlockDecision:
        close_index = state.active_block.find(_A2UI_CLOSE_TAG)
        if close_index < 0 and len(state.active_block) <= _MAX_BLOCK_CHARS:
            return TeamA2UIBlockDecision(key=key, suppress=True)

        if close_index < 0:
            raw_block = state.active_block
            trailing = ""
        else:
            block_end = close_index + len(_A2UI_CLOSE_TAG)
            raw_block = state.active_block[:block_end]
            trailing = state.active_block[block_end:]
        state.active_block = ""
        return TeamA2UIBlockDecision(
            key=key,
            raw_block=raw_block,
            trailing=trailing,
            suppress=True,
        )


__all__ = ["TeamA2UIBlockBuffer", "TeamA2UIBlockDecision"]
