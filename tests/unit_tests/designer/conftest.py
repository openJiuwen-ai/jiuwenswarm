# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer unit tests execute graphs without a complete image/video Settings block."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _skip_media_config_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_media_models",
        lambda *, image=False, video=False: None,
    )


_DIRECTOR_LLM_PHASES = (
    "author_creative_brief",
    "review_brief",
    "author_storyboard",
    "review_storyboard",
    "design_execution_graph",
    "plan",
    "validate_plan",
    "review_storyboard_once",
    "finalize",
    "review",
    "assign_dual_raters",
)


@pytest.fixture()
def stub_director_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Director's chat-model phases with empty acknowledgements.

    Executor scheduling tests run full waves; the Director fails closed without a
    configured chat model, so its LLM phases are answered with no-op results.
    """
    from jiuwenswarm.server.runtime.designer.orchestration import Director

    async def _ack(self: Director, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    for name in _DIRECTOR_LLM_PHASES:
        monkeypatch.setattr(Director, name, _ack)
