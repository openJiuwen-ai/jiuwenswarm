# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic expert inventory, graph construction, and team mining.

This module is deliberately independent from Symphony's Skill graph runtime.  It
adapts the expert-package contracts that Xiaoyi Work already owns into a small,
versioned projection suitable for RPC/UI consumers.  Precise ``can_feed`` edges
come only from declared I/O contracts, but every graph relation remains routing
evidence: the team leader decides the actual members and task DAG per query.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import mimetypes
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jiuwenswarm.server.runtime.expert.expert_store import (
    ExpertNotFound,
    ExpertPackageSource,
    ExpertRepoUnavailable,
    ExpertSummary,
    InvalidExpertPackage,
    validate_expert_package,
)
from jiuwenswarm.server.runtime.expert.skill_contract import inspect_skill_contracts
from jiuwenswarm.server.runtime.expert.team_contract import (
    EXPERT_TEAM_DISPATCH_CONTRACT,
    EXPERT_TEAM_MATERIALIZER_VERSION,
)

INVENTORY_SCHEMA_VERSION = "xiaoyi.expert-inventory.v1"
GRAPH_SCHEMA_VERSION = "xiaoyi.expert-graph.v1"
TEAM_CANDIDATE_SCHEMA_VERSION = "xiaoyi.expert-team-candidates.v2"

_CURRENT_AGENT_PACKAGE_TYPE = "agent_template"
_TEAM_PACKAGE_TYPE = "agent_group"
_EXECUTABLE_STATUS = "available"
_PORT_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", re.IGNORECASE)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any, *, length: int = 64) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:length]


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    values: list[str] = []
    for item in value:
        text = str(item).strip() if isinstance(item, (str, int, float)) else ""
        if text and text not in values:
            values.append(text)
    return tuple(values)


def _localized_text(value: Any) -> str:
    """Flatten legacy multilingual display fields with a deterministic locale order."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for locale in ("zh", "cn", "zh-CN", "en"):
            candidate = value.get(locale)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for locale in sorted(value):
            candidate = value[locale]
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _summary_value(
    summary: ExpertSummary | Mapping[str, Any] | None, key: str, default: Any = None
) -> Any:
    if summary is None:
        return default
    if isinstance(summary, Mapping):
        return summary.get(key, default)
    return getattr(summary, key, default)


def _normalize_media_type(value: Any, *, port_id: str = "") -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "json": "application/json",
        "html": "text/html",
        "csv": "text/csv",
        "markdown": "text/markdown",
        "md": "text/markdown",
        "text": "text/plain",
        "plain": "text/plain",
        "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
    }
    if raw in aliases:
        return aliases[raw]
    if raw:
        return raw
    suffix = Path(port_id).suffix.lower()
    if suffix:
        guessed, _ = mimetypes.guess_type(f"artifact{suffix}")
        return guessed or ""
    return ""


def _normalize_schema(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping) or isinstance(value, list):
        return _canonical_json(value)
    return str(value).strip()


def _tokens(*values: str) -> set[str]:
    result: set[str] = set()
    for value in values:
        text = str(value or "").lower()
        for token in _PORT_TOKEN_RE.findall(text):
            if len(token) > 1:
                result.add(token)
        # CJK phrases need small n-grams to match e.g. “经营洞察” and “洞察报告”.
        for phrase in re.findall(r"[\u4e00-\u9fff]{3,}", text):
            result.update(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return result


@dataclass(frozen=True)
class ExpertPort:
    id: str
    media_type: str = ""
    schema: str = ""
    description: str = ""
    primary: bool = False
    visibility: str = "internal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mediaType": self.media_type,
            "schema": self.schema,
            "description": self.description,
            "primary": self.primary,
            "visibility": self.visibility,
        }


def _normalize_ports(value: Any) -> tuple[ExpertPort, ...]:
    if not isinstance(value, list):
        return ()
    ports: list[ExpertPort] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            payload: dict[str, Any] = {"id": item}
        elif isinstance(item, Mapping):
            payload = dict(item)
        else:
            continue
        port_id = str(
            payload.get("id")
            or payload.get("name")
            or payload.get("artifact")
            or f"port-{index + 1}"
        ).strip()
        media_type = _normalize_media_type(
            payload.get("mediaType")
            or payload.get("media_type")
            or payload.get("mimeType")
            or payload.get("mime_type")
            or payload.get("type"),
            port_id=port_id,
        )
        schema = _normalize_schema(
            payload.get("schema") or payload.get("schemaId") or payload.get("schema_id")
        )
        key = (port_id, media_type, schema)
        if not port_id or key in seen:
            continue
        seen.add(key)
        ports.append(
            ExpertPort(
                id=port_id,
                media_type=media_type,
                schema=schema,
                description=str(payload.get("description") or "").strip(),
                primary=bool(payload.get("primary", False)),
                visibility=str(payload.get("visibility") or "internal").strip(),
            )
        )
    return tuple(ports)


@dataclass(frozen=True)
class ExpertDescriptor:
    id: str
    name: str
    description: str
    type: str
    source: str
    status: str
    reusable: bool
    tags: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    inputs: tuple[ExpertPort, ...] = ()
    outputs: tuple[ExpertPort, ...] = ()
    content_hash: str = ""
    package_schema: str = ""
    utility_skills: tuple[str, ...] = ()
    quick_prompts: tuple[str, ...] = ()
    deliverables: tuple[str, ...] = ()
    complements_with: tuple[str, ...] = ()
    overlaps_with: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()

    def to_node_dict(self) -> dict[str, Any]:
        """Return the stable frontend graph-node protocol (no package paths)."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "source": self.source,
            "status": self.status,
            "reusable": self.reusable,
            "tags": list(self.tags),
            "skills": list(self.skills),
            "inputs": [port.to_dict() for port in self.inputs],
            "outputs": [port.to_dict() for port in self.outputs],
        }

    def to_inventory_dict(self) -> dict[str, Any]:
        value = self.to_node_dict()
        value.update(
            {
                "contentHash": self.content_hash,
                "packageSchema": self.package_schema,
                "utilitySkills": list(self.utility_skills),
                "quickPrompts": list(self.quick_prompts),
                "deliverables": list(self.deliverables),
                "relationships": {
                    "complementsWith": list(self.complements_with),
                    "overlapsWith": list(self.overlaps_with),
                    "conflictsWith": list(self.conflicts_with),
                },
            }
        )
        return value


