import asyncio
import json

from jiuwenswarm.symphony.orchestration.artifacts import ScoreArtifacts
from jiuwenswarm.symphony.orchestration.planning.beam import (
    BidirectionalBeamPlanner,
)


class _FakeBeamLLM:
    def __init__(
        self,
        scores: dict[str, float],
        *,
        delay: float = 0.0,
        rerank_selection: dict | list | None = None,
    ) -> None:
        self.scores = scores
        self.delay = delay
        self.rerank_selection = rerank_selection or {"selected_plan_index": 1}
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def complete_json_async(self, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            payload = json.loads(kwargs["user_content"])
            if "candidate_plans" in payload:
                return json.dumps(self.rerank_selection)
            return json.dumps(
                {
                    "judgements": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "score": self.scores.get(candidate["skill"]["id"], 0.9),
                            "reason": f"{candidate['skill']['id']} is useful",
                        }
                        for candidate in payload["candidates"]
                    ]
                }
            )
        finally:
            self.active -= 1

    @property
    def judge_calls(self):
        return [
            call
            for call in self.calls
            if "candidates" in json.loads(call["user_content"])
        ]

    @property
    def rerank_calls(self):
        return [
            call
            for call in self.calls
            if "candidate_plans" in json.loads(call["user_content"])
        ]


async def test_beam_batches_outgoing_neighbors_and_filters_low_scores(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-b", confidence=0.91),
            _edge("skill-a", "skill-c", confidence=0.88),
            _edge("skill-a", "skill-d", confidence=0.2),
        ],
    )
    llm = _FakeBeamLLM({"skill-b": 0.9, "skill-c": 0.4})

    result = await _planner(
        artifacts,
        llm,
        top_k=2,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
    ).plan("compose an alpha plan")

    assert result["planning_mode"] == "bidirectional_beam"
    assert result["llm_call_count"] == 2
    assert result["decision"]["judge_llm_call_count"] == 1
    assert result["recommended_plans"][0]["title"] == "compose an alpha plan"
    assert result["beam_search"]["round_index"] == 1
    graph_nodes = {item["id"]: item for item in result["beam_search"]["graph"]["nodes"]}
    assert graph_nodes["skill-a"]["status"] == "final"
    assert graph_nodes["skill-a"]["seed"] is True
    assert graph_nodes["skill-b"]["status"] == "final"
    assert graph_nodes["skill-c"]["status"] == "rejected"
    payload = json.loads(llm.judge_calls[0]["user_content"])
    assert set(payload) == {
        "query",
        "direction",
        "current_skill",
        "candidates",
    }
    assert payload["direction"] == "forward"
    assert [item["skill"]["id"] for item in payload["candidates"]] == [
        "skill-b",
        "skill-c",
    ]
    assert all(set(item) == {"candidate_id", "skill"} for item in payload["candidates"])
    assert (
        "Write all user-visible natural-language fields in Simplified Chinese"
        in (llm.judge_calls[0]["system_prompt"])
    )
    assert _plan_signatures(result) == {("skill-a", "skill-b")}


async def test_beam_scores_incoming_neighbors_for_backward_expansion(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-c", confidence=0.91),
            _edge("skill-b", "skill-c", confidence=0.89),
        ],
    )
    llm = _FakeBeamLLM({"skill-a": 0.8, "skill-b": 0.9})

    result = await _planner(
        artifacts,
        llm,
        top_k=2,
        max_depth=2,
        candidate_skill_ids=["skill-c"],
    ).plan("prepare the final output")

    payload = json.loads(llm.judge_calls[0]["user_content"])
    assert payload["direction"] == "backward"
    assert {item["skill"]["id"] for item in payload["candidates"]} == {
        "skill-a",
        "skill-b",
    }
    assert _plan_signatures(result) == {
        ("skill-a", "skill-c"),
        ("skill-b", "skill-c"),
    }


