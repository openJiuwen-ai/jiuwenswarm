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
_MAX_MEMBERS = 6
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
        raise TeamMaterializationError("candidate must contain 2 to 6 experts")
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
    advisory_workflow = _validated_advisory_workflow(
        candidate.get("workflow"), member_ids
    )
    workflow = _workflow_steps(advisory_workflow, limit=12, item_limit=1200)
    quick_prompts = _string_list(candidate.get("quickPrompts"), limit=3, item_limit=300)
    deliverables = _string_list(candidate.get("deliverables"), limit=8, item_limit=200)
    graph_id = _bounded_text(candidate.get("graphId"), limit=160)
    routing_policy = _routing_policy(member_ids)
    member_profiles = _member_profiles(candidate.get("memberProfiles"), member_ids)
    route_examples = _validated_route_examples(
        candidate.get("routeExamples"), member_ids
    )

    manifest = {
        "package_type": "agent_group",
        "name": team_id,
        "agents": ["leader", *member_ids],
        "instruction": _team_instruction(
            name,
            member_ids,
            workflow,
            deliverables,
            member_profiles=member_profiles,
            route_examples=route_examples,
        ),
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
            "routingPolicy": routing_policy,
            "memberProfiles": member_profiles,
            "routeExamples": route_examples,
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
    member_profiles = _member_profiles(candidate.get("memberProfiles"), member_ids)
    route_examples = _validated_route_examples(
        candidate.get("routeExamples"), member_ids
    )
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
        "你负责理解用户目标、选择最小充分成员、决定串行或并行调度、检查结果，"
        "并把最终成品交给用户。专家关系图只提供能力与协作证据，不是固定执行链。\n\n"
        f"## 可调度成员\n\n{roster}\n",
        encoding="utf-8",
    )
    (leader_dir / "AGENT.md").write_text(
        "# 专家团调度规则\n\n"
        '1. 每次收到新的用户请求，先调用 `view_task(action="list")` 查看当前看板。'
        "若已存在与当前请求匹配且未终结的同一组阶段任务，继续使用原任务，禁止重复创建；"
        "否则生成本次请求唯一的 `run_key`：优先使用当前 request id 的短后缀，"
        "拿不到时使用 UTC 时间戳加 4 位随机小写字母/数字。\n"
        "2. 先判断用户意图，再从成员池中选择能完成请求的最小充分集合（1～N 位）。"
        "简单请求优先只选 1 位；只有存在独立子问题或明确前后置交接时才增加成员。"
        "专家团 roster 不是本次执行清单，禁止为了展示协作而调用无关成员。\n"
        "3. 为选中成员设计本次任务 DAG，并在一次 `create_task` 调用中只创建这些任务。"
        "每个任务必须显式填写唯一 `task_id`、成员 ID 作为 `assignee`、任务 ID 组成的 "
        "`depends_on`。互不依赖的任务使用空依赖并行；确有产物依赖时才串行。\n"
        f"\n{_leader_dynamic_routing(member_profiles, route_examples)}\n"
        "4. 所有专业成员均已由 AgentGroup 预注册，禁止调用 `spawn_teammate` 重复创建成员。\n"
        "5. 当前专家团由 scheduled scheduler 独占依赖放行和任务派发；禁止通过 broadcast "
        "或 `send_message` 提前启动 pending/blocked 下游。只有成员已经执行且确需澄清时，"
        "才可定向发送补充信息。\n"
        "6. 不得模拟成员输出。等待全部已选任务完成；多成员结果由主理人检查、去重和汇总。"
        "图谱路线仅为示例，必须按当前 Query 调整，不得机械照抄。\n"
        "7. 运行时主理人成员名固定为 `team-leader`；成员向主理人回传时必须使用该名称。\n"
        "8. 只向用户发送其当前请求需要、且已经打开检查的成品；不得强制生成成员声明过"
        "但本次 Query 未要求的其他文件。\n",
        encoding="utf-8",
    )


def _routing_policy(member_ids: list[str]) -> dict[str, Any]:
    """Return the executable v3 policy instead of trusting candidate metadata."""
    return {
        "mode": "leader_selected",
        "selection": "minimal_sufficient",
        "minSelected": 1,
        "maxSelected": len(member_ids),
        "allowSingleMember": True,
        "allowSerial": True,
        "allowParallel": True,
        "graphRole": "discovery_and_routing_evidence",
    }


