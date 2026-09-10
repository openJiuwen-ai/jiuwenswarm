# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""专家团（AgentGroup）包解析层测试：严格加载器、validate 分派、LocalDir 桥接。

正例使用 testdata/expert_groups/sample-expert-group（leader+member1+member2+1 共享skill）
原样加载；负例在 tmp_path 复制样例包后注入故障，覆盖校验清单全项
（失败全终止：任一违规整体抛错）。
"""

import json
import shutil
from pathlib import Path

import pytest
from openjiuwen.harness.schema.extension_spec import PromptSectionSpec

from jiuwenswarm.server.runtime.expert import agent_group as agent_group_module
from jiuwenswarm.server.runtime.expert import expert_store as es
from jiuwenswarm.server.runtime.expert.agent_group import (
    INSTRUCTION_SECTION_NAME,
    TEAM_STAGE_SECTION_NAME,
    AgentGroupPackageError,
    load_agent_group_package,
    read_group_display,
    read_group_members,
    validate_agent_group_package,
)

TESTDATA_GROUP = (
    Path(__file__).parent / "testdata" / "expert_groups" / "sample-expert-group"
)


def _copy_sample(tmp_path: Path) -> Path:
    target = tmp_path / TESTDATA_GROUP.name
    shutil.copytree(TESTDATA_GROUP, target)
    return target


def _read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _section_texts(template) -> str:
    parts: list[str] = []
    for section in template.prompt_sections:
        parts.extend(str(v) for v in section.content.values())
    return "\n".join(parts)


def test_load_sample_package_ok() -> None:
    templates = load_agent_group_package(TESTDATA_GROUP)

    assert list(templates.keys()) == ["leader", "member1", "member2"]

    leader = templates["leader"]
    assert leader.agent_card.id == "leader"
    assert leader.agent_card.name == "主理人"
    # persona.dir="." → AGENT.md 与 persona/leader.md 都被读入 prompt sections
    leader_text = _section_texts(leader)
    assert "主理人工作规则" in leader_text
    assert "主理人人设" in leader_text

    member1 = templates["member1"]
    assert member1.agent_card.id == "member1"
    member1_text = _section_texts(member1)
    assert "成员一人设" in member1_text
    assert "主理人工作规则" not in member1_text  # 成员不含 AGENT.md

    # instruction 注入全员（priority 20 保留 section 名）
    for name, template in templates.items():
        instruction_sections = [
            s for s in template.prompt_sections if s.name == INSTRUCTION_SECTION_NAME
        ]
        assert len(instruction_sections) == 1, name
        assert instruction_sections[0].priority == 20
        assert "协作专家团" in instruction_sections[0].content["cn"]

    # 共享 skill 去重合并进各成员（绝对路径）
    for name, template in templates.items():
        skill_dirs = [s.dir for s in template.skills]
        assert any(
            d.replace("\\", "/").endswith("skills/skill_name_1") for d in skill_dirs
        ), name
        assert all(Path(d).is_absolute() for d in skill_dirs), name


def test_load_member_team_stage_as_final_priority_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _copy_sample(tmp_path)
    member_dir = pkg / "agents" / "member1"
    stage_file = member_dir / "EXPERT_TEAM_STAGE.txt"
    stage_file.write_text(
        "# 阶段覆盖规则\n\n只交付声明的 handoff。\n", encoding="utf-8"
    )
    manifest_path = member_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    manifest["metadata"] = {"expertTeamStageFile": stage_file.name}
    _write_manifest(manifest_path, manifest)

    original_loader = agent_group_module.load_agent_template_package

    def load_with_late_source_contract(path: Path):
        template = original_loader(path)
        if Path(path).parent.name != "member1":
            return template
        source_contract = PromptSectionSpec(
            name="source_output_contract",
            content={"cn": "生成 standalone.md", "en": "generate standalone.md"},
            priority=100,
        )
        return template.model_copy(
            update={
                "prompt_sections": [*template.prompt_sections, source_contract]
            }
        )

    monkeypatch.setattr(
        agent_group_module, "load_agent_template_package", load_with_late_source_contract
    )

    template = load_agent_group_package(pkg)["member1"]
    stage_sections = [
        section
        for section in template.prompt_sections
        if section.name == TEAM_STAGE_SECTION_NAME
    ]

    assert len(stage_sections) == 1
    assert stage_sections[0].priority == 101
    assert stage_sections[0].priority > max(
        section.priority
        for section in template.prompt_sections
        if section.name != TEAM_STAGE_SECTION_NAME
    )
    assert "只交付声明的 handoff" in stage_sections[0].content["cn"]


def test_reject_member_team_stage_path_escape(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    outside = pkg / "outside-stage.md"
    outside.write_text("outside\n", encoding="utf-8")
    member_manifest = pkg / "agents" / "member1" / "manifest.json"
    manifest = _read_manifest(member_manifest)
    manifest["metadata"] = {"expertTeamStageFile": "../../outside-stage.md"}
    _write_manifest(member_manifest, manifest)

    with pytest.raises(AgentGroupPackageError, match="阶段规则"):
        load_agent_group_package(pkg)


def test_read_group_display() -> None:
    display = read_group_display(TESTDATA_GROUP)
    assert display["name"] == "sample-expert-group"
    assert "主理人" in display["description"]


def test_read_group_members() -> None:
    members = read_group_members(TESTDATA_GROUP)
    assert [m["id"] for m in members] == ["leader", "member1", "member2"]
    assert members[0]["role"] == "lead"
    assert members[0]["name"] == "主理人"
    assert members[1]["role"] == "member"
    assert members[1]["name"] == "成员一"
    assert members[1]["description"] == "调研与资料整理专家"
    assert read_group_members(TESTDATA_GROUP.parent / "nonexistent") == []


def test_validate_group_package_ok() -> None:
    assert validate_agent_group_package(TESTDATA_GROUP) == []


def test_validate_member_model_field_warns(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    # model 字段不生效（warning 请移除）；须是 core loader 可解析的合法引用，
    # 非法形态会在装载期被 core loader 硬失败（与单专家现状同款语义）
    (pkg / "agents" / "member1" / "model.json").write_text(
        json.dumps(
            {
                "model": {
                    "model_client_config": {
                        "client_provider": "OpenAI",
                        "api_key": "sk-dummy",
                        "api_base": "https://example.com/v1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    member_manifest = pkg / "agents" / "member1" / "manifest.json"
    payload = _read_manifest(member_manifest)
    payload["model"] = {"file": "model.json"}
    _write_manifest(member_manifest, payload)

    warnings = validate_agent_group_package(pkg)

    assert any("member1" in w and "model" in w for w in warnings)


def _break_top_manifest(pkg: Path, **updates) -> None:
    manifest_path = pkg / "manifest.json"
    payload = _read_manifest(manifest_path)
    payload.update(updates)
    _write_manifest(manifest_path, payload)


def test_reject_top_name_mismatch(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, name="other-name")
    with pytest.raises(AgentGroupPackageError, match="目录名"):
        load_agent_group_package(pkg)


def test_reject_agents_without_leader(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, agents=["member1", "member2"])
    with pytest.raises(AgentGroupPackageError, match="leader"):
        load_agent_group_package(pkg)


def test_reject_agents_empty(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, agents=[])
    with pytest.raises(AgentGroupPackageError, match="非空"):
        load_agent_group_package(pkg)


def test_reject_agents_duplicate(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, agents=["leader", "member1", "member1"])
    with pytest.raises(AgentGroupPackageError, match="重复"):
        load_agent_group_package(pkg)


def test_reject_member_name_path_separator(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, agents=["leader", "../escape"])
    with pytest.raises(AgentGroupPackageError, match="分隔符|非法"):
        load_agent_group_package(pkg)


def test_reject_member_dir_missing(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, agents=["leader", "member1", "ghost"])
    with pytest.raises(AgentGroupPackageError, match="ghost"):
        load_agent_group_package(pkg)


def test_reject_leader_missing_agent_md(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    (pkg / "agents" / "leader" / "AGENT.md").unlink()
    with pytest.raises(AgentGroupPackageError, match="AGENT.md"):
        load_agent_group_package(pkg)


def test_reject_member_with_agent_md(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    (pkg / "agents" / "member1" / "AGENT.md").write_text("# 越权", encoding="utf-8")
    with pytest.raises(AgentGroupPackageError, match="不允许包含 AGENT.md"):
        load_agent_group_package(pkg)


@pytest.mark.parametrize("forbidden", ["rails", "subagents"])
def test_reject_member_forbidden_fields(tmp_path: Path, forbidden: str) -> None:
    pkg = _copy_sample(tmp_path)
    member_manifest = pkg / "agents" / "member1" / "manifest.json"
    payload = _read_manifest(member_manifest)
    payload[forbidden] = (
        [{"file": "x.py", "class": "X"}] if forbidden == "rails" else [{"dir": "sub/x"}]
    )
    _write_manifest(member_manifest, payload)
    with pytest.raises(AgentGroupPackageError, match=forbidden):
        load_agent_group_package(pkg)


def test_reject_member_wrong_package_type(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    member_manifest = pkg / "agents" / "member1" / "manifest.json"
    payload = _read_manifest(member_manifest)
    payload["packageType"] = "plugin"
    _write_manifest(member_manifest, payload)
    with pytest.raises(AgentGroupPackageError, match="agent_template"):
        load_agent_group_package(pkg)


def test_reject_member_persona_missing(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    member_manifest = pkg / "agents" / "member1" / "manifest.json"
    payload = _read_manifest(member_manifest)
    payload["persona"] = {"dir": "nowhere"}
    _write_manifest(member_manifest, payload)
    with pytest.raises(AgentGroupPackageError, match="persona"):
        load_agent_group_package(pkg)


def test_reject_shared_skill_missing_skill_md(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    (pkg / "skills" / "skill_name_1" / "SKILL.md").unlink()
    with pytest.raises(AgentGroupPackageError, match="SKILL.md"):
        load_agent_group_package(pkg)


def test_reject_shared_skill_not_listed_dir_missing(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, skills=["ghost-skill"])
    with pytest.raises(AgentGroupPackageError, match="ghost-skill"):
        load_agent_group_package(pkg)


def test_validate_expert_package_dispatches_group(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    assert es.validate_expert_package(pkg) == []


def test_validate_expert_package_group_failure_as_invalid_package(
    tmp_path: Path,
) -> None:
    pkg = _copy_sample(tmp_path)
    (pkg / "agents" / "leader" / "AGENT.md").unlink()
    with pytest.raises(es.InvalidExpertPackage, match="AGENT.md"):
        es.validate_expert_package(pkg)


def test_validate_expert_package_rejects_unknown_package_type(tmp_path: Path) -> None:
    pkg = _copy_sample(tmp_path)
    _break_top_manifest(pkg, package_type="something_else")
    with pytest.raises(es.InvalidExpertPackage, match="package_type"):
        es.validate_expert_package(pkg)


def test_validate_expert_package_single_expert_regression(tmp_path: Path) -> None:
    pkg = tmp_path / "security-reviewer"
    (pkg / "agents").mkdir(parents=True)
    (pkg / "agents" / "00-identity.md").write_text("# 人设", encoding="utf-8")
    _write_manifest(
        pkg / "manifest.json",
        {
            "packageType": "agent_template",
            "agentCard": {
                "id": "security-reviewer",
                "name": "安全评审专家",
                "description": "描述",
            },
            "persona": {"dir": "agents"},
        },
    )
    assert es.validate_expert_package(pkg) == []


@pytest.mark.asyncio
async def test_local_dir_source_lists_group_as_team(tmp_path: Path) -> None:
    _copy_sample(tmp_path)
    source = es.LocalDirExpertPackageSource(experts_dir=tmp_path)

    summaries = await source.list()

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.id == "sample-expert-group"
    assert summary.type == "team"
    assert summary.available is True
    assert summary.name == "sample-expert-group"
    assert "主理人" in summary.description
    # B1：团队条目携带成员摘要（leader 置顶）
    assert [m["id"] for m in summary.members] == ["leader", "member1", "member2"]
    assert summary.members[0]["role"] == "lead"


# ---- current_expert_identity_extra：历史落盘的专家身份快照 ----


@pytest.fixture(autouse=False)
def _clear_name_cache():
    from jiuwenswarm.server.runtime.expert import expert_service as svc

    svc._expert_name_cache.clear()
    yield
    svc._expert_name_cache.clear()


def _patch_metadata(monkeypatch: pytest.MonkeyPatch, metadata: dict) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _sid, *a, **k: metadata,
    )


def test_identity_extra_no_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.server.runtime.expert import expert_service as svc

    _patch_metadata(monkeypatch, {})
    assert svc.current_expert_identity_extra("s1") == {}


def test_identity_extra_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _clear_name_cache
) -> None:
    from jiuwenswarm.server.runtime.expert import expert_service as svc

    pkg = tmp_path / "doc-writer"
    pkg.mkdir()
    _write_manifest(
        pkg / "manifest.json",
        {
            "packageType": "agent_template",
            "agentCard": {"id": "doc-writer", "name": "小雯", "description": "d"},
            "persona": {"dir": "agents"},
        },
    )
    _patch_metadata(monkeypatch, {"expert_id": "doc-writer", "expert_type": "agent"})
    monkeypatch.setattr(es, "get_cached_expert_package_dir", lambda _id: pkg)

    extra = svc.current_expert_identity_extra("s1")

    assert extra == {
        "expert_id": "doc-writer",
        "expert_type": "agent",
        "expert_name": "小雯",
    }


def test_identity_extra_team_uses_lead_name(
    monkeypatch: pytest.MonkeyPatch, _clear_name_cache
) -> None:
    from jiuwenswarm.server.runtime.expert import expert_service as svc

    _patch_metadata(
        monkeypatch, {"expert_id": "sample-expert-group", "expert_type": "team"}
    )
    monkeypatch.setattr(es, "get_cached_expert_package_dir", lambda _id: TESTDATA_GROUP)

    extra = svc.current_expert_identity_extra("s1")

    assert extra["expert_id"] == "sample-expert-group"
    assert extra["expert_type"] == "team"
    # 团的身份名快照 = 主理人花名（身份行展示主理人，不是团名）
    assert extra["expert_name"] == "主理人"


def test_identity_extra_package_missing_keeps_id(
    monkeypatch: pytest.MonkeyPatch, _clear_name_cache
) -> None:
    from jiuwenswarm.server.runtime.expert import expert_service as svc

    _patch_metadata(monkeypatch, {"expert_id": "ghost", "expert_type": "agent"})
    monkeypatch.setattr(es, "get_cached_expert_package_dir", lambda _id: None)

    extra = svc.current_expert_identity_extra("s1")

    # 包已删/无缓存：id/type 仍落盘，name 缺省（前端按 id + 首字回退）
    assert extra == {"expert_id": "ghost", "expert_type": "agent"}


def test_history_identity_extra_unbound_writes_explicit_default_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未绑定专家的会话：主应答落盘显式写 expert_id=""（区分默认角色作答与存量无字段）。"""
    from jiuwenswarm.server.runtime.expert import expert_service as svc

    _patch_metadata(monkeypatch, {})
    assert svc.history_expert_identity_extra("s1") == {
        "expert_id": "",
        "expert_type": "agent",
    }


def test_history_identity_extra_bound_delegates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _clear_name_cache
) -> None:
    """已绑定专家：与 current_expert_identity_extra 一致（含显示名快照）。"""
    from jiuwenswarm.server.runtime.expert import expert_service as svc

    pkg = tmp_path / "doc-writer"
    pkg.mkdir()
    _write_manifest(
        pkg / "manifest.json",
        {
            "packageType": "agent_template",
            "agentCard": {"id": "doc-writer", "name": "小雯", "description": "d"},
            "persona": {"dir": "agents"},
        },
    )
    _patch_metadata(monkeypatch, {"expert_id": "doc-writer", "expert_type": "agent"})
    monkeypatch.setattr(es, "get_cached_expert_package_dir", lambda _id: pkg)

    extra = svc.history_expert_identity_extra("s1")

    assert extra["expert_id"] == "doc-writer"
    assert extra["expert_name"] == "小雯"
