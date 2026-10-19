# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.expert_team_migration import adapt_workswarm_skills as adapter


MAPPING = json.loads(adapter.DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))
GROUP_IDS = tuple(MAPPING["groups"])


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_skill(path: Path, skill_id: str, body: str = "legacy") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: fixture Skill\n---\n\n# Fixture\n\n{body}\n",
        encoding="utf-8",
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _legacy_for_capability(capability: str, group_id: str) -> str:
    if capability == "image_generation" and group_id in {
        "home-cook",
        "home-design",
        "personal-styling",
    }:
        return "seedream-image-gen-win"
    return MAPPING["capability_replacements"][capability]["legacy_skill_ids"][0]


def _write_converted_fixture(parent: Path, group_id: str) -> Path:
    group_spec = MAPPING["groups"][group_id]
    affected_members = list(group_spec["affected_members"])
    members = ["leader"]
    members.extend(member for member in affected_members if member != "leader")
    if len(members) == 1:
        members.append("member")

    root = parent / group_id
    shared_skills: list[str] = []
    if group_id in {"home-cook", "home-design"}:
        shared_skills.append("xiaoyi-web-search-win")
    if group_id == "travel":
        shared_skills.extend(["flyai-travel", "wendao-skill"])
    instruction_parts = ["团长按任务选择最少必要成员。"]
    root_changes = [
        change
        for change in group_spec["file_changes"]
        if change["path"] == "manifest.json"
    ]
    if any(
        change["op"] == "replace_capability_reference"
        and change.get("capability") == "web_research"
        for change in root_changes
    ):
        instruction_parts.append("运行时已挂载 xiaoyi-web-search-win 时使用它。")
    if any(change["op"] == "rewrite_delivery_contract" for change in root_changes):
        instruction_parts.append(
            "除主理人外不得调用 `send_file_to_user` 或 `send_html_card`。"
        )
    _write_json(
        root / "manifest.json",
        {
            "version": "1.0.0",
            "package_type": "agent_group",
            "name": group_id,
            "description": "fixture AgentGroup",
            "display_name": {"zh": group_id, "en": group_id},
            "display_description": {"zh": "fixture", "en": "fixture"},
            "agents": members,
            "instruction": "".join(instruction_parts),
            "skills": shared_skills,
        },
    )
    (root / "README.md").write_text(
        f"# Fixture\n\n- 类型：WorkSwarm AgentGroup\n{adapter.README_PENDING_LINE}\n",
        encoding="utf-8",
    )

    direct = {
        member["member_id"]: member["capabilities"]
        for member in MAPPING["inventory"]["direct_skill_binding_members"]
        if member["group_id"] == group_id
    }
    delete_skill_dirs: dict[str, list[str]] = {}
    for change in group_spec["file_changes"]:
        path = Path(change["path"])
        if change["op"] != "delete_tree" or path.parts[0] != "agents":
            continue
        delete_skill_dirs.setdefault(path.parts[1], []).append(path.parts[-1])

    for member_id in members:
        skill_ids = delete_skill_dirs.get(member_id, [])
        skills = [
            {"dir": f"skills/{skill_id}", "mode": "auto_list"} for skill_id in skill_ids
        ]
        _write_json(
            root / "agents" / member_id / "manifest.json",
            {
                "version": "1.0.0",
                "package_type": "agent_template",
                "name": member_id,
                "description": f"{member_id} fixture",
                "display_name": {"zh": member_id, "en": member_id},
                "display_description": {"zh": "fixture", "en": "fixture"},
                "persona": {"dir": "persona"},
                "skills": skills,
            },
        )
        persona = root / "agents" / member_id / "persona" / "ROLE.md"
        persona.parent.mkdir(parents=True, exist_ok=True)
        capability_text = " ".join(
            _legacy_for_capability(capability, group_id)
            for capability in direct.get(member_id, [])
        )
        persona.write_text(f"# {member_id}\n\n{capability_text}\n", encoding="utf-8")
        for skill_id in skill_ids:
            _write_skill(
                root / "agents" / member_id / "skills" / skill_id,
                skill_id,
                skill_id,
            )
    leader_agent = root / "agents" / "leader" / "AGENT.md"
    if not leader_agent.exists():
        leader_agent.write_text("# Leader\n", encoding="utf-8")

    for skill_id in shared_skills:
        if skill_id == "wendao-skill":
            body = (
                'source: "xiaoyi"\n'
                "[https://wendao-skill-prod.ctrip.com/skill/query]"
                "(https://wendao-skill-prod.ctrip.com/skill/query)"
            )
        else:
            body = skill_id
        _write_skill(root / "skills" / skill_id, skill_id, body)

    for change in group_spec["file_changes"]:
        path = root / change["path"]
        operation = change["op"]
        if operation in {"remove_skill_reference", "delete_tree"}:
            continue
        if operation == "remove_obsolete_notice_entries":
            path.write_text(
                "# Notice\n" + "\n".join(change["legacy_skill_ids"]) + "\nkept\n",
                encoding="utf-8",
            )
            continue
        if operation == "apply_shared_skill_patch":
            continue
        if path.suffix == ".json":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        additions: list[str] = []
        if "capability" in change:
            additions.append(_legacy_for_capability(change["capability"], group_id))
        for capability in change.get("capabilities", []):
            additions.append(_legacy_for_capability(capability, group_id))
        if operation == "rewrite_delivery_contract":
            additions.append("send_html_card")
        if path.name == "SKILL.md" and not existing:
            _write_skill(path.parent, path.parent.name, " ".join(additions))
        else:
            path.write_text(
                existing + "\n" + " ".join(additions) + "\n", encoding="utf-8"
            )
    return root


