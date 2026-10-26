# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for create_subagent RequestSummaryRail attachment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.perf.config import PerfSummaryConfig
from jiuwenswarm.perf import subagent_hooks


class RequestSummaryRail:
    """Stand-in matching the real class name for idempotency checks."""

    def __init__(self, *, record_only: bool = False) -> None:
        self.record_only = record_only


class _FakeSubagent:
    def __init__(self, *, rails: list | None = None) -> None:
        self._rails = list(rails or [])
        self.card = SimpleNamespace(id="sub-1")
        self.add_rail_calls: list[object] = []

    def configured_rails(self) -> list:
        return list(self._rails)

    def add_rail(self, rail: object) -> None:
        self.add_rail_calls.append(rail)
        self._rails.append(rail)


@pytest.fixture(autouse=True)
def _stub_request_summary_rail(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "jiuwenswarm.perf.request_summary_rail.RequestSummaryRail",
        RequestSummaryRail,
    )


def test_attach_request_summary_rail_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subagent_hooks,
        "get_perf_summary_config",
        lambda: PerfSummaryConfig(enabled=True),
    )
    sub = _FakeSubagent()
    assert subagent_hooks.attach_request_summary_rail(sub) is True
    assert len(sub.add_rail_calls) == 1
    assert getattr(sub.add_rail_calls[0], "record_only", None) is True


def test_attach_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subagent_hooks,
        "get_perf_summary_config",
        lambda: PerfSummaryConfig(enabled=False),
    )
    sub = _FakeSubagent()
    assert subagent_hooks.attach_request_summary_rail(sub) is False
    assert sub.add_rail_calls == []


def test_attach_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subagent_hooks,
        "get_perf_summary_config",
        lambda: PerfSummaryConfig(enabled=True),
    )
    existing = RequestSummaryRail(record_only=True)
    sub = _FakeSubagent(rails=[existing])
    assert subagent_hooks.attach_request_summary_rail(sub) is False
    assert sub.add_rail_calls == []


def test_apply_create_subagent_perf_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagent_hooks, "_PATCH_APPLIED", False)
    monkeypatch.setattr(
        subagent_hooks,
        "get_perf_summary_config",
        lambda: PerfSummaryConfig(enabled=True),
    )

    class _DeepAgent:
        perf_summary_subagent_patch_applied = False

        def create_subagent(self, subagent_type: str, subsession_id: str):
            return _FakeSubagent()

    fake_mod = SimpleNamespace(DeepAgent=_DeepAgent)
    monkeypatch.setitem(
        __import__("sys").modules,
        "openjiuwen.harness.deep_agent",
        fake_mod,
    )

    subagent_hooks.apply_create_subagent_perf_patch()
    assert _DeepAgent.perf_summary_subagent_patch_applied is True

    parent = _DeepAgent()
    sub = parent.create_subagent("general-purpose", "sess_sub")
    assert len(sub.add_rail_calls) == 1
    assert getattr(sub.add_rail_calls[0], "record_only", None) is True

    # Second apply is a no-op.
    subagent_hooks.apply_create_subagent_perf_patch()


def test_install_perf_hooks_applies_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    applied = {"ok": False}
    # install_hooks binds apply_create_subagent_perf_patch at import time;
    # patch the name used by that module, not subagent_hooks.
    import jiuwenswarm.perf.install_hooks as install_hooks_mod

    monkeypatch.setattr(
        install_hooks_mod,
        "apply_create_subagent_perf_patch",
        lambda: applied.__setitem__("ok", True),
    )
    install_hooks_mod.install_perf_hooks()
    assert applied["ok"] is True