@dataclass(frozen=True)
class ExpertInventorySnapshot:
    created_at: str
    source_hash: str
    experts: tuple[ExpertDescriptor, ...]
    inventory_id: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for expert in self.experts:
            status_counts[expert.status] = status_counts.get(expert.status, 0) + 1
            type_counts[expert.type] = type_counts.get(expert.type, 0) + 1
        return {
            "schemaVersion": INVENTORY_SCHEMA_VERSION,
            "inventoryId": self.inventory_id,
            "createdAt": self.created_at,
            "sourceHash": self.source_hash,
            "experts": [expert.to_inventory_dict() for expert in self.experts],
            "stats": {
                "expertCount": len(self.experts),
                "reusableCount": sum(expert.reusable for expert in self.experts),
                "statusCounts": dict(sorted(status_counts.items())),
                "typeCounts": dict(sorted(type_counts.items())),
            },
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ExpertGraphEdge:
    id: str
    source: str
    target: str
    type: str
    confidence: float
    evidence: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "type": self.type,
            "confidence": self.confidence,
            "evidence": [dict(item) for item in self.evidence],
        }


@dataclass(frozen=True)
class ExpertGraphSnapshot:
    graph_id: str
    created_at: str
    source_hash: str
    nodes: tuple[ExpertDescriptor, ...]
    edges: tuple[ExpertGraphEdge, ...]

    def to_dict(self) -> dict[str, Any]:
        edge_counts: dict[str, int] = {}
        for edge in self.edges:
            edge_counts[edge.type] = edge_counts.get(edge.type, 0) + 1
        return {
            "schemaVersion": GRAPH_SCHEMA_VERSION,
            "graphId": self.graph_id,
            "createdAt": self.created_at,
            "sourceHash": self.source_hash,
            "nodes": [node.to_node_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "stats": {
                "nodeCount": len(self.nodes),
                "edgeCount": len(self.edges),
                "reusableNodeCount": sum(node.reusable for node in self.nodes),
                "teamNodeCount": sum(node.type == "team" for node in self.nodes),
                "edgeTypeCounts": dict(sorted(edge_counts.items())),
            },
        }


@dataclass(frozen=True)
class ExpertTeamCandidate:
    id: str
    name: str
    description: str
    member_ids: tuple[str, ...]
    leader_id: str
    workflow: tuple[dict[str, Any], ...]
    score: float
    score_breakdown: dict[str, float]
    quick_prompts: tuple[str, ...]
    deliverables: tuple[str, ...]
    routing_policy: dict[str, Any]
    member_profiles: tuple[dict[str, Any], ...]
    route_examples: tuple[dict[str, Any], ...]
    status: str = "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "memberIds": list(self.member_ids),
            "leaderId": self.leader_id,
            "workflow": [dict(step) for step in self.workflow],
            "score": self.score,
            "scoreBreakdown": dict(self.score_breakdown),
            "quickPrompts": list(self.quick_prompts),
            "deliverables": list(self.deliverables),
            "routingPolicy": dict(self.routing_policy),
            "memberProfiles": [dict(profile) for profile in self.member_profiles],
            "routeExamples": [dict(route) for route in self.route_examples],
            "status": self.status,
        }