@pytest.mark.parametrize("group_id", GROUP_IDS)
def test_all_ten_group_fixtures_adapt_and_are_idempotent(
    tmp_path: Path,
    group_id: str,
) -> None:
    root = _write_converted_fixture(tmp_path, group_id)
    first = adapter.adapt_package(root, validate_runtime=False)
    first_digest = _tree_digest(root)
    second = adapter.adapt_package(root, validate_runtime=False)

    assert first["status"] == "ok"
    assert first["runtimeValidated"] is False
    assert first["changed"] is True
    assert second["changed"] is False
    assert _tree_digest(root) == first_digest
    assert adapter.check_package(root, validate_runtime=False)["status"] == "ok"


def test_platform_specific_commands_and_paths_are_normalized(tmp_path: Path) -> None:
    root = _write_converted_fixture(tmp_path, "healthy-habit-companion")
    skill = root / "agents" / "leader" / "skills" / "autonomous-tasks" / "SKILL.md"
    _write_skill(
        skill.parent,
        "autonomous-tasks",
        'openclaw cron add --name "autonomous-tasks" '
        '--message "run autonomous tasks" --every 1h\n'
        "/home/sirius/.openclaw/skills/natural-language-planner\n"
        "不得继承小艺侧模型路由\n"
        "Call mcp_free_search, then mcp_fetch_webpage.",
    )

    adapter.adapt_package(root, validate_runtime=False)

    text = skill.read_text(encoding="utf-8")
    assert "openclaw cron add" not in text
    assert "/.openclaw/skills/" not in text
    assert "cron_create_job" in text
    assert "<SKILL_INSTALL_DIR>/natural-language-planner" in text
    assert "小艺侧模型路由" not in text
    assert "原平台侧模型路由" in text
    assert "mcp_free_search" not in text
    assert "mcp_fetch_webpage" not in text
    assert "Call free_search, then fetch_webpage." in text


@pytest.mark.parametrize(
    "group_id,member_id,expected_skills",
    [
        (
            member["group_id"],
            member["member_id"],
            {
                MAPPING["capability_replacements"][capability]["portable_skill_id"]
                for capability in member["capabilities"]
            },
        )
        for member in MAPPING["inventory"]["direct_skill_binding_members"]
    ],
)
def test_direct_member_bindings_use_portable_skills(
    tmp_path: Path,
    group_id: str,
    member_id: str,
    expected_skills: set[str],
) -> None:
    root = _write_converted_fixture(tmp_path, group_id)
    adapter.adapt_package(root, validate_runtime=False)

    manifest = json.loads(
        (root / "agents" / member_id / "manifest.json").read_text(encoding="utf-8")
    )
    actual = {adapter._skill_name(skill) for skill in manifest["skills"]}
    assert expected_skills <= actual
    for skill_id in expected_skills:
        assert (
            root / "agents" / member_id / "skills" / skill_id / "SKILL.md"
        ).is_file()


