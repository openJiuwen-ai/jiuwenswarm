# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

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


@dataclass(frozen=True)
class NodeResult:
    output_ref: AssetRef | None = None
    output_refs: list[AssetRef] | None = None
    message: str = ""