async def test_beam_reuses_same_round_judgement_for_duplicate_skill_edge(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-shared", confidence=0.91),
            _edge("skill-c", "skill-shared", confidence=0.9),
            _edge("skill-shared", "skill-d", confidence=0.89),
        ],
    )
    llm = _FakeBeamLLM({"skill-shared": 0.9, "skill-d": 0.95})

    result = await _planner(
        artifacts,
        llm,
        top_k=2,
        max_depth=3,
        candidate_skill_ids=["skill-a", "skill-c"],
    ).plan("finish through the shared skill")

    assert result["llm_call_count"] == 4
    assert len(llm.judge_calls) == 3
    assert len(llm.rerank_calls) == 1
    first_round_current_skills = {
        json.loads(call["user_content"])["current_skill"]["id"]
        for call in llm.judge_calls[:2]
    }
    assert first_round_current_skills == {"skill-a", "skill-c"}
    final_payload = json.loads(llm.judge_calls[-1]["user_content"])
    assert [item["skill"]["id"] for item in final_payload["candidates"]] == ["skill-d"]
    assert result["decision"]["judge_cache_misses"] >= 3
    assert any("skill-d" in signature for signature in _plan_signatures(result))


async def test_beam_judges_same_candidate_separately_by_direction(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-shared", confidence=0.91),
            _edge("skill-shared", "skill-b", confidence=0.9),
        ],
    )
    llm = _FakeBeamLLM({"skill-shared": 0.9})

    result = await _planner(
        artifacts,
        llm,
        top_k=2,
        max_depth=2,
        candidate_skill_ids=["skill-a", "skill-b"],
    ).plan("use the shared skill")

    shared_calls = [
        json.loads(call["user_content"])
        for call in llm.judge_calls
        if any(
            candidate["skill"]["id"] == "skill-shared"
            for candidate in json.loads(call["user_content"])["candidates"]
        )
    ]
    assert result["llm_call_count"] == 3
    assert {payload["direction"] for payload in shared_calls} == {
        "forward",
        "backward",
    }


async def test_beam_limits_concurrent_judge_requests(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-b", confidence=0.91),
            _edge("skill-c", "skill-d", confidence=0.9),
            _edge("skill-e", "skill-f", confidence=0.89),
        ],
    )
    llm = _FakeBeamLLM(
        {"skill-b": 0.9, "skill-d": 0.9, "skill-f": 0.9},
        delay=0.02,
    )

    result = await _planner(
        artifacts,
        llm,
        top_k=3,
        max_depth=2,
        candidate_skill_ids=["skill-a", "skill-c", "skill-e"],
    ).plan("run several independent branches")

    assert result["llm_call_count"] == 4
    assert llm.max_active == 3


async def test_beam_merges_converging_paths_into_dag_plan(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-c", confidence=0.91),
            _edge("skill-b", "skill-c", confidence=0.9),
        ],
    )
    llm = _FakeBeamLLM({"skill-c": 0.9})

    result = await _planner(
        artifacts,
        llm,
        top_k=3,
        max_depth=2,
        candidate_skill_ids=["skill-a", "skill-b"],
    ).plan("merge two preparation skills")

    assert ("skill-a", "skill-b", "skill-c") in _plan_signatures(result)
    merged = next(
        plan
        for plan in result["recommended_plans"]
        if tuple(step["skill_id"] for step in plan["steps"])
        == ("skill-a", "skill-b", "skill-c")
    )
    assert {
        (edge["source_id"], edge["target_id"]) for edge in merged["can_feed_edges"]
    } == {("skill-a", "skill-c"), ("skill-b", "skill-c")}
    graph = result["beam_search"]["graph"]
    assert sorted(node["id"] for node in graph["nodes"]) == [
        "skill-a",
        "skill-b",
        "skill-c",
    ]
    assert {(edge["source"], edge["target"]) for edge in graph["edges"]} == {
        ("skill-a", "skill-c"),
        ("skill-b", "skill-c"),
    }
    assert {edge["status"] for edge in graph["edges"]} == {"final", "selected"}


async def test_beam_keeps_diverging_paths_as_separate_plans(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-b", confidence=0.91),
            _edge("skill-a", "skill-c", confidence=0.9),
        ],
    )
    llm = _FakeBeamLLM({"skill-b": 0.9, "skill-c": 0.9})

    result = await _planner(
        artifacts,
        llm,
        top_k=3,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
    ).plan("choose one downstream path")

    assert _plan_signatures(result) == {
        ("skill-a", "skill-b"),
        ("skill-a", "skill-c"),
    }


