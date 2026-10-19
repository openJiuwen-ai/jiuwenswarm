# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adapt converted XiaoYi AgentGroup contents to WorkSwarm-native capabilities.

The input must be one already-converted AgentGroup directory. Package-structure
normalization is intentionally handled by ``convert_xiaoyi_agent_groups.py``;
this script owns only the independently reviewable Skill/tool adaptation step.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MAPPING_PATH = SCRIPT_DIR / "workswarm_skill_replacements.json"
PORTABLE_SKILLS_ROOT = SCRIPT_DIR / "portable_skills"
README_PENDING_LINE = "- Skill 内容保持来源包原样，平台 Skill 替换需另行完成。"
README_ADAPTED_LINE = (
    "- 已完成 WorkSwarm 平台适配；需替换的平台专属能力已接入原生工具，"
    "其余通用 Skill 按原名称保留。"
)
PLATFORM_TEXT_REPLACEMENTS = {
    "小艺 Work": "WorkSwarm",
    "小艺侧模型路由": "原平台侧模型路由",
    "mcp_free_search": "free_search",
    "mcp_fetch_webpage": "fetch_webpage",
    "小艺不可用时明确说明，不使用第三方媒体降级。": (
        "WorkSwarm 媒体生成能力不可用时明确说明，不使用未配置的第三方服务降级。"
    ),
    "## 仓库内 Xiaoyi Skill 适配": "## WorkSwarm 原生媒体能力适配",
    (
        "`workswarm-image-generation` 与 `workswarm-video-generation` "
        "来自仓库内既有 Xiaoyi Skill，不是 ClawHub 下载项。"
        "两项底层 Skill 保持原文不变，家庭烹饪不得修改其模型路由、参数校验、"
        "确认、任务轮询或文件交付正文。"
    ): (
        "`workswarm-image-generation` 与 `workswarm-video-generation` 是本次迁移"
        "生成的 portable Skills，分别绑定 WorkSwarm 的 `generate_image` 与 "
        "`generate_video` / `check_video_status` 原生工具。不得继承原平台侧模型路由、"
        "积分或凭据约定；确认、轮询和文件交付遵循当前 WorkSwarm 契约。"
    ),
}
PORTABILITY_TEXT_REPLACEMENTS = {
    (
        'openclaw cron add --name "autonomous-tasks" '
        '--message "run autonomous tasks" --every 1h'
    ): (
        "在 WorkSwarm 中通过内置定时任务能力创建提醒；调用运行时提供的 "
        "`cron_create_job`（或等价 `cron` add 操作），参数以当前工具 schema 为准"
    ),
    "/home/sirius/.openclaw/skills/natural-language-planner": (
        "<SKILL_INSTALL_DIR>/natural-language-planner"
    ),
}
PORTABILITY_FORBIDDEN_TOKENS = (
    "openclaw cron add",
    "/.openclaw/skills/",
    "小艺 Work",
    "小艺侧模型路由",
    "mcp_free_search",
    "mcp_fetch_webpage",
)
ADAPTABLE_TEXT_SUFFIXES = frozenset(
    {".json", ".md", ".rst", ".toml", ".txt", ".yaml", ".yml"}
)
ADAPTABLE_TEXT_FILENAMES = frozenset({"NOTICE"})
NON_LEADER_DELIVERY = (
    "非主理人成员只将文件作为内部产物交给主理人，"
    "不得调用 `send_file_to_user` 直接向用户发送附件。"
)
LEADER_HTML_DELIVERY = (
    "主理人必须先使用 `write_file` 将完整、自包含的 HTML 写入真实 .html "
    "文件并确认文件存在，再以该文件的绝对路径调用 `send_file_to_user`；"
    "不得把内存字符串或临时测试文件当作最终交付。"
)


class AdaptationError(ValueError):
    """Raised when an AgentGroup cannot be adapted safely."""


