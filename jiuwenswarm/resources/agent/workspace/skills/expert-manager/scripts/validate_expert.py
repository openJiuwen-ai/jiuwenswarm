#!/usr/bin/env python3
"""Expert Validator — jiuwenswarm 专家包校验。

优先调用运行中的 jiuwenswarm 真实校验器
（``jiuwenswarm.server.runtime.expert.expert_store.validate_expert_package``），
不可 import 时回退到本脚本内置的同构实现（与源码校验规则保持一致）。

Usage:
    validate_expert.py <path/to/expert-dir>

Example:
    python3 validate_expert.py ~/.jiuwenswarm/agent/workspace/experts/my-expert
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Windows 默认 GBK 控制台无法编码 emoji，强制 UTF-8 输出
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        lines: list[str] = []
        if self.errors:
            lines.append(f" {len(self.errors)} 个错误:")
            for e in self.errors:
                lines.append(f"   • {e}")
        if self.warnings:
            lines.append(f"  {len(self.warnings)} 个警告:")
            for w in self.warnings:
                lines.append(f"   • {w}")
        if self.is_valid:
            lines.append(" 专家包校验通过")
        return "\n".join(lines)


# ───────────────────────── 真实校验器代理 ─────────────────────────

def _try_real_validator(package_dir: Path) -> tuple[bool, list[str], list[str]] | None:
    """尝试 import jiuwenswarm 真实校验器；成功返回 (ok, errors, warnings)，失败返回 None。"""
    try:
        from jiuwenswarm.server.runtime.expert.expert_store import (  # type: ignore
            InvalidExpertPackage,
            validate_expert_package,
        )
    except Exception:
        return None

    try:
        warnings = validate_expert_package(package_dir)
        return True, [], warnings
    except InvalidExpertPackage as exc:
        return False, [str(exc)], []
    except Exception:
        # 真实校验器异常，回退到内置实现
        return None


# ───────────────────────── 内置同构实现（回退） ─────────────────────────

def _read_json(path: Path, *, label: str, result: ValidationResult) -> dict | None:
    if not path.is_file():
        result.error(f"{label} 缺失: {path}")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.error(f"{label} 不是合法 JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        result.error(f"{label} 必须是 JSON 对象")
        return None
    return payload


def _safe_component(value: Any, *, label: str, result: ValidationResult) -> str | None:
    if not isinstance(value, str):
        result.error(f"{label} 必须是字符串: {value!r}")
        return None
    name = value.strip()
    if not name or name in {".", ".."}:
        result.error(f"{label} 非法: {value!r}")
        return None
    if "/" in name or "\\" in name:
        result.error(f"{label} 不允许路径分隔符: {name!r}")
        return None
    return name


def _validate_agent_template(member_dir: Path, *, is_leader: bool, result: ValidationResult) -> None:
    """校验单个 agent_template 子包（leader / member 通用；单专家根包同规）。"""
    manifest = _read_json(member_dir / "manifest.json", label=f"{member_dir.name} 的 manifest", result=result)
    if manifest is None:
        return

    if manifest.get("packageType") != "agent_template":
        result.error(f"{member_dir.name}: packageType 必须是 'agent_template'，当前 {manifest.get('packageType')!r}")
        return

    # rails / subagents 禁止
    for forbidden in ("rails", "subagents"):
        if forbidden in manifest:
            result.error(f"{member_dir.name}: 不允许声明 {forbidden}")

    # agentCard.id / name
    card = manifest.get("agentCard")
    if not isinstance(card, dict) or not card.get("id") or not card.get("name"):
        result.error(f"{member_dir.name}: agentCard.id / agentCard.name 缺失")
    elif str(card["id"]) != member_dir.name:
        result.error(
            f"{member_dir.name}: agentCard.id（{card['id']}）与目录名（{member_dir.name}）不一致"
        )

    # persona.dir
    persona = manifest.get("persona")
    if not isinstance(persona, dict) or not persona.get("dir"):
        result.error(f"{member_dir.name}: persona.dir 缺失")
    else:
        raw_dir = str(persona["dir"])
        if Path(raw_dir).expanduser().is_absolute():
            result.error(f"{member_dir.name}: persona.dir 必须是包内相对路径: {raw_dir!r}")
        else:
            persona_dir = (member_dir / raw_dir).resolve()
            if not persona_dir.is_relative_to(member_dir.resolve()) or not persona_dir.is_dir():
                result.error(f"{member_dir.name}: persona 目录不存在或逃逸: {raw_dir!r}")
            elif not list(persona_dir.rglob("*.md")):
                result.error(f"{member_dir.name}: persona 目录没有 markdown 文件: {raw_dir!r}")

    # AGENT.md：leader 必有，member 禁有
    agent_md = member_dir / "AGENT.md"
    if is_leader:
        if not agent_md.is_file():
            result.error(f"leader 必须包含 AGENT.md: {member_dir}")
    else:
        if agent_md.exists() or agent_md.is_symlink():
            result.error(f"{member_dir.name}: 成员不允许包含 AGENT.md（职责请写进 persona）")

    # tools 引用文件必须存在
    pkg_root = member_dir.resolve()
    for tool_entry in manifest.get("tools") or []:
        tool_file = tool_entry.get("file") if isinstance(tool_entry, dict) else None
        if not tool_file:
            result.error(f"{member_dir.name}: tools 条目缺少 file: {tool_entry!r}")
            continue
        tool_path = (member_dir / str(tool_file)).resolve()
        if pkg_root not in tool_path.parents and tool_path != pkg_root:
            result.error(f"{member_dir.name}: tools 条目路径逃逸包目录：{tool_file!r}")
            continue
        if not tool_path.is_file():
            result.error(f"{member_dir.name}: tools 条目引用的文件不存在：{tool_entry!r}")

    # skills 目录叶子形态（直接含 SKILL.md）
    pkg_root = member_dir.resolve()
    for skill_entry in manifest.get("skills") or []:
        skill_dir_raw = skill_entry.get("dir") if isinstance(skill_entry, dict) else None
        if not skill_dir_raw:
            result.error(f"{member_dir.name}: skills 条目缺少 dir: {skill_entry!r}")
            continue
        skill_path = (member_dir / str(skill_dir_raw)).resolve()
        if pkg_root not in skill_path.parents and skill_path != pkg_root:
            result.error(f"{member_dir.name}: skills dir 逃逸包目录: {skill_dir_raw}")
        elif not skill_path.is_dir():
            result.error(f"{member_dir.name}: skills dir 不存在: {skill_dir_raw}")
        elif not (skill_path / "SKILL.md").is_file():
            result.error(f"{member_dir.name}: skills dir 下缺少 SKILL.md: {skill_dir_raw}")

    # model 字段无效（仅警告）
    if "model" in manifest:
        result.warn(f"{member_dir.name}: model 字段不生效，请移除")


def _validate_single_expert(package_dir: Path, result: ValidationResult) -> None:
    """单专家（agent_template）根包校验。"""
    manifest = _read_json(package_dir / "manifest.json", label="manifest.json", result=result)
    if manifest is None:
        return
    if manifest.get("packageType") != "agent_template":
        result.error(f"packageType 必须是 agent_template，当前 {manifest.get('packageType')!r}")
        return
    # metadata.avatar 文件必须存在
    avatar = (manifest.get("metadata") or {}).get("avatar")
    if avatar:
        avatar_path = (package_dir / str(avatar)).resolve()
        if package_dir.resolve() not in avatar_path.parents or not avatar_path.is_file():
            result.error(f"metadata.avatar 声明的头像文件不存在: {avatar}")
    _validate_agent_template(package_dir, is_leader=False, result=result)


def _validate_agent_group(package_dir: Path, result: ValidationResult) -> None:
    """专家团（agent_group）根包校验。"""
    manifest = _read_json(package_dir / "manifest.json", label="顶层 manifest", result=result)
    if manifest is None:
        return
    if manifest.get("package_type") != "agent_group":
        result.error(f"package_type 必须是 agent_group，当前 {manifest.get('package_type')!r}")
        return
    if str(manifest.get("name", "")) != package_dir.name:
        result.error(f"顶层 manifest name 必须等于目录名: {manifest.get('name')!r} != {package_dir.name!r}")

    # agents 列表
    raw_agents = manifest.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        result.error("顶层 manifest agents 必须是非空列表")
        return
    seen: set[str] = set()
    for raw in raw_agents:
        name = _safe_component(raw, label="成员名", result=result)
        if name is None:
            continue
        if name in seen:
            result.error(f"agents 存在重复成员名: {name!r}")
        seen.add(name)
    if "leader" not in seen:
        result.error("顶层 manifest agents 必须包含 'leader'")

    # instruction
    instruction = manifest.get("instruction", "")
    if not isinstance(instruction, str):
        result.error("顶层 manifest instruction 必须是字符串")

    # 共享 skills（顶层 skills/<name>/SKILL.md）
    raw_skills = manifest.get("skills", [])
    if not isinstance(raw_skills, list):
        result.error("顶层 manifest skills 必须是列表")
    else:
        skills_seen: set[str] = set()
        for raw in raw_skills:
            name = _safe_component(raw, label="共享技能名", result=result)
            if name is None:
                continue
            if name in skills_seen:
                result.error(f"skills 存在重复技能名: {name!r}")
            skills_seen.add(name)
            skill_md = package_dir / "skills" / name / "SKILL.md"
            if not skill_md.is_file():
                result.error(f"共享技能 {name!r} 缺少 skills/{name}/SKILL.md")

    # 各成员子包
    agents_root = package_dir / "agents"
    if not agents_root.is_dir():
        result.error("agents/ 目录不存在")
        return
    for agent_name in seen:
        member_dir = agents_root / agent_name
        if not member_dir.is_dir():
            result.error(f"成员目录不存在: agents/{agent_name}")
            continue
        _validate_agent_template(member_dir, is_leader=(agent_name == "leader"), result=result)


def _validate_builtin(package_dir: Path) -> ValidationResult:
    """内置同构校验（不依赖 jiuwenswarm 包）。"""
    result = ValidationResult()
    if not package_dir.is_dir():
        result.error(f"不是目录: {package_dir}")
        return result
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        result.error("manifest.json 缺失")
        return result
    manifest = _read_json(manifest_path, label="manifest.json", result=result)
    if manifest is None:
        return result
    if "package_type" in manifest:
        _validate_agent_group(package_dir, result)
    else:
        _validate_single_expert(package_dir, result)
    return result


# ───────────────────────── 入口 ─────────────────────────

def validate_expert(expert_path: str | Path) -> ValidationResult:
    package_dir = Path(expert_path).expanduser().resolve()
    # 优先真实校验器
    real = _try_real_validator(package_dir)
    if real is not None:
        ok, errors, warnings = real
        result = ValidationResult()
        for e in errors:
            result.error(e)
        for w in warnings:
            result.warn(w)
        return result
    # 回退内置实现
    return _validate_builtin(package_dir)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python3 validate_expert.py <path/to/expert-dir>")
        print("\nExample:")
        print("  python3 validate_expert.py ~/.jiuwenswarm/agent/workspace/experts/my-expert")
        sys.exit(1)

    expert_path = sys.argv[1]
    print(f" 校验专家包: {expert_path}\n")
    result = validate_expert(expert_path)
    print(result.summary())
    sys.exit(0 if result.is_valid else 1)


if __name__ == "__main__":
    main()
