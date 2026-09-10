# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Domain tests for expert inventory -> graph -> team candidate mining."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.expert.expert_graph import (
    ExpertDescriptor,
    ExpertPort,
    build_and_mine_expert_teams,
    build_expert_graph,
    build_expert_inventory_snapshot,
    mine_expert_team_candidates,
    mine_expert_teams,
    normalize_expert_manifest,
)
from jiuwenswarm.server.runtime.expert.expert_store import (
    ExpertNotFound,
    ExpertSummary,
    LocalDirExpertPackageSource,
)

TESTDATA_GROUP = (
    Path(__file__).parent / "testdata" / "expert_groups" / "sample-expert-group"
)
NOW = "2026-09-10T10:00:00Z"


def _write_agent_package(
    root: Path,
    expert_id: str,
    *,
    skill_dir: str = "skill-folder",
    skill_name: str = "skill-folder",
    collaboration: dict | None = None,
) -> Path:
    package = root / expert_id
    (package / "persona").mkdir(parents=True)
    (package / "persona" / "identity.md").write_text("# Identity", encoding="utf-8")
    (package / "skills" / skill_dir).mkdir(parents=True)
    (package / "skills" / skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: test\n---\n# Skill\n",
        encoding="utf-8",
    )
    metadata = {
        "tags": ["经营", "内容"],
        "quickPrompts": ["帮我完成这个任务"],
        "deliverables": ["最终 HTML"],
    }
    if collaboration is not None:
        metadata["collaboration"] = collaboration
    manifest = {
        "packageType": "agent_template",
        "agentCard": {
            "id": expert_id,
            "name": f"{expert_id} 展示名",
            "description": "用于领域测试",
        },
        "persona": {"dir": "persona"},
        "skills": [{"dir": f"skills/{skill_dir}", "mode": "all"}],
        "metadata": metadata,
    }
    (package / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return package


def _descriptor(
    expert_id: str,
    *,
    inputs: tuple[ExpertPort, ...] = (),
    outputs: tuple[ExpertPort, ...] = (),
    skills: tuple[str, ...] = (),
    utility_skills: tuple[str, ...] = (),
    tags: tuple[str, ...] = (),
    expert_type: str = "agent",
    status: str = "available",
    reusable: bool = True,
    complements_with: tuple[str, ...] = (),
    overlaps_with: tuple[str, ...] = (),
    conflicts_with: tuple[str, ...] = (),
) -> ExpertDescriptor:
    return ExpertDescriptor(
        id=expert_id,
        name=f"{expert_id}专家",
        description=f"{expert_id}描述",
        type=expert_type,
        source="local",
        status=status,
        reusable=reusable,
        tags=tags,
        skills=skills,
        inputs=inputs,
        outputs=outputs,
        content_hash=f"hash-{expert_id}",
        package_schema="agent_group" if expert_type == "team" else "agent_template",
        utility_skills=utility_skills,
        quick_prompts=(f"请完成{expert_id}任务",),
        deliverables=(f"{expert_id}成品",),
        complements_with=complements_with,
        overlaps_with=overlaps_with,
        conflicts_with=conflicts_with,
    )


def _inventory(*descriptors: ExpertDescriptor):
    return build_expert_inventory_snapshot(descriptors, created_at=NOW)


def test_normalize_current_manifest_preserves_declared_skill_name_and_ports(
    tmp_path: Path,
) -> None:
    package = _write_agent_package(
        tmp_path,
        "sales-analyst",
        skill_dir="excel-analysis",
        skill_name="excel-analysis",
        collaboration={
            "contractVersion": "1",
            "inputs": [
                {
                    "name": "source.xlsx",
                    "media_type": "xlsx",
                    "schema_id": "sales-table.v1",
                }
            ],
            "outputs": [
                {
                    "id": "insights.json",
                    "mediaType": "json",
                    "schema": {"$id": "sales-insight.v1"},
                    "primary": True,
                }
            ],
            "utilitySkills": ["webapp-testing"],
        },
    )

    descriptor = normalize_expert_manifest(package)

    assert descriptor.status == "available"
    assert descriptor.reusable is True
    assert descriptor.skills == ("excel-analysis",)
    assert descriptor.inputs[0].media_type.endswith("spreadsheetml.sheet")
    assert descriptor.inputs[0].schema == "sales-table.v1"
    assert descriptor.outputs[0].media_type == "application/json"
    assert descriptor.outputs[0].schema == '{"$id":"sales-insight.v1"}'
    assert len(descriptor.content_hash) == 64


def test_uncallable_skill_name_is_quarantined_before_team_mining(
    tmp_path: Path,
) -> None:
    producer = _write_agent_package(
        tmp_path,
        "producer",
        skill_dir="runtime-skill-id",
        skill_name="Original Display Name",
        collaboration={
            "outputs": [
                {
                    "id": "brief.json",
                    "mediaType": "application/json",
                    "schema": "brief.v1",
                }
            ]
        },
    )
    consumer = _write_agent_package(
        tmp_path,
        "consumer",
        skill_dir="copywriter",
        skill_name="copywriter",
        collaboration={
            "inputs": [
                {
                    "id": "brief.json",
                    "mediaType": "application/json",
                    "schema": "brief.v1",
                }
            ],
            "outputs": [
                {
                    "id": "result.html",
                    "mediaType": "text/html",
                    "schema": "result.v1",
                    "primary": True,
                    "visibility": "public",
                }
            ],
        },
    )

    producer_descriptor = normalize_expert_manifest(producer)
    consumer_descriptor = normalize_expert_manifest(consumer)
    graph = build_expert_graph(_inventory(producer_descriptor, consumer_descriptor))
    candidates = mine_expert_team_candidates(graph)

    assert producer_descriptor.skills == ("Original Display Name",)
    assert producer_descriptor.status == "invalid"
    assert producer_descriptor.reusable is False
    assert all("producer" not in candidate.member_ids for candidate in candidates)


def test_skill_inventory_skips_parent_directory_escape_without_name_leak(
    tmp_path: Path,
) -> None:
    package = _write_agent_package(tmp_path, "safe-expert")
    external = tmp_path / "external-secret"
    external.mkdir()
    (external / "SKILL.md").write_text(
        "---\nname: must-not-leak\ndescription: external\n---\n",
        encoding="utf-8",
    )
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"].append({"dir": "../external-secret"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    descriptor = normalize_expert_manifest(package)

    assert descriptor.status == "invalid"
    assert descriptor.skills == ("skill-folder",)
    assert "must-not-leak" not in descriptor.skills


def test_skill_inventory_skips_external_symlink_without_name_leak(
    tmp_path: Path,
) -> None:
    package = _write_agent_package(tmp_path, "safe-expert")
    external = tmp_path / "external-symlink-target"
    external.mkdir()
    (external / "SKILL.md").write_text(
        "---\nname: symlink-secret\ndescription: external\n---\n",
        encoding="utf-8",
    )
    linked = package / "skills" / "linked-external"
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"platform does not permit directory symlinks: {exc}")
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"].append({"dir": "skills/linked-external"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    descriptor = normalize_expert_manifest(package)

    assert descriptor.status == "invalid"
    assert descriptor.skills == ("skill-folder",)
    assert "symlink-secret" not in descriptor.skills


