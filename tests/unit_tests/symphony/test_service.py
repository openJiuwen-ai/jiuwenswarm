import json

import pytest

from jiuwenswarm.symphony.config import SymphonyOrchestrationConfig
from jiuwenswarm.symphony.orchestration import service
from jiuwenswarm.symphony.orchestration.artifacts import ScoreArtifacts
from jiuwenswarm.symphony.orchestration.planning.fast import FastOneShotPlanner


class _FakeLLMClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls = []

    async def complete_json_async(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.response)


def _artifacts(tmp_path, *, graph_skill_prefix: bool = False):
    def graph_id(value: str) -> str:
        return f"skill:{value}" if graph_skill_prefix else value

    return ScoreArtifacts(
        score_dir=tmp_path,
        manifest={},
        skills=[
            {
                "id": "skill-a",
                "name": "Alpha Skill",
                "description": "Creates an alpha draft.",
                "inputs": [{"name": "brief", "type": "text", "required": True}],
                "outputs": [{"name": "draft", "type": "markdown"}],
            },
            {
                "id": "skill-b",
                "name": "Beta Skill",
                "description": "Reviews an alpha draft for beta quality.",
                "inputs": [{"name": "draft", "type": "markdown", "required": True}],
                "outputs": [{"name": "review", "type": "markdown"}],
            },
            {
                "id": "skill-c",
                "name": "Gamma Skill",
                "description": "Publishes a gamma report.",
                "inputs": [{"name": "review", "type": "markdown", "required": True}],
                "outputs": [{"name": "report", "type": "markdown"}],
            },
        ],
        graph={
            "edges": [
                {
                    "type": "can_feed",
                    "source": graph_id("skill-a"),
                    "target": graph_id("skill-b"),
                    "confidence": 0.91,
                    "method": "llm",
                    "evidence": {
                        "reasons": ["draft feeds review"],
                        "supporting_fields": {
                            "source_outputs": ["draft"],
                            "target_inputs": ["draft"],
                        },
                    },
                },
                {
                    "type": "can_feed",
                    "source": graph_id("skill-a"),
                    "target": graph_id("skill-c"),
                    "confidence": 0.2,
                    "method": "llm",
                    "evidence": {"reasons": ["weak relation"]},
                },
                {
                    "type": "can_feed",
                    "source": graph_id("skill-b"),
                    "target": graph_id("skill-c"),
                    "confidence": 0.88,
                    "method": "llm",
                    "evidence": {
                        "reasons": ["review feeds report"],
                        "supporting_fields": {
                            "source_outputs": ["review"],
                            "target_inputs": ["review"],
                        },
                    },
                },
            ]
        },
        lookup={},
    )


async def test_plan_from_score_fast_uses_one_shot_planner(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "title": "Fast plan",
            "status": "ready",
            "reason": "Alpha feeds beta.",
            "steps": [
                {"skill_id": "skill-a", "reason": "Create draft."},
                {"skill_id": "skill-b", "reason": "Review draft."},
            ],
            "can_feed_edges": [
                {"source_id": "skill-a", "target_id": "skill-b"},
            ],
        }
    )

    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "unrelated user request",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(
            mode="fast",
            min_edge_confidence=0.7,
        ),
        language="en",
    )

    assert len(llm.calls) == 1
    assert result["planning_mode"] == "one_shot_fast"
    assert result["language"] == "en"
    assert result["llm_call_count"] == 1
    assert result["recommended_plans"][0]["title"] == "Fast plan"
    assert result["recommended_plans"][0]["steps"][0]["inputs"] == [
        {"name": "brief", "type": "text", "required": True}
    ]
    assert result["recommended_plans"][0]["steps"][0]["outputs"] == [
        {"name": "draft", "type": "markdown"}
    ]
    assert result["execution_graph"]["edges"][0]["source"] == "skill-a"

    prompt_payload = json.loads(llm.calls[0]["user_content"])
    assert set(prompt_payload) == {"query", "skills", "can_feed_edges"}
    assert "language" not in prompt_payload
    assert "planning_instructions" not in prompt_payload
    assert "Write all user-visible natural-language fields in English" in (
        llm.calls[0]["system_prompt"]
    )
    assert "Keep Skill IDs, original Skill names, status values" in (
        llm.calls[0]["system_prompt"]
    )
    assert prompt_payload["can_feed_edges"] == [
        {
            "source_id": "skill-a",
            "target_id": "skill-b",
        },
        {
            "source_id": "skill-b",
            "target_id": "skill-c",
        },
    ]
    assert all("inputs" not in skill for skill in prompt_payload["skills"])
    assert all("outputs" not in skill for skill in prompt_payload["skills"])
    system_prompt = llm.calls[0]["system_prompt"]
    assert "Infer a missing relationship only when needed" in system_prompt
    assert "explicitly including an inferred edge" in system_prompt
    assert "Symphony infers adjacent step edges during validation" in system_prompt
    assert "missing_inputs does not describe a Skill I/O schema" in system_prompt
    assert llm.calls[0]["request_overrides"] == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }


async def test_plan_from_score_fast_accepts_prefixed_graph_skill_ids(
    monkeypatch,
    tmp_path,
):
    artifacts = _artifacts(tmp_path, graph_skill_prefix=True)
    llm = _FakeLLMClient(
        {
            "title": "Fast plan",
            "status": "ready",
            "steps": [
                {"skill_id": "skill-a"},
                {"skill_id": "skill-b"},
            ],
            "can_feed_edges": [
                {"source_id": "skill-a", "target_id": "skill-b"},
            ],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "prefixed graph",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(
            mode="fast",
            min_edge_confidence=0.7,
        ),
    )

    prompt_payload = json.loads(llm.calls[0]["user_content"])
    assert result["candidate_edge_count"] == 2
    assert prompt_payload["can_feed_edges"] == [
        {"source_id": "skill-a", "target_id": "skill-b"},
        {"source_id": "skill-b", "target_id": "skill-c"},
    ]
    assert result["recommended_plans"][0]["can_feed_edges"][0]["source_id"] == "skill-a"
    assert result["execution_graph"]["edges"][0]["source"] == "skill-a"


async def test_plan_from_score_fast_rejects_unknown_skill_once(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "title": "Invalid",
            "status": "ready",
            "steps": [{"skill_id": "missing-skill"}],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "unrelated user request",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(mode="fast"),
    )

    assert len(llm.calls) == 1
    assert result["success"] is False
    assert "unknown skill IDs" in result["detail"]
    assert result["execution_graph"]["nodes"] == []


async def test_plan_from_score_fast_no_plan_calls_llm_once(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "title": "No useful plan",
            "status": "no_plan",
            "reason": "Candidates do not satisfy the request.",
            "steps": [],
            "can_feed_edges": [],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "unrelated user request",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(mode="fast"),
    )

    assert len(llm.calls) == 1
    assert result["status"] == "no_plan"
    assert result["recommended_plans"] == []
    assert result["execution_graph"]["nodes"] == []


async def test_plan_from_score_fast_accepts_low_confidence_edge_as_inferred(
    monkeypatch,
    tmp_path,
):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "title": "Invalid edge",
            "status": "ready",
            "steps": [
                {"skill_id": "skill-a"},
                {"skill_id": "skill-c"},
            ],
            "can_feed_edges": [
                {
                    "source_id": "skill-a",
                    "target_id": "skill-c",
                    "reason": "The selected capabilities should run in sequence.",
                },
            ],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "unrelated user request",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(
            mode="fast",
            min_edge_confidence=0.7,
        ),
    )

    prompt_payload = json.loads(llm.calls[0]["user_content"])
    assert len(llm.calls) == 1
    assert prompt_payload["can_feed_edges"] == [
        {
            "source_id": "skill-a",
            "target_id": "skill-b",
        },
        {
            "source_id": "skill-b",
            "target_id": "skill-c",
        },
    ]
    inferred_edge = result["recommended_plans"][0]["can_feed_edges"][0]
    assert inferred_edge["method"] == "fast_llm_inferred"
    assert inferred_edge["confidence"] is None
    assert inferred_edge["reason"] == "The selected capabilities should run in sequence."
    assert result["execution_graph"]["edges"][0]["method"] == "fast_llm_inferred"


