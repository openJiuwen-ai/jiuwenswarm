# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Materialize an ExpertGraph team candidate as a beta3 AgentGroup package.

The graph/mining layer deliberately emits a product-neutral mapping.  This
module is the beta3 renderer: it embeds standalone expert packages as
member-local AgentTemplate packages and creates the synthetic ``leader``
required by the beta3 AgentGroup loader.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from jiuwenswarm.server.runtime.expert.agent_group import (
    validate_agent_group_package,
)
from jiuwenswarm.server.runtime.expert.expert_store import (
    InvalidExpertPackage,
    validate_expert_package,
)

_SAFE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_MAX_MEMBERS = 4


class TeamMaterializationError(ValueError):
    """A candidate cannot be rendered as a safe beta3 AgentGroup package."""


def materialize_team_candidate(
    candidate: Mapping[str, Any],
    *,
    expert_packages: Mapping[str, Path],
    destination_root: Path,
) -> Path:
    """Render *candidate* into ``destination_root/<candidate-id>`` atomically.

    Existing packages are never overwritten.  Callers must explicitly choose a
    new candidate id when the graph or member versions change.
    """

    team_id = _safe_id(candidate.get("id"), label="candidate.id")
    member_ids = _member_ids(candidate)
    missing = [
        member_id for member_id in member_ids if member_id not in expert_packages
    ]
    if missing:
        raise TeamMaterializationError(
            f"candidate references missing expert packages: {', '.join(missing)}"
        )

    destination_root = Path(destination_root).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / team_id
    if destination.exists() or destination.is_symlink():
        raise TeamMaterializationError(f"expert package already exists: {team_id}")

    staging_root = destination_root / f".{team_id}.staging-{uuid.uuid4().hex}"
    staging = staging_root / team_id
    staging.mkdir(parents=True, exist_ok=False)
    try:
        _write_top_level(staging, candidate, team_id=team_id, member_ids=member_ids)
        _write_leader(staging, candidate, member_ids=member_ids)
        for member_id in member_ids:
            _embed_member(
                Path(expert_packages[member_id]),
                staging / "agents" / member_id,
                member_id=member_id,
            )
        validate_agent_group_package(staging)
        os.replace(staging, destination)
        staging_root.rmdir()
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return destination


def _member_ids(candidate: Mapping[str, Any]) -> list[str]:
    raw = candidate.get("memberIds")
    if not isinstance(raw, list):
        raise TeamMaterializationError("candidate.memberIds must be a list")
    member_ids = [_safe_member_id(value) for value in raw]
    if not 2 <= len(member_ids) <= _MAX_MEMBERS:
        raise TeamMaterializationError("candidate must contain 2 to 4 experts")
    if len(set(member_ids)) != len(member_ids):
        raise TeamMaterializationError("candidate.memberIds contains duplicates")
    if "leader" in member_ids:
        raise TeamMaterializationError("'leader' is reserved for the team orchestrator")
    return member_ids


def _safe_id(value: Any, *, label: str) -> str:
    clean = str(value or "").strip()
    if not _SAFE_ID.fullmatch(clean):
        raise TeamMaterializationError(f"{label} is not a safe package id: {clean!r}")
    return clean


def _safe_member_id(value: Any) -> str:
    """Match AgentGroup's safe-component contract without slug-only restrictions."""
    if not isinstance(value, str):
        raise TeamMaterializationError(
            f"candidate.memberIds must contain strings: {value!r}"
        )
    clean = value.strip()
    if not clean or clean in {".", ".."}:
        raise TeamMaterializationError(
            f"candidate.memberIds contains an unsafe member id: {value!r}"
        )
    if "/" in clean or "\\" in clean:
        raise TeamMaterializationError(
            f"candidate.memberIds contains a path separator: {clean!r}"
        )
    if Path(clean).is_absolute() or PureWindowsPath(clean).is_absolute():
        raise TeamMaterializationError(
            f"candidate.memberIds contains an absolute path: {clean!r}"
        )
    return clean