def test_normalize_legacy_agent_is_visible_but_not_reusable(tmp_path: Path) -> None:
    package = tmp_path / "legacy-id"
    (package / "persona").mkdir(parents=True)
    (package / "persona" / "identity.md").write_text("# old", encoding="utf-8")
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "agent_template",
                "name": "legacy-id",
                "display_name": {"zh": "旧版产品专家", "en": "Legacy Product"},
                "display_description": {"cn": "旧 schema 描述"},
                "tags": ["产品"],
                "quick_inputs": ["帮我梳理产品需求"],
                "persona": {"dir": "persona"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    descriptor = normalize_expert_manifest(package)

    assert descriptor.id == "legacy-id"
    assert descriptor.name == "旧版产品专家"
    assert descriptor.description == "旧 schema 描述"
    assert descriptor.tags == ("产品",)
    assert descriptor.quick_prompts == ("帮我梳理产品需求",)
    assert descriptor.package_schema == "legacy_agent_template"
    assert descriptor.status == "needs_conversion"
    assert descriptor.reusable is False


def test_normalize_agent_group_is_team_and_never_reusable(tmp_path: Path) -> None:
    package = tmp_path / TESTDATA_GROUP.name
    shutil.copytree(TESTDATA_GROUP, package)
    # Keep the group fixture shape but use the strict nested identity accepted by
    # the pinned core loader in this integration environment.
    for member in ("leader", "member1", "member2"):
        manifest_path = package / "agents" / member / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["agentCard"] = {
            "id": member,
            "name": manifest.pop("name"),
            "description": manifest.pop("description"),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
    top_manifest_path = package / "manifest.json"
    top_manifest = json.loads(top_manifest_path.read_text(encoding="utf-8"))
    top_manifest["metadata"] = {
        "tags": ["专家团"],
        "workflow": ["成员一调研", "成员二成稿"],
        "deliverables": ["最终报告"],
    }
    top_manifest_path.write_text(
        json.dumps(top_manifest, ensure_ascii=False), encoding="utf-8"
    )
    member_skill = package / "agents" / "member1" / "skills" / "private-dir"
    member_skill.mkdir(parents=True)
    (member_skill / "SKILL.md").write_text(
        "---\nname: original-private-skill\ndescription: private\n---\n# Skill\n",
        encoding="utf-8",
    )
    member_manifest_path = package / "agents" / "member1" / "manifest.json"
    member_manifest = json.loads(member_manifest_path.read_text(encoding="utf-8"))
    member_manifest["skills"] = [{"dir": "skills/private-dir", "mode": "all"}]
    member_manifest_path.write_text(
        json.dumps(member_manifest, ensure_ascii=False), encoding="utf-8"
    )

    descriptor = normalize_expert_manifest(package)

    assert descriptor.id == TESTDATA_GROUP.name
    assert descriptor.type == "team"
    assert descriptor.status == "available"
    assert descriptor.reusable is False
    # Top-level shared Skill name comes from SKILL.md, not a UI alias.
    assert descriptor.skills == ("skill_name_1",)

    summary = LocalDirExpertPackageSource._summarize(package)
    assert summary.metadata["workflow"] == ["成员一调研", "成员二成稿"]
    assert summary.metadata["deliverables"] == ["最终报告"]
    assert [skill["name"] for skill in summary.skills] == ["skill_name_1"]
    member1 = next(member for member in summary.members if member["id"] == "member1")
    assert [skill["name"] for skill in member1["skills"]] == ["original-private-skill"]


def test_inventory_deduplicates_same_hash_and_quarantines_id_conflict() -> None:
    base = _descriptor("same")
    same_content_other_source = ExpertDescriptor(**{**base.__dict__, "source": "repo"})
    different_content = ExpertDescriptor(
        **{**base.__dict__, "source": "import", "content_hash": "different"}
    )

    deduplicated = _inventory(base, same_content_other_source)
    assert len(deduplicated.experts) == 1
    assert deduplicated.experts[0].source == "local+repo"
    assert deduplicated.experts[0].reusable is True

    conflicted = _inventory(base, different_content)
    assert conflicted.experts[0].status == "conflict"
    assert conflicted.experts[0].reusable is False
    assert "已隔离" in conflicted.warnings[0]


def test_graph_builds_contract_and_semantic_edges_with_stable_wire_shape() -> None:
    producer = _descriptor(
        "producer",
        outputs=(
            ExpertPort(
                id="insight",
                media_type="application/json",
                schema="insight.v1",
            ),
            ExpertPort(
                id="brief",
                media_type="application/json",
                schema="brief.v1",
            ),
        ),
        skills=("analysis", "generic-render"),
        utility_skills=("generic-render",),
        tags=("经营",),
    )
    exact_consumer = _descriptor(
        "exact-consumer",
        inputs=(
            ExpertPort(
                id="insight", media_type="application/json", schema="insight.v1"
            ),
        ),
        skills=("campaign", "generic-render"),
        utility_skills=("generic-render",),
        tags=("经营",),
    )
    adapter_consumer = _descriptor(
        "adapter-consumer",
        inputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v2"),
        ),
    )
    overlapping = _descriptor(
        "overlapping",
        skills=("analysis",),
        overlaps_with=("producer",),
    )
    conflicting = _descriptor("conflicting", conflicts_with=("producer",))

    graph = build_expert_graph(
        _inventory(
            producer,
            exact_consumer,
            adapter_consumer,
            overlapping,
            conflicting,
        )
    )
    edge_types = {(edge.source, edge.target, edge.type) for edge in graph.edges}

    assert ("producer", "exact-consumer", "can_feed") in edge_types
    assert ("producer", "adapter-consumer", "needs_adapter") in edge_types
    assert ("exact-consumer", "producer", "complements") in edge_types
    assert ("overlapping", "producer", "overlaps") in edge_types
    assert ("conflicting", "producer", "conflicts") in edge_types

    payload = graph.to_dict()
    assert set(payload) == {
        "schemaVersion",
        "graphId",
        "createdAt",
        "sourceHash",
        "nodes",
        "edges",
        "stats",
    }
    assert set(payload["nodes"][0]) == {
        "id",
        "name",
        "description",
        "type",
        "source",
        "status",
        "reusable",
        "tags",
        "skills",
        "inputs",
        "outputs",
    }
    assert payload["createdAt"] == NOW
    assert payload["graphId"].endswith(payload["sourceHash"][:12])


def test_missing_schema_is_needs_adapter_not_executable_can_feed() -> None:
    producer = _descriptor(
        "producer",
        outputs=(ExpertPort(id="brief", media_type="application/json"),),
    )
    consumer = _descriptor(
        "consumer",
        inputs=(ExpertPort(id="brief", media_type="application/json"),),
    )
    graph = build_expert_graph(_inventory(producer, consumer))

    assert [edge.type for edge in graph.edges] == ["needs_adapter"]
    assert mine_expert_team_candidates(graph) == ()


def test_miner_returns_connected_dag_candidates_and_excludes_team_nodes() -> None:
    first = _descriptor(
        "first",
        outputs=(ExpertPort(id="a", media_type="application/json", schema="a.v1"),),
        skills=("research",),
    )
    second = _descriptor(
        "second",
        inputs=(ExpertPort(id="a", media_type="application/json", schema="a.v1"),),
        outputs=(ExpertPort(id="b", media_type="application/json", schema="b.v1"),),
        skills=("planning",),
    )
    last = _descriptor(
        "last",
        inputs=(ExpertPort(id="b", media_type="application/json", schema="b.v1"),),
        outputs=(
            ExpertPort(
                id="page.html",
                media_type="text/html",
                schema="campaign-page.v1",
                primary=True,
                visibility="public",
            ),
        ),
        skills=("writing",),
    )
    existing_team = _descriptor(
        "existing-team",
        inputs=(ExpertPort(id="b", media_type="application/json", schema="b.v1"),),
        expert_type="team",
        reusable=False,
    )

    graph = build_expert_graph(_inventory(first, second, last, existing_team))
    candidates = mine_expert_team_candidates(graph, limit=20)

    assert candidates
    full_chain = next(
        candidate
        for candidate in candidates
        if set(candidate.member_ids) == {"first", "second", "last"}
    )
    assert full_chain.member_ids == ("first", "second", "last")
    assert full_chain.leader_id == "last"
    assert full_chain.status == "ready"
    assert full_chain.score > 80
    assert sum(full_chain.score_breakdown.values()) == pytest.approx(full_chain.score)
    assert full_chain.workflow[1]["dependsOn"] == ["first"]
    assert full_chain.workflow[2]["dependsOn"] == ["second"]
    assert full_chain.workflow[2]["finalOutput"] == {
        "id": "page.html",
        "mediaType": "text/html",
        "schema": "campaign-page.v1",
        "description": "",
        "primary": True,
        "visibility": "public",
    }
    assert full_chain.deliverables == ("page.html",)
    assert "existing-team" not in {
        member_id for candidate in candidates for member_id in candidate.member_ids
    }

    payload = mine_expert_teams(graph)
    wire_candidate = next(
        candidate
        for candidate in payload["candidates"]
        if set(candidate["memberIds"]) == {"first", "second", "last"}
    )
    assert set(wire_candidate) == {
        "id",
        "name",
        "description",
        "memberIds",
        "leaderId",
        "workflow",
        "score",
        "scoreBreakdown",
        "quickPrompts",
        "deliverables",
        "status",
    }


def test_candidate_id_is_stable_when_unrelated_team_is_added() -> None:
    producer = _descriptor(
        "producer",
        outputs=(
            ExpertPort(
                id="brief",
                media_type="application/json",
                schema="brief.v1",
            ),
        ),
    )
    consumer = _descriptor(
        "consumer",
        inputs=(
            ExpertPort(
                id="brief",
                media_type="application/json",
                schema="brief.v1",
            ),
        ),
        outputs=(
            ExpertPort(
                id="result.html",
                media_type="text/html",
                schema="result.v1",
                description="最终页面",
                primary=True,
                visibility="public",
            ),
        ),
    )
    before = mine_expert_team_candidates(
        build_expert_graph(_inventory(producer, consumer))
    )[0]
    existing_team = _descriptor(
        "installed-team",
        expert_type="team",
        reusable=False,
    )
    after = mine_expert_team_candidates(
        build_expert_graph(_inventory(producer, consumer, existing_team))
    )[0]

    assert after.id == before.id


def test_installed_team_marks_same_candidate_as_installed() -> None:
    producer = _descriptor(
        "producer",
        outputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v1"),
        ),
    )
    consumer = _descriptor(
        "consumer",
        inputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v1"),
        ),
        outputs=(
            ExpertPort(
                id="result.html",
                media_type="text/html",
                schema="result.v1",
                primary=True,
                visibility="public",
            ),
        ),
    )
    initial = mine_expert_team_candidates(
        build_expert_graph(_inventory(producer, consumer))
    )[0]
    installed_team = _descriptor(
        initial.id,
        expert_type="team",
        reusable=False,
    )

    refreshed = mine_expert_team_candidates(
        build_expert_graph(_inventory(producer, consumer, installed_team))
    )[0]

    assert refreshed.id == initial.id
    assert refreshed.status == "installed"