async def test_plan_from_score_fast_uses_input_candidates_and_neighbors(
    monkeypatch,
    tmp_path,
):
    artifacts = _artifacts(tmp_path)
    artifacts.skills.extend(
        [
            {
                "id": "skill-d",
                "name": "Delta Skill",
                "description": "Unrelated high-confidence source.",
                "inputs": [],
                "outputs": [{"name": "delta", "type": "markdown"}],
            },
            {
                "id": "skill-e",
                "name": "Echo Skill",
                "description": "Unrelated high-confidence target.",
                "inputs": [{"name": "delta", "type": "markdown"}],
                "outputs": [{"name": "echo", "type": "markdown"}],
            },
        ]
    )
    artifacts.graph["edges"].append(
        {
            "type": "can_feed",
            "source": "skill-d",
            "target": "skill-e",
            "confidence": 0.99,
            "method": "llm",
            "evidence": {"reasons": ["unrelated"]},
        }
    )
    llm = _FakeLLMClient(
        {
            "title": "Retrieved fast plan",
            "status": "ready",
            "steps": [{"skill_id": "skill-b", "reason": "Retrieved seed."}],
            "can_feed_edges": [],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "use beta",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(
            mode="fast",
            min_edge_confidence=0.7,
        ),
        candidate_skill_ids=["skill-b"],
    )

    prompt_payload = json.loads(llm.calls[0]["user_content"])
    prompt_skill_ids = {skill["id"] for skill in prompt_payload["skills"]}
    assert prompt_skill_ids == {"skill-a", "skill-b", "skill-c"}
    assert "skill-d" not in prompt_skill_ids
    assert result["skill_retrieval"]["source"] == "input"
    assert result["skill_retrieval"]["used"] is True
    assert result["skill_retrieval"]["candidate_skill_ids"] == ["skill-b"]


async def test_plan_from_score_filters_disabled_skills_from_prompt(
    monkeypatch,
    tmp_path,
):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "title": "Enabled-only plan",
            "status": "ready",
            "steps": [{"skill_id": "skill-a", "reason": "Enabled seed."}],
            "can_feed_edges": [],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "use beta",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(
            mode="fast",
            min_edge_confidence=0.7,
        ),
        candidate_skill_ids=["skill-b"],
        disabled_skill_names=["Beta Skill"],
    )

    prompt_payload = json.loads(llm.calls[0]["user_content"])
    prompt_skill_ids = {skill["id"] for skill in prompt_payload["skills"]}
    assert prompt_skill_ids == {"skill-a", "skill-c"}
    assert prompt_payload["can_feed_edges"] == []
    assert result["skill_retrieval"] == {
        "source": "input",
        "used": False,
        "candidate_skill_ids": [],
        "candidate_count": 0,
        "fallback_reason": "candidate_skill_ids did not match current score",
    }