def _member_profiles(value: Any, member_ids: list[str]) -> list[dict[str, Any]]:
    raw_profiles = value if isinstance(value, (list, tuple)) else []
    profile_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_profiles:
        if not isinstance(raw, Mapping):
            continue
        member_id = str(raw.get("id") or "").strip()
        if member_id not in member_ids or member_id in profile_by_id:
            continue
        profile_by_id[member_id] = {
            "id": member_id,
            "name": _bounded_text(raw.get("name"), fallback=member_id, limit=100),
            "description": _bounded_text(raw.get("description"), limit=500),
            "tags": _string_list(raw.get("tags"), limit=12, item_limit=80),
            "skills": _string_list(raw.get("skills"), limit=32, item_limit=120),
            "quickPrompts": _string_list(
                raw.get("quickPrompts"), limit=3, item_limit=300
            ),
            "deliverables": _string_list(
                raw.get("deliverables"), limit=8, item_limit=200
            ),
        }
    return [
        profile_by_id.get(
            member_id,
            {
                "id": member_id,
                "name": member_id,
                "description": "",
                "tags": [],
                "skills": [],
                "quickPrompts": [],
                "deliverables": [],
            },
        )
        for member_id in member_ids
    ]


def _validated_route_examples(
    value: Any, member_ids: list[str]
) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    routes: list[dict[str, Any]] = []
    for index, raw in enumerate(value[:18], start=1):
        if not isinstance(raw, Mapping):
            continue
        steps = _validated_advisory_workflow(raw.get("steps"), member_ids)
        if not steps:
            continue
        selected = [str(step.get("expertId") or "").strip() for step in steps]
        # Rendering validates all artifact paths and caps the resulting prompt
        # clauses; route examples are advisory, but may not carry unsafe text.
        _workflow_steps(steps, limit=6, item_limit=1200)
        route_type = str(raw.get("type") or "").strip()
        if route_type not in {"single", "serial", "parallel"}:
            route_type = (
                "serial"
                if any(step.get("dependsOn") for step in steps)
                else ("single" if len(steps) == 1 else "parallel")
            )
        routes.append(
            {
                "id": _bounded_text(
                    raw.get("id"), fallback=f"route-{index}", limit=160
                ),
                "type": route_type,
                "title": _bounded_text(raw.get("title"), limit=160),
                "intent": _bounded_text(raw.get("intent"), limit=500),
                "selectedMemberIds": selected,
                "steps": [dict(step) for step in steps],
                "relationEdgeIds": _string_list(
                    raw.get("relationEdgeIds"), limit=8, item_limit=160
                ),
            }
        )
    return routes


def _leader_dynamic_routing(
    member_profiles: list[dict[str, Any]], route_examples: list[dict[str, Any]]
) -> str:
    lines = [
        "## 成员能力路由表",
        "",
        "| member ID | 擅长内容 | 可交付 |",
        "| --- | --- | --- |",
    ]
    for profile in member_profiles:
        capabilities = "、".join(
            [
                *profile.get("tags", [])[:4],
                *profile.get("skills", [])[:4],
            ]
        )
        if not capabilities:
            capabilities = str(profile.get("description") or "按成员专业说明执行")[:120]
        deliverables = "、".join(profile.get("deliverables", [])[:4]) or "按 Query 交付"
        lines.append(f"| `{profile['id']}` | {capabilities} | {deliverables} |")
    lines.extend(
        [
            "",
            "## 任务创建约束",
            "",
            "- task_id 使用 `{run_key}-s01`、`{run_key}-s02`…；assignee 必须逐字使用上表 member ID。",
            "- depends_on 只允许引用本次已选任务的 task_id，绝不能填写 member ID。",
            "- 批量创建失败或恢复时先查任务看板；已有相同 task_id 就复用，禁止重复创建。",
        ]
    )
    if route_examples:
        lines.extend(["", "## 可调整的路由示例（不是固定流程）", ""])
        for route in route_examples[:8]:
            selected = "、".join(f"`{item}`" for item in route["selectedMemberIds"])
            intent = str(route.get("intent") or route.get("title") or "")
            lines.append(f"- {route['type']}：{intent} → {selected}")
    return "\n".join(lines)