def test_miner_rejects_fork_with_multiple_sinks() -> None:
    source = _descriptor(
        "source",
        outputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v1"),
        ),
    )
    sink_one = _descriptor(
        "sink-one",
        inputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v1"),
        ),
        outputs=(
            ExpertPort(
                id="one.html",
                media_type="text/html",
                schema="page.v1",
                primary=True,
                visibility="public",
            ),
        ),
    )
    sink_two = _descriptor(
        "sink-two",
        inputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v1"),
        ),
        outputs=(
            ExpertPort(
                id="two.html",
                media_type="text/html",
                schema="page.v1",
                primary=True,
                visibility="public",
            ),
        ),
    )

    candidates = mine_expert_team_candidates(
        build_expert_graph(_inventory(source, sink_one, sink_two))
    )

    assert not any(len(candidate.member_ids) == 3 for candidate in candidates)


def test_miner_requires_unique_public_primary_output_on_sink() -> None:
    producer = _descriptor(
        "producer",
        outputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v1"),
        ),
    )
    no_public_primary = _descriptor(
        "consumer",
        inputs=(
            ExpertPort(id="brief", media_type="application/json", schema="brief.v1"),
        ),
        outputs=(
            ExpertPort(
                id="draft.html",
                media_type="text/html",
                schema="result.v1",
                primary=True,
                visibility="internal",
            ),
        ),
    )

    candidates = mine_expert_team_candidates(
        build_expert_graph(_inventory(producer, no_public_primary))
    )

    assert candidates == ()


