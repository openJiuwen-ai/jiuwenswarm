"""Host distill port injection (HOST_DISTILL_INJECTION scope A: T1 / T2)."""

from __future__ import annotations

import asyncio

import pytest
from openjiuwen.harness.personal_context.distill import SqliteImCorpus

import jiuwenswarm.server.personal_context.host_api as host_module
from jiuwenswarm.server.personal_context.distill_ports import (
    build_distill_corpus,
)
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


def _pin_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "get_default_models", list)
    monkeypatch.setattr(
        host_module, "_global_embedding_values", lambda: (None, None, None)
    )


def test_build_distill_corpus_returns_sqlite_im_corpus(tmp_path) -> None:
    corpus = build_distill_corpus(tmp_path)
    assert isinstance(corpus, SqliteImCorpus)


def test_host_injects_distill_ports_on_construction(tmp_path) -> None:
    """T1: Host construction injects corpus and runner while Core is stopped."""

    host = PersonalContextHostAPI(home=tmp_path)
    assert host.distill_ports_wired() is True
    core = host._personal_context  # pylint: disable=protected-access
    assert isinstance(core._distill_corpus, SqliteImCorpus)  # pylint: disable=protected-access
    assert core._distill_runner is not None  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_host_keeps_distill_ports_after_configure(tmp_path, monkeypatch) -> None:
    """T1: configure / apply path still leaves ports wired."""

    _pin_models(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": False,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "fetch_services": [],
                "distill": {"enabled": False},
            }
        )
        assert host.distill_ports_wired() is True
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_distill_scheduler_skips_when_disabled(tmp_path, monkeypatch) -> None:
    """T2: distill.enabled=false → scheduler must not call the runner."""

    _pin_models(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    calls: list[object] = []

    async def mock_runner(
        home: str,
        *,
        window_end_ms: int,
        learning_since_ms: int | None = None,
        max_messages: int = 800,
        force_full_window: bool = False,
    ):
        del home, window_end_ms, learning_since_ms, max_messages, force_full_window
        calls.append("ran")
        raise AssertionError("runner must not be called when distill is disabled")

    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": True,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "fetch_services": [],
                "distill": {
                    "enabled": False,
                    "interval_seconds": 1,
                    "message_threshold": 1,
                    "poll_seconds": 0.05,
                },
            }
        )
        core = host._personal_context  # pylint: disable=protected-access
        await core.deactivate_runtime(timeout_seconds=5.0)
        core.set_distill_runner(mock_runner)
        await core.activate_runtime()
        await asyncio.sleep(0.2)
        assert calls == []
    finally:
        await host.stop()
