"""JiuwenSwarm wiring for Core Symphony execution evidence and packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from openjiuwen.harness.rails.evolution import (
    PublishedCapabilitySnapshotProvider,
    SymphonyGraphEvolutionRail,
    TeamSymphonyGraphEvolutionRail,
)


def _build_graph_evolution_rail(
    graph_dir: Path,
    *,
    capture_mode: Literal["agent", "team"],
    model: Any,
    channel_id: Callable[[], str | None],
    trajectory_span_processor: Any = None,
) -> Any:
    """Share evidence wiring while callers retain their model and route ownership."""
    from openjiuwen.extensions.observability.demand import get_trajectory_span_processor
    from jiuwenswarm.runtime.symphony.service import get_swarm_symphony_service

    service = get_swarm_symphony_service()
    runtime = service.runtime()
    mode = capture_mode

    async def submit_evolution(
        planned_graph: dict[str, Any] | None,
        execution_graph: dict[str, Any],
        *,
        session_id: str,
        capture_mode: str,
    ) -> None:
        await service.submit_evolution_and_notify(
            planned_graph,
            execution_graph,
            session_id=session_id,
            capture_mode=mode,
            channel_id=channel_id(),
        )

    rail = (
        TeamSymphonyGraphEvolutionRail
        if mode == "team"
        else SymphonyGraphEvolutionRail
    )
    return rail(
        trajectory_span_processor=trajectory_span_processor
        or get_trajectory_span_processor(),
        graph_snapshot_provider=runtime.capture_graph_snapshot,
        capability_snapshot_provider=PublishedCapabilitySnapshotProvider(graph_dir),
        edge_evaluator_llm=model,
        submit_evolution=submit_evolution,
    )


def _parse_recipe_reference(recipe_id: Any, raw_version: Any) -> tuple[str, int]:
    """Normalize a candidate reference, preserving the Host error reasons."""
    recipe_id = str(recipe_id or "").strip()
    if isinstance(raw_version, bool) or not isinstance(raw_version, (int, str)):
        raise ValueError("invalid_recipe_version")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_recipe_version") from exc
    if not recipe_id or version < 1:
        raise ValueError("invalid_recipe")
    return recipe_id, version