async def test_beam_final_rerank_selects_second_plan(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-b", confidence=0.91),
            _edge("skill-a", "skill-c", confidence=0.9),
        ],
    )
    llm = _FakeBeamLLM(
        {"skill-b": 0.9, "skill-c": 0.9},
        rerank_selection={
            "selected_plan_index": 2,
            "reason": "skill c better matches the task",
        },
    )

    result = await _planner(
        artifacts,
        llm,
        top_k=2,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
    ).plan("choose the best downstream skill")

    assert tuple(
        step["skill_id"] for step in result["recommended_plans"][0]["steps"]
    ) == ("skill-a", "skill-c")
    assert result["decision"]["final_rerank"] == {
        "applied": True,
        "llm_call_count": 1,
        "cross_plan_skill_merge": False,
        "error": "",
    }
    rerank_payload = json.loads(llm.rerank_calls[0]["user_content"])
    assert set(rerank_payload) == {
        "query",
        "required_output_types",
        "candidate_skill_pool",
        "candidate_can_feed_edges",
        "candidate_plans",
    }
    assert {item["id"] for item in rerank_payload["candidate_skill_pool"]} == {
        "skill-a",
        "skill-b",
        "skill-c",
    }
    assert {
        (edge["source_id"], edge["target_id"])
        for edge in rerank_payload["candidate_can_feed_edges"]
    } == {("skill-a", "skill-b"), ("skill-a", "skill-c")}


async def test_beam_final_rerank_merges_paths_without_seed_guard(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-b", confidence=0.91),
            _edge("skill-a", "skill-c", confidence=0.9),
        ],
    )
    llm = _FakeBeamLLM(
        {"skill-b": 0.9, "skill-c": 0.9},
        rerank_selection={
            "selected_plan_index": 1,
            "reason": "both delivery skills are required",
            "steps": [
                {"skill_id": "skill-b", "reason": "first deliverable"},
                {"skill_id": "skill-c", "reason": "second deliverable"},
            ],
            "can_feed_edges": [],
        },
    )

    result = await _planner(
        artifacts,
        llm,
        top_k=2,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
    ).plan("produce two independent deliverables")

    primary = result["recommended_plans"][0]
    assert tuple(step["skill_id"] for step in primary["steps"]) == (
        "skill-b",
        "skill-c",
    )
    assert primary["can_feed_edges"] == []
    assert [step["skill_id"] for step in primary["stages"][0]["skills"]] == [
        "skill-b",
        "skill-c",
    ]
    assert primary["source"] == "bidirectional_beam_final_rerank"
    assert result["decision"]["final_rerank"]["cross_plan_skill_merge"] is True


async def test_beam_final_rerank_drops_unknown_items_without_rejecting(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[_edge("skill-a", "skill-b", confidence=0.91)],
    )
    llm = _FakeBeamLLM(
        {"skill-b": 0.9},
        rerank_selection={
            "steps": ["unknown-skill", "skill-a", "skill-b"],
            "can_feed_edges": [
                {"source_id": "skill-b", "target_id": "skill-a"},
                {"source_id": "skill-a", "target_id": "skill-b"},
            ],
        },
    )

    result = await _planner(
        artifacts,
        llm,
        top_k=1,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
    ).plan("compose a plan")

    primary = result["recommended_plans"][0]
    assert tuple(step["skill_id"] for step in primary["steps"]) == (
        "skill-a",
        "skill-b",
    )
    assert [
        (edge["source_id"], edge["target_id"]) for edge in primary["can_feed_edges"]
    ] == [("skill-a", "skill-b")]
    assert result["decision"]["final_rerank"]["applied"] is True
    assert result["decision"]["final_rerank"]["error"] == ""


async def test_beam_final_rerank_malformed_response_falls_back(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[_edge("skill-a", "skill-b", confidence=0.91)],
    )
    llm = _FakeBeamLLM(
        {"skill-b": 0.9},
        rerank_selection=["not", "an", "object"],
    )

    result = await _planner(
        artifacts,
        llm,
        top_k=1,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
    ).plan("compose a plan")

    assert _plan_signatures(result) == {("skill-a", "skill-b")}
    assert result["decision"]["final_rerank"]["applied"] is False
    assert (
        result["decision"]["final_rerank"]["error"]
        == "Final rerank returned non-object JSON."
    )


