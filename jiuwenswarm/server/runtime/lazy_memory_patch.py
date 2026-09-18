# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Defer openjiuwen.core.memory.long_term_memory import to first use.

Ported from the harmony_dev branch (jiuwenclaw.runtime.lazy_memory_patch) as
part of the HarmonyOS HNP deployment support. Verified against openjiuwen
0.1.16: ``openjiuwen/core/memory/__init__.py`` still eagerly imports
``LongTermMemory``, whose chain pulls in the sqlalchemy/alembic-backed store
machinery (~560ms). On OHOS devices this startup cost is significant, so the
AgentServer entrypoint applies this patch before openjiuwen-heavy imports
when running on an OHOS runtime.

This patch pre-registers ``openjiuwen.core.memory`` as a stub package that:
- eagerly loads only the cheap ``config`` submodule
- exposes ``LongTermMemory`` as a module-level attribute that defers the
  heavy chain until first access

Must be called BEFORE any code triggers the real package import
(i.e. very early in app_agentserver.py, before other openjiuwen imports).
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from typing import Any


def apply_lazy_memory_patch() -> None:
    pkg_name = "openjiuwen.core.memory"
    if pkg_name in sys.modules:
        # Already imported - too late to patch safely.
        return

    # Locate the package without triggering its __init__.
    try:
        spec = importlib.util.find_spec(pkg_name)
    except (ImportError, ValueError):
        return
    if spec is None or spec.submodule_search_locations is None:
        return

    # Install an empty stub package so that submodule imports
    # (`openjiuwen.core.memory.config`, etc.) skip running the real __init__.
    stub = types.ModuleType(pkg_name)
    # Mark as a package: __path__ must be a list of strings.
    stub.__path__ = list(spec.submodule_search_locations)
    stub.__spec__ = spec
    stub.__package__ = pkg_name
    stub.__all__ = [
        "MemoryEngineConfig",
        "MemoryScopeConfig",
        "AgentMemoryConfig",
        "LongTermMemory",
    ]

    sys.modules[pkg_name] = stub

    # Eagerly load the cheap config submodule so its classes are accessible
    # via the package. In openjiuwen 0.1.16 ``core.memory.config`` is a
    # package whose __init__ only re-exports the pydantic models defined in
    # ``config/config.py`` - none of the heavy migration machinery.
    config = importlib.import_module(f"{pkg_name}.config")
    stub.MemoryEngineConfig = config.MemoryEngineConfig
    stub.MemoryScopeConfig = config.MemoryScopeConfig
    stub.AgentMemoryConfig = config.AgentMemoryConfig

    # `LongTermMemory` (and any other attribute defined only in the real
    # __init__) is resolved lazily via __getattr__. Once resolved, the
    # value is written into stub.__dict__ so subsequent accesses bypass
    # __getattr__ entirely.
    def __getattr__(name: str) -> Any:
        if name == "LongTermMemory":
            cached = stub.__dict__.get(name)
            if cached is not None:
                return cached
            ltm_module = importlib.import_module(f"{pkg_name}.long_term_memory")
            setattr(stub, name, ltm_module.LongTermMemory)
            return stub.__dict__[name]
        raise AttributeError(
            f"module {pkg_name!r} has no attribute {name!r}: the package is "
            f"served by a lazy stub that skips the real __init__. If "
            f"{name!r} is defined in openjiuwen.core.memory.__init__, add "
            f"it to apply_lazy_memory_patch or import the submodule "
            f"directly (e.g. `from openjiuwen.core.memory.X import ...`)."
        )

    stub.__getattr__ = __getattr__
