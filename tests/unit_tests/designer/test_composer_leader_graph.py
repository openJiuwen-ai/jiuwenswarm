# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.designer.composer import (
    compose_execution_graph,
    detect_scenario,
)
from jiuwenswarm.server.runtime.designer.model_tools import (
    DesignerLlmError,
    LLM_NOT_CONFIGURED,
)

_NAPOLEON = (
    "《奶破龙翻越阿尔卑斯山》（致敬达维特）原画精髓。"
    "画面完美保留了原作巴洛克式的厚重油画质感、雪山寒风以及戏剧性的逆光。"
)


def test_chinese_conjunction_is_not_multimodal() -> None:
    assert detect_scenario(_NAPOLEON) == "video"
    assert detect_scenario("雪山寒风以及戏剧性的逆光") == "video"


def test_compose_fails_when_llm_not_configured(monkeypatch) -> None:
    """Compose calls the LLM bluntly; missing credentials raise DesignerLlmError."""

    async def fake_analyze(*_args, **_kwargs):
        raise DesignerLlmError(
            "Chat model is not configured. Configure a model in Settings before using Design.",
            code=LLM_NOT_CONFIGURED,
        )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.script_analysis.analyze_creative_brief",
        fake_analyze,
    )
    with pytest.raises(DesignerLlmError) as excinfo:
        compose_execution_graph(
            project_id="proj_canvas01",
            prompt=_NAPOLEON,
            scenario="multimodal",
        )
    assert excinfo.value.code == LLM_NOT_CONFIGURED
