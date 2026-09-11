# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable

from .events import EventRecord


_CONFIDENTIALITY_RANK = {"low": 0, "medium": 1, "high": 2}
_INTEGRITY_RANK = {"low": 0, "medium": 1, "high": 2}

_DEFAULT_SENSITIVE_PATTERNS = (
    (
        "api_key",
        r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{12,}",
    ),
    ("private_key", r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    ("credit_card", r"\b(?:\d[ -]*?){13,19}\b"),
    (
        "china_id",
        r"\b[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
    ),
)

_HIGH_ENTROPY_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|ghp|xox[baprs]?|AKIA)[-_A-Za-z0-9]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b"),
)

_SENSITIVE_RESOURCE_PATTERNS = (
    re.compile(
        r"(?i)(?:^|/)(?:\.env(?:\.[^/]+)?|credentials(?:\.json)?|secrets?\.ya?ml)$"
    ),
    re.compile(r"(?i)(?:^|/)\.ssh/(?:id_rsa|id_ed25519|id_ecdsa)$"),
    re.compile(r"(?i)^/(?:etc/(?:shadow|sudoers)|var/run/docker\.sock|dev/mem)$"),
)

_RELEVANT_EVENT_TYPES = {
    "chat_request",
    "memory_before_chat",
    "memory_after_chat",
    "model_call",
    "model_output",
    "system_prompt_build",
    "tool_call",
    "tool_result",
}


@dataclass
class SecurityLabel:
    confidentiality: str = "low"
    integrity: str = "medium"
    sensitive_kinds: list[str] = field(default_factory=list)


@dataclass
class PdgNode:
    node_id: str
    event_id: str
    node_type: str
    label: SecurityLabel
    action_class: str = "none"
    resource_ids: list[str] = field(default_factory=list)
    evidence_fingerprints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PdgEdge:
    source: str
    target: str
    edge_type: str
    reason: str


