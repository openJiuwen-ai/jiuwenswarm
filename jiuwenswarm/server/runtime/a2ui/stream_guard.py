# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming guard for tagged A2UI blocks."""

from __future__ import annotations

from typing import Any

from a2ui.schema.constants import A2UI_CLOSE_TAG, A2UI_OPEN_TAG


class A2UIStreamGuard:
    """Buffers A2UI blocks during streaming until they can be validated."""

    def __init__(self, spec: Any | None = None) -> None:
        if spec is None:
            from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

            spec = get_protocol_spec()
        self._spec = spec
        self._buffer = ""
        self._inside_block = False

    def feed(self, content: str) -> list[str]:
        if not content:
            return []
        self._buffer += content
        return self._drain()

    def finish(self) -> list[str]:
        if not self._buffer:
            return []
        if self._inside_block:
            fallback = self._spec.format_for_text_channel(self._buffer)
            self._buffer = ""
            self._inside_block = False
            return [fallback] if fallback else []
        remaining = self._buffer
        self._buffer = ""
        return [remaining] if remaining else []

    def _drain(self) -> list[str]:
        emitted: list[str] = []
        while self._buffer:
            if not self._inside_block:
                start = self._buffer.find(A2UI_OPEN_TAG)
                if start < 0:
                    # Keep a small suffix in case the tag is split across chunks.
                    keep = max(len(A2UI_OPEN_TAG) - 1, 0)
                    if len(self._buffer) <= keep:
                        break
                    emitted.append(self._buffer[:-keep])
                    self._buffer = self._buffer[-keep:]
                    break
                if start > 0:
                    emitted.append(self._buffer[:start])
                    self._buffer = self._buffer[start:]
                self._inside_block = True

            end = self._buffer.find(A2UI_CLOSE_TAG)
            if end < 0:
                break

            block_end = end + len(A2UI_CLOSE_TAG)
            block = self._buffer[:block_end]
            self._buffer = self._buffer[block_end:]
            self._inside_block = False
            validation = self._spec.validate_response(block)
            if validation.valid:
                emitted.append(block)
            else:
                fallback = self._spec.format_for_text_channel(block)
                if fallback:
                    emitted.append(fallback)
        return [item for item in emitted if item]


__all__ = ["A2UIStreamGuard"]
