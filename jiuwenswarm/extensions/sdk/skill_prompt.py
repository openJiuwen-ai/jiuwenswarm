"""Connect an extension's Skill prompt to the host retrieval rail."""

from collections.abc import Callable
from typing import Any


def attach_skill_prompt_provider(
    agent: Any,
    provider: Callable[[], tuple[frozenset[str] | None, str]],
) -> None:
    from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import (
        SkillRetrievalPromptRail,
    )

    for rail in agent.find_rails_by_type((SkillRetrievalPromptRail,)):
        rail.set_skill_prompt_provider(provider)
