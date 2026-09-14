# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Versioned contract shared by ExpertGraph mining, rendering, and assembly."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping

from jiuwenswarm.server.runtime.expert.metadata_safety import (
    is_safe_prompt_identifier,
    normalize_media_type,
    normalize_schema_id,
)

EXPERT_GRAPH_GENERATOR = "expert-graph"
EXPERT_TEAM_MATERIALIZER_VERSION = 3
EXPERT_TEAM_DISPATCH_CONTRACT = "xiaoyi.expert-team.dynamic-scheduled.v1"
# Packages generated before query-level routing remain runnable.  Keep the
# exact contract/version pair together: accepting either field independently
# would turn a partially upgraded package into a prompt/runtime split-brain.
LEGACY_EXPERT_TEAM_MATERIALIZER_VERSION = 2
LEGACY_EXPERT_TEAM_DISPATCH_CONTRACT = "xiaoyi.expert-team.scheduled.v1"
_SUPPORTED_SCHEDULED_CONTRACTS = frozenset(
    {
        (
            LEGACY_EXPERT_TEAM_DISPATCH_CONTRACT,
            LEGACY_EXPERT_TEAM_MATERIALIZER_VERSION,
        ),
        (EXPERT_TEAM_DISPATCH_CONTRACT, EXPERT_TEAM_MATERIALIZER_VERSION),
    }
)
EXPERT_TEAM_STAGE_FILE = "EXPERT_TEAM_STAGE.txt"
EXPERT_TEAM_STAGE_METADATA_KEY = "expertTeamStageFile"


def _is_unique_string_list(value: Any, *, prompt_safe: bool = False) -> bool:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return False
    if len(set(value)) != len(value):
        return False
    return not prompt_safe or all(is_safe_prompt_identifier(item) for item in value)


