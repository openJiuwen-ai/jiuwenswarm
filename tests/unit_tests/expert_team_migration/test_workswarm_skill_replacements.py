# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for the XiaoYi-to-WorkSwarm Skill migration mapping."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
MAPPING_PATH = (
    REPO_ROOT
    / "scripts"
    / "expert_team_migration"
    / "workswarm_skill_replacements.json"
)


def _mapping() -> dict:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def test_mapping_covers_all_ten_groups_and_the_seven_affected_groups() -> None:
    mapping = _mapping()
    inventory = mapping["inventory"]
    groups = mapping["groups"]

    expected_affected = {
        "family-weekend-trip",
        "family-party-planner",
        "home-storage-makeover",
        "travel",
        "home-cook",
        "personal-styling",
        "home-design",
    }
    expected_unaffected = {
        "healthy-habit-companion",
        "rental-move-decision",
        "family-learning-companion",
    }

    assert inventory["total_groups"] == 10
    assert set(groups) == expected_affected | expected_unaffected
    assert set(inventory["affected_group_ids"]) == expected_affected
    assert set(inventory["unaffected_group_ids"]) == expected_unaffected
    assert {
        group_id for group_id, spec in groups.items() if spec["status"] == "affected"
    } == expected_affected
    assert {
        group_id for group_id, spec in groups.items() if spec["status"] == "unchanged"
    } == expected_unaffected
    assert all(not groups[group_id]["file_changes"] for group_id in expected_unaffected)


def test_mapping_uses_only_the_four_verified_native_capabilities() -> None:
    mapping = _mapping()
    replacements = mapping["capability_replacements"]

    assert set(replacements) == {
        "web_research",
        "image_understanding",
        "image_generation",
        "video_generation",
    }
    assert replacements["web_research"]["native_tools"] == [
        "mcp_free_search",
        "mcp_fetch_webpage",
    ]
    assert replacements["image_understanding"]["native_tools"] == [
        "visual_question_answering"
    ]
    assert replacements["image_generation"]["native_tools"] == ["generate_image"]
    assert replacements["video_generation"]["native_tools"] == [
        "generate_video",
        "check_video_status",
    ]
    assert {spec["strategy"] for spec in replacements.values()} == {"native_tools"}


def test_legacy_ids_members_and_shared_skills_are_complete() -> None:
    inventory = _mapping()["inventory"]

    assert set(inventory["legacy_skill_ids"]) == {
        "xiaoyi-web-search-win",
        "xiaoyi-image-understanding",
        "seedream-image_gen",
        "seedream-image-gen",
        "seedream-image-gen-win",
        "seedance-video-gen-win",
    }
    assert set(inventory["legacy_concept_aliases"]) == {
        "xiaoyi-image-gen",
        "xiaoyi-video-gen",
    }
    assert set(inventory["shared_skill_ids"]) == {
        "xiaoyi-web-search-win",
        "flyai-travel",
        "wendao-skill",
    }

    direct = inventory["direct_skill_binding_members"]
    indirect = inventory["indirect_native_search_reference_members"]
    assert len(direct) == 8
    assert len(indirect) == 7
    identities = {
        (member["group_id"], member["member_id"]) for member in direct + indirect
    }
    assert len(identities) == 15


def test_old_skill_directories_and_manifest_bindings_are_explicit() -> None:
    groups = _mapping()["groups"]
    delete_trees = {
        (group_id, change["path"])
        for group_id, group in groups.items()
        for change in group["file_changes"]
        if change["op"] == "delete_tree"
    }

    assert delete_trees == {
        (
            "family-weekend-trip",
            "agents/visual-generator/skills/seedream-image_gen",
        ),
        (
            "family-party-planner",
            "agents/invitation-visual-designer/skills/seedream-image_gen",
        ),
        (
            "home-storage-makeover",
            "agents/space-photo-reviewer/skills/xiaoyi-image-understanding",
        ),
        (
            "home-storage-makeover",
            "agents/storage-visual-page-designer/skills/seedream-image_gen",
        ),
        ("home-cook", "skills/xiaoyi-web-search-win"),
        (
            "home-cook",
            "agents/visualization_director/skills/seedream-image-gen-win",
        ),
        (
            "home-cook",
            "agents/visualization_director/skills/seedance-video-gen-win",
        ),
        ("personal-styling", "agents/occasion/skills/xiaoyi-web-search-win"),
        (
            "personal-styling",
            "agents/visual/skills/seedream-image-gen-win",
        ),
        (
            "personal-styling",
            "agents/visual/skills/seedance-video-gen-win",
        ),
        ("home-design", "skills/xiaoyi-web-search-win"),
        ("home-design", "agents/visual/skills/seedream-image-gen-win"),
        ("home-design", "agents/visual/skills/seedance-video-gen-win"),
    }

    manifest_changes = [
        change
        for group in groups.values()
        for change in group["file_changes"]
        if change["op"] == "remove_skill_reference"
    ]
    assert manifest_changes
    assert all(change["path"].endswith("manifest.json") for change in manifest_changes)
    assert all(
        "legacy_skill_id" in change or "legacy_skill_dir" in change
        for change in manifest_changes
    )