@dataclass
class ProgramDependenceGraph:
    nodes: list[PdgNode] = field(default_factory=list)
    edges: list[PdgEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [asdict(node) for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
        }


@dataclass(frozen=True)
class PdgViolation:
    rule_id: str
    risk_score: int
    target_node_id: str
    evidence_node_ids: tuple[str, ...]
    finding: str


@dataclass
class PdgInspection:
    risk_score: int = 0
    findings: list[str] = field(default_factory=list)
    violations: list[PdgViolation] = field(default_factory=list)
    graph: ProgramDependenceGraph = field(default_factory=ProgramDependenceGraph)


@dataclass(frozen=True)
class _TraceEvent:
    event_id: str
    timestamp: float
    event_type: str
    subject: str
    payload: dict[str, Any]
    findings: tuple[str, ...]
    prev_event_ids: tuple[str, ...]


@dataclass
class _NodeFacts:
    text: str = ""
    resources: set[str] = field(default_factory=set)
    fingerprints: set[str] = field(default_factory=set)


def _config_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class DataLeakagePDGDetector:
    """Build and inspect a runtime PDG before an outbound tool executes.

    The implementation follows AgentArmor's constructor -> annotator -> inspector
    split, but keeps dependency inference deterministic. Exact secret fingerprints,
    resource identity, tool-call identity, and explicit event order are used as
    proof-bearing edges. Ambiguous natural-language dependencies are not promoted
    to data-flow facts.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.enabled = _config_bool(cfg, "analyze_pdg_data_leakage", True)
        self.max_events = max(5, int(cfg.get("pdg_max_events", 80)))
        self.sensitive_patterns = self._compile_sensitive_patterns(
            cfg.get("sensitive_patterns")
        )
        trusted = cfg.get("pdg_trusted_egress_patterns")
        self.trusted_egress_patterns = self._compile_regex_list(trusted)
        self.egress_tool_patterns = self._compile_regex_list(
            cfg.get("pdg_egress_tool_patterns")
        )

    def inspect(
        self,
        *,
        event_type: str,
        subject: str,
        payload: dict[str, Any],
        history: Iterable[EventRecord],
    ) -> PdgInspection:
        if not self.enabled or event_type != "tool_call":
            return PdgInspection()

        trace = self._trace_with_candidate(
            event_type=event_type,
            subject=subject,
            payload=payload,
            history=history,
        )
        graph = self._build_graph(trace)
        action_id = _node_id("candidate", "tool_action")
        nodes_by_id = {node.node_id: node for node in graph.nodes}
        action = nodes_by_id.get(action_id)
        if action is None or action.action_class != "public_egress":
            return PdgInspection(graph=graph)
        if self._is_trusted_egress(subject, payload):
            return PdgInspection(graph=graph)
        if action.label.confidentiality != "high":
            return PdgInspection(graph=graph)

        upstream_ids = self._upstream_data_nodes(graph, action_id)
        upstream = [
            nodes_by_id[node_id] for node_id in upstream_ids if node_id in nodes_by_id
        ]
        evidence_ids = tuple(sorted({action_id, *upstream_ids}))

        has_staging = any(
            node.node_type == "tool_action"
            and node.action_class == "write"
            and node.event_id != "candidate"
            for node in upstream
        )
        has_sensitive_resource = any(
            "sensitive_resource" in node.label.sensitive_kinds for node in upstream
        )
        has_low_integrity_source = any(
            node.event_id != "candidate"
            and _INTEGRITY_RANK.get(node.label.integrity, 1) < _INTEGRITY_RANK["medium"]
            for node in upstream
        )

        if has_staging:
            rule_id = "pdg-sensitive-data-staging-to-public-egress"
            finding = (
                "pdg:data-leak: staged high-confidentiality data reaches public egress"
            )
            risk_score = 96
        elif has_sensitive_resource:
            rule_id = "pdg-sensitive-resource-to-public-egress"
            finding = "pdg:data-leak: sensitive resource reaches public egress"
            risk_score = 95
        elif has_low_integrity_source:
            rule_id = "pdg-low-integrity-confidential-egress"
            finding = "pdg:data-leak: low-integrity source controls confidential public egress"
            risk_score = 95
        else:
            rule_id = "pdg-high-confidentiality-to-public-egress"
            finding = "pdg:data-leak: high-confidentiality data reaches public egress"
            risk_score = 95

        violation = PdgViolation(
            rule_id=rule_id,
            risk_score=risk_score,
            target_node_id=action_id,
            evidence_node_ids=evidence_ids,
            finding=finding,
        )
        return PdgInspection(
            risk_score=risk_score,
            findings=[
                finding,
                f"pdg:rule:{rule_id}",
                f"pdg:evidence: {len(evidence_ids)} graph nodes linked to {subject or 'tool'}",
            ],
            violations=[violation],
            graph=graph,
        )

    def build_graph(self, events: Iterable[EventRecord]) -> ProgramDependenceGraph:
        trace = [self._trace_event(record) for record in events]
        trace = [event for event in trace if event.event_type in _RELEVANT_EVENT_TYPES]
        trace.sort(key=lambda event: (event.timestamp, event.event_id))
        return self._build_graph(trace[-self.max_events :])

    def _trace_with_candidate(
        self,
        *,
        event_type: str,
        subject: str,
        payload: dict[str, Any],
        history: Iterable[EventRecord],
    ) -> list[_TraceEvent]:
        trace = [self._trace_event(record) for record in history]
        trace = [event for event in trace if event.event_type in _RELEVANT_EVENT_TYPES]
        trace.sort(key=lambda event: (event.timestamp, event.event_id))
        trace = trace[-(self.max_events - 1) :]
        timestamp = trace[-1].timestamp + 0.000001 if trace else 0.0
        trace.append(
            _TraceEvent(
                event_id="candidate",
                timestamp=timestamp,
                event_type=event_type,
                subject=subject,
                payload=payload,
                findings=(),
                prev_event_ids=tuple(event.event_id for event in trace[-5:]),
            )
        )
        return trace

    @staticmethod
    def _trace_event(record: EventRecord) -> _TraceEvent:
        return _TraceEvent(
            event_id=record.event_id,
            timestamp=float(record.timestamp),
            event_type=record.event_type,
            subject=record.subject,
            payload=record.payload,
            findings=tuple(record.findings),
            prev_event_ids=tuple(record.prev_event_ids),
        )

    def _build_graph(self, trace: list[_TraceEvent]) -> ProgramDependenceGraph:
        graph = ProgramDependenceGraph()
        facts: dict[str, _NodeFacts] = {}
        nodes_by_event: dict[str, list[PdgNode]] = {}
        edge_keys: set[tuple[str, str, str, str]] = set()

        for event in trace:
            event_nodes, event_edges, event_facts = self._decompose_event(event)
            graph.nodes.extend(event_nodes)
            facts.update(event_facts)
            nodes_by_event[event.event_id] = event_nodes
            for edge in event_edges:
                self._add_edge(graph, edge_keys, edge)

        for previous, current in zip(trace, trace[1:]):
            previous_nodes = nodes_by_event.get(previous.event_id, [])
            current_nodes = nodes_by_event.get(current.event_id, [])
            if previous_nodes and current_nodes:
                self._add_edge(
                    graph,
                    edge_keys,
                    PdgEdge(
                        previous_nodes[-1].node_id,
                        current_nodes[0].node_id,
                        "control_flow",
                        "runtime_order",
                    ),
                )

        nodes_by_id = {node.node_id: node for node in graph.nodes}
        for event in trace:
            for parent_id in event.prev_event_ids:
                parent_nodes = nodes_by_event.get(parent_id, [])
                current_nodes = nodes_by_event.get(event.event_id, [])
                if parent_nodes and current_nodes:
                    self._add_edge(
                        graph,
                        edge_keys,
                        PdgEdge(
                            parent_nodes[-1].node_id,
                            current_nodes[0].node_id,
                            "control_dependency",
                            "declared_predecessor",
                        ),
                    )

        call_actions: dict[str, PdgNode] = {}
        for event in trace:
            call_id = _tool_call_id(event.payload)
            if not call_id:
                continue
            if event.event_type == "tool_call":
                action = nodes_by_id.get(_node_id(event.event_id, "tool_action"))
                if action is not None:
                    call_actions[call_id] = action
            elif event.event_type == "tool_result":
                action = call_actions.get(call_id)
                observation = nodes_by_id.get(_node_id(event.event_id, "observation"))
                if action is not None and observation is not None:
                    self._add_edge(
                        graph,
                        edge_keys,
                        PdgEdge(
                            action.node_id,
                            observation.node_id,
                            "data_dependency",
                            "tool_result",
                        ),
                    )

        ordered_nodes = [
            node for event in trace for node in nodes_by_event.get(event.event_id, [])
        ]
        node_position = {
            node.node_id: index for index, node in enumerate(ordered_nodes)
        }
        target_types = {"tool_param", "resource"}
        source_types = {
            "user_prompt",
            "system_prompt",
            "llm_response",
            "observation",
            "resource",
        }
        for target in ordered_nodes:
            if target.node_type not in target_types:
                continue
            target_facts = facts.get(target.node_id, _NodeFacts())
            if not target_facts.fingerprints and not target_facts.resources:
                continue
            for source in ordered_nodes[: node_position[target.node_id]]:
                if source.node_type not in source_types:
                    continue
                source_facts = facts.get(source.node_id, _NodeFacts())
                shared_fingerprints = (
                    source_facts.fingerprints & target_facts.fingerprints
                )
                shared_resources = source_facts.resources & target_facts.resources
                if not shared_fingerprints and not shared_resources:
                    continue
                reason = (
                    "secret_identity" if shared_fingerprints else "resource_identity"
                )
                self._add_edge(
                    graph,
                    edge_keys,
                    PdgEdge(source.node_id, target.node_id, "data_dependency", reason),
                )
                if source.label.integrity == "low":
                    target_action = nodes_by_id.get(
                        _node_id(target.event_id, "tool_action")
                    )
                    if target_action is not None:
                        self._add_edge(
                            graph,
                            edge_keys,
                            PdgEdge(
                                source.node_id,
                                target_action.node_id,
                                "control_dependency",
                                "low_integrity_source",
                            ),
                        )

        self._propagate_labels(graph)
        return graph

    def _decompose_event(
        self,
        event: _TraceEvent,
    ) -> tuple[list[PdgNode], list[PdgEdge], dict[str, _NodeFacts]]:
        nodes: list[PdgNode] = []
        edges: list[PdgEdge] = []
        facts: dict[str, _NodeFacts] = {}
        integrity = self._event_integrity(event.event_type)

        if event.event_type == "tool_call":
            params = _tool_params(event.payload)
            action_class = _classify_action(
                event.subject,
                params,
                self.egress_tool_patterns,
            )
            tool_name = PdgNode(
                node_id=_node_id(event.event_id, "tool_name"),
                event_id=event.event_id,
                node_type="tool_name",
                label=SecurityLabel(integrity=integrity),
                action_class=action_class,
            )
            nodes.append(tool_name)
            facts[tool_name.node_id] = self._facts(event.subject, event.findings)

            parameter_nodes: list[PdgNode] = []
            for key, value in _flatten_mapping(params):
                node = self._content_node(
                    event=event,
                    node_type="tool_param",
                    suffix=key,
                    text=_json_text(value),
                    integrity=integrity,
                    action_class=action_class,
                )
                nodes.append(node)
                parameter_nodes.append(node)
                facts[node.node_id] = self._facts(_json_text(value), event.findings)

            action = PdgNode(
                node_id=_node_id(event.event_id, "tool_action"),
                event_id=event.event_id,
                node_type="tool_action",
                label=SecurityLabel(integrity=integrity),
                action_class=action_class,
            )
            nodes.append(action)
            facts[action.node_id] = self._facts(_json_text(params), event.findings)
            edges.append(
                PdgEdge(
                    tool_name.node_id,
                    action.node_id,
                    "control_dependency",
                    "tool_selection",
                )
            )
            for parameter in parameter_nodes:
                edges.append(
                    PdgEdge(
                        parameter.node_id,
                        action.node_id,
                        "data_dependency",
                        "tool_parameter",
                    )
                )

            resources = sorted(_resource_ids(params))
            if action_class in {"read", "write"}:
                for index, resource in enumerate(resources):
                    resource_node = self._content_node(
                        event=event,
                        node_type="resource",
                        suffix=str(index),
                        text=resource,
                        integrity=integrity,
                        action_class=action_class,
                    )
                    nodes.append(resource_node)
                    facts[resource_node.node_id] = self._facts(resource, event.findings)
                    if action_class == "read":
                        edges.append(
                            PdgEdge(
                                resource_node.node_id,
                                action.node_id,
                                "data_dependency",
                                "resource_read",
                            )
                        )
                    else:
                        edges.append(
                            PdgEdge(
                                action.node_id,
                                resource_node.node_id,
                                "data_dependency",
                                "resource_write",
                            )
                        )

            return nodes, edges, facts

        if event.event_type == "tool_result":
            node_type = "observation"
        elif event.event_type in {"model_output", "memory_after_chat"}:
            node_type = "llm_response"
        elif event.event_type == "system_prompt_build":
            node_type = "system_prompt"
        else:
            node_type = "user_prompt"

        text = _json_text(event.payload)
        node = self._content_node(
            event=event,
            node_type=node_type,
            suffix="",
            text=text,
            integrity=integrity,
            action_class="read" if node_type != "llm_response" else "compute",
        )
        nodes.append(node)
        facts[node.node_id] = self._facts(text, event.findings)

        if node_type == "observation":
            for index, resource in enumerate(sorted(_resource_ids(event.payload))):
                resource_node = self._content_node(
                    event=event,
                    node_type="resource",
                    suffix=str(index),
                    text=resource,
                    integrity=integrity,
                    action_class="read",
                )
                nodes.append(resource_node)
                facts[resource_node.node_id] = self._facts(resource, event.findings)
                edges.append(
                    PdgEdge(
                        resource_node.node_id,
                        node.node_id,
                        "data_dependency",
                        "resource_observation",
                    )
                )
        return nodes, edges, facts

    def _content_node(
        self,
        *,
        event: _TraceEvent,
        node_type: str,
        suffix: str,
        text: str,
        integrity: str,
        action_class: str,
    ) -> PdgNode:
        node_facts = self._facts(text, event.findings)
        sensitive_kinds = self._sensitive_kinds(text, event.findings)
        return PdgNode(
            node_id=_node_id(event.event_id, node_type, suffix),
            event_id=event.event_id,
            node_type=node_type,
            label=SecurityLabel(
                confidentiality="high" if sensitive_kinds else "low",
                integrity=integrity,
                sensitive_kinds=sensitive_kinds,
            ),
            action_class=action_class,
            resource_ids=sorted(node_facts.resources),
            evidence_fingerprints=sorted(node_facts.fingerprints),
        )

    def _facts(self, text: str, findings: Iterable[str]) -> _NodeFacts:
        resources = _resource_ids(text)
        fingerprints: set[str] = set()
        for _, pattern in self.sensitive_patterns:
            for match in pattern.finditer(text):
                fingerprints.add(_fingerprint(match.group(0)))
        for pattern in _HIGH_ENTROPY_VALUE_PATTERNS:
            for match in pattern.finditer(text):
                fingerprints.add(_fingerprint(match.group(0)))
        return _NodeFacts(text=text, resources=resources, fingerprints=fingerprints)

    def _sensitive_kinds(self, text: str, findings: Iterable[str]) -> list[str]:
        kinds = {
            finding.split(":", 1)[1]
            for finding in findings
            if finding.startswith("sensitive:") and ":" in finding
        }
        for name, pattern in self.sensitive_patterns:
            if pattern.search(text):
                kinds.add(name)
        if any(pattern.search(text) for pattern in _HIGH_ENTROPY_VALUE_PATTERNS):
            kinds.add("high_entropy_secret")
        if any(_is_sensitive_resource(resource) for resource in _resource_ids(text)):
            kinds.add("sensitive_resource")
        return sorted(kinds)

    @staticmethod
    def _event_integrity(event_type: str) -> str:
        if event_type == "system_prompt_build":
            return "high"
        if event_type in {"chat_request", "memory_before_chat"}:
            return "high"
        if event_type in {"tool_result", "memory_after_chat"}:
            return "low"
        return "medium"

    @staticmethod
    def _add_edge(
        graph: ProgramDependenceGraph,
        edge_keys: set[tuple[str, str, str, str]],
        edge: PdgEdge,
    ) -> None:
        key = (edge.source, edge.target, edge.edge_type, edge.reason)
        if edge.source == edge.target or key in edge_keys:
            return
        edge_keys.add(key)
        graph.edges.append(edge)

    @staticmethod
    def _propagate_labels(graph: ProgramDependenceGraph) -> None:
        nodes = {node.node_id: node for node in graph.nodes}
        dependency_edges = [
            edge for edge in graph.edges if edge.edge_type == "data_dependency"
        ]
        for _ in range(max(1, len(nodes))):
            changed = False
            for edge in dependency_edges:
                source = nodes.get(edge.source)
                target = nodes.get(edge.target)
                if source is None or target is None:
                    continue
                confidentiality = _join_confidentiality(
                    target.label.confidentiality,
                    source.label.confidentiality,
                )
                integrity = _join_integrity(
                    target.label.integrity, source.label.integrity
                )
                kinds = sorted(
                    {*target.label.sensitive_kinds, *source.label.sensitive_kinds}
                )
                if (
                    confidentiality != target.label.confidentiality
                    or integrity != target.label.integrity
                    or kinds != target.label.sensitive_kinds
                ):
                    target.label.confidentiality = confidentiality
                    target.label.integrity = integrity
                    target.label.sensitive_kinds = kinds
                    changed = True
            if not changed:
                break

    @staticmethod
    def _upstream_data_nodes(graph: ProgramDependenceGraph, target_id: str) -> set[str]:
        incoming: dict[str, list[str]] = {}
        for edge in graph.edges:
            if edge.edge_type != "data_dependency":
                continue
            incoming.setdefault(edge.target, []).append(edge.source)
        visited: set[str] = set()
        queue = [target_id]
        while queue and len(visited) < 200:
            current = queue.pop(0)
            for source in incoming.get(current, []):
                if source in visited:
                    continue
                visited.add(source)
                queue.append(source)
        return visited

    def _is_trusted_egress(self, subject: str, payload: dict[str, Any]) -> bool:
        if not self.trusted_egress_patterns:
            return False
        text = subject + "\n" + _json_text(payload)
        return any(pattern.search(text) for pattern in self.trusted_egress_patterns)

    @staticmethod
    def _compile_regex_list(raw: Any) -> list[re.Pattern[str]]:
        if not isinstance(raw, list):
            return []
        compiled: list[re.Pattern[str]] = []
        for value in raw:
            try:
                compiled.append(re.compile(str(value)))
            except re.error:
                continue
        return compiled

    @staticmethod
    def _compile_sensitive_patterns(raw: Any) -> list[tuple[str, re.Pattern[str]]]:
        entries = raw if isinstance(raw, list) else []
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("pattern"):
                continue
            try:
                compiled.append(
                    (
                        str(entry.get("name") or "custom"),
                        re.compile(str(entry["pattern"])),
                    )
                )
            except re.error:
                continue
        if compiled:
            return compiled
        return [
            (name, re.compile(pattern)) for name, pattern in _DEFAULT_SENSITIVE_PATTERNS
        ]


def _join_confidentiality(left: str, right: str) -> str:
    return (
        left
        if _CONFIDENTIALITY_RANK.get(left, 0) >= _CONFIDENTIALITY_RANK.get(right, 0)
        else right
    )


def _join_integrity(left: str, right: str) -> str:
    return (
        left if _INTEGRITY_RANK.get(left, 1) <= _INTEGRITY_RANK.get(right, 1) else right
    )


def _fingerprint(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).lower()
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _node_id(event_id: str, node_type: str, suffix: str = "") -> str:
    if not suffix:
        return f"{event_id}#{node_type}"
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", suffix)[:80]
    return f"{event_id}#{node_type}:{safe_suffix}"


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
    except Exception:
        return repr(value)


def _decode_jsonish(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                return _decode_jsonish(json.loads(stripped), depth + 1)
            except (json.JSONDecodeError, TypeError, ValueError):
                return value
        return value
    if isinstance(value, dict):
        return {
            str(key): _decode_jsonish(item, depth + 1) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_decode_jsonish(item, depth + 1) for item in value]
    return value


def _tool_params(payload: dict[str, Any]) -> dict[str, Any]:
    decoded = _decode_jsonish(payload)
    if not isinstance(decoded, dict):
        return {"value": decoded}
    result: dict[str, Any] = {}
    for key in ("arguments", "params", "tool_args"):
        value = decoded.get(key)
        if isinstance(value, dict):
            result.update(value)
        elif value not in (None, ""):
            result[key] = value
    tool_call = decoded.get("tool_call")
    if isinstance(tool_call, dict):
        arguments = _decode_jsonish(tool_call.get("arguments"))
        if isinstance(arguments, dict):
            result.update(arguments)
        elif arguments not in (None, ""):
            result["tool_call.arguments"] = arguments
    inputs = decoded.get("inputs")
    if isinstance(inputs, dict):
        nested = _tool_params(inputs)
        result.update(nested)
    if result:
        return result
    ignored = {
        "extra",
        "tool_name",
        "tool_call",
        "tool_result",
        "session_id",
        "request_id",
    }
    return {key: value for key, value in decoded.items() if key not in ignored}


def _flatten_mapping(
    value: Any, prefix: str = "", depth: int = 0
) -> list[tuple[str, Any]]:
    if depth >= 5:
        return [(prefix or "value", value)]
    if isinstance(value, dict):
        result: list[tuple[str, Any]] = []
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            result.extend(_flatten_mapping(item, child, depth + 1))
            if len(result) >= 64:
                break
        return result[:64]
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value[:32]):
            child = f"{prefix}[{index}]" if prefix else str(index)
            result.extend(_flatten_mapping(item, child, depth + 1))
        return result[:64]
    return [(prefix or "value", value)]


def _tool_call_id(payload: dict[str, Any]) -> str:
    decoded = _decode_jsonish(payload)
    if not isinstance(decoded, dict):
        return ""
    tool_call = decoded.get("tool_call")
    if isinstance(tool_call, dict) and tool_call.get("id"):
        return str(tool_call["id"])
    inputs = decoded.get("inputs")
    if isinstance(inputs, dict):
        return _tool_call_id(inputs)
    return str(decoded.get("tool_call_id") or "")


def _classify_action(
    subject: str,
    params: dict[str, Any],
    egress_tool_patterns: Iterable[re.Pattern[str]] = (),
) -> str:
    name = subject.lower()
    text = (name + "\n" + _json_text(params)).lower()
    registered_egress = any(pattern.search(name) for pattern in egress_tool_patterns)
    egress_name = any(
        marker in name
        for marker in (
            "send_email",
            "send_http",
            "send_message",
            "send_file",
            "upload",
            "webhook",
            "http_post",
            "http_put",
            "post_request",
            "scp",
            "sftp",
            "rsync",
        )
    )
    egress_command = bool(re.search(r"\b(?:curl|wget|scp|sftp|rsync|ftp)\b", text))
    http_request_egress = bool(
        any(marker in name for marker in ("http", "request", "api"))
        and re.search(
            r"(?:\bpost\b|\bput\b|\bpatch\b|\bupload\b|\bbody\b|\bdata\b)", text
        )
    )
    if registered_egress or egress_name or egress_command or http_request_egress:
        return "public_egress"
    if any(
        marker in name for marker in ("write", "edit", "append", "save", "create_file")
    ):
        return "write"
    if any(
        marker in name
        for marker in ("read", "cat", "grep", "search", "glob", "list_file")
    ):
        return "read"
    if any(
        marker in name
        for marker in ("bash", "shell", "terminal", "python", "execute", "run_command")
    ):
        return "compute"
    return "other"


def _resource_ids(value: Any) -> set[str]:
    text = value if isinstance(value, str) else _json_text(value)
    resources: set[str] = set()
    patterns = (
        r"(?<![A-Za-z0-9:])@?((?:/|\./|\.\./)[A-Za-z0-9_./~+\-]+)",
        r"(?i)(?<![A-Za-z0-9_.-])((?:\.env(?:\.[A-Za-z0-9_-]+)?|credentials(?:\.json)?|secrets?\.ya?ml))(?![A-Za-z0-9_.-])",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            resource = match.group(1).rstrip(".,;:)]}\"'")
            if resource.startswith("//") or "://" in resource:
                continue
            resources.add(resource)
    return resources


def _is_sensitive_resource(resource: str) -> bool:
    normalized = resource.rstrip("/")
    return any(pattern.search(normalized) for pattern in _SENSITIVE_RESOURCE_PATTERNS)
