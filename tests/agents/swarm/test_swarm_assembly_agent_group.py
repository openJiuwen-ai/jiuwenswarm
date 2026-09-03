# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""专家团（AgentGroup）spec 组装层测试：_apply_agent_group 七步断言。

正例经 ``enrich_team_spec_for_swarm(..., agent_group_name=...)`` 走完整接缝；
包来源用 monkeypatch 把 expert_store 缓存目录指向 tmp_path 里的样例包副本，
不触网络、不触真实缓存。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from openjiuwen.agent_teams.schema.blueprint import LeaderSpec, TeamAgentSpec
from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec

from jiuwenswarm.agents.swarm import enrich_team_spec_for_swarm
from jiuwenswarm.server.runtime.expert import expert_store as es

TESTDATA_GROUP = (
        Path(__file__).parent.parent.parent
        / "unit_tests"
        / "agentserver"
        / "testdata"
        / "expert_groups"
        / "sample-expert-group"
)


def _make_team_spec(leader_prompt: str = "") -> TeamAgentSpec:
    return TeamAgentSpec(
        agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
        team_name="unit_team",
        leader=LeaderSpec(member_name="team_leader", prompt=leader_prompt),
    )


@pytest.fixture
def group_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache_root = tmp_path / "experts_cache"
    shutil.copytree(TESTDATA_GROUP, cache_root / TESTDATA_GROUP.name)
    monkeypatch.setattr(es, "get_expert_cache_dir", lambda: cache_root)
    return cache_root


def _enrich(spec: TeamAgentSpec, agent_group_name: str | None) -> None:
    enrich_team_spec_for_swarm(
        spec,
        session_id="sess-1",
        mode="team",
        agent_group_name=agent_group_name,
    )


def test_apply_agent_group_full_assembly(group_cache: Path) -> None:
    spec = _make_team_spec(leader_prompt="原有主理人规则")

    _enrich(spec, "sample-expert-group")

    # 7. 模式字段显式写值（不依赖版本默认值）
    assert spec.team_mode == "hybrid"
    assert spec.dispatch_mode == "autonomous"
    assert spec.enable_task_verification is False

    # 4. leader prompt = 模板原 prompt + switch notice + AGENT.md + persona + instruction
    assert "原有主理人规则" in spec.leader.prompt
    assert "专家团「sample-expert-group」" in spec.leader.prompt
    assert "主理人工作规则" in spec.leader.prompt
    assert "主理人人设" in spec.leader.prompt
    assert "协作专家团" in spec.leader.prompt
    # 运行时显示名跟随包主理人花名（member_name 不动）
    assert spec.leader.display_name == "主理人"
    assert spec.leader.member_name == "team_leader"

    # 5. leader 快照：剥 prompt_sections、可 JSON round-trip、技能种子去重合并
    leader_snapshot = spec.agents["leader"].agent_template_spec
    assert isinstance(leader_snapshot, dict)
    assert leader_snapshot["prompt_sections"] == []
    assert leader_snapshot["agent_card"]["id"] == "leader"
    assert "skill_name_1" in (spec.agents["leader"].skills or [])

    # 6. predefined roster 替换为包成员（leader 不在 roster 内）
    roster = {m.member_name: m for m in spec.predefined_members}
    assert set(roster) == {"member1", "member2"}
    assert roster["member1"].display_name == "成员一"
    assert roster["member1"].desc == "调研与资料整理专家"
    # TeamMemberSpec.prompt = persona + instruction，不含 AGENT.md
    assert "成员一人设" in roster["member1"].prompt
    assert "协作专家团" in roster["member1"].prompt
    assert "主理人工作规则" not in roster["member1"].prompt

    # 成员 agents 覆写：独立 spec（deepcopy teammate_base）+ 快照
    for name in ("member1", "member2"):
        member_spec = spec.agents[name]
        assert member_spec is not spec.agents["teammate"]
        snapshot = member_spec.agent_template_spec
        assert isinstance(snapshot, dict)
        assert snapshot["prompt_sections"] == []
        assert snapshot["agent_card"]["id"] == name
        assert "skill_name_1" in (member_spec.skills or [])


def test_apply_agent_group_cache_missing_raises(group_cache: Path) -> None:
    spec = _make_team_spec()

    with pytest.raises(FileNotFoundError, match="缓存缺失"):
        _enrich(spec, "ghost-group")


