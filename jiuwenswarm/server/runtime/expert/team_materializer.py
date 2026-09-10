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
from jiuwenswarm.server.runtime.expert.skill_contract import inspect_skill_contracts
from jiuwenswarm.server.runtime.expert.team_contract import (
    EXPERT_GRAPH_GENERATOR,
    EXPERT_TEAM_DISPATCH_CONTRACT,
    EXPERT_TEAM_MATERIALIZER_VERSION,
    EXPERT_TEAM_STAGE_FILE,
    EXPERT_TEAM_STAGE_METADATA_KEY,
)

_SAFE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_WINDOWS_DEVICE_NAME = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE
)
_PROMPT_OR_WINDOWS_RESERVED_CHARS = frozenset('<>:"|?*`')
_MAX_MEMBERS = 4
_TEXT_ARTIFACT_WRITE_GUIDANCE = (
    "文本主产物写入：不得把大型完整正文塞入单次 `write_file`/`edit_file` "
    "tool arguments；优先使用简短本地渲染脚本读取结构化交接并动态生成，"
    "无法采用时按有界小段分段写入"
)


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
                workflow=candidate.get("workflow"),
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
            "generatedBy": EXPERT_GRAPH_GENERATOR,
            "dispatchMode": "scheduled",
            "dispatchContract": EXPERT_TEAM_DISPATCH_CONTRACT,
            "materializerVersion": EXPERT_TEAM_MATERIALIZER_VERSION,
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
        '1. 每次收到新的用户请求，先调用 `view_task(action="list")` 查看当前看板。'
        "若已存在与当前请求匹配且未终结的同一组阶段任务，继续使用原任务，禁止重复创建；"
        "否则生成本次请求唯一的 `run_key`：优先使用当前 request id 的短后缀，"
        "拿不到时使用 UTC 时间戳加 4 位随机小写字母/数字。\n"
        "2. 首次执行必须按下方映射在一次 `create_task` 调用中创建完整任务 DAG。"
        "每个任务都必须显式填写唯一 `task_id`、对应成员 `assignee` 和 `depends_on`；"
        "`depends_on` 只能引用同批任务的 `task_id`，绝不能填写 expertId。\n"
        f"\n{_leader_task_dag(candidate.get('workflow'), member_ids)}\n"
        "3. 所有专业成员均已由 AgentGroup 预注册，禁止调用 `spawn_teammate` 重复创建成员。\n"
        "4. 当前图谱团由 scheduled scheduler 独占依赖放行和任务派发；禁止通过 broadcast "
        "或 `send_message` 提前启动 pending/blocked 下游。只有成员已经执行且确需澄清时，"
        "才可定向发送补充信息。\n"
        "5. 不得模拟成员输出；严格按共享 instruction 的调用链等待真实成员完成。"
        "前序结果通过声明的 `.expert-handoffs/` 契约完整传给后序成员。\n"
        "6. 运行时主理人成员名固定为 `team-leader`；成员向主理人回传时必须使用该名称。\n"
        "7. 中间交接不得冒充用户最终成品；只把调用链最后阶段生成且已经打开检查的"
        "主成品发送给用户。\n",
        encoding="utf-8",
    )


