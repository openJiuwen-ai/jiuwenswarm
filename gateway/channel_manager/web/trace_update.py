# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-local copy of a committed trajectory watermark.

The AgentServer writer keeps the store model. This process only forwards the
push dict, so the six wire fields live here and the two types are not required
to be the same object.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommittedTraceUpdate:
    """Highest committed revision for one session and trace in a writer batch.

    ``frame_seq`` is a second, independent watermark. Records and stream
    frames advance on their own sequences, so a reader that only ever saw
    ``revision`` would never learn that new frames had landed for a span
    whose record did not change.
    """

    session_id: str
    trace_id: str
    revision: int
    store_epoch: str | None = None
    lifecycle: str = "final"
    frame_seq: int = 0
