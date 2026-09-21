"""JiuwenSwarm adapters for Symphony execution evidence and packages."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any, Callable, Literal, Mapping

from openjiuwen.agent_evolving.trajectory.model import Trajectory
from openjiuwen.extensions.observability import semconv
from openjiuwen.harness.rails.evolution import (  # type: ignore[import-untyped]
    CapabilityIdentity,
    SymphonyGraphEvolutionInput,
    SymphonyGraphEvolutionRail,
    TeamSymphonyGraphEvolutionRail,
    project_symphony_execution_fragments,
)

from jiuwenswarm.symphony.graph_storage import resolve_graph_artifact_dir


# Compatibility for older Core revisions that only recognise the historical
# suffix below. Keep this conversion on detached trajectory copies so runtime
# observations remain authoritative across mixed Core deployments.
_OTEL_ATTRIBUTE_TRUNCATED_SUFFIX = re.compile(
    r"\.\.\.<OTel attribute truncated: ([1-9]\d*) chars omitted>\Z"
)
_TOOL_PAYLOAD_ATTRIBUTE_KEYS = frozenset(
    {
        semconv.GEN_AI_TOOL_CALL_ARGUMENTS,
        semconv.GEN_AI_TOOL_CALL_RESULT,
    }
)


def _core_compatible_truncation_value(value: str) -> str | None:
    match = _OTEL_ATTRIBUTE_TRUNCATED_SUFFIX.search(value)
    if match is None:
        return None
    return f"{value[: match.start()]}...<truncated {match.group(1)} chars>"


def _core_compatible_truncation_trajectory(trajectory: Trajectory) -> Trajectory:
    """Return a detached trajectory whose OTel truncation suffixes Core understands."""

    payload = deepcopy(trajectory.to_otlp())
    changed = False
    for resource_spans in payload.get("resourceSpans", ()):
        if not isinstance(resource_spans, dict):
            continue
        for scope_spans in resource_spans.get("scopeSpans", ()):
            if not isinstance(scope_spans, dict):
                continue
            for span in scope_spans.get("spans", ()):
                if not isinstance(span, dict):
                    continue
                for attribute in span.get("attributes", ()):
                    if (
                        not isinstance(attribute, dict)
                        or attribute.get("key") not in _TOOL_PAYLOAD_ATTRIBUTE_KEYS
                    ):
                        continue
                    encoded = attribute.get("value")
                    if not isinstance(encoded, dict):
                        continue
                    value = encoded.get("stringValue")
                    if not isinstance(value, str):
                        continue
                    normalized = _core_compatible_truncation_value(value)
                    if normalized is None:
                        continue
                    encoded["stringValue"] = normalized
                    changed = True
    return Trajectory.from_otlp(payload) if changed else trajectory


class _SwarmOtelTruncationCompatMixin:
    """Adapt current OTel truncation markers to the pinned Core rail contract."""

    def _capture_quality_issues(
        self,
        trajectory: Trajectory | None,
    ) -> tuple[Mapping[str, object], ...]:
        normalized = (
            _core_compatible_truncation_trajectory(trajectory)
            if trajectory is not None
            else None
        )
        capture_quality_issues = getattr(super(), "_capture_quality_issues")
        return capture_quality_issues(normalized)

    async def _prepare_evolution_input(
        self,
        trajectory: Trajectory,
        ctx: Any,
    ) -> SymphonyGraphEvolutionInput | None:
        prepare_evolution_input = getattr(super(), "_prepare_evolution_input")
        prepared = await prepare_evolution_input(trajectory, ctx)
        if prepared is None:
            return None
        if not isinstance(prepared, SymphonyGraphEvolutionInput):
            raise TypeError(
                "Core Symphony rail returned an incompatible evolution input"
            )

        normalized_trajectory = _core_compatible_truncation_trajectory(
            prepared.trajectory
        )
        normalized_continuities = tuple(
            (index, _core_compatible_truncation_trajectory(item))
            for index, item in prepared.execution_continuities
        )
        if normalized_trajectory is prepared.trajectory and all(
            normalized is original
            for (_, normalized), (_, original) in zip(
                normalized_continuities,
                prepared.execution_continuities,
                strict=True,
            )
        ):
            return prepared

        fragments = project_symphony_execution_fragments(
            normalized_continuities,
            team_members_only=prepared.capture_mode == "team",
        )
        return replace(
            prepared,
            trajectory=normalized_trajectory,
            execution_continuities=normalized_continuities,
            execution_fragments=fragments,
        )


class _SwarmSymphonyGraphEvolutionRail(
    _SwarmOtelTruncationCompatMixin,
    SymphonyGraphEvolutionRail,
):
    """Single-agent graph rail compatible with current OTel truncation."""


class _SwarmTeamSymphonyGraphEvolutionRail(
    _SwarmOtelTruncationCompatMixin,
    TeamSymphonyGraphEvolutionRail,
):
    """Team graph rail compatible with current OTel truncation."""


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
    from jiuwenswarm.symphony.service import get_swarm_symphony_service

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
        _SwarmTeamSymphonyGraphEvolutionRail
        if mode == "team"
        else _SwarmSymphonyGraphEvolutionRail
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


class PublishedCapabilitySnapshotProvider:
    """Freeze identities from the currently published Symphony graph version."""

    def __init__(self, graph_dir: str | Path) -> None:
        self._graph_dir = Path(graph_dir)

    def snapshot_capabilities(self) -> tuple[CapabilityIdentity, ...]:
        artifact_dir = resolve_graph_artifact_dir(self._graph_dir)
        graph = _read_json_object(artifact_dir / "graph.json")
        identities: list[CapabilityIdentity] = []
        capabilities = graph.get("capabilities")
        for raw in capabilities if isinstance(capabilities, list) else ():
            if not isinstance(raw, dict):
                continue
            capability_type = _snapshot_text(
                raw.get("capability_type") or raw.get("type")
            )
            capability_id = _snapshot_text(raw.get("capability_id") or raw.get("id"))
            capability_name = _snapshot_text(raw.get("name") or capability_id)
            version = _snapshot_text(raw.get("version"))
            description = _snapshot_optional_text(raw.get("description"))
            inputs = _snapshot_ports(raw.get("inputs"))
            outputs = _snapshot_ports(raw.get("outputs"))
            if (
                capability_type not in {"skill", "tool", "subagent"}
                or not capability_id
                or not capability_name
                or not version
                or description is None
                or inputs is None
                or outputs is None
            ):
                continue
            identities.append(
                CapabilityIdentity(
                    capability_id=capability_id,
                    capability_type=capability_type,
                    capability_name=capability_name,
                    version=version,
                    description=description,
                    inputs=inputs,
                    outputs=outputs,
                )
            )
        return tuple(
            sorted(
                identities, key=lambda item: (item.capability_type, item.capability_id)
            )
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _snapshot_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value == value.strip() else ""


def _snapshot_optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return ""
    return _snapshot_text(value) or None


def _snapshot_ports(value: Any) -> tuple[Mapping[str, Any], ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    ports: list[Mapping[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        name = _snapshot_text(raw.get("name"))
        port_type = _snapshot_text(raw.get("type"))
        if not name or not port_type:
            return None
        port: dict[str, Any] = {"name": name, "type": port_type}
        if "required" in raw:
            if not isinstance(raw["required"], bool):
                return None
            port["required"] = raw["required"]
        description = _snapshot_optional_text(raw.get("description"))
        if description is None:
            return None
        if description:
            port["description"] = description
        ports.append(port)
    return tuple(ports)


__all__ = [
    "PublishedCapabilitySnapshotProvider",
]
