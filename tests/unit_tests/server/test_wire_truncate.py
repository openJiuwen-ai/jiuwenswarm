from jiuwenswarm.server.wire_truncate import (
    _fit_workflow_detail_to_budget,
    _workflow_list_summary_item,
)


def test_workflow_list_summary_preserves_budget_mapping():
    budget = {"spent": 2, "total": 5, "remaining": 3}

    summary = _workflow_list_summary_item(
        {"id": "workflow-1", "status": "running", "budget": budget}
    )

    assert summary["budget"] == budget
    assert isinstance(summary["budget"], dict)


def test_fit_workflow_detail_keeps_waiting_human_prompt_under_tiny_budget():
    """Final shrink floor must keep HITL nodes (prompt may be truncated)."""
    prompt = "请确认下一步：" + ("详" * 4000)
    item = {
        "id": "wf-hitl",
        "name": "hitl-run",
        "status": "paused",
        "summary": "x" * 200,
        "phases": [
            {
                "id": "p1",
                "name": "phase-1",
                "status": "paused",
                "agent_count": 1,
                "completed_agent_count": 0,
                "agents": [
                    {
                        "id": "human-1",
                        "name": "Human",
                        "status": "waiting_for_human",
                        "kind": "human",
                        "human_prompt": prompt,
                    }
                ],
            }
        ],
        "logs": ["noise"] * 20,
    }
    preserved = {"human-1": prompt}
    # Tiny budget forces carve-out + prompt shrink; must not drop to bare metadata.
    fitted = _fit_workflow_detail_to_budget(
        item, budget=900, preserved_prompts=preserved
    )
    assert fitted.get("truncated") is True
    phases = fitted.get("phases")
    assert isinstance(phases, list) and phases
    agents = phases[0].get("agents")
    assert isinstance(agents, list) and agents
    human = agents[0]
    assert human.get("status") == "waiting_for_human"
    assert human.get("kind") == "human"
    assert isinstance(human.get("human_prompt"), str)
    assert human["human_prompt"]
    assert "请确认下一步" in human["human_prompt"] or human["human_prompt"].endswith(
        " [truncated]"
    )