def test_dry_run_and_check_do_not_mutate_source(tmp_path: Path) -> None:
    root = _write_converted_fixture(tmp_path, "home-storage-makeover")
    before = _tree_digest(root)

    before_check = adapter.check_package(root, validate_runtime=False)
    dry_run = adapter.adapt_package(root, dry_run=True, validate_runtime=False)

    assert before_check["status"] == "needs_adaptation"
    assert dry_run["status"] == "ok"
    assert dry_run["mode"] == "dry-run"
    assert dry_run["changed"] is True
    assert _tree_digest(root) == before


def test_apply_validation_failure_does_not_pollute_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_converted_fixture(tmp_path, "home-storage-makeover")
    before = _tree_digest(root)

    def reject_staged_package(*args, **kwargs) -> dict:
        del args, kwargs
        return {
            "schemaVersion": "workswarm.agent-group-skill-adaptation.v1",
            "package": "home-storage-makeover",
            "status": "needs_adaptation",
            "issues": ["WorkSwarm loader rejected package: fixture failure"],
            "runtimeValidated": False,
        }

    monkeypatch.setattr(adapter, "check_package", reject_staged_package)

    with pytest.raises(adapter.AdaptationError, match="loader rejected"):
        adapter.adapt_package(root, validate_runtime=True)

    assert _tree_digest(root) == before
    assert not any(root.parent.glob(f".{root.name}.workswarm-adapt-*"))


def test_bulk_rewrite_ignores_binary_and_unrelated_text_files(tmp_path: Path) -> None:
    root = _write_converted_fixture(tmp_path, "healthy-habit-companion")
    unrelated = root / "assets" / "embedded.html"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text(
        "<p>小艺 Work xiaoyi-web-search-win openclaw cron add</p>",
        encoding="utf-8",
    )
    binary = root / "assets" / "preview.png"
    binary.write_bytes(
        b"\x89PNG\r\n\x1a\n" + "小艺 Work xiaoyi-web-search-win".encode("utf-8")
    )
    unrelated_before = unrelated.read_bytes()
    binary_before = binary.read_bytes()

    adapter.adapt_package(root, validate_runtime=False)

    assert unrelated.read_bytes() == unrelated_before
    assert binary.read_bytes() == binary_before


def test_travel_preserves_external_skills_and_patches_wendao(tmp_path: Path) -> None:
    root = _write_converted_fixture(tmp_path, "travel")
    flyai_before = (root / "skills" / "flyai-travel" / "SKILL.md").read_bytes()

    adapter.adapt_package(root, validate_runtime=False)

    assert (root / "skills" / "flyai-travel" / "SKILL.md").read_bytes() == flyai_before
    wendao = (root / "skills" / "wendao-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert 'source: "workswarm"' in wendao
    assert "[https://wendao-skill-prod.ctrip.com/skill/query]" not in wendao
    root_manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert "workswarm-web-research" in root_manifest["skills"]
    assert "flyai-travel" in root_manifest["skills"]
    assert "wendao-skill" in root_manifest["skills"]


def test_delivery_rewrite_keeps_leader_and_member_boundaries(tmp_path: Path) -> None:
    root = _write_converted_fixture(tmp_path, "home-design")

    adapter.adapt_package(root, validate_runtime=False)

    all_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".txt"}
    )
    assert "send_html_card" not in all_text
    assert adapter.NON_LEADER_DELIVERY in all_text
    assert adapter.LEADER_HTML_DELIVERY in all_text


def test_unknown_group_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "unknown-team"
    _write_json(
        root / "manifest.json",
        {
            "version": "1.0.0",
            "package_type": "agent_group",
            "name": "unknown-team",
            "agents": ["leader"],
            "skills": [],
        },
    )

    with pytest.raises(adapter.AdaptationError, match="not covered by mapping"):
        adapter.adapt_package(root, validate_runtime=False)
