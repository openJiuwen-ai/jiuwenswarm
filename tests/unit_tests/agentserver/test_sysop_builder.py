"""Tests for cross-platform system-operation policy construction."""

from __future__ import annotations

from jiuwenswarm.server.runtime.agent_adapter import sysop_builder


def test_build_process_policy_skips_process_policy_without_posix_identity(
    monkeypatch,
) -> None:
    monkeypatch.delattr(sysop_builder.os, "geteuid", raising=False)

    assert sysop_builder.build_process_policy() == {}
