# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for GovernanceReviewRail's post-call scan for unsupported novelty claims."""

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs

from jiuwenswarm.agents.harness.team.rails.governance_review_rail import GovernanceReviewRail


async def _signal_count(text: str) -> int:
    class Resp:
        content = text

    rail = GovernanceReviewRail()
    await rail.after_model_call(
        AgentCallbackContext(agent=None, inputs=ModelCallInputs(response=Resp()))
    )
    return rail._violation_count


@pytest.mark.asyncio
@pytest.mark.parametrize("cited", [
    r"Ours is the first method to do this \cite{smith2020}.",
    r"Ours is the first method to do this~\citep{smith2020}.",
    r"As \citet{smith2020} note, ours is the first method to do this.",
    "Ours is the first method to do this [3].",
])
async def test_cited_novelty_claim_is_not_flagged(cited):
    assert await _signal_count(cited) == 0


@pytest.mark.asyncio
async def test_uncited_novelty_claim_is_flagged():
    assert await _signal_count("Ours is the first method to do this.") == 1
