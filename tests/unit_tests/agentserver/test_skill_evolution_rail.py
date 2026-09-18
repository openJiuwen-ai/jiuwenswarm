# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JiuClawSkillEvolutionRail bootstrap exclusions."""

# pylint: disable=protected-access

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.harness.rails import SkillEvolutionRail

from jiuwenclaw.agentserver.deep_agent.skill_evolution_rail import JiuClawSkillEvolutionRail
from jiuwenclaw.agentserver.llm_usage import bind_aux_llm_usage_sink, reset_aux_llm_usage_sink


@pytest.fixture
def evolution_rail(monkeypatch: pytest.MonkeyPatch) -> JiuClawSkillEvolutionRail:
    """Construct rail with parent init stubbed; attach store required by super()."""
    monkeypatch.setattr(
        SkillEvolutionRail,
        "__init__",
        lambda self, *args, **kwargs: None,
    )
    rail = JiuClawSkillEvolutionRail(
        skills_dir="/tmp/skills",
        llm=MagicMock(),
        model="test-model",
    )
    rail._evolution_store = MagicMock()
    rail._evolution_store.resolve_skill_dir.return_value = None
    return rail


@pytest.mark.unit
def test_eligible_skill_names_excludes_bootstrap_builtin(evolution_rail, monkeypatch):
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.deep_agent.skill_evolution_rail.is_bootstrap_builtin_skill",
        lambda name: name == "xlsx",
    )
    assert evolution_rail._eligible_skill_names(["xlsx", "custom-skill"]) == ["custom-skill"]


@pytest.mark.unit
def test_generate_and_emit_experience_skips_bootstrap_builtin(evolution_rail, monkeypatch):
    monkeypatch.setattr(
        "jiuwenclaw.agentserver.deep_agent.skill_evolution_rail.is_bootstrap_builtin_skill",
        lambda name: name == "xlsx",
    )

    async def _unexpected(*_args, **_kwargs):
        raise AssertionError("super().generate_and_emit_experience should not run for builtin skills")

    monkeypatch.setattr(
        SkillEvolutionRail,
        "generate_and_emit_experience",
        _unexpected,
    )

    result = asyncio.run(evolution_rail.generate_and_emit_experience("xlsx", [], []))
    assert result is False


@pytest.mark.unit
def test_evolution_llm_reports_usage_to_active_request_sink(monkeypatch):
    """Direct evolution LLM calls must join request-scoped token accounting."""
    captured: dict[str, object] = {}

    def _capture_parent_init(_self, *args, **kwargs):
        captured["llm"] = kwargs["llm"]

    monkeypatch.setattr(SkillEvolutionRail, "__init__", _capture_parent_init)
    response = SimpleNamespace(
        content="ok",
        usage_metadata={"input_tokens": 17, "output_tokens": 5, "total_tokens": 22},
    )
    delegate = MagicMock()
    delegate.invoke = AsyncMock(return_value=response)
    rail = JiuClawSkillEvolutionRail(
        skills_dir="/tmp/skills",
        llm=delegate,
        model="test-model",
    )
    reported: list[object] = []

    async def _sink(usage_metadata):
        reported.append(usage_metadata)

    async def _run():
        token = bind_aux_llm_usage_sink(_sink)
        try:
            wrapped = captured["llm"]
            assert wrapped is not delegate
            assert await wrapped.invoke(model="test-model", messages=[]) is response
        finally:
            reset_aux_llm_usage_sink(token)

    asyncio.run(_run())

    assert reported == [response.usage_metadata]
    assert rail is not None


@pytest.mark.unit
def test_evolution_llm_hot_update_keeps_usage_reporting(monkeypatch):
    """Runtime model reloads must not replace the accounting wrapper with the raw model."""
    updated: dict[str, object] = {}
    monkeypatch.setattr(SkillEvolutionRail, "__init__", lambda *_args, **_kwargs: None)

    def _capture_update(_self, llm, model):
        updated["llm"] = llm
        updated["model"] = model

    monkeypatch.setattr(SkillEvolutionRail, "update_llm", _capture_update)
    rail = JiuClawSkillEvolutionRail(
        skills_dir="/tmp/skills",
        llm=MagicMock(),
        model="old-model",
    )
    replacement = MagicMock()

    rail.update_llm(replacement, "new-model")

    assert updated["llm"] is not replacement
    assert updated["model"] == "new-model"


@pytest.mark.unit
def test_late_evolution_usage_uses_authenticated_session_callback(monkeypatch):
    """Usage completing after chat.done must still reach the originating ledger."""
    from jiuwenclaw.agentserver import llm_usage as usage_mod

    requests: list[tuple[str, dict]] = []

    class _Response:
        @staticmethod
        def raise_for_status():
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json):
            requests.append((url, json))
            return _Response()

    monkeypatch.setattr(usage_mod.httpx, "AsyncClient", lambda **_kwargs: _Client())
    reporter = usage_mod.build_late_llm_usage_reporter(
        {
            "office_claw_mcp": {
                "env": {
                    "OFFICE_CLAW_API_URL": "http://127.0.0.1:3004/",
                    "OFFICE_CLAW_INVOCATION_ID": "invocation-1",
                    "OFFICE_CLAW_CALLBACK_TOKEN": "secret-token",
                }
            }
        },
        "officeclaw-session-1",
    )
    assert reporter is not None

    asyncio.run(
        reporter(
            {
                "input_tokens": 111,
                "output_tokens": 9,
                "total_tokens": 120,
                "cache_tokens": 7,
                "total_cost": 0.0012,
            }
        )
    )

    assert len(requests) == 1
    url, payload = requests[0]
    assert url == "http://127.0.0.1:3004/api/callbacks/report-llm-usage"
    assert payload["invocationId"] == "invocation-1"
    assert payload["callbackToken"] == "secret-token"
    assert payload["sessionId"] == "officeclaw-session-1"
    assert payload["usage"] == {
        "inputTokens": 111,
        "outputTokens": 9,
        "cacheReadTokens": 7,
        "costUsd": 0.0012,
    }
    assert payload["usageEventId"]
    assert payload["timestamp"] > 0
