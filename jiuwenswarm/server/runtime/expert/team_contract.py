# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Versioned contract shared by ExpertGraph mining, rendering, and assembly."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping

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