def test_miner_rejects_cycles_conflicts_and_non_reusable_nodes() -> None:
    a = _descriptor(
        "a",
        inputs=(ExpertPort(id="b", media_type="application/json", schema="b.v1"),),
        outputs=(ExpertPort(id="a", media_type="application/json", schema="a.v1"),),
        conflicts_with=("c",),
    )
    b = _descriptor(
        "b",
        inputs=(ExpertPort(id="a", media_type="application/json", schema="a.v1"),),
        outputs=(ExpertPort(id="b", media_type="application/json", schema="b.v1"),),
    )
    c = _descriptor(
        "c",
        inputs=(ExpertPort(id="a", media_type="application/json", schema="a.v1"),),
    )
    pending = _descriptor(
        "pending",
        inputs=(ExpertPort(id="a", media_type="application/json", schema="a.v1"),),
        status="needs_conversion",
        reusable=False,
    )

    candidates = mine_expert_team_candidates(
        build_expert_graph(_inventory(a, b, c, pending))
    )

    assert not any(set(candidate.member_ids) == {"a", "b"} for candidate in candidates)
    assert not any(set(candidate.member_ids) == {"a", "c"} for candidate in candidates)
    assert not any("pending" in candidate.member_ids for candidate in candidates)


class _FakeSource:
    def __init__(self, packages: dict[str, Path]) -> None:
        self.packages = packages

    async def list(self) -> list[ExpertSummary]:
        return [
            ExpertSummary(
                id=expert_id,
                name=expert_id,
                description="",
                source="local",
                available=True,
            )
            for expert_id in sorted(self.packages)
        ]

    async def fetch(self, expert_id: str) -> Path:
        try:
            return self.packages[expert_id]
        except KeyError as exc:
            raise ExpertNotFound(expert_id) from exc


@pytest.mark.asyncio
async def test_rpc_ready_end_to_end_service(tmp_path: Path) -> None:
    producer = _write_agent_package(
        tmp_path,
        "producer",
        collaboration={
            "outputs": [{"id": "brief", "mediaType": "json", "schema": "brief.v1"}]
        },
    )
    consumer = _write_agent_package(
        tmp_path,
        "consumer",
        collaboration={
            "inputs": [{"id": "brief", "mediaType": "json", "schema": "brief.v1"}],
            "outputs": [
                {
                    "id": "page.html",
                    "mediaType": "html",
                    "schema": "page.v1",
                    "primary": True,
                    "visibility": "public",
                }
            ],
        },
    )

    payload = await build_and_mine_expert_teams(
        _FakeSource({"producer": producer, "consumer": consumer}),
        created_at=NOW,
    )

    assert payload["inventory"]["stats"]["expertCount"] == 2
    assert payload["graph"]["stats"]["edgeTypeCounts"]["can_feed"] == 1
    assert payload["mining"]["stats"]["candidateCount"] == 1
    assert payload["mining"]["candidates"][0]["memberIds"] == [
        "producer",
        "consumer",
    ]
