# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loop-local, non-blocking observation of actual shared model use."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol
import weakref

logger = logging.getLogger(__name__)
_observers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class ModelUseObserver(Protocol):
    def note_model_use(self, model: Any) -> bool:
        """Observe an actual model object returned by its resource owner."""


def bind_model_observer(observer: ModelUseObserver) -> None:
    _observers[asyncio.get_running_loop()] = weakref.ref(observer)


def unbind_model_observer(observer: ModelUseObserver) -> None:
    loop = asyncio.get_running_loop()
    reference = _observers.get(loop)
    if reference is not None and reference() is observer:
        _observers.pop(loop, None)


def record_model_use(model: Any) -> None:
    """Optional telemetry must never prevent required execution."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    reference = _observers.get(loop)
    observer = reference() if reference is not None else None
    if observer is not None:
        try:
            observer.note_model_use(model)
        except Exception as exc:  # noqa: BLE001 - an observer is never an execution barrier
            logger.debug("model use observation failed: %s", type(exc).__name__)
