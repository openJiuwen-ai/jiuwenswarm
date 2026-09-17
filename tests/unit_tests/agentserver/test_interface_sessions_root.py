# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for request-scoped session directory resolution."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


def test_append_history_record_resolves_sessions_root_from_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_root = tmp_path / "agent_default_sessions"
    correct_root = tmp_path / "agent_agentteam_sessions"
    wrong_root.mkdir(parents=True)
    correct_root.mkdir(parents=True)

    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda: wrong_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.handlers._shared.resolve_tenant_sessions_dir",
        lambda workspace_key=None, *, service_id=None, agent_id=None: (
            correct_root if agent_id == "agentteam" else wrong_root
        ),
    )

    captured: list[dict[str, object]] = []

    def _capture_append(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface.append_history_record",
        _capture_append,
    )

    request = SimpleNamespace(
        channel_id="officeclaw",
        agent_id="agentteam",
        service_id="default",
        workspace_key="default",
    )
    swarm = JiuWenSwarm()
    swarm._append_history_record(
        request=request,
        session_id="officeclaw_test_sid",
        request_id="req-scope",
        channel_id="officeclaw",
        role="user",
        content="hello",
        timestamp=time.time(),
        mode="team",
    )

    assert len(captured) == 1
    assert captured[0]["sessions_root"] == correct_root