def _package_hash(package_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        relative = path.relative_to(package_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(str(path.readlink()).encode("utf-8"))
        else:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _collaboration_metadata(
    manifest: Mapping[str, Any], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    nested = metadata.get("collaboration")
    if isinstance(nested, Mapping):
        return dict(nested)
    top_level = manifest.get("collaboration")
    return dict(top_level) if isinstance(top_level, Mapping) else {}


def _relation_ids(
    collaboration: Mapping[str, Any], camel: str, snake: str
) -> tuple[str, ...]:
    return _as_string_list(collaboration.get(camel) or collaboration.get(snake))


def normalize_expert_manifest(
    package_dir: Path,
    *,
    summary: ExpertSummary | Mapping[str, Any] | None = None,
) -> ExpertDescriptor:
    """Normalize current/legacy expert manifests without mutating the package.

    Legacy ``package_type=agent_template`` packages remain visible but are marked
    ``needs_conversion`` and cannot enter executable team candidates.
    """
    package_dir = Path(package_dir).resolve()
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping):
        raise InvalidExpertPackage("manifest.json 不是合法 JSON 对象")

    raw_package_type = manifest.get("package_type")
    is_team = (
        raw_package_type == _TEAM_PACKAGE_TYPE
        or manifest.get("packageType") == _TEAM_PACKAGE_TYPE
    )
    legacy = raw_package_type == _CURRENT_AGENT_PACKAGE_TYPE
    current_agent = manifest.get("packageType") == _CURRENT_AGENT_PACKAGE_TYPE
    if not (is_team or legacy or current_agent):
        raise InvalidExpertPackage("无法识别专家包 schema")

    metadata = _as_mapping(manifest.get("metadata"))
    card = _as_mapping(manifest.get("agentCard"))
    if is_team:
        expert_id = str(manifest.get("name") or package_dir.name).strip()
        name = _localized_text(
            metadata.get("displayName")
            or manifest.get("display_name")
            or manifest.get("name")
            or package_dir.name
        )
        description = _localized_text(
            metadata.get("description") or manifest.get("display_description")
        )
        if not description:
            leader_path = package_dir / "agents" / "leader" / "manifest.json"
            try:
                leader = json.loads(leader_path.read_text(encoding="utf-8"))
                leader_card = (
                    _as_mapping(leader.get("agentCard"))
                    if isinstance(leader, Mapping)
                    else {}
                )
                description = str(
                    leader_card.get("description") or leader.get("description") or ""
                ).strip()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
        package_schema = "agent_group"
    elif legacy:
        expert_id = str(
            card.get("id")
            or manifest.get("id")
            or manifest.get("name")
            or package_dir.name
        ).strip()
        name = _localized_text(
            manifest.get("display_name")
            or card.get("name")
            or manifest.get("name")
            or expert_id
        )
        description = _localized_text(
            manifest.get("display_description")
            or card.get("description")
            or manifest.get("description")
        )
        package_schema = "legacy_agent_template"
    else:
        expert_id = str(card.get("id") or package_dir.name).strip()
        name = str(card.get("name") or expert_id).strip()
        description = str(card.get("description") or "").strip()
        package_schema = "agent_template"

    if not expert_id:
        raise InvalidExpertPackage("专家 ID 缺失")

    collaboration = _collaboration_metadata(manifest, metadata)
    tags = _as_string_list(
        metadata.get("tags")
        or manifest.get("tags")
        or _summary_value(summary, "tags", [])
    )
    quick_prompts = _as_string_list(
        metadata.get("quickPrompts")
        or metadata.get("quick_prompts")
        or manifest.get("quick_inputs")
    )
    deliverables = _as_string_list(
        metadata.get("deliverables") or collaboration.get("deliverables")
    )
    skill_contract = inspect_skill_contracts(package_dir, manifest, is_team=is_team)
    skills = skill_contract.names
    if not skills:
        summary_skills = _summary_value(summary, "skills", []) or []
        skills = tuple(
            str(item.get("name") or "").strip()
            for item in summary_skills
            if isinstance(item, Mapping) and str(item.get("name") or "").strip()
        )

    if legacy:
        status = "needs_conversion"
    else:
        try:
            validate_expert_package(package_dir)
            status = _EXECUTABLE_STATUS
            if not is_team and skill_contract.errors:
                status = "invalid"
        except InvalidExpertPackage:
            status = "invalid"

    declared_reusable = collaboration.get("reusable", metadata.get("reusable", True))
    reusable = bool(declared_reusable) and not is_team and status == _EXECUTABLE_STATUS
    return ExpertDescriptor(
        id=expert_id,
        name=name,
        description=description,
        type="team" if is_team else "agent",
        source=str(_summary_value(summary, "source", "package") or "package"),
        status=status,
        reusable=reusable,
        tags=tags,
        skills=skills,
        inputs=_normalize_ports(collaboration.get("inputs")),
        outputs=_normalize_ports(collaboration.get("outputs")),
        content_hash=_package_hash(package_dir),
        package_schema=package_schema,
        utility_skills=_as_string_list(
            collaboration.get("utilitySkills") or collaboration.get("utility_skills")
        ),
        quick_prompts=quick_prompts,
        deliverables=deliverables,
        complements_with=_relation_ids(
            collaboration, "complementsWith", "complements_with"
        ),
        overlaps_with=_relation_ids(collaboration, "overlapsWith", "overlaps_with"),
        conflicts_with=_relation_ids(collaboration, "conflictsWith", "conflicts_with"),
    )


def normalize_expert_summary(
    summary: ExpertSummary | Mapping[str, Any],
) -> ExpertDescriptor:
    """Best-effort descriptor when a repository package cannot be fetched."""
    metadata = _as_mapping(_summary_value(summary, "metadata", {}))
    collaboration = _as_mapping(metadata.get("collaboration"))
    skills = tuple(
        str(item.get("name") or "").strip()
        for item in (_summary_value(summary, "skills", []) or [])
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    )
    expert_type = str(_summary_value(summary, "type", "agent") or "agent")
    available = bool(_summary_value(summary, "available", False))
    payload = {
        "id": str(_summary_value(summary, "id", "")),
        "name": str(_summary_value(summary, "name", "")),
        "description": str(_summary_value(summary, "description", "")),
        "source": str(_summary_value(summary, "source", "repo")),
        "metadata": metadata,
        "skills": list(skills),
        "type": expert_type,
    }
    return ExpertDescriptor(
        id=payload["id"],
        name=payload["name"] or payload["id"],
        description=payload["description"],
        type="team" if expert_type == "team" else "agent",
        source=payload["source"],
        status=_EXECUTABLE_STATUS if available else "unavailable",
        reusable=available
        and expert_type != "team"
        and bool(collaboration.get("reusable", True)),
        tags=_as_string_list(
            _summary_value(summary, "tags", []) or metadata.get("tags")
        ),
        skills=skills,
        inputs=_normalize_ports(collaboration.get("inputs")),
        outputs=_normalize_ports(collaboration.get("outputs")),
        content_hash=str(metadata.get("contentHash") or _digest(payload)),
        package_schema="summary",
        utility_skills=_as_string_list(
            collaboration.get("utilitySkills") or collaboration.get("utility_skills")
        ),
        quick_prompts=_as_string_list(
            metadata.get("quickPrompts") or metadata.get("quick_prompts")
        ),
        deliverables=_as_string_list(
            metadata.get("deliverables") or collaboration.get("deliverables")
        ),
        complements_with=_relation_ids(
            collaboration, "complementsWith", "complements_with"
        ),
        overlaps_with=_relation_ids(collaboration, "overlapsWith", "overlaps_with"),
        conflicts_with=_relation_ids(collaboration, "conflictsWith", "conflicts_with"),
    )


def build_expert_inventory_snapshot(
    descriptors: Iterable[ExpertDescriptor],
    *,
    created_at: str | None = None,
) -> ExpertInventorySnapshot:
    """Deduplicate descriptors by ID/hash and quarantine divergent same-ID packages."""
    grouped: dict[str, list[ExpertDescriptor]] = {}
    for descriptor in descriptors:
        if descriptor.id:
            grouped.setdefault(descriptor.id, []).append(descriptor)

    experts: list[ExpertDescriptor] = []
    warnings: list[str] = []
    for expert_id in sorted(grouped):
        variants = sorted(
            grouped[expert_id], key=lambda item: (item.content_hash, item.source)
        )
        hashes = {item.content_hash for item in variants}
        if len(hashes) == 1:
            preferred = max(
                variants,
                key=lambda item: (
                    item.status == _EXECUTABLE_STATUS,
                    item.reusable,
                    item.source == "local",
                ),
            )
            sources = sorted({item.source for item in variants})
            experts.append(replace(preferred, source="+".join(sources)))
            continue
        base = variants[0]
        experts.append(
            replace(
                base,
                source="+".join(sorted({item.source for item in variants})),
                status="conflict",
                reusable=False,
                content_hash=_digest(sorted(hashes)),
            )
        )
        warnings.append(f"专家 {expert_id!r} 存在 {len(hashes)} 个不同内容版本，已隔离")

    source_payload = [expert.to_inventory_dict() for expert in experts]
    source_hash = _digest(source_payload)
    return ExpertInventorySnapshot(
        created_at=created_at or _utc_now(),
        source_hash=source_hash,
        experts=tuple(experts),
        inventory_id=f"expert-inventory-{source_hash[:12]}",
        warnings=tuple(warnings),
    )


async def collect_expert_inventory(
    source: ExpertPackageSource,
    *,
    created_at: str | None = None,
    resolve_packages: bool = True,
) -> ExpertInventorySnapshot:
    """Collect a source into a stable snapshot; usable directly by an RPC handler."""
    summaries = await source.list()
    descriptors: list[ExpertDescriptor] = []
    for summary in summaries:
        if not resolve_packages:
            descriptors.append(normalize_expert_summary(summary))
            continue
        try:
            package_dir = await source.fetch(summary.id)
            descriptors.append(normalize_expert_manifest(package_dir, summary=summary))
        except (
            ExpertNotFound,
            ExpertRepoUnavailable,
            InvalidExpertPackage,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            descriptors.append(normalize_expert_summary(summary))
    return build_expert_inventory_snapshot(descriptors, created_at=created_at)


def _make_edge(
    source: str,
    target: str,
    edge_type: str,
    confidence: float,
    evidence: Sequence[dict[str, Any]],
) -> ExpertGraphEdge:
    edge_key = {"source": source, "target": target, "type": edge_type}
    return ExpertGraphEdge(
        id=f"edge-{_digest(edge_key, length=16)}",
        source=source,
        target=target,
        type=edge_type,
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        evidence=tuple(dict(item) for item in evidence),
    )


def _port_relation(
    source: ExpertDescriptor, target: ExpertDescriptor
) -> list[ExpertGraphEdge]:
    matches: dict[str, tuple[float, dict[str, Any]]] = {}
    for output in source.outputs:
        for input_ in target.inputs:
            media_equal = bool(
                output.media_type and output.media_type == input_.media_type
            )
            schema_equal = bool(output.schema and output.schema == input_.schema)
            semantic_overlap = _tokens(output.id, output.description) & _tokens(
                input_.id, input_.description
            )
            if media_equal and schema_equal:
                relation, confidence, reason = (
                    "can_feed",
                    1.0,
                    "media type 与 schema 精确匹配",
                )
            elif media_equal:
                relation = "needs_adapter"
                confidence = 0.85 if output.schema and input_.schema else 0.72
                reason = "media type 相同，但 schema 缺失或不一致"
            elif semantic_overlap:
                relation, confidence, reason = (
                    "needs_adapter",
                    0.62,
                    "端口语义相关，但 media type 不一致",
                )
            else:
                continue
            evidence = {
                "output": output.to_dict(),
                "input": input_.to_dict(),
                "reason": reason,
            }
            current = matches.get(relation)
            if current is None or confidence > current[0]:
                matches[relation] = (confidence, evidence)
    return [
        _make_edge(source.id, target.id, relation, value[0], [value[1]])
        for relation, value in sorted(matches.items())
    ]


def _effective_skills(node: ExpertDescriptor) -> set[str]:
    return set(node.skills) - set(node.utility_skills)


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def build_expert_graph(inventory: ExpertInventorySnapshot) -> ExpertGraphSnapshot:
    """Build hard contract edges plus non-executable semantic discovery edges."""
    nodes = tuple(sorted(inventory.experts, key=lambda item: item.id))
    edges: dict[tuple[str, str, str], ExpertGraphEdge] = {}

    for source, target in itertools.permutations(nodes, 2):
        for edge in _port_relation(source, target):
            edges[(edge.source, edge.target, edge.type)] = edge

    for left, right in itertools.combinations(nodes, 2):
        source, target = sorted((left.id, right.id))
        explicit_conflict = (
            right.id in left.conflicts_with or left.id in right.conflicts_with
        )
        if explicit_conflict:
            edge = _make_edge(
                source, target, "conflicts", 1.0, [{"reason": "专家包显式声明能力冲突"}]
            )
            edges[(source, target, edge.type)] = edge
            continue

        left_skills, right_skills = _effective_skills(left), _effective_skills(right)
        skill_overlap = _jaccard(left_skills, right_skills)
        explicitly_overlaps = (
            right.id in left.overlaps_with or left.id in right.overlaps_with
        )
        if explicitly_overlaps or skill_overlap >= 0.5:
            confidence = 1.0 if explicitly_overlaps else 0.55 + 0.45 * skill_overlap
            evidence = [
                {
                    "reason": "显式声明能力重叠"
                    if explicitly_overlaps
                    else "非工具型 Skill 高度重叠",
                    "sharedSkills": sorted(left_skills & right_skills),
                }
            ]
            edge = _make_edge(source, target, "overlaps", confidence, evidence)
            edges[(source, target, edge.type)] = edge

        shared_tags = set(left.tags) & set(right.tags)
        explicitly_complements = (
            right.id in left.complements_with or left.id in right.complements_with
        )
        if explicitly_complements or (shared_tags and skill_overlap < 0.5):
            confidence = (
                1.0
                if explicitly_complements
                else min(0.8, 0.5 + 0.1 * len(shared_tags))
            )
            evidence = [
                {
                    "reason": "显式声明能力互补"
                    if explicitly_complements
                    else "领域标签相关且核心 Skill 不重叠",
                    "sharedTags": sorted(shared_tags),
                }
            ]
            edge = _make_edge(source, target, "complements", confidence, evidence)
            edges[(source, target, edge.type)] = edge

    # Ignore explicit relationship IDs that do not exist in this inventory.
    ordered_edges = tuple(
        sorted(edges.values(), key=lambda edge: (edge.source, edge.target, edge.type))
    )
    source_hash = inventory.source_hash
    return ExpertGraphSnapshot(
        graph_id=f"expert-graph-{source_hash[:12]}",
        created_at=inventory.created_at,
        source_hash=source_hash,
        nodes=nodes,
        edges=ordered_edges,
    )


_TEAM_RELATION_FACTORS = {"can_feed": 1.0, "complements": 0.85}


def _pair_key(left: str, right: str) -> frozenset[str]:
    return frozenset((left, right))


def _relation_strength(edge: ExpertGraphEdge) -> float:
    return edge.confidence * _TEAM_RELATION_FACTORS.get(edge.type, 0.0)


def _candidate_member_sets(
    eligible: Sequence[ExpertDescriptor],
    positive_edges: Sequence[ExpertGraphEdge],
    conflict_pairs: set[frozenset[str]],
    *,
    min_members: int,
    max_members: int,
) -> tuple[frozenset[str], ...]:
    """Grow a bounded number of high-affinity groups without n-choose-k search.

    A seed is created for every node and for its strongest relationship.  Each
    seed then greedily adds the compatible node with the strongest aggregate
    affinity to the current group.  For dozens of experts this is O(n² * k),
    instead of enumerating every 3..6 member combination.
    """

    by_id = {member.id: member for member in eligible}
    affinity: dict[frozenset[str], float] = {}
    neighbors: dict[str, set[str]] = {member.id: set() for member in eligible}
    for edge in positive_edges:
        pair = _pair_key(edge.source, edge.target)
        if len(pair) != 2:
            continue
        affinity[pair] = max(affinity.get(pair, 0.0), _relation_strength(edge))
        neighbors[edge.source].add(edge.target)
        neighbors[edge.target].add(edge.source)

    seeds: set[tuple[str, ...]] = {(member.id,) for member in eligible}
    for member_id in sorted(by_id):
        ranked_neighbors = sorted(
            neighbors[member_id],
            key=lambda target: (-affinity[_pair_key(member_id, target)], target),
        )
        if ranked_neighbors:
            seeds.add(tuple(sorted((member_id, ranked_neighbors[0]))))

    groups: set[frozenset[str]] = set()
    for seed in sorted(seeds):
        selected = list(seed)
        if any(pair <= set(selected) for pair in conflict_pairs):
            continue
        while len(selected) < max_members:
            options: list[tuple[float, float, int, str]] = []
            selected_set = set(selected)
            for candidate_id in sorted(by_id.keys() - selected_set):
                if any(
                    _pair_key(candidate_id, existing) in conflict_pairs
                    for existing in selected
                ):
                    continue
                strengths = [
                    affinity.get(_pair_key(candidate_id, existing), 0.0)
                    for existing in selected
                ]
                if not any(strengths):
                    continue
                descriptor = by_id[candidate_id]
                options.append(
                    (
                        sum(strengths),
                        max(strengths),
                        len(_effective_skills(descriptor)) + len(descriptor.tags),
                        candidate_id,
                    )
                )
            if not options:
                break
            best = min(
                options,
                key=lambda item: (-item[0], -item[1], -item[2], item[3]),
            )
            selected.append(best[3])
            if len(selected) >= min_members:
                groups.add(frozenset(selected))
    return tuple(sorted(groups, key=lambda group: (len(group), sorted(group))))


def _public_primary_output(member: ExpertDescriptor) -> ExpertPort | None:
    return next(
        (
            output
            for output in member.outputs
            if output.primary and output.visibility == "public"
        ),
        None,
    )


def _route_step(
    member: ExpertDescriptor,
    *,
    index: int,
    dependencies: Sequence[str] = (),
    handoff: ExpertGraphEdge | None = None,
    include_final_output: bool = True,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step": index,
        "expertId": member.id,
        "expertName": member.name,
        "dependsOn": list(dependencies),
        "handoffs": [],
    }
    if handoff is not None:
        step["handoffs"] = [
            {
                "edgeId": handoff.id,
                "fromExpertId": handoff.source,
                "evidence": [dict(item) for item in handoff.evidence],
            }
        ]
    final_output = _public_primary_output(member) if include_final_output else None
    if final_output is not None:
        step["finalOutput"] = final_output.to_dict()
    return step


def _route_examples(
    members: Sequence[ExpertDescriptor],
    positive_edges: Sequence[ExpertGraphEdge],
) -> tuple[dict[str, Any], ...]:
    member_by_id = {member.id: member for member in members}
    routes: list[dict[str, Any]] = []
    for member in sorted(members, key=lambda item: item.id):
        intent = (
            member.quick_prompts[0]
            if member.quick_prompts
            else f"只需要{member.name}独立完成其擅长任务"
        )
        routes.append(
            {
                "id": f"single-{member.id}",
                "type": "single",
                "title": f"{member.name}直达",
                "intent": intent,
                "selectedMemberIds": [member.id],
                "steps": [_route_step(member, index=1)],
            }
        )

    for edge in sorted(
        positive_edges,
        key=lambda item: (item.type != "can_feed", -item.confidence, item.id),
    ):
        source = member_by_id[edge.source]
        target = member_by_id[edge.target]
        if edge.type == "can_feed":
            steps = [
                _route_step(source, index=1, include_final_output=False),
                _route_step(
                    target,
                    index=2,
                    dependencies=(source.id,),
                    handoff=edge,
                ),
            ]
            route_type = "serial"
            title = f"{source.name} → {target.name}"
            intent = f"先由{source.name}完成前置工作，再交给{target.name}产出结果"
        else:
            steps = [
                _route_step(source, index=1),
                _route_step(target, index=2),
            ]
            route_type = "parallel"
            title = f"{source.name} + {target.name}"
            intent = f"需要{source.name}和{target.name}从不同角度并行协作"
        routes.append(
            {
                "id": f"{route_type}-{edge.id}",
                "type": route_type,
                "title": title,
                "intent": intent,
                "selectedMemberIds": [source.id, target.id],
                "steps": steps,
                "relationEdgeIds": [edge.id],
            }
        )
    # Keep every single-member route visible, then reserve space for both serial
    # and parallel examples so a dense graph cannot crowd out one execution mode.
    singles = [route for route in routes if route["type"] == "single"]
    serial = [route for route in routes if route["type"] == "serial"]
    parallel = [route for route in routes if route["type"] == "parallel"]
    selected = [*singles, *serial[:6], *parallel[:6]]
    if len(selected) < 18:
        selected_ids = {route["id"] for route in selected}
        selected.extend(route for route in routes if route["id"] not in selected_ids)
    return tuple(selected[:18])


def _candidate_for(
    graph: ExpertGraphSnapshot,
    members: Sequence[ExpertDescriptor],
    positive_edges: Sequence[ExpertGraphEdge],
) -> ExpertTeamCandidate:
    members = tuple(sorted(members, key=lambda item: item.id))
    member_ids = {member.id for member in members}
    semantic_edges = [
        edge
        for edge in graph.edges
        if edge.source in member_ids and edge.target in member_ids
    ]
    overlap_confidence = max(
        (edge.confidence for edge in semantic_edges if edge.type == "overlaps"),
        default=0.0,
    )
    best_relation_by_member = {
        member.id: max(
            (
                _relation_strength(edge)
                for edge in positive_edges
                if member.id in {edge.source, edge.target}
            ),
            default=0.0,
        )
        for member in members
    }
    relationship_points = 30.0 * sum(best_relation_by_member.values()) / len(members)
    connectivity_points = 20.0 * min(
        1.0,
        len({_pair_key(edge.source, edge.target) for edge in positive_edges})
        / (len(members) - 1),
    )
    pair_diversities = [
        1.0 - _jaccard(_effective_skills(left), _effective_skills(right))
        for left, right in itertools.combinations(members, 2)
    ]
    diversity_points = 20.0 * (
        sum(pair_diversities) / len(pair_diversities) if pair_diversities else 1.0
    )
    route_types = {"single"}
    route_types.update(
        "serial" if edge.type == "can_feed" else "parallel" for edge in positive_edges
    )
    route_flexibility_points = 10.0 * len(route_types) / 3.0
    readiness_points = (
        15.0
        * sum(
            member.reusable and member.status == _EXECUTABLE_STATUS
            for member in members
        )
        / len(members)
    )
    simplicity_points = max(2.0, 5.0 - 0.75 * (len(members) - 3))
    overlap_penalty = 10.0 * overlap_confidence
    breakdown = {
        "relationshipQuality": round(relationship_points, 2),
        "connectivity": round(connectivity_points, 2),
        "capabilityDiversity": round(diversity_points, 2),
        "routeFlexibility": round(route_flexibility_points, 2),
        "readiness": round(readiness_points, 2),
        "simplicity": round(simplicity_points, 2),
        "overlapPenalty": round(-overlap_penalty, 2),
    }
    score = round(max(0.0, min(100.0, sum(breakdown.values()))), 2)

    routes = _route_examples(members, positive_edges)
    preferred_route = next(
        (route for route in routes if route["type"] == "serial"),
        next((route for route in routes if route["type"] == "parallel"), routes[0]),
    )
    workflow = tuple(dict(step) for step in preferred_route["steps"])
    member_profiles = tuple(
        {
            "id": member.id,
            "name": member.name,
            "description": member.description,
            "tags": list(member.tags),
            "skills": list(member.skills),
            "quickPrompts": list(member.quick_prompts[:3]),
            "deliverables": list(member.deliverables[:8]),
            "inputs": [item.to_dict() for item in member.inputs],
            "outputs": [item.to_dict() for item in member.outputs],
        }
        for member in members
    )

    deliverable_values: list[str] = []
    quick_prompt_values: list[str] = []
    for member in members:
        for value in member.deliverables:
            if value and value not in deliverable_values:
                deliverable_values.append(value)
        for output in member.outputs:
            if output.primary and output.visibility == "public":
                value = output.description or output.id
                if value and value not in deliverable_values:
                    deliverable_values.append(value)
        for value in member.quick_prompts:
            if value and value not in quick_prompt_values:
                quick_prompt_values.append(value)
    deliverables = tuple(deliverable_values[:8] or ["可直接使用的任务结果"])
    quick_prompts = tuple(
        quick_prompt_values[:3]
        or [
            f"帮我完成一个需要{'、'.join(member.name for member in members[:2])}协作的任务"
        ]
    )

    incident_count = {
        member.id: sum(
            member.id in {edge.source, edge.target} for edge in positive_edges
        )
        for member in members
    }
    anchor = max(
        members,
        key=lambda member: (
            incident_count[member.id],
            _public_primary_output(member) is not None,
            len(member.deliverables),
            member.id,
        ),
    )
    shared_tags = set(members[0].tags)
    for member in members[1:]:
        shared_tags &= set(member.tags)
    team_name = (
        f"{sorted(shared_tags)[0]}专家协作团" if shared_tags else f"{anchor.name}协作团"
    )
    key = {
        "members": sorted((member.id, member.content_hash) for member in members),
        "edges": sorted(edge.id for edge in positive_edges),
        "materializerVersion": EXPERT_TEAM_MATERIALIZER_VERSION,
        "dispatchContract": EXPERT_TEAM_DISPATCH_CONTRACT,
    }
    return ExpertTeamCandidate(
        id=f"expert-team-{_digest(key, length=16)}",
        name=team_name,
        description=(
            f"主理人会理解需求，从 {len(members)} 位专业成员中按需选择最小充分的 1～{len(members)} 位；"
            "简单任务单专家直达，复杂任务可串行交接或并行协作。"
        ),
        member_ids=tuple(member.id for member in members),
        leader_id="leader",
        workflow=workflow,
        score=score,
        score_breakdown=breakdown,
        quick_prompts=quick_prompts,
        deliverables=deliverables,
        routing_policy={
            "mode": "leader_selected",
            "selection": "minimal_sufficient",
            "minSelected": 1,
            "maxSelected": len(members),
            "allowSingleMember": True,
            "allowSerial": True,
            "allowParallel": True,
            "graphRole": "discovery_and_routing_evidence",
        },
        member_profiles=member_profiles,
        route_examples=routes,
    )


def mine_expert_team_candidates(
    graph: ExpertGraphSnapshot,
    *,
    min_members: int = 3,
    max_members: int = 6,
    limit: int = 20,
) -> tuple[ExpertTeamCandidate, ...]:
    """Mine bounded affinity groups; graph relations do not dictate execution.

    ``can_feed`` is useful evidence for a possible serial route and
    ``complements`` for a possible parallel route.  Neither relation requires
    every roster member to run for every query.  ``min_members=2`` remains
    accepted for callers that explicitly request legacy-sized candidates,
    while product defaults mine 3..6 member teams.
    """
    if min_members < 2 or max_members < min_members or max_members > 6:
        raise ValueError("成员数范围必须满足 2 <= min_members <= max_members <= 6")
    eligible = [
        node
        for node in graph.nodes
        if node.type == "agent" and node.reusable and node.status == _EXECUTABLE_STATUS
    ]
    eligible_ids = {node.id for node in eligible}
    positive_edges = [
        edge
        for edge in graph.edges
        if edge.type in _TEAM_RELATION_FACTORS
        and edge.source in eligible_ids
        and edge.target in eligible_ids
    ]
    conflict_pairs = {
        frozenset((edge.source, edge.target))
        for edge in graph.edges
        if edge.type == "conflicts"
    }
    installed_team_ids = {
        node.id
        for node in graph.nodes
        if node.type == "team" and node.status == _EXECUTABLE_STATUS
    }
    member_by_id = {member.id: member for member in eligible}
    groups = _candidate_member_sets(
        eligible,
        positive_edges,
        conflict_pairs,
        min_members=min_members,
        max_members=min(max_members, len(eligible)),
    )
    candidates: list[ExpertTeamCandidate] = []
    for member_ids in groups:
        members = [member_by_id[member_id] for member_id in sorted(member_ids)]
        internal = [
            edge
            for edge in positive_edges
            if edge.source in member_ids and edge.target in member_ids
        ]
        candidate = _candidate_for(graph, members, internal)
        if candidate.id in installed_team_ids:
            candidate = replace(candidate, status="installed")
        candidates.append(candidate)
    candidates.sort(key=lambda item: (-item.score, len(item.member_ids), item.id))
    return tuple(candidates[: max(0, limit)])


def mine_expert_teams(
    graph: ExpertGraphSnapshot,
    *,
    min_members: int = 3,
    max_members: int = 6,
    limit: int = 20,
) -> dict[str, Any]:
    """RPC-ready wrapper around :func:`mine_expert_team_candidates`."""
    candidates = mine_expert_team_candidates(
        graph,
        min_members=min_members,
        max_members=max_members,
        limit=limit,
    )
    return {
        "schemaVersion": TEAM_CANDIDATE_SCHEMA_VERSION,
        "graphId": graph.graph_id,
        "sourceHash": graph.source_hash,
        "candidates": [candidate.to_dict() for candidate in candidates],
        "stats": {"candidateCount": len(candidates)},
    }


async def build_and_mine_expert_teams(
    source: ExpertPackageSource,
    *,
    created_at: str | None = None,
    min_members: int = 3,
    max_members: int = 6,
    limit: int = 20,
) -> dict[str, Any]:
    """One-call service for product adapters; all returned values are JSON-safe."""
    inventory = await collect_expert_inventory(source, created_at=created_at)
    graph = build_expert_graph(inventory)
    mined = mine_expert_teams(
        graph,
        min_members=min_members,
        max_members=max_members,
        limit=limit,
    )
    return {
        "inventory": inventory.to_dict(),
        "graph": graph.to_dict(),
        "mining": mined,
    }


__all__ = [
    "ExpertDescriptor",
    "ExpertGraphEdge",
    "ExpertGraphSnapshot",
    "ExpertInventorySnapshot",
    "ExpertPort",
    "ExpertTeamCandidate",
    "build_and_mine_expert_teams",
    "build_expert_graph",
    "build_expert_inventory_snapshot",
    "collect_expert_inventory",
    "mine_expert_team_candidates",
    "mine_expert_teams",
    "normalize_expert_manifest",
    "normalize_expert_summary",
]