def _leader_task_dag(workflow: Any, member_ids: list[str]) -> str:
    """Render the candidate's expert DAG as unambiguous task-id templates."""
    mapping_steps = _validated_workflow_dag(workflow, member_ids)
    ordered_experts = [
        str(item.get("expertId") or "").strip() for item in mapping_steps
    ]
    stage_by_expert = {
        expert_id: index for index, expert_id in enumerate(ordered_experts, start=1)
    }
    lines = [
        "## 固定任务 ID 映射",
        "",
        "下表的 `{run_key}` 在同一次用户请求中必须保持不变。`task_id` 是任务标识，"
        "`assignee` 才是专家成员名，两者不得混用。",
        "",
        "| 阶段 | task_id | assignee | depends_on（task_id） |",
        "| --- | --- | --- | --- |",
    ]
    for index, item in enumerate(mapping_steps, start=1):
        expert_id = ordered_experts[index - 1]
        raw_dependencies = item.get("dependsOn")
        dependencies = (
            [str(value or "").strip() for value in raw_dependencies]
            if isinstance(raw_dependencies, (list, tuple))
            else []
        )
        unknown = [
            dependency
            for dependency in dependencies
            if dependency and dependency not in stage_by_expert
        ]
        if unknown:
            raise TeamMaterializationError(
                "candidate workflow dependsOn references unknown experts: "
                + ", ".join(unknown)
            )
        dependency_ids = [
            f"`{{run_key}}-s{stage_by_expert[dependency]:02d}`"
            for dependency in dependencies
            if dependency
        ]
        depends_on = "[" + ", ".join(dependency_ids) + "]"
        lines.append(
            f"| {index} | `{{run_key}}-s{index:02d}` | `{expert_id}` | {depends_on} |"
        )
    lines.extend(
        [
            "",
            "批量创建失败或进程恢复时，必须再次查看看板：若这些 `task_id` 已存在，"
            "复用并继续它们；只有确认本次请求尚无对应任务时才能创建新 `run_key`。",
        ]
    )
    return "\n".join(lines)


def _validated_workflow_dag(
    workflow: Any, member_ids: list[str]
) -> list[Mapping[str, Any]]:
    """Fail closed unless the workflow is one complete, ordered, single-sink DAG."""

    if not isinstance(workflow, (list, tuple)) or not workflow:
        raise TeamMaterializationError("candidate workflow must be a non-empty DAG")
    if not all(isinstance(item, Mapping) for item in workflow):
        raise TeamMaterializationError(
            "candidate workflow must contain structured expert stages"
        )
    steps = list(workflow)
    expert_ids = [str(item.get("expertId") or "").strip() for item in steps]
    if expert_ids != member_ids or len(set(expert_ids)) != len(expert_ids):
        raise TeamMaterializationError(
            "candidate workflow must cover memberIds exactly once and in stage order"
        )

    stage_by_expert = {expert_id: index for index, expert_id in enumerate(expert_ids)}
    outgoing = {expert_id: set() for expert_id in expert_ids}
    undirected = {expert_id: set() for expert_id in expert_ids}
    final_output_experts: list[str] = []
    for index, item in enumerate(steps):
        expert_id = expert_ids[index]
        raw_dependencies = item.get("dependsOn", [])
        if not isinstance(raw_dependencies, (list, tuple)):
            raise TeamMaterializationError(
                f"candidate workflow dependsOn must be a list: {expert_id}"
            )
        dependencies = [str(value or "").strip() for value in raw_dependencies]
        if any(not value for value in dependencies) or len(set(dependencies)) != len(
            dependencies
        ):
            raise TeamMaterializationError(
                f"candidate workflow has blank or duplicate dependencies: {expert_id}"
            )
        unknown = [value for value in dependencies if value not in stage_by_expert]
        if unknown:
            raise TeamMaterializationError(
                "candidate workflow dependsOn references unknown experts: "
                + ", ".join(unknown)
            )
        if any(stage_by_expert[value] >= index for value in dependencies):
            raise TeamMaterializationError(
                "candidate workflow must be acyclic and topologically ordered"
            )
        for dependency in dependencies:
            outgoing[dependency].add(expert_id)
            undirected[dependency].add(expert_id)
            undirected[expert_id].add(dependency)
        if _final_output_clause(item.get("finalOutput")):
            final_output_experts.append(expert_id)

    visited: set[str] = set()
    frontier = [expert_ids[0]]
    while frontier:
        current = frontier.pop()
        if current in visited:
            continue
        visited.add(current)
        frontier.extend(undirected[current] - visited)
    if visited != set(expert_ids):
        raise TeamMaterializationError("candidate workflow DAG must be connected")

    sinks = [expert_id for expert_id, targets in outgoing.items() if not targets]
    if len(sinks) != 1:
        raise TeamMaterializationError(
            "candidate workflow DAG must contain exactly one sink"
        )
    if final_output_experts != sinks:
        raise TeamMaterializationError(
            "candidate workflow finalOutput must exist only on its unique sink"
        )
    return steps


