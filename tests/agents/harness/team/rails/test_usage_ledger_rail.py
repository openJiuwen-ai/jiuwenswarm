# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""UsageLedgerRail charges every model call, and marks a call without usage as unmeasured."""

import asyncio
from types import SimpleNamespace

from jiuwenswarm.agents.harness.team.rails.usage_ledger_rail import UsageLedgerRail


def _ctx(response):
    return SimpleNamespace(inputs=SimpleNamespace(response=response))


def test_usage_on_the_response_is_recorded_with_its_stage():
    rows = []
    rail = UsageLedgerRail(rows.append, stage="write", step="draft")
    usage = SimpleNamespace(model_name="m", input_tokens=10, output_tokens=5, total_tokens=15)
    asyncio.run(rail.after_model_call(_ctx(SimpleNamespace(usage_metadata=usage))))
    assert rows == [{"stage": "write", "step": "draft", "call": 1, "model": "m", "input_tokens": 10,
                     "output_tokens": 5, "total_tokens": 15, "measured": True}]


def test_a_response_without_usage_is_recorded_not_skipped():
    rows = []
    rail = UsageLedgerRail(rows.append, stage="write")
    asyncio.run(rail.after_model_call(_ctx(SimpleNamespace())))
    asyncio.run(rail.after_model_call(_ctx(None)))
    assert [r["measured"] for r in rows] == [False, False] and rail.calls == 2
