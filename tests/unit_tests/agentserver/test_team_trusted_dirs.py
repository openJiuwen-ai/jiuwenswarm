# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-request trusted dirs reach team members' rails, as they do a single agent."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.member_rails import _build_runtime_prompt_rail
from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _request_trusted_dirs


def _context(**overrides) -> SwarmBuildContext:
    base = {
        "session_id": "sess-trusted",
        "channel": "tui",
        "mode": "team",
        "project_dir": "/work/project",
        "trusted_dirs": ["/work/project", "/data/shared"],
    }
    base.update(overrides)
    return SwarmBuildContext(**base)


def test_request_trusted_dirs_reads_and_trims_params():
    request = SimpleNamespace(params={"trusted_dirs": [" /work/project ", "/data/shared"]})

    assert _request_trusted_dirs(request) == ["/work/project", "/data/shared"]


def test_request_trusted_dirs_ignores_blank_and_non_string_entries():
    request = SimpleNamespace(params={"trusted_dirs": ["/work", "  ", 7, None]})

    assert _request_trusted_dirs(request) == ["/work"]


def test_request_trusted_dirs_without_params_returns_empty():
    assert _request_trusted_dirs(SimpleNamespace(params=None)) == []
    assert _request_trusted_dirs(SimpleNamespace(params={})) == []


def test_build_context_seed_round_trips_trusted_dirs():
    seed = _context().to_seed()
    restored = SwarmBuildContext.from_seed(seed, config=None, trajectory_span_processor=None)

    assert seed["trusted_dirs"] == ["/work/project", "/data/shared"]
    assert restored.trusted_dirs == ["/work/project", "/data/shared"]


def test_build_context_seed_keeps_none_when_unset():
    seed = _context(trusted_dirs=None).to_seed()
    restored = SwarmBuildContext.from_seed(seed, config=None, trajectory_span_processor=None)

    assert seed["trusted_dirs"] is None
    assert restored.trusted_dirs is None


def test_member_runtime_prompt_rail_receives_trusted_dirs():
    rail = _build_runtime_prompt_rail({}, _context())

    assert rail._trusted_dirs == ["/work/project", "/data/shared"]


def test_member_runtime_prompt_rail_without_trusted_dirs():
    rail = _build_runtime_prompt_rail({}, _context(trusted_dirs=None))

    assert rail._trusted_dirs is None