def _embed_member(
    source: Path,
    destination: Path,
    *,
    member_id: str,
    workflow: Any,
) -> None:
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
    metadata[EXPERT_TEAM_STAGE_METADATA_KEY] = EXPERT_TEAM_STAGE_FILE
    embedded["metadata"] = metadata
    _inject_team_stage_persona(
        destination,
        embedded,
        member_id=member_id,
        workflow=workflow,
        skill_names=_declared_skill_names(source, manifest),
    )
    _write_json(destination / "manifest.json", embedded)


def _inject_team_stage_persona(
    member_root: Path,
    manifest: Mapping[str, Any],
    *,
    member_id: str,
    workflow: Any,
    skill_names: list[str],
) -> None:
    """Write a loader-mounted stage section without replacing source persona files."""
    persona = manifest.get("persona")
    if not isinstance(persona, Mapping) or not isinstance(persona.get("dir"), str):
        raise TeamMaterializationError(
            f"embedded member has no safe persona.dir: {member_id}"
        )
    raw_dir = str(persona["dir"]).strip()
    _safe_package_dir(
        member_root,
        raw_dir,
        label=f"embedded member {member_id!r} persona.dir",
    )

    override = member_root / EXPERT_TEAM_STAGE_FILE
    if override.exists() or override.is_symlink():
        raise TeamMaterializationError(
            f"embedded member reserves {EXPERT_TEAM_STAGE_FILE!r}: {member_id}"
        )
    override.write_text(
        _team_stage_persona(member_id, workflow, skill_names), encoding="utf-8"
    )