def test_apply_agent_group_prefetched_package_dir_bypasses_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """调用方经 resolve_expert_package_dir 预取后传入 package_dir，
    缓存为空（LocalDir 调试源不落缓存场景）也能装配。

    有意不挂 group_cache fixture（缓存目录指向空的 tmp_path），
    证明走通靠的是预取路径而非缓存。
    """
    empty_cache = tmp_path / "experts_cache"
    monkeypatch.setattr(es, "get_expert_cache_dir", lambda: empty_cache)
    spec = _make_team_spec()

    enrich_team_spec_for_swarm(
        spec,
        session_id="sess-1",
        mode="team",
        agent_group_name="sample-expert-group",
        agent_group_package_dir=TESTDATA_GROUP,
    )

    assert "主理人工作规则" in spec.leader.prompt
    roster = {m.member_name: m for m in spec.predefined_members}
    assert set(roster) == {"member1", "member2"}


def test_apply_agent_group_requires_teammate_base(group_cache: Path) -> None:
    spec = TeamAgentSpec(
        agents={"leader": DeepAgentSpec()},
        team_name="unit_team",
        leader=LeaderSpec(member_name="team_leader"),
    )

    with pytest.raises(ValueError, match="leader.*teammate|teammate"):
        _enrich(spec, "sample-expert-group")


def test_apply_agent_group_capability_probe(
        group_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openjiuwen.harness.deep_agent import DeepAgent

    monkeypatch.delattr(DeepAgent, "load_agent_template_spec")
    spec = _make_team_spec()

    with pytest.raises(RuntimeError, match="快照导入能力"):
        _enrich(spec, "sample-expert-group")


def test_enrich_without_agent_group_keeps_vanilla_behavior(group_cache: Path) -> None:
    spec = _make_team_spec(leader_prompt="原有主理人规则")

    _enrich(spec, None)

    assert spec.leader.prompt == "原有主理人规则"
    assert spec.predefined_members in (None, [])
    assert spec.team_mode in (None, "default")
    assert "member1" not in spec.agents
    assert getattr(spec.agents["leader"], "agent_template_spec", None) is None


def test_switch_group_rebuilds_prompts_and_roster(
        group_cache: Path, tmp_path: Path
) -> None:
    """切换专家团（A→B）：新包重建的 spec 提示词/roster 完全是 B 的，无 A 残留。

    运行时侧的对偶保证：expert.load 团分支换绑先停旧 team 运行时
    （stop_session_runtime → _clear_terminal_session_markers 清 initialized 标记），
    下次 chat is_first_request=True 走全量重建（team_helpers.py:1554-1558）。
    """
    import json

    # 组 B：复制样例包后改名（name 须=目录名）+ 内容差异化
    pkg_b = group_cache / "another-group"
    shutil.copytree(TESTDATA_GROUP, pkg_b)
    top_path = pkg_b / "manifest.json"
    top = json.loads(top_path.read_text(encoding="utf-8"))
    top["name"] = "another-group"
    top["instruction"] = "B团协作契约"
    top_path.write_text(json.dumps(top, ensure_ascii=False), encoding="utf-8")
    (pkg_b / "agents" / "leader" / "AGENT.md").write_text(
        "# B团主理人规则", encoding="utf-8"
    )
    (pkg_b / "agents" / "leader" / "persona" / "leader.md").write_text(
        "# B团主人设", encoding="utf-8"
    )
    (pkg_b / "agents" / "member1" / "persona" / "member1.md").write_text(
        "# B团成员一人设", encoding="utf-8"
    )

    spec_a = _make_team_spec()
    _enrich(spec_a, "sample-expert-group")
    assert "主理人工作规则" in spec_a.leader.prompt
    assert "协作专家团" in spec_a.leader.prompt

    spec_b = _make_team_spec()
    _enrich(spec_b, "another-group")
    # 提示词完全是 B 的，无 A 残留
    assert "B团主理人规则" in spec_b.leader.prompt
    assert "B团主人设" in spec_b.leader.prompt
    assert "B团协作契约" in spec_b.leader.prompt
    assert "主理人工作规则" not in spec_b.leader.prompt
    assert "协作专家团" not in spec_b.leader.prompt
    # roster 同样按 B 重建
    roster_b = {m.member_name: m for m in spec_b.predefined_members}
    assert "B团成员一人设" in roster_b["member1"].prompt
    assert "成员一人设" not in roster_b["member1"].prompt or "B团" in roster_b["member1"].prompt