def _write_top_level(
    root: Path,
    candidate: Mapping[str, Any],
    *,
    team_id: str,
    member_ids: list[str],
) -> None:
    name = _bounded_text(candidate.get("name"), fallback=team_id, limit=100)
    description = _bounded_text(candidate.get("description"), limit=500)
    workflow = _workflow_steps(candidate.get("workflow"), limit=12, item_limit=1200)
    quick_prompts = _string_list(candidate.get("quickPrompts"), limit=3, item_limit=300)
    deliverables = _string_list(candidate.get("deliverables"), limit=8, item_limit=200)
    graph_id = _bounded_text(candidate.get("graphId"), limit=160)

    manifest = {
        "package_type": "agent_group",
        "name": team_id,
        "agents": ["leader", *member_ids],
        "instruction": _team_instruction(name, member_ids, workflow, deliverables),
        "skills": [],
        "metadata": {
            "displayName": name,
            "description": description,
            "tags": ["专家团", "智能协作", "图谱生成"],
            "quickPrompts": quick_prompts,
            "profession": name,
            "categoryId": "IndustryConsultant",
            "generatedBy": "expert-graph",
            "graphId": graph_id,
            "candidateId": team_id,
            "memberExpertIds": member_ids,
            "deliverables": deliverables,
            "workflow": workflow,
        },
    }
    _write_json(root / "manifest.json", manifest)


def _write_leader(
    root: Path,
    candidate: Mapping[str, Any],
    *,
    member_ids: list[str],
) -> None:
    name = _bounded_text(candidate.get("name"), fallback="专家团", limit=100)
    description = _bounded_text(candidate.get("description"), limit=500)
    leader_dir = root / "agents" / "leader"
    persona_dir = leader_dir / "persona"
    persona_dir.mkdir(parents=True)
    manifest = {
        "packageType": "agent_template",
        "agentCard": {
            "id": "leader",
            "name": f"{name}主理人",
            "description": description or f"负责{name}的任务拆解、成员调度和最终交付。",
        },
        "persona": {"dir": "persona"},
        "skills": [],
    }
    _write_json(leader_dir / "manifest.json", manifest)
    roster = "\n".join(f"- `{member_id}`" for member_id in member_ids)
    (persona_dir / "ROLE.md").write_text(
        f"# {name}主理人\n\n"
        "你负责理解用户目标、按既定调用链调度真实成员、检查交接内容，并把最终成品交给用户。\n\n"
        f"## 可调度成员\n\n{roster}\n",
        encoding="utf-8",
    )
    (leader_dir / "AGENT.md").write_text(
        "# 专家团调度规则\n\n"
        "1. 必须使用团队运行时提供的 `create_task`、`spawn_teammate` 和 "
        "`send_message` 调度成员，不得模拟成员输出。\n"
        "2. 运行时主理人成员名固定为 `team-leader`；成员向主理人回传时必须使用该名称。\n"
        "3. 严格按共享 instruction 中的调用链执行；前序结果完整传给后序成员。\n"
        "4. 中间交接写入 `.expert-handoffs/`，不得冒充用户最终成品。\n"
        "5. 只把调用链最后阶段生成的成品作为主产物发送给用户，并确认文件可打开。\n",
        encoding="utf-8",
    )


def _embed_member(source: Path, destination: Path, *, member_id: str) -> None:
    try:
        source = source.expanduser().resolve(strict=True)
    except OSError as exc:
        raise TeamMaterializationError(
            f"expert package does not exist: {member_id}"
        ) from exc
    if not source.is_dir():
        raise TeamMaterializationError(
            f"expert package is not a directory: {member_id}"
        )
    try:
        validate_expert_package(source)
    except InvalidExpertPackage as exc:
        raise TeamMaterializationError(
            f"expert package is not beta3-compatible: {member_id}: {exc}"
        ) from exc
    manifest = _read_manifest(source)
    if manifest.get("package_type") == "agent_group":
        raise TeamMaterializationError(
            f"nested expert teams are not supported: {member_id}"
        )

    destination.mkdir(parents=True)
    _copy_package_files(source, destination)
    embedded = dict(manifest)
    card = dict(embedded.get("agentCard") or {})
    card["id"] = member_id
    embedded["agentCard"] = card
    embedded["packageType"] = "agent_template"
    embedded.pop("model", None)
    embedded.pop("rails", None)
    embedded.pop("subagents", None)
    metadata = dict(embedded.get("metadata") or {})
    metadata["originExpertId"] = str(
        (manifest.get("agentCard") or {}).get("id") or member_id
    )
    embedded["metadata"] = metadata
    _write_json(destination / "manifest.json", embedded)


