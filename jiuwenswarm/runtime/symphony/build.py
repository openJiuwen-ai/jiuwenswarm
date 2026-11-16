"""Host scheduling and application artifacts around Core-owned graph builds."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from openjiuwen.symphony import SymphonyRuntime
from jiuwenswarm.common.symphony_config import SymphonyConfig
from jiuwenswarm.runtime.symphony.adapter import model_from_config
from jiuwenswarm.runtime.symphony.llm import LLMConfig, get_llm_token_usage_summary, reset_llm_token_usage


@dataclass(frozen=True)
class GraphStatus:
    success: bool
    graph_dir: str
    exists: bool
    stale: bool
    skill_count: int
    changed_count: int
    added_count: int
    removed_count: int
    resume_available: bool = False
    checkpoint_dir: str = ""
    detail: str = ""
    enabled: bool = True
    build_id: str = ""
    version: str = ""
    expert_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphBuildResult:
    success: bool
    graph_dir: str
    skill_count: int
    reused_count: int
    extracted_count: int
    removed_count: int
    edge_count: int
    diagnostics_count: int
    relation_reused_count: int = 0
    relation_resolved_count: int = 0
    version: str = ""
    detail: str = ""
    build_id: str = ""
    expert_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def graph_status(config: SymphonyConfig, *, llm_config: LLMConfig | None = None) -> GraphStatus:
    runtime = SymphonyRuntime(model_from_config(llm_config) if llm_config else None, config)
    status = runtime.orchestration.status()
    counts = status.counts
    return GraphStatus(
        success=True, graph_dir=str(Path(config.storage.artifact_root) / "graph"),
        exists=status.exists, stale=not status.fresh,
        skill_count=counts.get("skill_count", counts.get("capability_count", 0)),
        expert_count=counts.get("expert_count", 0),
        changed_count=counts.get("changed_count", 0), added_count=counts.get("added_count", 0),
        removed_count=counts.get("removed_count", 0), enabled=status.enabled,
        build_id=status.build_id, version=status.version or "",
        detail="graph is fresh" if status.fresh else "Symphony graph requires a build",
    )


async def build_graph(config: SymphonyConfig, llm_config: LLMConfig, *, force: bool = False,
                      build_log: Callable[..., None] | None = None) -> GraphBuildResult:
    runtime = SymphonyRuntime(model_from_config(llm_config), config)
    reset_llm_token_usage()

    def progress(event):
        if build_log is not None:
            build_log(
                str(event.get("event") or "graph.progress"),
                **{key: value for key, value in event.items() if key != "event"},
            )

    def prepare_artifact(version_dir: Path):
        # Application metadata is complete before Core atomically publishes the version.
        (version_dir / "llm_usage.json").write_text(
            json.dumps(get_llm_token_usage_summary(), ensure_ascii=False),
            encoding="utf-8",
        )

    result = None
    try:
        result = await runtime.orchestration.build(force=force, progress=progress, prepare_artifact=prepare_artifact)
        counts = result.counts
        return GraphBuildResult(
            success=True, graph_dir=str(Path(config.storage.artifact_root) / "graph"),
            skill_count=counts.get("skill_count", counts.get("capability_count", 0)),
            expert_count=counts.get("expert_count", 0), reused_count=counts.get("reused_count", 0),
            extracted_count=counts.get("extracted_count", 0), removed_count=counts.get("removed_count", 0),
            edge_count=counts.get("edge_count", 0), diagnostics_count=counts.get("diagnostics_count", 0),
            relation_reused_count=counts.get("relation_reused_count", 0),
            relation_resolved_count=counts.get("relation_resolved_count", 0),
            version=result.version, build_id=result.build_id,
        )
    finally:
        try:
            await runtime.aclose()
        except asyncio.CancelledError:
            # Closing is shared and shielded inside Runtime. A published build
            # remains successful even when the host cancels its notification tail.
            await runtime.aclose()
            if result is None:
                raise
