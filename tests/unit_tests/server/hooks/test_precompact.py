# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for jiuwenswarm.server.hooks.precompact."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.common.hooks_config import HooksConfig, HookMatcher
from jiuwenswarm.server.hooks import precompact
from jiuwenswarm.server.hooks.executor import HookOutcome, HookResult
from jiuwenswarm.server.hooks.precompact import (
    fire_pre_compact_hooks,
    fire_pre_compact_hooks_background,
)


def _make_config(matcher: str) -> HooksConfig:
    return HooksConfig(events={
        "PreCompact": [HookMatcher(
            matcher=matcher,
            hooks=[{"type": "command", "command": "echo ok", "timeout": 5}],
        )]
    })


class RecordingExecutor:
    """Stand-in for HookExecutor that records run_all calls."""

    calls: list[dict] = []
    result = HookResult()

    async def run_all(self, hook_configs, hook_input, session_id=""):
        RecordingExecutor.calls.append({
            "hook_configs": hook_configs,
            "hook_input": hook_input,
            "session_id": session_id,
        })
        return [RecordingExecutor.result]


@pytest.fixture
def recording_executor(monkeypatch):
    RecordingExecutor.calls = []
    RecordingExecutor.result = HookResult()
    monkeypatch.setattr(precompact, "HookExecutor", RecordingExecutor)
    return RecordingExecutor


class TestMatcherRouting:
    @pytest.mark.asyncio
    async def test_manual_matcher_fires_for_manual_trigger(self, monkeypatch, recording_executor):
        monkeypatch.setattr(
            precompact, "load_hooks_config", lambda config_base=None: _make_config("manual"),
        )
        results = await fire_pre_compact_hooks("manual", "s1")
        assert len(recording_executor.calls) == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_manual_matcher_skips_auto_trigger(self, monkeypatch, recording_executor):
        monkeypatch.setattr(
            precompact, "load_hooks_config", lambda config_base=None: _make_config("manual"),
        )
        results = await fire_pre_compact_hooks("auto", "s1")
        assert recording_executor.calls == []
        assert results == []

    @pytest.mark.asyncio
    async def test_star_matcher_fires_for_both_triggers(self, monkeypatch, recording_executor):
        monkeypatch.setattr(
            precompact, "load_hooks_config", lambda config_base=None: _make_config("*"),
        )
        await fire_pre_compact_hooks("manual", "s1")
        await fire_pre_compact_hooks("auto", "s1")
        assert len(recording_executor.calls) == 2


class TestHookInput:
    @pytest.mark.asyncio
    async def test_hook_input_shape(self, monkeypatch, recording_executor):
        monkeypatch.setattr(
            precompact, "load_hooks_config", lambda config_base=None: _make_config("*"),
        )
        await fire_pre_compact_hooks("manual", "session-42")
        hook_input = recording_executor.calls[0]["hook_input"]
        assert hook_input == {
            "event": "PreCompact",
            "trigger": "manual",
            "custom_instructions": "",
            "session_id": "session-42",
        }
        assert recording_executor.calls[0]["session_id"] == "session-42"

    @pytest.mark.asyncio
    async def test_blocking_result_passed_through(self, monkeypatch, recording_executor):
        monkeypatch.setattr(
            precompact, "load_hooks_config", lambda config_base=None: _make_config("*"),
        )
        RecordingExecutor.result = HookResult(outcome=HookOutcome.BLOCKING, error="not now")
        results = await fire_pre_compact_hooks("manual", "s1")
        assert results[0].outcome == "blocking"
        assert results[0].error == "not now"


class TestFastPathAndFailureIsolation:
    @pytest.mark.asyncio
    async def test_no_hooks_returns_empty(self, monkeypatch, recording_executor):
        monkeypatch.setattr(
            precompact, "load_hooks_config", lambda config_base=None: HooksConfig(),
        )
        results = await fire_pre_compact_hooks("manual", "s1")
        assert results == []
        assert recording_executor.calls == []

    @pytest.mark.asyncio
    async def test_config_load_failure_returns_empty(self, monkeypatch, recording_executor):
        def _boom(config_base=None):
            raise RuntimeError("config unavailable")

        monkeypatch.setattr(precompact, "load_hooks_config", _boom)
        results = await fire_pre_compact_hooks("manual", "s1")
        assert results == []


class TestBackgroundFire:
    @pytest.mark.asyncio
    async def test_background_task_runs_and_cleans_up(self, monkeypatch, recording_executor):
        monkeypatch.setattr(
            precompact, "load_hooks_config", lambda config_base=None: _make_config("*"),
        )
        fire_pre_compact_hooks_background("auto", "s1")
        assert len(precompact._background_tasks) == 1
        await asyncio.gather(*precompact._background_tasks)
        await asyncio.sleep(0)
        assert len(precompact._background_tasks) == 0
        assert recording_executor.calls[0]["hook_input"]["trigger"] == "auto"
