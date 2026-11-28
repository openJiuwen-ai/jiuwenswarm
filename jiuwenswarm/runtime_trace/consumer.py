# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consumer fed by Core's ``SpanRecordProcessor.on_end``.

The callback must be fast and must not touch ``record.raw_json`` or
``record.sequences``: both encode the deferred OTLP payload on first access.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


class _Pending(NamedTuple):
    record: Any
    name: str
    outcome: str | None


class RuntimeRecordSink:
    """Classify one ended span by ``span_id`` and hand it to the writer."""

    def __init__(self, writer: Any, kinds: Any) -> None:
        self._writer = writer
        self._kinds = kinds

    def consume(self, record: Any) -> None:
        try:
            level, name, outcome = self._kinds.take(record.span_id)
            self._writer.offer(_Pending(record, name, outcome), level=level)
        except Exception:  # noqa: BLE001 - consumer failures must stay local
            logger.warning("runtime trace: consumer failed", exc_info=True)