async def test_beam_progress_callback_receives_lightweight_graph_events(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[
            _edge("skill-a", "skill-b", confidence=0.91),
            _edge("skill-a", "skill-c", confidence=0.88),
        ],
    )
    llm = _FakeBeamLLM({"skill-b": 0.9, "skill-c": 0.4})
    events = []

    async def progress_callback(event):
        events.append(event)

    result = await _planner(
        artifacts,
        llm,
        top_k=2,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
        progress_callback=progress_callback,
    ).plan("compose an alpha plan")

    assert [event["event"] for event in events] == [
        "started",
        "candidates_found",
        "candidates_judged",
        "graph_merged",
        "completed",
    ]
    assert result["language"] == "cn"
    assert result["beam_search"]["language"] == "cn"
    assert all(event["language"] == "cn" for event in events)
    assert all(
        set(event) == {"event", "language", "round_index", "graph"} for event in events
    )
    judged_nodes = {node["id"]: node for node in events[2]["graph"]["nodes"]}
    assert judged_nodes["skill-b"]["status"] == "selected"
    assert judged_nodes["skill-c"]["status"] == "rejected"


async def test_beam_judge_reason_uses_english_without_seed_copy(tmp_path):
    artifacts = _artifacts(
        tmp_path,
        edges=[_edge("skill-a", "skill-b", confidence=0.91)],
    )
    llm = _FakeBeamLLM({"skill-b": 0.9})

    result = await _planner(
        artifacts,
        llm,
        top_k=1,
        max_depth=2,
        candidate_skill_ids=["skill-a"],
        language="en",
    ).plan("compose a plan")

    prompt = llm.calls[0]
    payload = json.loads(prompt["user_content"])
    assert "language" not in payload
    assert "language_instruction" not in payload
    assert "state" not in payload
    assert (
        "Write all user-visible natural-language fields in English"
        in (prompt["system_prompt"])
    )
    assert result["reason"] == "skill-b is useful"


def _planner(
    artifacts: ScoreArtifacts,
    llm: _FakeBeamLLM,
    *,
    top_k: int,
    max_depth: int,
    candidate_skill_ids: list[str],
    progress_callback=None,
    language="cn",
) -> BidirectionalBeamPlanner:
    return BidirectionalBeamPlanner(
        artifacts,
        llm_config=None,
        llm_client=llm,
        min_edge_confidence=0.7,
        top_k=top_k,
        max_depth=max_depth,
        candidate_skill_ids=candidate_skill_ids,
        progress_callback=progress_callback,
        language=language,
    )


def _artifacts(
    tmp_path,
    *,
    edges: list[dict[str, object]],
) -> ScoreArtifacts:
    skill_ids = sorted(
        {endpoint for edge in edges for endpoint in (edge["source"], edge["target"])}
    )
    return ScoreArtifacts(
        score_dir=tmp_path,
        manifest={},
        skills=[
            {
                "id": current_skill_id,
                "name": current_skill_id.replace("-", " ").title(),
                "description": f"{current_skill_id} description",
                "inputs": [{"name": "input", "type": "text", "required": True}],
                "outputs": [{"name": current_skill_id, "type": "text"}],
            }
            for current_skill_id in skill_ids
        ],
        graph={"edges": edges},
        lookup={},
    )


def _edge(
    source: str,
    target: str,
    *,
    confidence: float,
) -> dict[str, object]:
    return {
        "type": "can_feed",
        "source": source,
        "target": target,
        "confidence": confidence,
        "method": "llm",
        "evidence": {
            "reasons": [f"{source} feeds {target}"],
            "supporting_fields": {
                "source_outputs": [source],
                "target_inputs": ["input"],
            },
        },
    }


def _plan_signatures(result: dict[str, object]) -> set[tuple[str, ...]]:
    return {
        tuple(step["skill_id"] for step in plan["steps"])
        for plan in result["recommended_plans"]
    }