def _validated_advisory_workflow(
    workflow: Any, member_ids: list[str]
) -> list[Mapping[str, Any]]:
    """Validate one optional route without requiring it to cover the roster."""

    if workflow in (None, [], ()):
        return []
    if not isinstance(workflow, (list, tuple)):
        raise TeamMaterializationError("candidate workflow must be a route list")
    if not all(isinstance(item, Mapping) for item in workflow):
        raise TeamMaterializationError(
            "candidate workflow must contain structured expert stages"
        )
    steps = list(workflow)
    expert_ids = [str(item.get("expertId") or "").strip() for item in steps]
    if any(
        not expert_id or expert_id not in member_ids for expert_id in expert_ids
    ) or len(set(expert_ids)) != len(expert_ids):
        raise TeamMaterializationError(
            "candidate workflow must use unique experts from memberIds"
        )

    stage_by_expert = {expert_id: index for index, expert_id in enumerate(expert_ids)}
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
    return steps


def _embed_member(
    source: Path,
    destination: Path,
    *,
    member_id: str,
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
        skill_names=_declared_skill_names(source, manifest),
    )
    _write_json(destination / "manifest.json", embedded)


def _inject_team_stage_persona(
    member_root: Path,
    manifest: Mapping[str, Any],
    *,
    member_id: str,
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
    override.write_text(_team_stage_persona(member_id, skill_names), encoding="utf-8")


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


def _team_stage_persona(member_id: str, skill_names: list[str]) -> str:
    """Render query-scoped member rules without imposing a graph stage."""
    lines = [
        "# 专家团按需执行覆盖规则（最高优先级）",
        "",
        "本文件仅在当前专家团中生效；如与原 persona 的产物要求冲突，以本文件为准。",
        "收到已指派任务后，先确认任务已进入可执行状态；仅当运行时提供 `claim_task` "
        "且任务仍为 pending 时调用，scheduled 已自动进入 in_progress 时直接执行，禁止重复领取。",
        "",
        "## 本次任务边界",
        "",
        f"你是成员 `{member_id}`。只有主理人把本次 Query 的任务指派给你时才执行；"
        "未被选择时保持空闲，不得自行加入。",
        "只完成任务描述明确要求的范围。专家包声明的全部能力和历史产物不是本次必做清单；"
        "不得为了展示能力额外生成无关文件。",
        "若任务含 `depends_on`，必须读取主理人/前序任务指定的交接材料后再继续；"
        "若任务要求给下游交接，使用 `.expert-handoffs/` 下的安全相对路径。",
        "完成后检查本次实际成品，通过 `send_message` 向 `team-leader` 汇报结果和文件路径；"
        "不要直接调度其他成员，也不要冒充主理人做最终汇总。",
    ]
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
    *,
    member_profiles: list[dict[str, Any]],
    route_examples: list[dict[str, Any]],
) -> str:
    examples = (
        [
            f"{route['type']}：{route.get('intent') or route.get('title')} → "
            + "、".join(f"`{member_id}`" for member_id in route["selectedMemberIds"])
            for route in route_examples[:8]
        ]
        or workflow
        or [
            f"可按需选择专家 `{member_id}` 独立完成匹配的任务"
            for member_id in member_ids
        ]
    )
    example_text = "\n".join(
        f"{index}. {example}" for index, example in enumerate(examples, 1)
    )
    member_text = "、".join(f"`{member_id}`" for member_id in member_ids)
    deliverable_text = "、".join(deliverables) or "一个可直接使用并可打开的最终成品"
    profile_text = "；".join(
        f"`{profile['id']}`："
        + (
            "、".join([*profile.get("tags", [])[:3], *profile.get("skills", [])[:3]])
            or str(profile.get("description") or "专业任务")[:100]
        )
        for profile in member_profiles
    )
    route_types = sorted({str(route.get("type") or "") for route in route_examples})
    return (
        f"你们是{name}。可用专业成员为 {member_text}。\n\n"
        f"## 成员能力\n{profile_text}\n\n"
        f"## 图谱提供的可选路线示例\n{example_text}\n\n"
        "关系图和路线示例仅用于发现与路由，不是固定工作流。主理人必须根据每次用户 Query "
        "选择最小充分的 1～N 位成员，只为选中成员创建任务；简单任务允许单成员直达，"
        "独立子任务并行，确有产物依赖时串行。"
        f"当前已知路线类型：{'、'.join(route_types) or 'single'}。"
        "scheduled scheduler 负责依赖放行，不得手工提前启动下游。"
        f"可能的交付能力包括：{deliverable_text}；只生成当前 Query 实际要求的成品。"
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
