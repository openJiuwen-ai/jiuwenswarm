# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run a callback after a named module finishes loading.

The finder itself does not import the target. Handshake code can register a
patch now and pay for BashTool / ReadFileTool / TaskTool only when something
else imports them.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from typing import Any, Callable

PostLoad = Callable[[Any], None]


class _CallbackLoader:
    def __init__(self, inner: Any, callbacks: list[PostLoad]) -> None:
        self._inner = inner
        self._callbacks = list(callbacks)

    def create_module(self, spec: Any) -> Any:
        create = getattr(self._inner, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module: Any) -> None:
        self._inner.exec_module(module)
        for callback in self._callbacks:
            callback(module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _PostImportFinder:
    def __init__(self) -> None:
        self._hooks: dict[str, list[PostLoad]] = {}
        self._finding = False
        self._lock = threading.Lock()

    def add(self, fullname: str, callback: PostLoad) -> None:
        with self._lock:
            callbacks = self._hooks.setdefault(fullname, [])
            if callback not in callbacks:
                callbacks.append(callback)
        existing = sys.modules.get(fullname)
        if existing is not None and getattr(existing, "__spec__", None) is not None:
            # Already finished loading. A partial module sitting in sys.modules
            # during its own import must not be patched twice.
            if fullname in sys.modules:
                callback(existing)

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname not in self._hooks or self._finding:
            return None
        self._finding = True
        try:
            try:
                sys.meta_path.remove(self)
                removed = True
            except ValueError:
                removed = False
            try:
                spec = importlib.util.find_spec(fullname)
            finally:
                if removed:
                    sys.meta_path.insert(0, self)
        finally:
            self._finding = False
        if spec is None or spec.loader is None:
            return None
        spec.loader = _CallbackLoader(spec.loader, self._hooks[fullname])
        return spec


_finder: _PostImportFinder | None = None
_finder_lock = threading.Lock()


def register_post_import_hook(fullname: str, callback: PostLoad) -> None:
    """Call ``callback(module)`` once ``fullname`` has executed."""
    global _finder
    with _finder_lock:
        if _finder is None:
            _finder = _PostImportFinder()
            sys.meta_path.insert(0, _finder)
        finder = _finder
    finder.add(fullname, callback)
