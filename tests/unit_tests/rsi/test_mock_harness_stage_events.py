# -*- coding: utf-8 -*-
"""Mock Harness 每用例阶段事件（node.stage）冒烟单测。"""

import pytest

from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineRequest
from jiuwenswarm.agents.harness.common.rsi.mock_harness_provider import (
    _MOCK_EVAL_CASES,
    MockHarnessProvider,
)


@pytest.mark.asyncio
async def test_mock_harness_emits_per_case_stage_events(tmp_path):
    provider = MockHarnessProvider(tmp_path, iteration_delay=0, case_delay=0)
    events = []

    async def on_event(event):
        events.append(event)

    request = HarnessEngineRequest(
        task_id="rsi-stage-task",
        dataset_files=("dataset.json",),
        harness_refs_path="",
        output_dir=str(tmp_path / "run"),
        dataset_id="single_harness_benchmark",
        max_iterations=1,
        search_width=1,
        model_refs={},
    )
    await provider.run(request, on_event=on_event)

    stage_events = [event for event in events if getattr(event, "event_type", None) == "node.stage"]
    assert len(stage_events) == _MOCK_EVAL_CASES * 2
    assert stage_events[0].stage["name"] == f"Case 1/{_MOCK_EVAL_CASES}"
    assert stage_events[-1].stage["name"].endswith("passed")
    assert [event.stage["name"] for event in stage_events[0::2]] == [
        f"Case {index}/{_MOCK_EVAL_CASES}" for index in range(1, _MOCK_EVAL_CASES + 1)
    ]

    node_events = [event for event in events if getattr(event, "event_type", None) == "node"]
    assert node_events[0].node.type == "provisional"
    assert node_events[-1].node.adopted is True
