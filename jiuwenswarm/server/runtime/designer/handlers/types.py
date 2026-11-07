# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

from dataclasses import dataclass
from typing import Callable

from jiuwenswarm.common.schema.designer_graph import (
    AssetRef,
    DesignerExecutionGraph,
    DesignerExecutionRun,
)

ActivityEmitter = Callable[..., None]
PromptArtifactCallback = Callable[[str], None]


@dataclass(frozen=True)
class NodeExecutionContext:
    graph: DesignerExecutionGraph
    run_id: str
    node_id: str
    run: DesignerExecutionRun | None = None
    emit_activity: ActivityEmitter | None = None
    # Fired when this node finalizes the prompt it will send to media tools so
    # downstream leaves can start as soon as that artifact exists.
    on_prompt_artifact: PromptArtifactCallback | None = None
    # Root directory for this node's generated media assets. When set (the
    # design backend points it at the owning team's team-workspace so assets
    # live and age with the team), _media_save_dir writes under it; None
    # falls back to the legacy global designer_media layout.
    media_dir: "Path | None" = None


def thread_snapshot(ctx: NodeExecutionContext) -> NodeExecutionContext:
    """Private copy of ``ctx`` for a worker thread: the loop keeps mutating the live records."""
    return dataclasses.replace(
        ctx,
        graph=copy.deepcopy(ctx.graph),
        run=copy.deepcopy(ctx.run),
        emit_activity=None,
        on_prompt_artifact=None,
    )


@dataclass(frozen=True)
class NodeResult:
    output_ref: AssetRef | None = None
    output_refs: list[AssetRef] | None = None
    message: str = ""