def _declared_skill_names(package_root: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Read original Skill names through the shared beta3 callability contract."""
    inspection = inspect_skill_contracts(package_root, manifest)
    if inspection.errors:
        raise TeamMaterializationError(
            "embedded member has uncallable Skills: " + "; ".join(inspection.errors)
        )
    return list(inspection.names)


def _safe_package_dir(root: Path, raw_dir: str, *, label: str) -> Path:
    """Resolve one package-relative directory without accepting traversal."""
    relative = Path(raw_dir)
    windows_relative = PureWindowsPath(raw_dir)
    if (
        not raw_dir
        or relative.is_absolute()
        or windows_relative.is_absolute()
        or ".." in relative.parts
        or ".." in windows_relative.parts
    ):
        raise TeamMaterializationError(f"{label} is unsafe: {raw_dir!r}")
    root = root.resolve(strict=True)
    try:
        resolved = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise TeamMaterializationError(f"{label} does not exist: {raw_dir!r}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise TeamMaterializationError(f"{label} escapes its package: {raw_dir!r}")
    return resolved


def _team_stage_persona(member_id: str, workflow: Any, skill_names: list[str]) -> str:
    """Render one member's strict graph-stage scope for its embedded persona."""
    raw_steps = workflow if isinstance(workflow, (list, tuple)) else []
    steps = [item for item in raw_steps if isinstance(item, Mapping)]
    stage = next(
        (
            item
            for item in steps
            if str(item.get("expertId") or "").strip() == member_id
        ),
        {},
    )
    outgoing_contracts: list[dict[str, str]] = []
    for item in steps:
        target = str(item.get("expertId") or "").strip()
        outgoing_contracts.extend(
            contract
            for contract in _step_handoff_contracts(item, target_expert_id=target)
            if contract["source"] == member_id
        )

    lines = [
        "# 专家团阶段执行覆盖规则（最高优先级）",
        "",
        "本文件仅在当前专家团中生效；如与原 persona 的产物要求冲突，以本文件为准。",
        "收到已指派任务后，先确认任务已进入可执行状态；仅当运行时提供 `claim_task` "
        "且任务仍为 pending 时调用，scheduled 已自动进入 in_progress 时直接执行，禁止重复领取。",
        "",
        "## 本阶段产物边界",
        "",
    ]
    final_output_clause = _final_output_clause(stage.get("finalOutput"))
    if final_output_clause:
        lines.extend(
            [
                "你是调用链终点。只生成并发送图谱声明的 `finalOutput`；"
                "短暂本地渲染脚本必须在完成后删除，不得作为产物。",
                f"- {final_output_clause}",
                "不得额外生成或发送其他 standalone/public/primary 主产物。",
            ]
        )
    else:
        lines.append(
            "你不是调用链终点。仅允许生成本阶段图谱声明的 hard-edge handoff；"
            "不得生成或发送任何 standalone/public/primary 主产物。"
        )
        if outgoing_contracts:
            for contract in outgoing_contracts:
                lines.append(
                    f"- `{contract['path']}`（{_handoff_contract_details(contract)}），"
                    f"交给 `{contract['target']}`"
                )
        else:
            lines.append("- 本阶段未声明 hard-edge handoff；不得自行虚构交接文件。")

    lines.extend(["", "## 本地 Skills", ""])
    if skill_names:
        lines.append(
            "按任务需要使用下列本地 Skills；其原始 name 已与 beta3 运行时调用 ID "
            "核验一致，调用时必须逐字使用："
        )
        lines.extend(
            f"- 原始 name：{json.dumps(name, ensure_ascii=False)}"
            for name in skill_names
        )
        lines.append("不得改名、另起别名或虚构不存在的 Skill。")
    else:
        lines.append(
            "本成员没有可按 `SKILL.md` 原始 name 确认的本地 Skills；"
            "不得虚构或声称调用了任何 Skill。"
        )
    return "\n".join(lines) + "\n"


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
        "每个阶段必须由对应真实成员执行。主理人一次性创建同构任务 DAG，"
        "用调用链中的 expertId 作为 assignee，并把依赖关系写入 depends_on；"
        "由 scheduled scheduler 自动放行，不得手工提前启动下游。"
        "主理人不得代写成员结果；前序中间结果放入 `.expert-handoffs/`。"
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
    output_id = str(value.get("id") or "")
    if not output_id.strip():
        return ""
    relative = _safe_relative_artifact_path(output_id, label="final output")
    media_type = str(value.get("mediaType") or value.get("media_type") or "").strip()
    schema = str(value.get("schema") or "").strip()
    details = []
    if media_type:
        details.append(f"mediaType=`{media_type}`")
    if schema:
        details.append(f"schema=`{schema}`")
    detail_text = "，".join(details) or "遵循图谱声明的原始格式"
    completion_separator = (
        f"；{_TEXT_ARTIFACT_WRITE_GUIDANCE}；"
        if media_type.casefold().startswith("text/")
        else "，"
    )
    return (
        f"最终主产物：必须生成 `{relative.as_posix()}`（{detail_text}）"
        f"{completion_separator}完成后打开检查，并只向用户发送这个主文件"
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
            output_id = str(output.get("id") or "")
            input_id = str(input_.get("id") or "")
            if not output_id.strip():
                continue
            relative = _safe_relative_artifact_path(output_id, label="handoff output")
            safe_input_id = (
                _safe_relative_artifact_path(input_id, label="handoff input").as_posix()
                if input_id.strip()
                else ""
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
                "output_id": relative.as_posix(),
                "input_id": safe_input_id,
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


def _safe_relative_artifact_path(value: str, *, label: str) -> Path:
    """Return one host-independent relative artifact path.

    ``Path`` on macOS does not recognize Windows drive or UNC paths as
    absolute, so both path dialects must be checked before a team package can
    be moved to another platform.
    """

    raw = str(value or "")
    normalized = raw.replace("\\", "/")
    relative = Path(normalized)
    windows = PureWindowsPath(raw)
    parts = normalized.split("/")
    invalid_segment = any(
        not part
        or part in {".", ".."}
        or part.endswith((" ", "."))
        or any(ord(char) < 32 for char in part)
        or any(char in _PROMPT_OR_WINDOWS_RESERVED_CHARS for char in part)
        or _WINDOWS_DEVICE_NAME.fullmatch(part)
        for part in parts
    )
    if (
        not raw
        or raw != raw.strip()
        or "\x00" in raw
        or normalized.endswith("/")
        or normalized in {".", "..", "~"}
        or (parts and parts[0].startswith("~"))
        or relative.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or invalid_segment
        or not relative.name
    ):
        raise TeamMaterializationError(f"{label} is not a safe relative file: {raw!r}")
    return relative


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
