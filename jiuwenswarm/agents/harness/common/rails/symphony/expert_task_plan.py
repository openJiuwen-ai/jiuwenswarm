# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task and handoff state for one returned expert plan in the current turn."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from typing import Any

from openjiuwen.symphony import IONameVocabulary


TASK_PLAN_PROVIDER = "_symphony_task_plan_provider"
MAX_PARALLEL_TASKS = 5
_PORT_NAMES = IONameVocabulary.empty()


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected non-empty text in expert task plan")
    return value


def _ports(ports: list[dict[str, Any]]) -> dict[str, str]:
    names = [_text(port["name"]) for port in ports]
    result = {_PORT_NAMES.resolve(name).token: name for name in names}
    if "" in result or len(result) != len(names):
        raise ValueError("Expert task port names must be unique")
    return result


@dataclass(frozen=True)
class ExpertTask:
    task: str
    role: str
    description: str
    expert_id: str | None
    inputs: dict[str, str]
    outputs: dict[str, str]
    external_input_names: tuple[str, ...] = ()

    @classmethod
    def from_node(cls, node: dict[str, Any]) -> ExpertTask:
        metadata = node["metadata"]
        if metadata["type"] == "inline":
            role = _text(metadata["role"]["name"])
            description = _text(metadata["role"]["description"])
            expert_id = None
        elif metadata["type"] == "expert":
            expert_id = _text(metadata["expert_id"])
            role, description = expert_id, ""
        else:
            raise ValueError("Unknown expert task type")
        outputs = _ports(metadata["outputs"])
        if not outputs:
            raise ValueError("Expert tasks must declare outputs")
        external_inputs = metadata.get("external_inputs", [])
        if not isinstance(external_inputs, list):
            raise ValueError("Expert task external inputs must be an array of names")
        return cls(
            _text(metadata["task"]), role, description, expert_id, _ports(metadata["inputs"]), outputs,
            tuple(dict.fromkeys(_text(name) for name in external_inputs)),
        )

    def query(
        self,
        request: str,
        inputs: dict[str, Any],
        external_inputs: dict[str, dict[str, str]],
    ) -> str:
        assignment = {
            "user_request": request,
            "inputs": inputs,
            "role": self.role,
            "role_description": self.description,
            "task": self.task,
            "outputs": list(self.outputs.values()),
        }
        if external_inputs:
            assignment["external_inputs"] = external_inputs
        return "Complete the assigned task described below.\n\n" + json.dumps(assignment, ensure_ascii=False)

    def read_outputs(self, output: object) -> dict[str, Any]:
        if not isinstance(output, str):
            raise ValueError("Expected a JSON object with named outputs")
        text = output.strip()
        if text.startswith(("```json\n", "```\n")) and text.endswith("```"):
            text = text.split("\n", 1)[1].removesuffix("```").strip()
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError):
            raise ValueError("Expected a JSON object with named outputs") from None
        return self.validate_outputs(payload.get("outputs") if isinstance(payload, dict) else None)

    def validate_outputs(self, outputs: object) -> dict[str, Any]:
        if not isinstance(outputs, dict):
            raise ValueError("Expected a JSON object with named outputs")
        try:
            json.dumps(outputs, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Expected a JSON object with named outputs") from None
        missing = [name for name in self.outputs.values() if name not in outputs or outputs[name] is None]
        if missing:
            raise ValueError(f"Missing declared outputs: {', '.join(missing)}")
        return {name: outputs[name] for name in self.outputs.values()}


@dataclass(frozen=True)
class _Handoff:
    source: str
    output: str
    input: str


@dataclass
class ExpertTaskPlan:
    id: str
    request: str
    nodes: dict[str, ExpertTask]
    incoming: dict[str, list[_Handoff]]
    downstream: dict[str, set[str]]
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    planning_inputs: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_result(
        cls,
        result: Mapping[str, Any],
        request: str,
        *,
        planning_inputs: Sequence[dict[str, str]] = (),
    ) -> ExpertTaskPlan | None:
        """Read the composer's graph and attach its host-validated inputs."""
        try:
            graph = result["planned_graph"]["graph"]
            metadata = graph["metadata"]
            if result.get("success") is not True or graph["type"] != "planned_graph" or graph["directed"] is not True:
                return None
            plan_type = (metadata.get("status"), metadata.get("capability_type"), metadata.get("plan_kind"))
            if plan_type != ("ready", "expert", "expert_tasks"):
                return None
            nodes = {_text(node_id): ExpertTask.from_node(node) for node_id, node in graph["nodes"].items()}
            if not nodes:
                return None
            incoming = {node_id: [] for node_id in nodes}
            downstream = {node_id: set() for node_id in nodes}
            order = TopologicalSorter({node_id: () for node_id in nodes})
            for edge in graph["edges"]:
                if edge["relation"] != "can_feed":
                    return None
                source, target = edge["source"], edge["target"]
                output = nodes[source].outputs[_PORT_NAMES.resolve(edge["source_output"]).token]
                input_name = nodes[target].inputs[_PORT_NAMES.resolve(edge["target_input"]).token]
                incoming[target].append(_Handoff(source, output, input_name))
                downstream[source].add(target)
                order.add(target, source)
            order.prepare()
            return cls(
                _text(graph["id"]), request, nodes, incoming, downstream,
                planning_inputs={item["name"]: dict(item) for item in planning_inputs},
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def claim(self, node_id: str) -> dict[str, Any]:
        """Claim a node before awaiting anything, or return its current status."""
        if node_id not in self.nodes:
            return {"status": "rejected", "reason": "Unknown node in this plan"}
        if node_id in self.results:
            return self.results[node_id]
        waiting = list(dict.fromkeys(
            edge.source for edge in self.incoming[node_id]
            if self.results.get(edge.source, {}).get("status") != "completed"
        ))
        if waiting:
            return {"status": "waiting", "reason": "Upstream tasks are pending", "waiting_for": waiting}
        if sum(result["status"] == "running" for result in self.results.values()) >= MAX_PARALLEL_TASKS:
            return {"status": "waiting", "reason": "Five plan nodes are already running"}
        self.results[node_id] = {"status": "running"}
        return {}

    def inputs_for(self, node_id: str) -> dict[str, Any]:
        inputs: dict[str, list[Any]] = {}
        for edge in self.incoming[node_id]:
            inputs.setdefault(edge.input, []).append(self.results[edge.source]["outputs"][edge.output])
        return {name: values[0] if len(values) == 1 else values for name, values in inputs.items()}

    def query_for(self, node_id: str) -> str:
        node = self.nodes[node_id]
        external_inputs = {
            name: self.planning_inputs[name]
            for name in node.external_input_names
            if name in self.planning_inputs
        }
        return node.query(self.request, self.inputs_for(node_id), external_inputs)

    def finish(self, node_id: str, result: dict[str, Any]) -> None:
        self.results[node_id] = result
        if result["status"] == "completed":
            return
        pending = list(self.downstream[node_id])
        while pending:
            target = pending.pop()
            if target not in self.results:
                self.results[target] = {"status": "blocked", "reason": f"Upstream task {node_id} did not complete"}
                pending.extend(self.downstream[target])
