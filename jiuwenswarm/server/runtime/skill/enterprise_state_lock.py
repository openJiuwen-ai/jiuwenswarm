# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-process transaction lock for enterprise workspace Skill state."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from weakref import WeakValueDictionary

import portalocker


_LOCK_TIMEOUT_SECONDS = 30.0
_LOCKS_META = threading.Lock()
_PROCESS_LOCKS: "WeakValueDictionary[str, threading.RLock]" = WeakValueDictionary()
_TRANSACTION_DEPTH = threading.local()


def _process_lock_for(state_file: Path) -> tuple[str, threading.RLock]:
    key = os.path.normcase(str(state_file.resolve()))
    with _LOCKS_META:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
    return key, lock


@contextmanager
def enterprise_skill_state_lock(state_file: Path) -> Iterator[bool]:
    """Lock one workspace Skill ledger across threads and AgentServer pods.

    The yielded flag is true only for the outermost transaction in the current
    thread. Callers use it to reload the authoritative JSON once, without
    discarding changes made by nested SkillManager helpers.
    """
    key, process_lock = _process_lock_for(state_file)
    with process_lock:
        depths = getattr(_TRANSACTION_DEPTH, "values", None)
        if depths is None:
            depths = {}
            _TRANSACTION_DEPTH.values = depths
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield False
            finally:
                depths[key] -= 1
            return

        state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file = state_file.with_name(f"{state_file.name}.lock")
        with portalocker.Lock(
            str(lock_file),
            mode="a+",
            timeout=_LOCK_TIMEOUT_SECONDS,
        ):
            depths[key] = 1
            try:
                yield True
            finally:
                depths.pop(key, None)
