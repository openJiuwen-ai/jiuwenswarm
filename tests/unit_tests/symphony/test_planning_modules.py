from jiuwenswarm.symphony.orchestration.planning.models import (
    ArtifactRef,
    PlanStep,
    SearchState,
)
from jiuwenswarm.symphony.orchestration.planning.plan_builder import (
    plan_stages,
    state_to_plan,
)


def test_plan_stages_parallelizes_roots():
    steps = [_step("review"), _step("draft"), _step("publish")]
    edges = [
        {"source_id": "draft", "target_id": "publish"},
        {"source_id": "review", "target_id": "publish"},
    ]

    stages = plan_stages(steps, edges)

    assert [[skill["skill_id"] for skill in stage["skills"]] for stage in stages] == [
        ["draft", "review"],
        ["publish"],
    ]


def test_state_to_plan_projects_inputs_outputs_and_edge_metadata():
    skills = {
        "draft": {
            "id": "draft",
            "name": "Draft",
            "inputs": [{"name": "brief", "type": "text", "required": True}],
            "outputs": [{"name": "draft", "type": "markdown"}],
        },
        "review": {
            "id": "review",
            "name": "Review",
            "inputs": [{"name": "draft", "type": "markdown", "required": True}],
            "outputs": [{"name": "review", "type": "markdown"}],
        },
    }
    edges = [
        {
            "source": "skill:draft",
            "target": "skill:review",
            "confidence": 0.92,
            "method": "llm",
            "evidence": {
                "reasons": ["draft feeds review"],
                "supporting_fields": {
                    "source_outputs": ["draft"],
                    "target_inputs": ["draft"],
                    "port_mappings": [
                        {"source_output": "draft", "target_input": "draft"}
                    ],
                },
            },
        }
    ]

    plan = state_to_plan(
        state=SearchState(skill_ids=("draft", "review"), edges=(0,)),
        seed_skill_ids=("review",),
        skill_by_id=skills,
        can_feed_edges=edges,
    )

    assert plan.status == "needs_input"
    assert [step.skill_id for step in plan.steps] == ["draft", "review"]
    assert plan.missing_inputs == [
        {"name": "brief", "type": "text", "skill_id": "draft"}
    ]
    assert plan.consumed_user_artifacts == 0
    assert plan.produced_artifacts == [
        ArtifactRef(name="draft", type="markdown", source="skill_output"),
        ArtifactRef(name="review", type="markdown", source="skill_output"),
    ]
    assert plan.can_feed_edges[0]["source_id"] == "draft"
    assert plan.can_feed_edges[0]["target_id"] == "review"


def _step(skill_id: str) -> PlanStep:
    return PlanStep(
        skill_id=skill_id,
        name=skill_id.title(),
        inputs=[],
        outputs=[],
    )
