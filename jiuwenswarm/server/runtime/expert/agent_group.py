# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""专家团（AgentGroup）包严格加载器与校验。

```text
<expert_id>/                  # 目录名 = 包名 = expert_id
├── manifest.json             # 顶层 agent_group manifest（snake_case）
├── agents/
│   ├── leader/               # 固定主理人：manifest.json + AGENT.md + persona/
│   └── <member>/             # 成员：manifest.json + persona/（禁 AGENT.md）
└── skills/<name>/SKILL.md    # 顶层共享技能
```

- 顶层 manifest（snake_case）：``name``（=目录名）、``package_type="agent_group"``、
  ``agents``（非空无重复、必含 ``leader``）、``instruction``（可选，全员共享协作契约）、
  ``skills``（可选，共享技能目录名列表，默认 []）。
- 成员子包（camelCase，core schema）：``packageType="agent_template"``；身份两形态
  均接受——嵌套 ``agentCard``（id 须=成员目录名）或平铺 ``name``/``description``
  （id 由目录名派生）；``persona.dir`` 必填；leader 必有 ``AGENT.md``，member 禁
  ``AGENT.md``；禁 ``rails``/``subagents``（mcps 与单专家同策略，不额外加禁）。

任一校验失败**整体终止**，不允许部分成员成功（失败全终止原则）。
"""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any

from openjiuwen.harness.resources import load_agent_template_package
from openjiuwen.harness.schema.extension_spec import (
    AgentTemplateSpec,
    PromptSectionSpec,
    SkillSpec,
)

from jiuwenswarm.server.runtime.expert.team_contract import (
    EXPERT_TEAM_STAGE_METADATA_KEY,
)

INSTRUCTION_SECTION_NAME = "agent_group_instruction"
LEADER_RULES_SECTION_NAME = "agent_group_leader_rules"
TEAM_STAGE_SECTION_NAME = "expert_team_stage_override"


class AgentGroupPackageError(ValueError):
    """专家团包结构/内容非法（加载期；校验期由 validate 包装为 INVALID_PACKAGE）。"""


def _read_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentGroupPackageError(f"{label} 缺失: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentGroupPackageError(f"{label} 不是合法 JSON: {path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise AgentGroupPackageError(f"{label} 必须是 JSON 对象: {path}")
    return payload


def _safe_component(value: Any, *, label: str) -> str:
    """单段安全目录名：非空、无路径分隔符、非绝对路径、非 . / ..。"""
    if not isinstance(value, str):
        raise AgentGroupPackageError(f"{label} 必须是字符串: {value!r}")
    name = value.strip()
    if not name or name in {".", ".."}:
        raise AgentGroupPackageError(f"{label} 非法: {value!r}")
    if "/" in name or "\\" in name:
        raise AgentGroupPackageError(f"{label} 不允许路径分隔符: {name!r}")
    if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
        raise AgentGroupPackageError(f"{label} 不允许绝对路径: {name!r}")
    return name


def _child_dir(root: Path, name: str, *, label: str) -> Path:
    try:
        candidate = (root / name).resolve(strict=True)
    except OSError as exc:
        raise AgentGroupPackageError(f"{label} 不存在: {root / name}") from exc
    if not candidate.is_relative_to(root):
        raise AgentGroupPackageError(f"{label} 逃逸包目录: {name!r}")
    if not candidate.is_dir():
        raise AgentGroupPackageError(f"{label} 不是目录: {candidate}")
    return candidate


def _package_file(path: Path, *, root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AgentGroupPackageError(f"{label} 缺失: {path}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise AgentGroupPackageError(f"{label} 必须是包内文件: {path}")
    return resolved


def _persona_dir(agent_dir: Path, manifest: dict[str, Any], *, agent_name: str) -> Path:
    persona = manifest.get("persona")
    if not isinstance(persona, dict) or not isinstance(persona.get("dir"), str):
        raise AgentGroupPackageError(f"成员 {agent_name!r} 缺少 persona.dir 声明")
    raw_dir = persona["dir"]
    if Path(raw_dir).expanduser().is_absolute():
        raise AgentGroupPackageError(
            f"成员 {agent_name!r} persona.dir 必须是包内相对路径: {raw_dir!r}"
        )
    try:
        resolved = (agent_dir / raw_dir).resolve(strict=True)
    except OSError as exc:
        raise AgentGroupPackageError(
            f"成员 {agent_name!r} persona.dir 不存在: {raw_dir!r}"
        ) from exc
    if not resolved.is_relative_to(agent_dir) or not resolved.is_dir():
        raise AgentGroupPackageError(
            f"成员 {agent_name!r} persona.dir 逃逸成员包目录: {raw_dir!r}"
        )
    markdown_files = [p for p in resolved.rglob("*.md") if p.is_file()]
    if not markdown_files:
        raise AgentGroupPackageError(
            f"成员 {agent_name!r} persona 目录没有 markdown 文件: {raw_dir!r}"
        )
    for markdown_file in markdown_files:
        _package_file(
            markdown_file, root=agent_dir, label=f"成员 {agent_name!r} persona 文件"
        )
    return resolved


def _agent_names(payload: dict[str, Any]) -> list[str]:
    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise AgentGroupPackageError("顶层 manifest agents 必须是非空列表")
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_agents:
        name = _safe_component(raw_name, label="成员名")
        if name in seen:
            raise AgentGroupPackageError(
                f"顶层 manifest agents 存在重复成员名: {name!r}"
            )
        seen.add(name)
        names.append(name)
    if "leader" not in seen:
        raise AgentGroupPackageError("顶层 manifest agents 必须包含 'leader'")
    return names


def _shared_skills(package_dir: Path, payload: dict[str, Any]) -> list[SkillSpec]:
    raw_skills = payload.get("skills", [])
    if not isinstance(raw_skills, list):
        raise AgentGroupPackageError("顶层 manifest skills 必须是列表")
    specs: list[SkillSpec] = []
    seen: set[str] = set()
    if not raw_skills:
        return specs
    skills_root = _child_dir(package_dir, "skills", label="共享技能目录")
    for raw_name in raw_skills:
        name = _safe_component(raw_name, label="共享技能名")
        if name in seen:
            raise AgentGroupPackageError(
                f"顶层 manifest skills 存在重复技能名: {name!r}"
            )
        seen.add(name)
        skill_dir = _child_dir(skills_root, name, label=f"共享技能 {name!r}")
        _package_file(
            skill_dir / "SKILL.md",
            root=skill_dir,
            label=f"共享技能 {name!r} 的 SKILL.md",
        )
        specs.append(SkillSpec(dir=str(skill_dir), mode="all"))
    return specs


def _load_member_template(package_dir: Path, agent_name: str) -> AgentTemplateSpec:
    agents_root = _child_dir(package_dir, "agents", label="agents 目录")
    agent_dir = _child_dir(agents_root, agent_name, label=f"成员 {agent_name!r} 目录")
    manifest_path = _package_file(
        agent_dir / "manifest.json",
        root=agent_dir,
        label=f"成员 {agent_name!r} 的 manifest.json",
    )
    manifest = _read_mapping(manifest_path, label=f"成员 {agent_name!r} 的 manifest")
    if manifest.get("packageType") != "agent_template":
        raise AgentGroupPackageError(
            f"成员 {agent_name!r} packageType 必须是 'agent_template'"
        )
    for forbidden_key in ("rails", "subagents"):
        if forbidden_key in manifest:
            raise AgentGroupPackageError(
                f"成员 {agent_name!r} 不允许声明 {forbidden_key}"
            )
    persona_dir = _persona_dir(agent_dir, manifest, agent_name=agent_name)

    agent_md = agent_dir / "AGENT.md"
    if agent_name == "leader":
        agent_md = _package_file(agent_md, root=agent_dir, label="leader 的 AGENT.md")
    elif agent_md.exists() or agent_md.is_symlink():
        raise AgentGroupPackageError(
            f"成员 {agent_name!r} 不允许包含 AGENT.md（职责请写进 persona）"
        )

    template = load_agent_template_package(manifest_path)
    if template.agent_card.id != agent_name:
        raise AgentGroupPackageError(
            f"成员 {agent_name!r} 身份不一致: 目录名={agent_name!r}, "
            f"agentCard.id={template.agent_card.id!r}"
        )

    metadata = manifest.get("metadata")
    stage_file = (
        metadata.get(EXPERT_TEAM_STAGE_METADATA_KEY)
        if isinstance(metadata, dict)
        else None
    )
    if stage_file is not None:
        if not isinstance(stage_file, str) or not stage_file.strip():
            raise AgentGroupPackageError(
                f"成员 {agent_name!r} 的 {EXPERT_TEAM_STAGE_METADATA_KEY} 必须是非空字符串"
            )
        stage_path = _package_file(
            agent_dir / stage_file,
            root=agent_dir,
            label=f"成员 {agent_name!r} 的专家团阶段规则",
        )
        stage_text = stage_path.read_text(encoding="utf-8").strip()
        if not stage_text:
            raise AgentGroupPackageError(
                f"成员 {agent_name!r} 的专家团阶段规则不能为空"
            )
        if any(s.name == TEAM_STAGE_SECTION_NAME for s in template.prompt_sections):
            raise AgentGroupPackageError(
                f"成员 {agent_name!r} 占用了保留 section 名 {TEAM_STAGE_SECTION_NAME!r}"
            )
        stage_priority = (
            max([20, *(section.priority for section in template.prompt_sections)]) + 1
        )
        stage_section = PromptSectionSpec(
            name=TEAM_STAGE_SECTION_NAME,
            content={"cn": stage_text, "en": stage_text},
            # This is an override contract, so it must flatten after every
            # source-owned section as well as the group instruction (priority 20).
            priority=stage_priority,
        )
        template = template.model_copy(
            update={"prompt_sections": [*template.prompt_sections, stage_section]}
        )

    # leader 常规约定 persona.dir="."，core loader 会把 AGENT.md 一并读入；
    # 若 persona 目录更窄，则显式补挂 AGENT.md（仅一次）保住协议。
    if agent_name == "leader" and not agent_md.resolve().is_relative_to(persona_dir):
        rules_text = agent_md.read_text(encoding="utf-8")
        leader_rules = PromptSectionSpec(
            name=LEADER_RULES_SECTION_NAME,
            content={"cn": rules_text, "en": rules_text},
            priority=15,
        )
        template = template.model_copy(
            update={"prompt_sections": [*template.prompt_sections, leader_rules]}
        )
    return template


def load_agent_group_package(path: Path) -> dict[str, AgentTemplateSpec]:
    """严格加载一个专家团包，返回按顶层 agents 顺序排列的成员模板字典。

    产物语义：instruction 已注入为 ``agent_group_instruction`` prompt section
    （priority 20，重名即报错）；共享 skills 以绝对路径 SkillSpec 去重合并进
    各成员 skills。成员模板经 agent-core loader 二次解析（双保险）。
    """
    try:
        package_dir = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise AgentGroupPackageError(f"专家团包目录不存在: {path}") from exc
    if not package_dir.is_dir():
        raise AgentGroupPackageError(f"专家团包路径不是目录: {path}")

    manifest_path = _package_file(
        package_dir / "manifest.json", root=package_dir, label="顶层 manifest.json"
    )
    payload = _read_mapping(manifest_path, label="顶层 manifest")
    if payload.get("package_type") != "agent_group":
        raise AgentGroupPackageError("顶层 manifest package_type 必须是 'agent_group'")
    if payload.get("name") != package_dir.name:
        raise AgentGroupPackageError(
            f"顶层 manifest name 必须等于目录名: "
            f"{payload.get('name')!r} != {package_dir.name!r}"
        )

    instruction = payload.get("instruction", "")
    if not isinstance(instruction, str):
        raise AgentGroupPackageError("顶层 manifest instruction 必须是字符串")
    instruction = instruction.strip()
    shared_skills = _shared_skills(package_dir, payload)

    templates: dict[str, AgentTemplateSpec] = {}
    for agent_name in _agent_names(payload):
        template = _load_member_template(package_dir, agent_name)
        prompt_sections = list(template.prompt_sections)
        if instruction:
            if any(s.name == INSTRUCTION_SECTION_NAME for s in prompt_sections):
                raise AgentGroupPackageError(
                    f"成员 {agent_name!r} 占用了保留 section 名 "
                    f"{INSTRUCTION_SECTION_NAME!r}"
                )
            prompt_sections.append(
                PromptSectionSpec(
                    name=INSTRUCTION_SECTION_NAME,
                    content={"cn": instruction, "en": instruction},
                    priority=20,
                )
            )
        skills = list(template.skills)
        skill_dirs = {str(Path(skill.dir).resolve()) for skill in skills}
        for skill in shared_skills:
            resolved_dir = str(Path(skill.dir).resolve())
            if resolved_dir not in skill_dirs:
                skills.append(skill)
                skill_dirs.add(resolved_dir)
        templates[agent_name] = template.model_copy(
            update={"prompt_sections": prompt_sections, "skills": skills}
        )
    return templates


def validate_agent_group_package(package_dir: Path) -> list[str]:
    """校验专家团包（= 完整跑一遍严格加载器），返回 warnings。

    非法抛 ``InvalidExpertPackage``（惰性 import 避免与 expert_store 循环依赖）。
    """
    from jiuwenswarm.server.runtime.expert.expert_store import InvalidExpertPackage

    try:
        load_agent_group_package(package_dir)
    except (AgentGroupPackageError, OSError, ValueError) as exc:
        raise InvalidExpertPackage(str(exc)) from exc

    warnings: list[str] = []
    agents_root = package_dir / "agents"
    if agents_root.is_dir():
        for member_dir in sorted(agents_root.iterdir()):
            manifest_path = member_dir / "manifest.json"
            if not member_dir.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and "model" in manifest:
                warnings.append(
                    f"成员 {member_dir.name} 的 model 字段不生效（团队模型分配走 team 线），请移除"
                )
    return warnings


def read_group_display(package_dir: Path) -> dict[str, str]:
    """列表展示用：展示名 + 描述（尽力而为，不抛错）。

    展示名优先级：顶层 metadata.displayName > 顶层 name（=目录名）；
    描述优先级：顶层 metadata.description > leader 子包描述。
    """
    display = {"name": package_dir.name, "description": ""}
    try:
        top = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        if isinstance(top, dict):
            metadata = top.get("metadata")
            if isinstance(metadata, dict) and metadata.get("displayName"):
                display["name"] = str(metadata["displayName"])
            elif top.get("name"):
                display["name"] = str(top["name"])
            if isinstance(metadata, dict) and metadata.get("description"):
                display["description"] = str(metadata["description"])
        leader_manifest = package_dir / "agents" / "leader" / "manifest.json"
        if not display["description"]:
            leader = json.loads(leader_manifest.read_text(encoding="utf-8"))
            if isinstance(leader, dict):
                card = leader.get("agentCard")
                if isinstance(card, dict) and card.get("description"):
                    display["description"] = str(card["description"])
                elif leader.get("description"):
                    display["description"] = str(leader["description"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return display


def read_group_members(package_dir: Path) -> list[dict[str, Any]]:
    """列表展示用：成员摘要（leader 置顶），尽力而为、不抛错。

    返回 [{"id", "name", "description", "role"}]，role 为 "lead" | "member"；
    身份取成员 manifest 的嵌套 agentCard 或平铺 name/description。
    """
    try:
        top = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        raw_agents = top.get("agents") if isinstance(top, dict) else None
        if not isinstance(raw_agents, list):
            return []
        # leader 置顶，其余按声明序
        ordered = sorted(raw_agents, key=lambda n: 0 if n == "leader" else 1)
        members: list[dict[str, Any]] = []
        for raw_name in ordered:
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            member_dir = package_dir / "agents" / name
            manifest_path = member_dir / "manifest.json"
            display_name, description = name, ""
            manifest: dict[str, Any] = {}
            try:
                parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    manifest = parsed
                    card = manifest.get("agentCard")
                    if isinstance(card, dict):
                        display_name = str(card.get("name") or name)
                        description = str(card.get("description") or "")
                    else:
                        display_name = str(manifest.get("name") or name)
                        description = str(manifest.get("description") or "")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            # 只展示该成员 manifest 真正挂载的原始 Skill；禁止将顶层共享
            # Skill 或其他成员 Skill 误标为该成员私有能力。
            member_skills: list[dict[str, str]] = []
            if isinstance(manifest, dict):
                from jiuwenswarm.server.runtime.expert.expert_store import (
                    _read_expert_skill_summaries,
                )

                member_skills = _read_expert_skill_summaries(member_dir, manifest)
            members.append(
                {
                    "id": name,
                    "name": display_name,
                    "description": description,
                    "role": "lead" if name == "leader" else "member",
                    "skills": member_skills,
                    # 成员头像（avatars/<id>.png 存在时）：本地源给绝对路径（前端只认
                    # http(s) 直链，本地路径会回退首字头像）；仓库源由仓库下发 URL
                    **(
                        {
                            "avatar": str(
                                (package_dir / "avatars" / f"{name}.png").resolve()
                            )
                        }
                        if (package_dir / "avatars" / f"{name}.png").is_file()
                        else {}
                    ),
                }
            )
        return members
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []


__all__ = [
    "AgentGroupPackageError",
    "load_agent_group_package",
    "validate_agent_group_package",
    "read_group_display",
    "read_group_members",
]
