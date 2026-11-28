# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-local runtime trace state shared with lightweight call sites.

``signals`` and the periodic-task helpers must be able to ask for the current
recorder without importing :mod:`jiuwenswarm.runtime_trace.lifecycle`, which
would pull in OpenTelemetry, Core, and SQLite. Only the standard library is
imported here.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_recorder: Any | None = None
_settings: Any | None = None


def set_state(recorder: Any, settings: Any) -> None:
    global _recorder, _settings
    with _lock:
        _recorder = recorder
        _settings = settings


def current_recorder() -> Any | None:
    with _lock:
        return _recorder


def current_settings() -> Any | None:
    with _lock:
        return _settings


def clear_state(recorder: Any | None = None) -> None:
    """Forget the recorder; when given, only clear this exact instance."""
    global _recorder, _settings
    with _lock:
        if recorder is not None and _recorder is not recorder:
            return
        _recorder = None
        _settings = None
