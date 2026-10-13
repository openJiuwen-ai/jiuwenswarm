# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leave native thread stacks before Supervisor times out a stalled Worker."""

from __future__ import annotations

import faulthandler
import time

_STALL_DUMP_TIMEOUT = 25.0  # Before the Supervisor's 30-second health deadline.
_enabled = False
_last_arm = 0.0


def enable_worker_diagnostics() -> None:
    global _enabled
    faulthandler.enable(all_threads=True)
    _enabled = True


def arm_stall_dump() -> None:
    global _last_arm
    now = time.monotonic()
    if _enabled and now - _last_arm >= min(5.0, _STALL_DUMP_TIMEOUT / 2):
        # This native timer still fires when Python's event loop is blocked.
        # Every incoming IPC frame rearms it. A detached resident Worker has
        # no active timer, so normal Front restarts do not create false dumps.
        faulthandler.dump_traceback_later(_STALL_DUMP_TIMEOUT)
        _last_arm = now


def cancel_stall_dump() -> None:
    global _last_arm
    if _enabled:
        faulthandler.cancel_dump_traceback_later()
        _last_arm = 0.0