def _valid_port(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    artifact_id = value.get("id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        return False
    if any(char in artifact_id for char in ("\n", "\r", "\0", "`")):
        return False
    media_type = normalize_media_type(
        value.get("mediaType") or value.get("media_type") or ""
    )
    schema_id = normalize_schema_id(value.get("schema") or "")
    return media_type is not None and schema_id is not None


def _valid_route_step(
    value: Any,
    *,
    roster: set[str],
    selected: set[str],
    previous: set[str],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    expert_id = value.get("expertId")
    dependencies = value.get("dependsOn")
    if (
        not is_safe_prompt_identifier(expert_id)
        or expert_id not in roster
        or expert_id not in selected
        or not _is_unique_string_list(dependencies, prompt_safe=True)
        or not set(dependencies).issubset(previous)
    ):
        return False
    final_output = value.get("finalOutput")
    if final_output is not None and not _valid_port(final_output):
        return False
    handoffs = value.get("handoffs", [])
    if not isinstance(handoffs, list):
        return False
    for handoff in handoffs:
        if not isinstance(handoff, Mapping):
            return False
        source = handoff.get("fromExpertId")
        evidence = handoff.get("evidence")
        if (
            not is_safe_prompt_identifier(source)
            or source not in previous
            or not isinstance(evidence, list)
        ):
            return False
        for item in evidence:
            if (
                not isinstance(item, Mapping)
                or not _valid_port(item.get("output"))
                or not _valid_port(item.get("input"))
            ):
                return False
    return True


def _valid_route_example(value: Any, *, roster: list[str]) -> bool:
    if not isinstance(value, Mapping) or not is_safe_prompt_identifier(value.get("id")):
        return False
    route_type = value.get("type")
    selected = value.get("selectedMemberIds")
    steps = value.get("steps")
    if (
        route_type not in {"single", "serial", "parallel"}
        or not _is_unique_string_list(selected, prompt_safe=True)
        or not selected
        or not set(selected).issubset(roster)
        or not isinstance(steps, list)
        or len(steps) != len(selected)
    ):
        return False
    previous: set[str] = set()
    step_ids: list[str] = []
    for step in steps:
        if not _valid_route_step(
            step,
            roster=set(roster),
            selected=set(selected),
            previous=previous,
        ):
            return False
        expert_id = step["expertId"]
        if expert_id in previous:
            return False
        step_ids.append(expert_id)
        previous.add(expert_id)
    if step_ids != selected:
        return False
    dependency_counts = [len(step.get("dependsOn", [])) for step in steps]
    if route_type == "single" and (len(steps) != 1 or dependency_counts != [0]):
        return False
    if route_type == "parallel" and (len(steps) < 2 or any(dependency_counts)):
        return False
    if route_type == "serial" and (len(steps) < 2 or not any(dependency_counts[1:])):
        return False
    relation_ids = value.get("relationEdgeIds", [])
    return _is_unique_string_list(relation_ids, prompt_safe=True)


def _valid_dynamic_v3_metadata(
    manifest: Mapping[str, Any], metadata: Mapping[str, Any]
) -> bool:
    agents = manifest.get("agents")
    if not isinstance(agents, list):
        return False
    roster = [value for value in agents if value != "leader"]
    if (
        not _is_unique_string_list(roster, prompt_safe=True)
        or metadata.get("memberExpertIds") != roster
    ):
        return False
    policy = metadata.get("routingPolicy")
    expected_policy = {
        "mode": "leader_selected",
        "selection": "minimal_sufficient",
        "minSelected": 1,
        "maxSelected": len(roster),
        "allowSingleMember": True,
        "allowSerial": True,
        "allowParallel": True,
        "graphRole": "discovery_and_routing_evidence",
    }
    if not isinstance(policy, Mapping) or any(
        policy.get(key) != expected for key, expected in expected_policy.items()
    ):
        return False
    profiles = metadata.get("memberProfiles")
    if not isinstance(profiles, list) or len(profiles) != len(roster):
        return False
    profile_ids: list[str] = []
    for profile in profiles:
        if not isinstance(profile, Mapping):
            return False
        member_id = profile.get("id")
        if not is_safe_prompt_identifier(member_id):
            return False
        skills = profile.get("skills")
        if not _is_unique_string_list(skills, prompt_safe=True):
            return False
        for field in ("tags", "quickPrompts", "deliverables"):
            if not _is_unique_string_list(profile.get(field)):
                return False
        if not all(
            isinstance(profile.get(field), str) for field in ("name", "description")
        ):
            return False
        profile_ids.append(member_id)
    if profile_ids != roster:
        return False
    routes = metadata.get("routeExamples")
    if not isinstance(routes, list):
        return False
    route_ids: list[str] = []
    for route in routes:
        if not _valid_route_example(route, roster=roster):
            return False
        route_ids.append(route["id"])
    return len(set(route_ids)) == len(route_ids)


def _declares_member_stage_contract(
    package_dir: Path, manifest: Mapping[str, Any]
) -> bool:
    """Detect staged-member evidence without following unsafe member paths."""

    agents = manifest.get("agents")
    if not isinstance(agents, list):
        return False
    root = Path(package_dir).resolve()
    for value in agents:
        member_id = str(value or "").strip()
        windows = PureWindowsPath(member_id)
        if (
            not member_id
            or member_id in {".", ".."}
            or Path(member_id).is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or "/" in member_id
            or "\\" in member_id
        ):
            continue
        try:
            member_manifest = json.loads(
                (root / "agents" / member_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        metadata = (
            member_manifest.get("metadata")
            if isinstance(member_manifest, Mapping)
            else None
        )
        if isinstance(metadata, Mapping) and EXPERT_TEAM_STAGE_METADATA_KEY in metadata:
            return True
    return False


def classify_expert_team_dispatch_contract(
    package_dir: Path, manifest: Mapping[str, Any]
) -> Literal["legacy", "scheduled", "invalid"]:
    """Classify a package as legacy, complete scheduled, or invalid partial.

    ``generatedBy`` alone is intentionally insufficient: older graph packages
    used autonomous leader instructions and must keep their legacy behavior.
    Once any versioned dispatch field is present, incomplete structure fails
    closed rather than mixing scheduled prompts with an autonomous runtime.
    """

    metadata = manifest.get("metadata")
    versioned_keys = {"dispatchMode", "dispatchContract", "materializerVersion"}
    has_top_level_contract = isinstance(metadata, Mapping) and any(
        key in metadata for key in versioned_keys
    )
    has_member_contract = _declares_member_stage_contract(package_dir, manifest)
    if not has_top_level_contract and not has_member_contract:
        return "legacy"
    contract_pair = (
        (
            metadata.get("dispatchContract"),
            metadata.get("materializerVersion"),
        )
        if isinstance(metadata, Mapping)
        else (None, None)
    )
    if not isinstance(metadata, Mapping) or not (
        metadata.get("generatedBy") == EXPERT_GRAPH_GENERATOR
        and metadata.get("dispatchMode") == "scheduled"
        and contract_pair in _SUPPORTED_SCHEDULED_CONTRACTS
    ):
        return "invalid"
    agents = manifest.get("agents")
    if (
        not isinstance(agents, list)
        or "leader" not in agents
        or len(agents) < 3
        or len(set(str(value) for value in agents)) != len(agents)
    ):
        return "invalid"
    root = Path(package_dir).resolve()
    for value in agents:
        member_id = str(value or "").strip()
        relative = Path(member_id)
        windows = PureWindowsPath(member_id)
        if (
            not member_id
            or member_id in {".", ".."}
            or relative.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or "/" in member_id
            or "\\" in member_id
        ):
            return "invalid"
        member_root = root / "agents" / member_id
        if not member_root.is_dir() or member_root.is_symlink():
            return "invalid"
        if member_id == "leader":
            leader_rules = member_root / "AGENT.md"
            if not leader_rules.is_file() or leader_rules.is_symlink():
                return "invalid"
            continue
        try:
            member_manifest = json.loads(
                (member_root / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "invalid"
        member_metadata = (
            member_manifest.get("metadata")
            if isinstance(member_manifest, Mapping)
            else None
        )
        if not isinstance(member_metadata, Mapping) or (
            member_metadata.get(EXPERT_TEAM_STAGE_METADATA_KEY)
            != EXPERT_TEAM_STAGE_FILE
        ):
            return "invalid"
        stage = member_root / EXPERT_TEAM_STAGE_FILE
        if not stage.is_file() or stage.is_symlink():
            return "invalid"
    if contract_pair == (
        EXPERT_TEAM_DISPATCH_CONTRACT,
        EXPERT_TEAM_MATERIALIZER_VERSION,
    ) and not _valid_dynamic_v3_metadata(manifest, metadata):
        return "invalid"
    return "scheduled"


def supports_scheduled_expert_team(
    package_dir: Path, manifest: Mapping[str, Any]
) -> bool:
    """Return whether a package proves the complete scheduled-team contract."""

    return classify_expert_team_dispatch_contract(package_dir, manifest) == "scheduled"


__all__ = [
    "EXPERT_GRAPH_GENERATOR",
    "EXPERT_TEAM_DISPATCH_CONTRACT",
    "EXPERT_TEAM_MATERIALIZER_VERSION",
    "EXPERT_TEAM_STAGE_FILE",
    "EXPERT_TEAM_STAGE_METADATA_KEY",
    "LEGACY_EXPERT_TEAM_DISPATCH_CONTRACT",
    "LEGACY_EXPERT_TEAM_MATERIALIZER_VERSION",
    "classify_expert_team_dispatch_contract",
    "supports_scheduled_expert_team",
]
