# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Script analysis timeout reporting."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.server.runtime.designer import model_tools, script_analysis
from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError


@pytest.mark.asyncio
async def test_script_analysis_timeout_reports_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        raise asyncio.TimeoutError

    monkeypatch.setattr(model_tools, "call_model_tool", fake_model_call)

    with pytest.raises(DesignerLlmError) as excinfo:
        await script_analysis.analyze_creative_brief("A short film.", timeout_sec=5)

    message = str(excinfo.value)
    assert "during script analysis (no reply arrived in time)" in message
    assert "after 120s" not in message


@pytest.mark.asyncio
async def test_script_analysis_timeout_reports_reinforce_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        calls.append(True)
        if len(calls) == 1:
            return {"ok": True, "text": "not a json object"}
        raise asyncio.TimeoutError

    monkeypatch.setattr(model_tools, "call_model_tool", fake_model_call)

    with pytest.raises(DesignerLlmError) as excinfo:
        await script_analysis.analyze_creative_brief("A short film.", timeout_sec=5)

    assert len(calls) == 2
    assert "the first reply was not usable JSON and the retry did not finish in time" in str(excinfo.value)
