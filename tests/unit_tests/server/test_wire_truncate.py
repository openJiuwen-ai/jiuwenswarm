from jiuwenswarm.server.wire_truncate import _workflow_list_summary_item


def test_workflow_list_summary_preserves_budget_mapping():
    budget = {"spent": 2, "total": 5, "remaining": 3}

    summary = _workflow_list_summary_item(
        {"id": "workflow-1", "status": "running", "budget": budget}
    )

    assert summary["budget"] == budget
    assert isinstance(summary["budget"], dict)