def _copy_package_files(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not relative.parts or relative == Path("manifest.json"):
            continue
        if (
            relative == Path("AGENT.md")
            or relative.name == ".managed-by-expert-manager"
        ):
            continue
        if path.is_symlink():
            raise TeamMaterializationError(
                f"symlinks are not allowed in expert package: {relative}"
            )
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _read_manifest(package_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeamMaterializationError(
            f"invalid expert manifest: {package_dir.name}"
        ) from exc
    if not isinstance(value, dict):
        raise TeamMaterializationError(f"invalid expert manifest: {package_dir.name}")
    return value


def _team_instruction(
    name: str,
    member_ids: list[str],
    workflow: list[str],
    deliverables: list[str],
) -> str:
    stages = workflow or [
        f"依次调度专家 `{member_id}` 完成其专业阶段" for member_id in member_ids
    ]
    stage_text = "\n".join(f"{index}. {stage}" for index, stage in enumerate(stages, 1))
    member_text = "、".join(f"`{member_id}`" for member_id in member_ids)
    deliverable_text = "、".join(deliverables) or "一个可直接使用并可打开的最终成品"
    return (
        f"你们是{name}。可用专业成员为 {member_text}。\n\n"
        f"## 调用链\n{stage_text}\n\n"
        "每个阶段必须由对应真实成员执行。主理人通过团队任务和消息机制传递完整交接，"
        "不得代写成员结果；前序中间结果放入 `.expert-handoffs/`。"
        f"最终交付目标：{deliverable_text}。只发送最后阶段的主成品给用户。"
    )


def _bounded_text(value: Any, *, fallback: str = "", limit: int) -> str:
    clean = str(value or "").strip() or fallback
    return clean[:limit]


def _string_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        clean = str(item or "").strip()
        if clean and clean not in result:
            result.append(clean[:item_limit])
        if len(result) >= limit:
            break
    return result


def _workflow_steps(value: Any, *, limit: int, item_limit: int) -> list[str]:
    """Render the graph workflow protocol into human-readable team instructions."""
    if not isinstance(value, (list, tuple)):
        return []
    entries: list[tuple[str, str]] = []
    handoff_contracts: list[dict[str, str]] = []
    for item in value[:limit]:
        if isinstance(item, Mapping):
            expert_id = str(item.get("expertId") or "").strip()
            expert_name = _bounded_text(item.get("expertName"), limit=100)
            dependencies = _string_list(item.get("dependsOn"), limit=4, item_limit=80)
            actor = expert_name or expert_id
            if not actor:
                continue
            dependency_text = (
                f"，接收 {'、'.join(dependencies)} 的交接产物" if dependencies else ""
            )
            clean = f"调度 `{expert_id}`（{actor}）完成其专业阶段{dependency_text}"
            handoff_contracts.extend(
                _step_handoff_contracts(item, target_expert_id=expert_id)
            )
            final_output_clause = _final_output_clause(item.get("finalOutput"))
            if final_output_clause:
                clean = f"{clean}；{final_output_clause}"
        else:
            expert_id = ""
            clean = str(item or "").strip()
        if clean and clean not in [entry[1] for entry in entries]:
            entries.append((expert_id, clean))

    result: list[str] = []
    for expert_id, clean in entries:
        clauses: list[str] = []
        for contract in handoff_contracts:
            details = _handoff_contract_details(contract)
            if contract["source"] == expert_id:
                clauses.append(
                    f"交接输出：必须生成 `{contract['path']}`（{details}），"
                    f"供 `{contract['target']}` 使用"
                )
            if contract["target"] == expert_id:
                input_id = contract["input_id"] or contract["output_id"]
                clauses.append(
                    f"交接输入：必须读取 `{contract['path']}`（{details}；"
                    f"目标输入=`{input_id}`），不得改用 Markdown 等其他格式"
                )
        rendered = clean if not clauses else f"{clean}；" + "；".join(clauses)
        if len(rendered) > item_limit:
            raise TeamMaterializationError(
                "rendered workflow step exceeds the safe item limit; "
                "handoff filename/mediaType/schema cannot be truncated"
            )
        result.append(rendered)
    return result


def _final_output_clause(value: Any) -> str:
    """Render the unique sink's user-visible primary artifact contract."""
    if not isinstance(value, Mapping):
        return ""
    output_id = str(value.get("id") or "").strip()
    if not output_id:
        return ""
    relative = Path(output_id.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise TeamMaterializationError(
            f"final output is not a safe relative file: {output_id!r}"
        )
    media_type = str(
        value.get("mediaType") or value.get("media_type") or ""
    ).strip()
    schema = str(value.get("schema") or "").strip()
    details = []
    if media_type:
        details.append(f"mediaType=`{media_type}`")
    if schema:
        details.append(f"schema=`{schema}`")
    detail_text = "，".join(details) or "遵循图谱声明的原始格式"
    return (
        f"最终主产物：必须生成 `{relative.as_posix()}`（{detail_text}），"
        "完成后打开检查，并只向用户发送这个主文件"
    )


def _step_handoff_contracts(
    step: Mapping[str, Any],
    *,
    target_expert_id: str,
) -> list[dict[str, str]]:
    """Extract hard-edge evidence carried by ``candidate.workflow.handoffs``."""
    raw_handoffs = step.get("handoffs")
    if not isinstance(raw_handoffs, (list, tuple)):
        return []
    contracts: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for handoff in raw_handoffs:
        if not isinstance(handoff, Mapping):
            continue
        source = str(handoff.get("fromExpertId") or "").strip()
        evidence_items = handoff.get("evidence")
        if not source or not isinstance(evidence_items, (list, tuple)):
            continue
        for evidence in evidence_items:
            if not isinstance(evidence, Mapping):
                continue
            output = evidence.get("output")
            input_ = evidence.get("input")
            if not isinstance(output, Mapping) or not isinstance(input_, Mapping):
                continue
            output_id = str(output.get("id") or "").strip()
            input_id = str(input_.get("id") or "").strip()
            if not output_id:
                continue
            relative = Path(output_id.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                raise TeamMaterializationError(
                    f"handoff output is not a safe relative file: {output_id!r}"
                )
            parts = list(relative.parts)
            if parts and parts[0] == ".expert-handoffs":
                parts = parts[1:]
            if not parts:
                raise TeamMaterializationError("handoff output file is empty")
            path = ".expert-handoffs/" + "/".join(parts)
            contract = {
                "source": source,
                "target": target_expert_id,
                "path": path,
                "output_id": output_id,
                "input_id": input_id,
                "output_media": str(
                    output.get("mediaType") or output.get("media_type") or ""
                ).strip(),
                "input_media": str(
                    input_.get("mediaType") or input_.get("media_type") or ""
                ).strip(),
                "output_schema": str(output.get("schema") or "").strip(),
                "input_schema": str(input_.get("schema") or "").strip(),
            }
            key = tuple(contract.values())
            if key not in seen:
                contracts.append(contract)
                seen.add(key)
    return contracts


def _handoff_contract_details(contract: Mapping[str, str]) -> str:
    """Render both ends of a contract, preserving mismatch evidence if present."""
    parts: list[str] = []
    output_media = contract.get("output_media", "")
    input_media = contract.get("input_media", "")
    output_schema = contract.get("output_schema", "")
    input_schema = contract.get("input_schema", "")
    if output_media:
        parts.append(f"mediaType=`{output_media}`")
    if output_schema:
        parts.append(f"schema=`{output_schema}`")
    if input_media and input_media != output_media:
        parts.append(f"输入 mediaType=`{input_media}`")
    if input_schema and input_schema != output_schema:
        parts.append(f"输入 schema=`{input_schema}`")
    return "，".join(parts) or "遵循图谱声明的原始格式"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "TeamMaterializationError",
    "materialize_team_candidate",
]