async def test_plan_from_score_fast_without_candidates_uses_default_subgraph(
    monkeypatch,
    tmp_path,
):
    artifacts = _artifacts(tmp_path)
    artifacts.skills.extend(
        [
            {
                "id": "skill-d",
                "name": "Delta Skill",
                "description": "Unrelated high-confidence source.",
                "inputs": [],
                "outputs": [{"name": "delta", "type": "markdown"}],
            },
            {
                "id": "skill-e",
                "name": "Echo Skill",
                "description": "Unrelated high-confidence target.",
                "inputs": [{"name": "delta", "type": "markdown"}],
                "outputs": [{"name": "echo", "type": "markdown"}],
            },
        ]
    )
    artifacts.graph["edges"].append(
        {
            "type": "can_feed",
            "source": "skill-d",
            "target": "skill-e",
            "confidence": 0.99,
            "method": "llm",
            "evidence": {"reasons": ["unrelated"]},
        }
    )
    llm = _FakeLLMClient(
        {
            "title": "Default fast plan",
            "status": "ready",
            "steps": [{"skill_id": "skill-d", "reason": "Default seed."}],
            "can_feed_edges": [],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "use beta",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(
            mode="fast",
            min_edge_confidence=0.7,
        ),
    )

    prompt_payload = json.loads(llm.calls[0]["user_content"])
    prompt_skill_ids = {skill["id"] for skill in prompt_payload["skills"]}
    assert {"skill-d", "skill-e"}.issubset(prompt_skill_ids)
    assert result["skill_retrieval"] == {
        "source": "input",
        "used": False,
        "candidate_skill_ids": [],
        "candidate_count": 0,
        "fallback_reason": "candidate_skill_ids not provided",
    }


async def test_plan_from_score_resolves_unique_skill_names(
    monkeypatch,
    tmp_path,
):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "title": "Named candidate plan",
            "status": "ready",
            "steps": [{"skill_id": "skill-b", "reason": "Named seed."}],
            "can_feed_edges": [],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "use beta",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(mode="fast"),
        candidate_skill_ids=["Beta Skill", "skill-a", "Beta Skill"],
    )

    assert result["skill_retrieval"]["candidate_skill_ids"] == [
        "skill-b",
        "skill-a",
    ]


async def test_plan_from_score_does_not_guess_ambiguous_skill_names(
    monkeypatch,
    tmp_path,
):
    artifacts = _artifacts(tmp_path)
    artifacts.skills.append(
        {
            "id": "skill-beta-copy",
            "name": "Beta Skill",
            "description": "Another skill with the same display name.",
            "inputs": [],
            "outputs": [],
        }
    )
    llm = _FakeLLMClient(
        {
            "title": "Fallback plan",
            "status": "ready",
            "steps": [{"skill_id": "skill-a", "reason": "Fallback."}],
            "can_feed_edges": [],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "use beta",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(mode="fast"),
        candidate_skill_ids=["Beta Skill"],
    )

    assert result["skill_retrieval"] == {
        "source": "input",
        "used": False,
        "candidate_skill_ids": [],
        "candidate_count": 0,
        "fallback_reason": "candidate_skill_ids did not match current score",
    }


@pytest.mark.parametrize(
    ("candidate_skill_ids", "fallback_reason"),
    [
        ([], "candidate_skill_ids is empty"),
        (
            ["missing-skill", "missing-skill", ""],
            "candidate_skill_ids did not match current score",
        ),
    ],
)
async def test_plan_from_score_fast_falls_back_for_empty_or_unknown_candidates(
    monkeypatch,
    tmp_path,
    candidate_skill_ids,
    fallback_reason,
):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "title": "Fallback fast plan",
            "status": "ready",
            "steps": [{"skill_id": "skill-a", "reason": "Fallback seed."}],
            "can_feed_edges": [],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "use beta",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(
            mode="fast",
            min_edge_confidence=0.7,
        ),
        candidate_skill_ids=candidate_skill_ids,
    )

    prompt_payload = json.loads(llm.calls[0]["user_content"])
    prompt_skill_ids = {skill["id"] for skill in prompt_payload["skills"]}
    assert prompt_skill_ids == {"skill-a", "skill-b", "skill-c"}
    assert result.get("success") is not False
    assert result["skill_retrieval"] == {
        "source": "input",
        "used": False,
        "candidate_skill_ids": [],
        "candidate_count": 0,
        "fallback_reason": fallback_reason,
    }