@dataclass
class ChangeTracker:
    """Apply deterministic filesystem mutations and record changed paths."""

    root: Path
    changed: set[str] = field(default_factory=set)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def write_text(self, path: Path, content: str) -> None:
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == content:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.changed.add(self.relative(path))

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def copy_skill(self, skill_id: str, destination: Path) -> None:
        source = PORTABLE_SKILLS_ROOT / skill_id / "SKILL.md"
        if not source.is_file():
            raise AdaptationError(f"portable Skill template not found: {skill_id}")
        self.write_text(destination / "SKILL.md", source.read_text(encoding="utf-8"))

    def delete_tree(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink():
            raise AdaptationError(f"package symlink is not supported: {path}")
        relative = self.relative(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        self.changed.add(relative)


def _is_adaptable_text_file(path: Path) -> bool:
    """Return whether platform-owned prose/config may be rewritten safely.

    AgentGroup packages can also contain user assets, generated HTML, source
    code and arbitrary binary files.  UTF-8 decodability alone is therefore
    not permission to rewrite a file.  Keep bulk normalization limited to the
    package metadata and documentation formats used by manifests, personas,
    Skills and READMEs.
    """

    return (
        path.name in ADAPTABLE_TEXT_FILENAMES
        or path.suffix.lower() in ADAPTABLE_TEXT_SUFFIXES
    )


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdaptationError(f"invalid replacement mapping: {path}") from exc
    if not isinstance(mapping, dict) or not isinstance(mapping.get("groups"), dict):
        raise AdaptationError("replacement mapping must define groups")
    return mapping


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdaptationError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AdaptationError(f"{label} must contain a JSON object: {path}")
    return payload


def _safe_path(root: Path, raw_path: str) -> Path:
    relative = PurePosixPath(raw_path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise AdaptationError(f"unsafe package-relative path: {raw_path!r}")
    target = root.joinpath(*relative.parts)
    if not target.resolve(strict=False).is_relative_to(root.resolve()):
        raise AdaptationError(f"path escapes AgentGroup root: {raw_path!r}")
    return target


def _package_identity(package_root: Path) -> tuple[Path, dict[str, Any]]:
    try:
        root = package_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AdaptationError(f"AgentGroup root not found: {package_root}") from exc
    if not root.is_dir():
        raise AdaptationError(f"AgentGroup root must be a directory: {root}")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise AdaptationError(f"package symlinks are not supported: {root}")
    manifest = _read_json(root / "manifest.json", label="AgentGroup manifest")
    if manifest.get("package_type") != "agent_group":
        raise AdaptationError("input must be an already-converted agent_group package")
    package_id = manifest.get("name")
    if not isinstance(package_id, str) or package_id != root.name:
        raise AdaptationError(
            "AgentGroup manifest name must match its root directory name"
        )
    return root, manifest


def _capability_specs(mapping: dict[str, Any]) -> dict[str, dict[str, Any]]:
    specs = mapping.get("capability_replacements")
    if not isinstance(specs, dict):
        raise AdaptationError("replacement mapping has no capability_replacements")
    for capability, spec in specs.items():
        if not isinstance(spec, dict) or not isinstance(
            spec.get("portable_skill_id"), str
        ):
            raise AdaptationError(f"capability {capability!r} has no portable_skill_id")
    return specs


def _legacy_replacements(mapping: dict[str, Any]) -> dict[str, str]:
    replacements: dict[str, str] = {}
    for spec in _capability_specs(mapping).values():
        portable_skill = spec["portable_skill_id"]
        for legacy in spec.get("legacy_skill_ids", []):
            replacements[str(legacy)] = portable_skill
        for alias in spec.get("legacy_aliases", []):
            replacements[str(alias)] = portable_skill
    return replacements


def _replace_legacy_text(text: str, replacements: dict[str, str]) -> str:
    result = text
    for legacy in sorted(replacements, key=len, reverse=True):
        result = result.replace(legacy, replacements[legacy])
    return result


def _replace_json_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _replace_legacy_text(value, replacements)
    if isinstance(value, list):
        return [_replace_json_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_json_strings(item, replacements)
            for key, item in value.items()
        }
    return value


def _skill_name(entry: Any) -> str | None:
    if isinstance(entry, str):
        return PurePosixPath(entry.replace("\\", "/")).name
    if isinstance(entry, dict) and isinstance(entry.get("dir"), str):
        return PurePosixPath(entry["dir"].replace("\\", "/")).name
    return None


def _deduplicate_skills(entries: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for entry in entries:
        name = _skill_name(entry)
        if not name:
            result.append(entry)
            continue
        if name in seen:
            continue
        seen.add(name)
        result.append(entry)
    return result


def _member_capabilities(
    mapping: dict[str, Any],
    group_id: str,
) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    inventory = mapping.get("inventory", {})
    for member in inventory.get("direct_skill_binding_members", []):
        if member.get("group_id") == group_id:
            assignments[str(member["member_id"])] = list(member["capabilities"])
    return assignments


def _root_capabilities(mapping: dict[str, Any], group_id: str) -> list[str]:
    capabilities: list[str] = []
    for policy in mapping.get("shared_skill_policies", []):
        if policy.get(
            "action"
        ) == "replace_with_native_tools" and group_id in policy.get("groups", []):
            capabilities.append("web_research")
    indirect_groups = {
        member.get("group_id")
        for member in mapping.get("inventory", {}).get(
            "indirect_native_search_reference_members", []
        )
    }
    if group_id in indirect_groups and "web_research" not in capabilities:
        capabilities.append("web_research")
    return capabilities


def _ensure_member_skills(
    root: Path,
    tracker: ChangeTracker,
    mapping: dict[str, Any],
    group_id: str,
    replacements: dict[str, str],
) -> None:
    specs = _capability_specs(mapping)
    for member_id, capabilities in _member_capabilities(mapping, group_id).items():
        manifest_path = root / "agents" / member_id / "manifest.json"
        manifest = _read_json(
            manifest_path,
            label=f"member manifest {group_id}/{member_id}",
        )
        manifest = _replace_json_strings(manifest, replacements)
        raw_skills = manifest.get("skills", [])
        if not isinstance(raw_skills, list):
            raise AdaptationError(
                f"member skills must be a list: {group_id}/{member_id}"
            )
        skills = _deduplicate_skills(raw_skills)
        existing = {_skill_name(entry) for entry in skills}
        for capability in capabilities:
            skill_id = specs[capability]["portable_skill_id"]
            if skill_id not in existing:
                skills.append({"dir": f"skills/{skill_id}", "mode": "all"})
                existing.add(skill_id)
            tracker.copy_skill(
                skill_id,
                root / "agents" / member_id / "skills" / skill_id,
            )
        manifest["skills"] = skills
        tracker.write_json(manifest_path, manifest)


def _ensure_root_skills(
    root: Path,
    tracker: ChangeTracker,
    mapping: dict[str, Any],
    group_id: str,
    replacements: dict[str, str],
) -> None:
    manifest_path = root / "manifest.json"
    manifest = _replace_json_strings(
        _read_json(manifest_path, label="AgentGroup manifest"),
        replacements,
    )
    raw_skills = manifest.get("skills", [])
    if not isinstance(raw_skills, list):
        raise AdaptationError("AgentGroup skills must be a list")
    skills = _deduplicate_skills(raw_skills)
    existing = {_skill_name(entry) for entry in skills}
    specs = _capability_specs(mapping)
    for capability in _root_capabilities(mapping, group_id):
        skill_id = specs[capability]["portable_skill_id"]
        if skill_id not in existing:
            skills.append(skill_id)
            existing.add(skill_id)
        tracker.copy_skill(skill_id, root / "skills" / skill_id)
    manifest["skills"] = skills
    tracker.write_json(manifest_path, manifest)


def _capability_markdown_block(
    capability: str,
    spec: dict[str, Any],
) -> str:
    marker = f"<!-- workswarm-adapter:capability:{capability} -->"
    tools = ", ".join(f"`{tool}`" for tool in spec["native_tools"])
    return (
        f"{marker}\n"
        f"## WorkSwarm capability: {capability}\n\n"
        f"Portable Skill: `{spec['portable_skill_id']}`. Native tools: {tools}.\n\n"
        f"{spec['persona_instruction'].strip()}\n"
    )


def _append_markdown_block(text: str, marker: str, block: str) -> str:
    if marker in text:
        return text
    return text.rstrip() + "\n\n" + block.rstrip() + "\n"


def _patch_capability_document(
    path: Path,
    tracker: ChangeTracker,
    capability: str,
    mapping: dict[str, Any],
    replacements: dict[str, str],
) -> None:
    if not path.is_file():
        raise AdaptationError(f"mapped capability document not found: {path}")
    spec = _capability_specs(mapping)[capability]
    if path.suffix.lower() == ".json":
        payload = _replace_json_strings(
            _read_json(path, label="mapped JSON document"),
            replacements,
        )
        instruction = payload.get("instruction", "")
        if not isinstance(instruction, str):
            raise AdaptationError(f"instruction must be a string: {path}")
        addition = spec["persona_instruction"].strip()
        if addition not in instruction:
            payload["instruction"] = instruction.rstrip() + "\n\n" + addition
        tracker.write_json(path, payload)
        return
    text = _replace_legacy_text(path.read_text(encoding="utf-8"), replacements)
    marker = f"<!-- workswarm-adapter:capability:{capability} -->"
    text = _append_markdown_block(
        text,
        marker,
        _capability_markdown_block(capability, spec),
    )
    tracker.write_text(path, text)


def _remove_legacy_delivery_reference(text: str) -> str:
    replacements = {
        "`send_file_to_user` 或 `send_html_card`": "`send_file_to_user`",
        "`send_html_card` 或 `send_file_to_user`": "`send_file_to_user`",
        "send_file_to_user 或 send_html_card": "send_file_to_user",
        "send_html_card 或 send_file_to_user": "send_file_to_user",
        "`send_html_card`": "HTML 文件交付",
        "send_html_card": "HTML 文件交付",
    }
    result = text
    for old, new in replacements.items():
        result = result.replace(old, new)
    return result


def _delivery_block(mode: str) -> tuple[str, str]:
    marker = f"<!-- workswarm-adapter:delivery:{mode} -->"
    if mode == "leader_html_file":
        instruction = LEADER_HTML_DELIVERY
    elif mode == "non_leader_guard":
        instruction = NON_LEADER_DELIVERY
    else:
        raise AdaptationError(f"unsupported delivery rewrite mode: {mode}")
    block = f"{marker}\n## WorkSwarm delivery contract\n\n{instruction}\n"
    return marker, block


def _patch_delivery_document(
    path: Path,
    tracker: ChangeTracker,
    mode: str,
) -> None:
    if not path.is_file():
        raise AdaptationError(f"mapped delivery document not found: {path}")
    if path.suffix.lower() == ".json":
        payload = _read_json(path, label="mapped delivery JSON")
        payload = _rewrite_json_delivery(payload)
        instruction = payload.get("instruction", "")
        if not isinstance(instruction, str):
            raise AdaptationError(f"instruction must be a string: {path}")
        additions = [NON_LEADER_DELIVERY]
        if path == tracker.root / "manifest.json":
            additions.append(LEADER_HTML_DELIVERY)
        elif mode == "leader_html_file":
            additions = [LEADER_HTML_DELIVERY]
        for addition in additions:
            if addition not in instruction:
                instruction = instruction.rstrip() + "\n\n" + addition
        payload["instruction"] = instruction
        tracker.write_json(path, payload)
        return
    text = _remove_legacy_delivery_reference(path.read_text(encoding="utf-8"))
    marker, block = _delivery_block(mode)
    text = _append_markdown_block(text, marker, block)
    tracker.write_text(path, text)


def _rewrite_json_delivery(value: Any) -> Any:
    if isinstance(value, str):
        return _remove_legacy_delivery_reference(value)
    if isinstance(value, list):
        return [_rewrite_json_delivery(item) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_json_delivery(item) for key, item in value.items()}
    return value


def _apply_shared_skill_patch(
    path: Path,
    tracker: ChangeTracker,
    skill_id: str,
    mapping: dict[str, Any],
) -> None:
    if not path.is_file():
        raise AdaptationError(f"shared Skill document not found: {path}")
    policies = {
        policy["skill_id"]: policy
        for policy in mapping.get("shared_skill_policies", [])
        if isinstance(policy, dict) and isinstance(policy.get("skill_id"), str)
    }
    policy = policies.get(skill_id)
    if not policy:
        raise AdaptationError(f"shared Skill policy not found: {skill_id}")
    text = path.read_text(encoding="utf-8")
    for patch in policy.get("required_patches", []):
        text = text.replace(patch["old"], patch["new"])
    tracker.write_text(path, text)


def _remove_notice_entries(
    path: Path,
    tracker: ChangeTracker,
    legacy_skill_ids: list[str],
) -> None:
    if not path.is_file():
        raise AdaptationError(f"NOTICE file not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    filtered = [
        line
        for line in lines
        if not any(skill_id in line for skill_id in legacy_skill_ids)
    ]
    tracker.write_text(path, "\n".join(filtered).rstrip() + "\n")


def _apply_file_changes(
    root: Path,
    tracker: ChangeTracker,
    group: dict[str, Any],
    mapping: dict[str, Any],
    replacements: dict[str, str],
) -> None:
    for change in group.get("file_changes", []):
        path = _safe_path(root, change["path"])
        operation = change["op"]
        if operation == "remove_skill_reference":
            if not path.is_file():
                raise AdaptationError(f"mapped manifest not found: {path}")
        elif operation in {
            "replace_capability_reference",
            "ensure_native_tool_instruction",
        }:
            _patch_capability_document(
                path,
                tracker,
                change["capability"],
                mapping,
                replacements,
            )
        elif operation == "rewrite_delivery_contract":
            _patch_delivery_document(path, tracker, change["mode"])
        elif operation == "delete_tree":
            tracker.delete_tree(path)
        elif operation == "apply_shared_skill_patch":
            _apply_shared_skill_patch(path, tracker, change["skill_id"], mapping)
        elif operation == "replace_provenance_note":
            for capability in change["capabilities"]:
                _patch_capability_document(
                    path,
                    tracker,
                    capability,
                    mapping,
                    replacements,
                )
        elif operation == "remove_obsolete_notice_entries":
            _remove_notice_entries(path, tracker, change["legacy_skill_ids"])
        else:
            raise AdaptationError(f"unsupported mapping operation: {operation}")


def _patch_remaining_text(
    root: Path,
    tracker: ChangeTracker,
    replacements: dict[str, str],
) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink() or not _is_adaptable_text_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = _replace_legacy_text(text, replacements)
        for source, target in PLATFORM_TEXT_REPLACEMENTS.items():
            updated = updated.replace(source, target)
        for source, target in PORTABILITY_TEXT_REPLACEMENTS.items():
            updated = updated.replace(source, target)
        if path.name == "README.md":
            updated = updated.replace(README_PENDING_LINE, README_ADAPTED_LINE)
        tracker.write_text(path, updated)


def _forbidden_tokens(mapping: dict[str, Any]) -> set[str]:
    inventory = mapping.get("inventory", {})
    return {
        *map(str, inventory.get("legacy_skill_ids", [])),
        *map(str, inventory.get("legacy_concept_aliases", [])),
        str(mapping.get("delivery_tool_rewrite", {}).get("legacy_tool", "")),
    } - {""}


def _scan_forbidden(root: Path, mapping: dict[str, Any]) -> list[str]:
    tokens = _forbidden_tokens(mapping)
    findings: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if any(token in relative for token in tokens):
            findings.append(f"forbidden path: {relative}")
        if not path.is_file() or path.is_symlink() or not _is_adaptable_text_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        matched = sorted(token for token in tokens if token in text)
        if matched:
            findings.append(f"forbidden token(s) {', '.join(matched)} in {relative}")
    return findings


def _scan_portability_leaks(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink() or not _is_adaptable_text_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        matched = sorted(
            token for token in PORTABILITY_FORBIDDEN_TOKENS if token in text
        )
        if matched:
            findings.append(
                f"platform portability token(s) {', '.join(matched)} in "
                f"{path.relative_to(root).as_posix()}"
            )
    return findings


def _manifest_skill_names(path: Path) -> set[str]:
    payload = _read_json(path, label="adapted manifest")
    raw_skills = payload.get("skills", [])
    if not isinstance(raw_skills, list):
        return set()
    return {name for item in raw_skills if (name := _skill_name(item))}


def check_package(
    package_root: Path,
    *,
    mapping_path: Path = DEFAULT_MAPPING_PATH,
    validate_runtime: bool = True,
) -> dict[str, Any]:
    """Check that one converted AgentGroup has been fully adapted."""
    mapping = _read_mapping(mapping_path)
    root, manifest = _package_identity(package_root)
    group_id = manifest["name"]
    group = mapping["groups"].get(group_id)
    if not isinstance(group, dict):
        raise AdaptationError(f"AgentGroup is not covered by mapping: {group_id}")
    issues = [*_scan_forbidden(root, mapping), *_scan_portability_leaks(root)]
    specs = _capability_specs(mapping)

    if group.get("status") == "affected":
        for member_id, capabilities in _member_capabilities(mapping, group_id).items():
            manifest_path = root / "agents" / member_id / "manifest.json"
            if not manifest_path.is_file():
                issues.append(
                    f"member manifest missing: agents/{member_id}/manifest.json"
                )
                continue
            skill_names = _manifest_skill_names(manifest_path)
            for capability in capabilities:
                skill_id = specs[capability]["portable_skill_id"]
                if skill_id not in skill_names:
                    issues.append(f"member {member_id} missing Skill {skill_id}")
                if not (
                    root / "agents" / member_id / "skills" / skill_id / "SKILL.md"
                ).is_file():
                    issues.append(f"member {member_id} missing Skill file {skill_id}")
        root_skills = _manifest_skill_names(root / "manifest.json")
        for capability in _root_capabilities(mapping, group_id):
            skill_id = specs[capability]["portable_skill_id"]
            if skill_id not in root_skills:
                issues.append(f"AgentGroup missing shared Skill {skill_id}")
            if not (root / "skills" / skill_id / "SKILL.md").is_file():
                issues.append(f"AgentGroup missing shared Skill file {skill_id}")
        if (root / "README.md").is_file() and README_PENDING_LINE in (
            root / "README.md"
        ).read_text(encoding="utf-8"):
            issues.append("README still says platform Skill replacement is pending")

    for change in group.get("file_changes", []):
        if change["op"] == "delete_tree" and _safe_path(root, change["path"]).exists():
            issues.append(f"obsolete Skill tree remains: {change['path']}")

    if group_id == "travel":
        for retained in ("flyai-travel", "wendao-skill"):
            if not (root / "skills" / retained / "SKILL.md").is_file():
                issues.append(f"retained travel Skill missing: {retained}")
        wendao = root / "skills" / "wendao-skill" / "SKILL.md"
        if wendao.is_file():
            text = wendao.read_text(encoding="utf-8")
            if 'source: "workswarm"' not in text:
                issues.append("wendao-skill source is not workswarm")
            markdown_url = (
                "[https://wendao-skill-prod.ctrip.com/skill/query]"
                "(https://wendao-skill-prod.ctrip.com/skill/query)"
            )
            if markdown_url in text:
                issues.append("wendao-skill endpoint still uses Markdown URL syntax")

    runtime_validated = False
    if validate_runtime and not issues:
        try:
            from jiuwenswarm.agents.swarm.agent_group import (
                load_agent_group_package_bundle,
            )

            load_agent_group_package_bundle(root)
        except Exception as exc:  # Loader error is part of the validation report.
            issues.append(f"WorkSwarm loader rejected package: {exc}")
        else:
            runtime_validated = True
    return {
        "schemaVersion": "workswarm.agent-group-skill-adaptation.v1",
        "package": group_id,
        "status": "ok" if not issues else "needs_adaptation",
        "issues": issues,
        "runtimeValidated": runtime_validated,
    }


def _adapt_in_place(
    package_root: Path,
    *,
    mapping_path: Path,
    validate_runtime: bool,
) -> dict[str, Any]:
    mapping = _read_mapping(mapping_path)
    root, manifest = _package_identity(package_root)
    group_id = manifest["name"]
    group = mapping["groups"].get(group_id)
    if not isinstance(group, dict):
        raise AdaptationError(f"AgentGroup is not covered by mapping: {group_id}")
    tracker = ChangeTracker(root=root)
    replacements = _legacy_replacements(mapping)

    if group.get("status") == "affected":
        _ensure_root_skills(root, tracker, mapping, group_id, replacements)
        _ensure_member_skills(root, tracker, mapping, group_id, replacements)
        _apply_file_changes(root, tracker, group, mapping, replacements)
    _patch_remaining_text(root, tracker, replacements)

    check = check_package(
        root,
        mapping_path=mapping_path,
        validate_runtime=validate_runtime,
    )
    if check["issues"]:
        raise AdaptationError("; ".join(check["issues"]))
    return {
        **check,
        "mode": "apply",
        "changed": bool(tracker.changed),
        "changedPaths": sorted(tracker.changed),
    }


def adapt_package(
    package_root: Path,
    *,
    mapping_path: Path = DEFAULT_MAPPING_PATH,
    dry_run: bool = False,
    validate_runtime: bool = True,
) -> dict[str, Any]:
    """Adapt one package transactionally, or simulate it without writes.

    Both modes execute all mutations and validation against a private staging
    tree.  Apply mode swaps that fully validated tree into place only after the
    adapter and (when enabled) the WorkSwarm loader have accepted it.  Any
    adaptation/validation failure therefore leaves the caller's source tree
    byte-for-byte unchanged.
    """
    root, manifest = _package_identity(package_root)
    with tempfile.TemporaryDirectory(
        prefix=f".{manifest['name']}.workswarm-adapt-",
        dir=root.parent,
    ) as raw_tmp:
        staged_root = Path(raw_tmp) / manifest["name"]
        shutil.copytree(root, staged_root)
        result = _adapt_in_place(
            staged_root,
            mapping_path=mapping_path,
            validate_runtime=validate_runtime,
        )
        if dry_run:
            return {**result, "mode": "dry-run"}
        if not result["changed"]:
            return result

        backup_root = Path(raw_tmp) / f"{manifest['name']}.original"
        try:
            root.rename(backup_root)
            try:
                staged_root.rename(root)
            except BaseException:
                backup_root.rename(root)
                raise
        except OSError as exc:
            raise AdaptationError(
                f"failed to commit validated AgentGroup transaction: {exc}"
            ) from exc
        return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replace XiaoYi-specific Skills in one converted AgentGroup directory "
            "with WorkSwarm-native portable Skills."
        )
    )
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-runtime-validation",
        action="store_true",
        help="Skip current-develop AgentGroup loader validation.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.check:
            report = check_package(
                args.package_root,
                mapping_path=args.mapping,
                validate_runtime=not args.skip_runtime_validation,
            )
        else:
            report = adapt_package(
                args.package_root,
                mapping_path=args.mapping,
                dry_run=args.dry_run,
                validate_runtime=not args.skip_runtime_validation,
            )
    except AdaptationError as exc:
        print(
            json.dumps(
                {
                    "schemaVersion": "workswarm.agent-group-skill-adaptation.v1",
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    # Dependencies may emit startup logs before this line.  Keep the last stdout
    # line as one complete JSON object so shell and CI callers can parse it
    # deterministically, matching the acceptance harness convention.
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