def test_every_change_is_safe_relative_and_references_a_known_capability() -> None:
    mapping = _mapping()
    capabilities = set(mapping["capability_replacements"])
    allowed_ops = {
        "remove_skill_reference",
        "replace_capability_reference",
        "ensure_native_tool_instruction",
        "rewrite_delivery_contract",
        "delete_tree",
        "apply_shared_skill_patch",
        "replace_provenance_note",
        "remove_obsolete_notice_entries",
    }

    for group in mapping["groups"].values():
        for change in group["file_changes"]:
            path = Path(change["path"])
            assert change["op"] in allowed_ops
            assert not path.is_absolute()
            assert ".." not in path.parts
            if "capability" in change:
                assert change["capability"] in capabilities
            if "capabilities" in change:
                assert set(change["capabilities"]) <= capabilities


def test_delivery_rewrite_removes_html_card_without_granting_member_delivery() -> None:
    mapping = _mapping()
    delivery = mapping["delivery_tool_rewrite"]

    assert delivery["legacy_tool"] == "send_html_card"
    assert delivery["native_tools"] == ["write_file", "send_file_to_user"]
    assert "do not call send_file_to_user" in delivery["non_leader_instruction"]

    rewrite_modes = {
        change["mode"]
        for group in mapping["groups"].values()
        for change in group["file_changes"]
        if change["op"] == "rewrite_delivery_contract"
    }
    assert rewrite_modes == {"leader_html_file", "non_leader_guard"}


def test_native_tool_names_exist_in_current_develop_sources() -> None:
    expected_definitions = {
        "mcp_free_search": (
            REPO_ROOT
            / "jiuwenswarm/agents/harness/common/tools/trusted_search_tool_adapter.py"
        ),
        "mcp_fetch_webpage": (
            REPO_ROOT / "jiuwenswarm/agents/harness/common/tools/web_fetch_tools.py"
        ),
        "visual_question_answering": (
            REPO_ROOT / "jiuwenswarm/agents/harness/common/tools/image_tools.py"
        ),
        "generate_image": (
            REPO_ROOT / "jiuwenswarm/agents/harness/common/tools/image_tools.py"
        ),
        "generate_video": (
            REPO_ROOT / "jiuwenswarm/agents/harness/common/tools/video_gen_tools.py"
        ),
        "check_video_status": (
            REPO_ROOT / "jiuwenswarm/agents/harness/common/tools/video_gen_tools.py"
        ),
        "send_file_to_user": (
            REPO_ROOT / "jiuwenswarm/agents/harness/common/tools/send_file_to_user.py"
        ),
    }

    for tool_name, source_path in expected_definitions.items():
        source = source_path.read_text(encoding="utf-8")
        assert f'name="{tool_name}"' in source


def test_shared_external_skills_are_retained_with_explicit_smoke_requirements() -> None:
    policies = {
        policy["skill_id"]: policy for policy in _mapping()["shared_skill_policies"]
    }

    assert policies["xiaoyi-web-search-win"]["action"] == "replace_with_native_tools"
    assert policies["flyai-travel"]["action"] == "retain_conditionally"
    assert policies["wendao-skill"]["action"] == "retain_with_text_patch"
    assert policies["flyai-travel"]["required_smoke"]
    assert policies["wendao-skill"]["required_smoke"]
    assert {patch["old"] for patch in policies["wendao-skill"]["required_patches"]} == {
        'source: "xiaoyi"',
        "[https://wendao-skill-prod.ctrip.com/skill/query](https://wendao-skill-prod.ctrip.com/skill/query)",
    }