async def test_plan_from_score_beam_uses_bidirectional_planner(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    seen = {}

    class FakeBeamPlanner:
        def __init__(self, artifacts_arg, **kwargs):
            seen["artifacts"] = artifacts_arg
            seen["kwargs"] = kwargs

        async def plan(self, query):
            seen["query"] = query
            return {
                "planning_mode": "bidirectional_beam",
                "recommended_plans": [],
                "plans": [],
                "status": "no_plan",
            }

    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)
    monkeypatch.setattr(service, "BidirectionalBeamPlanner", FakeBeamPlanner)

    result = await service.plan_from_score(
        tmp_path,
        "beam plan",
        llm_client=object(),
        orchestration_config=SymphonyOrchestrationConfig(
            mode="beam",
            top_k=2,
            max_depth=3,
            min_edge_confidence=0.7,
        ),
        candidate_skill_ids=["skill-a", "unknown"],
    )

    assert result["planning_mode"] == "bidirectional_beam"
    assert result["execution_graph"]["nodes"] == []
    assert result["skill_retrieval"]["candidate_skill_ids"] == ["skill-a"]
    assert seen["artifacts"] is artifacts
    assert seen["query"] == "beam plan"
    assert seen["kwargs"]["top_k"] == 2
    assert seen["kwargs"]["max_depth"] == 3
    assert seen["kwargs"]["candidate_skill_ids"] == ("skill-a",)


