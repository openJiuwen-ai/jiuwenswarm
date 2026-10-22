# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reuse native counters until agent-core provides artifact instance caching."""

from __future__ import annotations

import functools
import os
import threading
from collections import OrderedDict
from pathlib import Path


def install_native_tokenizer_cache() -> None:
    """Share successful, immutable counters across warm-up and context binding.

    Keep model/source metadata in the key and invalidate replaced artifacts.
    Failures are never cached, so a repaired/downloaded tokenizer can recover.
    Only the selector's factory is adapted; the core counter API is unchanged.
    """
    from openjiuwen.core.context_engine.token import tokenizer_selector

    original = tokenizer_selector.NativeTokenizerCounter
    if getattr(original, "jiuwen_instance_cache", False):
        return
    counters: OrderedDict[tuple, object] = OrderedDict()
    lock = threading.Lock()

    @functools.wraps(original)
    def cached_counter(
        tokenizer_path: str | Path,
        *,
        model: str = "",
        tokenizer_model: str | None = None,
        measurement_source: str = "native_tokenizer",
        fallback_reason: str | None = None,
        fallback_tokenizer_model: str | None = None,
    ):
        path = os.path.abspath(tokenizer_path)
        stat = os.stat(path)
        key = (
            os.path.normcase(path), stat.st_dev, stat.st_ino,
            stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size,
            model, tokenizer_model, measurement_source, fallback_reason,
            fallback_tokenizer_model,
        )
        # Warm-up can run concurrently for aliases; load a key only once.
        with lock:
            counter = counters.get(key)
            if counter is not None:
                counters.move_to_end(key)
                return counter
            counter = original(
                path, model=model, tokenizer_model=tokenizer_model,
                measurement_source=measurement_source,
                fallback_reason=fallback_reason,
                fallback_tokenizer_model=fallback_tokenizer_model,
            )
            counters[key] = counter
            while len(counters) > 32:
                counters.popitem(last=False)
            return counter

    cached_counter.jiuwen_instance_cache = True
    tokenizer_selector.NativeTokenizerCounter = cached_counter
