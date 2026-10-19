import pytest

from jiuwenswarm.symphony.llm import create_llm_client
from jiuwenswarm.symphony.orchestration.artifacts import ScoreArtifacts
from jiuwenswarm.symphony.orchestration.planning.fast import FastOneShotPlanner


def test_create_llm_client_requires_config():
    with pytest.raises(ValueError, match="requires LLMConfig"):
        create_llm_client(None)


@pytest.mark.asyncio
async def test_fast_planner_requires_llm_config_or_client(tmp_path):
    planner = FastOneShotPlanner(
        ScoreArtifacts(
            score_dir=tmp_path,
            manifest={},
            skills=[],
            graph={"edges": []},
            lookup={},
        ),
        llm_config=None,
        llm_client=None,
        min_edge_confidence=0.7,
    )

    with pytest.raises(ValueError, match="requires llm_config or llm_client"):
        await planner.plan("test")