async def test_plan_from_score_rejects_unknown_mode(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    with pytest.raises(ValueError, match="Unsupported orchestration mode"):
        await service.plan_from_score(
            tmp_path,
            "graph plan",
            llm_client=object(),
            orchestration_config=SymphonyOrchestrationConfig(mode="graph"),
        )


def _custom_artifacts(tmp_path, skill_ids, edges):
    return ScoreArtifacts(
        score_dir=tmp_path,
        manifest={},
        skills=[
            {
                "id": current_skill_id,
                "name": current_skill_id,
                "description": f"Capability {current_skill_id}",
                "inputs": [],
                "outputs": [],
            }
            for current_skill_id in skill_ids
        ],
        graph={"edges": edges},
        lookup={},
    )


def _score_edge(source, target, confidence=0.9):
    return {
        "type": "can_feed",
        "source": source,
        "target": target,
        "confidence": confidence,
        "method": "test",
        "evidence": {"reasons": [f"{source} feeds {target}"]},
    }


async def test_retrieval_forest_prefers_shortest_path(monkeypatch, tmp_path):
    artifacts = _custom_artifacts(
        tmp_path,
        ["seed-a", "seed-b", "bridge", "incoming", "outgoing"],
        [
            _score_edge("seed-a", "seed-b", 0.71),
            _score_edge("seed-a", "bridge", 0.99),
            _score_edge("bridge", "seed-b", 0.99),
            _score_edge("incoming", "seed-a", 0.98),
            _score_edge("seed-b", "outgoing", 0.97),
        ],
    )
    llm = _FakeLLMClient(
        {
            "status": "ready",
            "steps": [{"skill_id": "seed-a"}, {"skill_id": "seed-b"}],
            "can_feed_edges": [
                {"source_id": "seed-a", "target_id": "seed-b"},
            ],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    await service.plan_from_score(
        tmp_path,
        "connect seeds",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(mode="fast", max_depth=3),
        candidate_skill_ids=["seed-a", "seed-b"],
    )

    prompt = json.loads(llm.calls[0]["user_content"])
    assert [skill["id"] for skill in prompt["skills"]] == ["seed-a", "seed-b"]
    assert prompt["can_feed_edges"] == [
        {"source_id": "seed-a", "target_id": "seed-b"}
    ]


async def test_retrieval_forest_prefers_higher_confidence_for_equal_hops(
    monkeypatch,
    tmp_path,
):
    artifacts = _custom_artifacts(
        tmp_path,
        ["seed-a", "seed-b", "weak", "strong"],
        [
            _score_edge("seed-a", "weak", 0.8),
            _score_edge("weak", "seed-b", 0.8),
            _score_edge("seed-a", "strong", 0.9),
            _score_edge("strong", "seed-b", 0.9),
        ],
    )
    llm = _FakeLLMClient({"status": "ready", "steps": [{"skill_id": "strong"}]})
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    await service.plan_from_score(
        tmp_path,
        "connect seeds",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(mode="fast", max_depth=2),
        candidate_skill_ids=["seed-a", "seed-b"],
    )

    prompt = json.loads(llm.calls[0]["user_content"])
    assert [skill["id"] for skill in prompt["skills"]] == [
        "seed-a",
        "seed-b",
        "strong",
    ]
    assert prompt["can_feed_edges"] == [
        {"source_id": "seed-a", "target_id": "strong"},
        {"source_id": "strong", "target_id": "seed-b"},
    ]


async def test_retrieval_forest_keeps_components_beyond_max_depth(
    monkeypatch,
    tmp_path,
):
    artifacts = _custom_artifacts(
        tmp_path,
        ["seed-a", "seed-b", "bridge"],
        [
            _score_edge("seed-a", "bridge"),
            _score_edge("bridge", "seed-b"),
        ],
    )
    llm = _FakeLLMClient({"status": "ready", "steps": [{"skill_id": "seed-a"}]})
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    await service.plan_from_score(
        tmp_path,
        "keep separate",
        llm_client=llm,
        orchestration_config=SymphonyOrchestrationConfig(mode="fast", max_depth=1),
        candidate_skill_ids=["seed-a", "seed-b"],
    )

    prompt = json.loads(llm.calls[0]["user_content"])
    assert {skill["id"] for skill in prompt["skills"]} == {
        "seed-a",
        "seed-b",
        "bridge",
    }
    assert prompt["can_feed_edges"] == [
        {"source_id": "seed-a", "target_id": "bridge"}
    ]


async def test_fast_default_subgraph_has_no_skill_or_edge_limit(monkeypatch, tmp_path):
    skill_ids = [f"skill-{index:02d}" for index in range(42)]
    edges = []
    for source_index in range(len(skill_ids)):
        for target_index in range(source_index + 1, len(skill_ids)):
            edges.append(_score_edge(skill_ids[source_index], skill_ids[target_index]))
            if len(edges) == 82:
                break
        if len(edges) == 82:
            break
    artifacts = _custom_artifacts(tmp_path, skill_ids, edges)
    llm = _FakeLLMClient({"status": "ready", "steps": [{"skill_id": skill_ids[0]}]})
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(tmp_path, "all candidates", llm_client=llm)

    prompt = json.loads(llm.calls[0]["user_content"])
    assert len(prompt["skills"]) == 42
    assert len(prompt["can_feed_edges"]) == 82
    assert result["candidate_skill_count"] == 42
    assert result["candidate_edge_count"] == 82


async def test_fast_retrieval_forest_has_no_skill_limit(monkeypatch, tmp_path):
    skill_ids = [f"skill-{index:02d}" for index in range(42)]
    edges = [
        _score_edge(skill_ids[index], skill_ids[index + 1])
        for index in range(len(skill_ids) - 1)
    ]
    edge_keys = {(edge["source"], edge["target"]) for edge in edges}
    for source_index in range(len(skill_ids)):
        for target_index in range(source_index + 1, len(skill_ids)):
            key = (skill_ids[source_index], skill_ids[target_index])
            if key not in edge_keys:
                edges.append(_score_edge(*key))
                edge_keys.add(key)
            if len(edges) == 82:
                break
        if len(edges) == 82:
            break
    artifacts = _custom_artifacts(tmp_path, skill_ids, edges)
    llm = _FakeLLMClient({"status": "ready", "steps": [{"skill_id": skill_ids[0]}]})
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "all retrieved candidates",
        llm_client=llm,
        candidate_skill_ids=skill_ids,
    )

    prompt = json.loads(llm.calls[0]["user_content"])
    assert len(edges) == 82
    assert len(prompt["skills"]) == 42
    assert result["candidate_skill_count"] == 42


async def test_fast_default_subgraph_uses_all_skills_without_edges(monkeypatch, tmp_path):
    skill_ids = [f"skill-{index:02d}" for index in range(45)]
    artifacts = _custom_artifacts(tmp_path, skill_ids, [])
    llm = _FakeLLMClient({"status": "ready", "steps": [{"skill_id": skill_ids[0]}]})
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    await service.plan_from_score(tmp_path, "all skills", llm_client=llm)

    prompt = json.loads(llm.calls[0]["user_content"])
    assert len(prompt["skills"]) == 45
    assert prompt["can_feed_edges"] == []


async def test_fast_plan_materializes_existing_and_inferred_edges(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    artifacts.graph["edges"] = artifacts.graph["edges"][:1]
    llm = _FakeLLMClient(
        {
            "status": "ready",
            "steps": [
                {"skill_id": "skill-a"},
                {"skill_id": "skill-b"},
                {"skill_id": "skill-c"},
            ],
            "can_feed_edges": [
                {"source_id": "skill-a", "target_id": "skill-b"},
                {
                    "source_id": "skill-b",
                    "target_id": "skill-c",
                    "reason": "Continue with the selected publisher.",
                },
            ],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(
        tmp_path,
        "mixed edges",
        llm_client=llm,
        candidate_skill_ids=["skill-a", "skill-b", "skill-c"],
    )

    edges = result["recommended_plans"][0]["can_feed_edges"]
    assert [edge["method"] for edge in edges] == ["llm", "fast_llm_inferred"]
    assert result["execution_graph"]["edges"][1]["method"] == "fast_llm_inferred"


async def test_fast_plan_infers_adjacent_edges_when_omitted(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "status": "ready",
            "steps": [{"skill_id": "skill-a"}, {"skill_id": "skill-c"}],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(tmp_path, "infer adjacency", llm_client=llm)

    edge = result["recommended_plans"][0]["can_feed_edges"][0]
    assert edge["source_id"] == "skill-a"
    assert edge["target_id"] == "skill-c"
    assert edge["method"] == "fast_llm_inferred"


@pytest.mark.parametrize(
    ("steps", "edges", "detail"),
    [
        (
            ["skill-a", "skill-b"],
            [{"source_id": "skill-a", "target_id": "skill-c"}],
            "outside plan steps",
        ),
        (
            ["skill-a", "skill-b"],
            [{"source_id": "skill-b", "target_id": "skill-a"}],
            "violate step order",
        ),
        (
            ["skill-a", "skill-b"],
            [
                {"source_id": "skill-a", "target_id": "skill-b"},
                {"source_id": "skill-a", "target_id": "skill-b"},
            ],
            "duplicate can_feed edges",
        ),
    ],
)
async def test_fast_plan_rejects_invalid_inferred_edges(
    monkeypatch,
    tmp_path,
    steps,
    edges,
    detail,
):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "status": "ready",
            "steps": [{"skill_id": skill} for skill in steps],
            "can_feed_edges": edges,
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(tmp_path, "invalid edge", llm_client=llm)

    assert result["success"] is False
    assert detail in result["detail"]
async def test_fast_plan_drops_single_step_self_loop_edge(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "status": "ready",
            "steps": [{"skill_id": "skill-a"}],
            "can_feed_edges": [
                {
                    "source_id": "skill-a",
                    "target_id": "skill-a",
                    "reason": "Self reuse should not become a can_feed edge.",
                }
            ],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(tmp_path, "self loop", llm_client=llm)

    assert result["validation"]["valid"] is True
    plan = result["recommended_plans"][0]
    assert [step["skill_id"] for step in plan["steps"]] == ["skill-a"]
    assert plan["can_feed_edges"] == []


async def test_fast_plan_drops_self_loop_and_keeps_valid_edge(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient(
        {
            "status": "ready",
            "steps": [{"skill_id": "skill-a"}, {"skill_id": "skill-b"}],
            "can_feed_edges": [
                {
                    "source_id": "skill-a",
                    "target_id": "skill-a",
                    "reason": "Self reuse should not become a can_feed edge.",
                },
                {"source_id": "skill-a", "target_id": "skill-b"},
            ],
        }
    )
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    result = await service.plan_from_score(tmp_path, "mixed edges", llm_client=llm)

    assert result["validation"]["valid"] is True
    edges = result["recommended_plans"][0]["can_feed_edges"]
    assert [(edge["source_id"], edge["target_id"]) for edge in edges] == [
        ("skill-a", "skill-b")
    ]


def _dynamic_planner(artifacts, overlay, *, candidate_skill_ids=None):
    return FastOneShotPlanner(
        artifacts,
        llm_config=None,
        llm_client=object(),
        min_edge_confidence=0.7,
        candidate_skill_ids=candidate_skill_ids,
        dynamic_overlay=overlay,
    )


def test_dynamic_overlay_reorders_query_relevant_static_edges(tmp_path):
    planner = _dynamic_planner(
        _artifacts(tmp_path),
        {
            "edges": {
                "skill-b->skill-c:can_feed": {
                    "source_id": "skill-b",
                    "target_id": "skill-c",
                    "relation_type": "can_feed",
                    "runtime_weight": 1.5,
                    "success_count": 10,
                    "failure_count": 0,
                    "attempt_count": 10,
                    "representative_queries": ["publish the gamma report"],
                }
            }
        },
    )

    edges = planner._sorted_eligible_edges("publish the gamma report")

    assert (edges[0]["source"], edges[0]["target"]) == ("skill-b", "skill-c")
    assert edges[0]["runtime_weight"] == 1.5
    assert edges[0]["effective_score"] == pytest.approx(0.88 * 1.5)


def test_dynamic_overlay_downranks_explicitly_failed_static_edge(tmp_path):
    planner = _dynamic_planner(
        _artifacts(tmp_path),
        {
            "edges": {
                "skill-a->skill-b:can_feed": {
                    "source_id": "skill-a",
                    "target_id": "skill-b",
                    "relation_type": "can_feed",
                    "runtime_weight": 0.4,
                    "success_count": 0,
                    "failure_count": 12,
                    "attempt_count": 12,
                    "representative_queries": ["review the alpha draft"],
                }
            }
        },
    )

    edges = planner._sorted_eligible_edges("review the alpha draft")

    assert [(edge["source"], edge["target"]) for edge in edges[:2]] == [
        ("skill-b", "skill-c"),
        ("skill-a", "skill-b"),
    ]
    assert edges[1]["runtime_evidence"]["failure_count"] == 12


@pytest.mark.parametrize("success_count", [1, 2])
def test_runtime_only_edge_requires_repeated_clean_success(tmp_path, success_count):
    planner = _dynamic_planner(
        _artifacts(tmp_path),
        {
            "edges": {
                "skill-c->skill-a:can_feed": {
                    "source_id": "skill-c",
                    "target_id": "skill-a",
                    "relation_type": "can_feed",
                    "runtime_weight": 1.1,
                    "success_count": success_count,
                    "failure_count": 0,
                    "attempt_count": success_count,
                }
            }
        },
        candidate_skill_ids=["skill-c"],
    )

    edges = planner._sorted_eligible_edges("create an alpha draft from gamma")
    runtime_edges = [edge for edge in edges if edge.get("runtime_only")]

    assert bool(runtime_edges) is (success_count >= 2)


def test_dynamic_overlay_is_neutral_for_unrelated_query(tmp_path):
    planner = _dynamic_planner(
        _artifacts(tmp_path),
        {
            "edges": {
                "skill-a->skill-b:can_feed": {
                    "source_id": "skill-a",
                    "target_id": "skill-b",
                    "relation_type": "can_feed",
                    "runtime_weight": 2.0,
                    "success_count": 20,
                    "failure_count": 0,
                    "attempt_count": 20,
                    "representative_queries": ["verify an invoice"],
                }
            }
        },
    )

    edges = planner._sorted_eligible_edges("show tomorrow's weather")

    assert edges[0].get("runtime_evidence") is None
    assert edges[0]["confidence"] == 0.91


async def test_disabled_dynamic_graph_does_not_load_overlay(monkeypatch, tmp_path):
    artifacts = _artifacts(tmp_path)
    llm = _FakeLLMClient({"status": "no_plan", "steps": []})
    monkeypatch.setattr(service, "load_score_artifacts", lambda score_dir: artifacts)

    def fail_if_loaded(score_dir):
        raise AssertionError(f"overlay should not be loaded from {score_dir}")

    monkeypatch.setattr(service, "load_dynamic_overlay", fail_if_loaded)

    result = await service.plan_from_score(
        tmp_path,
        "static only",
        llm_client=llm,
        dynamic_graph_enabled=False,
    )

    assert result["dynamic_graph_enabled"] is False
    assert result["dynamic_overlay_used"] is False
