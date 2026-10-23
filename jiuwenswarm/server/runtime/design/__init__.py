# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""team.design execution layer: graph → swarmflow script → per-node workers.

Replaces the picked designer scheduler (executor.py stays untouched as a
library); the classes here are the only scheduling surface:

* :class:`GraphScriptCompiler` — deterministic graph → script compilation
  under the "frozen-once-produced" rule (completed nodes inline as literals,
  only the requested nodes execute, downstream never enters the script).
* :class:`RunCoordinator` — one run_id per graph for its whole life, direct
  ``run_workflow`` calls, run record + node history persistence.
* :class:`DesignEventBridge` — engine progress events → designer run payload.
"""

from jiuwenswarm.server.runtime.design.context import (
    DesignContextRegistry,
    StoreNodeSpawner,
    get_design_context_registry,
    get_graph_coordinator,
)
from jiuwenswarm.server.runtime.design.event_bridge import DesignEventBridge
from jiuwenswarm.server.runtime.design.graph_compiler import GraphScriptCompiler
from jiuwenswarm.server.runtime.design.run_coordinator import RunCoordinator

__all__ = [
    "DesignContextRegistry",
    "DesignEventBridge",
    "GraphScriptCompiler",
    "RunCoordinator",
    "StoreNodeSpawner",
    "get_design_context_registry",
    "get_graph_coordinator",
]
