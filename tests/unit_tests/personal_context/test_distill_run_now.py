"""Host process-local distill run_now (HOST_DISTILL_INJECTION scope A: T3 / T4 / T5)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from openjiuwen.harness.personal_context import PersonalContext
from openjiuwen.harness.personal_context.distill import (
    DistillRunResult,
    FixtureCorpus,
    default_fixture_messages,
    try_claim_distill_lease,
)
from openjiuwen.harness.personal_context.distill.store import get_distill_lease

import jiuwenswarm.server.personal_context.host_api as host_module
from jiuwenswarm.server.personal_context.distill_ports import build_distill_runner
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI

_DISTILL_ENABLED_CONFIG = {
    "collection_enabled": False,
    "agent_use_enabled": False,
    "strategy_profile": "rules",
    "model_index": 0,
    "fetch_services": [],
    "distill": {"enabled": True},
}


async def _wait_manual_distill(host: PersonalContextHostAPI) -> None:
    task = host._distill_manual_task  # pylint: disable=protected-access
    assert task is not None
    await task


def _pin_empty_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "get_default_models", list)
    monkeypatch.setattr(
        host_module, "_global_embedding_values", lambda: (None, None, None)
    )


def _pin_one_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        host_module,
        "get_default_models",
        lambda: [
            {
                "model_client_config": {
                    "client_provider": "OpenAI",
                    "api_key": "test-key",
                    "api_base": "https://example.invalid/v1",
                    "model_name": "test-model",
                },
                "model_config_obj": {"temperature": 0.2},
            }
        ],
    )
    monkeypatch.setattr(
        host_module, "_global_embedding_values", lambda: (None, None, None)
    )


class _FakeLlm:
    """Deterministic LlmPort stand-in for disk-product distill tests."""

    async def complete(self, *, system: str, user: str) -> str:
        del user
        if "Persona 分析 Prompt" in system:
            return '口头禅：["先看现有实现"]\n'
        if "Persona 生成模板" in system:
            return "# 本人 — Persona\n\n## Layer 2：表达风格\n- 先看现有实现\n"
        if "Work Skill 分析 Prompt" in system:
            return "负责领域：前端\n"
        if "Work Skill 生成模板" in system:
            return "# 本人 — Work Skill\n\n## 职责范围\n- 前端相关改动\n"
        return "fallback\n"


@pytest.mark.asyncio
async def test_run_distill_now_invokes_injected_runner(tmp_path, monkeypatch) -> None:
    """T3: enabled=true → process-local run_distill_now calls the injected runner."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    calls: list[dict[str, object]] = []

    async def mock_runner(
        home: str,
        *,
        window_end_ms: int,
        learning_since_ms: int | None = None,
        max_messages: int = 800,
        force_full_window: bool = False,
    ) -> DistillRunResult:
        calls.append(
            {
                "home": home,
                "window_end_ms": window_end_ms,
                "learning_since_ms": learning_since_ms,
                "max_messages": max_messages,
                "force_full_window": force_full_window,
            }
        )
        return DistillRunResult(
            job_id="job-mock",
            status="success",
            window_start_ms=0,
            window_end_ms=window_end_ms,
            message_count=0,
            sampled=False,
        )

    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": False,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "model_index": 0,
                "fetch_services": [],
                "distill": {"enabled": True, "message_threshold": 1},
            }
        )
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(mock_runner)
        accepted = await host.run_distill_now()
        assert accepted == {"accepted": True}
        await _wait_manual_distill(host)
        assert len(calls) == 1
        assert calls[0]["home"] == str(host._home)  # pylint: disable=protected-access
        assert get_distill_lease(str(host._home)) is None  # pylint: disable=protected-access
        status = await host.get_distill_status()
        assert status["last_error"] is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_writes_profile_version(tmp_path, monkeypatch) -> None:
    """T4: fixture corpus + fake LLM → versions/ + current.json on disk."""

    _pin_one_model(monkeypatch)
    home = tmp_path / "pc"
    host = PersonalContextHostAPI(home=home)
    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": False,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "model_index": 0,
                "fetch_services": [],
                "distill": {"enabled": True, "max_messages": 800},
            }
        )
        corpus = FixtureCorpus(default_fixture_messages())
        runner = build_distill_runner(
            home=home,
            corpus=corpus,
            resolve_llm=lambda: _FakeLlm(),
        )
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_corpus(corpus)
        core.set_distill_runner(runner)

        assert await host.run_distill_now() == {"accepted": True}
        await _wait_manual_distill(host)

        current = json.loads((home / "im" / "profiles" / "current.json").read_text(
            encoding="utf-8"
        ))
        job_id = current["job_id"]
        version_dir = home / "im" / "profiles" / "versions" / job_id
        assert (version_dir / "persona.md").is_file()
        assert (version_dir / "work.md").is_file()
        assert (version_dir / "meta.json").is_file()

        status = await host.get_distill_status()
        assert status["last_error"] is None
        assert status["last_success_job_id"] == job_id
        assert status["lease"] is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_rejects_when_disabled(tmp_path, monkeypatch) -> None:
    """T5: enabled=false → explicit failure, no success payload."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": False,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "model_index": 0,
                "fetch_services": [],
                "distill": {"enabled": False},
            }
        )
        with pytest.raises(PersonalContext.Error, match="distill_disabled"):
            await host.run_distill_now()
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_rejects_when_no_model(tmp_path, monkeypatch) -> None:
    """T5: no model_index → explicit model-unavailable failure."""

    _pin_empty_models(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": False,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "fetch_services": [],
                "distill": {"enabled": True},
            }
        )
        with pytest.raises(PersonalContext.Error, match="distill model unavailable"):
            await host.run_distill_now()
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_rejects_when_not_wired(tmp_path, monkeypatch) -> None:
    """T5: cleared ports → distill_not_wired."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    await host.start()
    try:
        await host.configure(
            {
                "collection_enabled": False,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "model_index": 0,
                "fetch_services": [],
                "distill": {"enabled": True},
            }
        )
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_corpus(None)
        core.set_distill_runner(None)
        with pytest.raises(PersonalContext.Error, match="distill_not_wired"):
            await host.run_distill_now()
    finally:
        await host.stop()


def _blocking_runner(started: asyncio.Event, release: asyncio.Event):
    async def runner(
        home: str,
        *,
        window_end_ms: int,
        learning_since_ms: int | None = None,
        max_messages: int = 800,
        force_full_window: bool = False,
    ) -> DistillRunResult:
        del home, learning_since_ms, max_messages, force_full_window
        started.set()
        await release.wait()
        return DistillRunResult(
            job_id="job-blocking",
            status="success",
            window_start_ms=0,
            window_end_ms=window_end_ms,
            message_count=1,
            sampled=False,
        )

    return runner


@pytest.mark.asyncio
async def test_run_distill_now_does_not_block_host_and_holds_lease(
    tmp_path, monkeypatch
) -> None:
    """G1/G2: RPC returns while runner is in flight; lease held until it ends."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    started = asyncio.Event()
    release = asyncio.Event()
    await host.start()
    try:
        await host.configure(dict(_DISTILL_ENABLED_CONFIG))
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(_blocking_runner(started, release))

        assert await host.run_distill_now() == {"accepted": True}
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # Host control plane stays responsive while the job runs.
        status = await asyncio.wait_for(host.get_distill_status(), timeout=1.0)
        assert status["lease"] is not None

        release.set()
        await _wait_manual_distill(host)
        assert get_distill_lease(str(host._home)) is None  # pylint: disable=protected-access
    finally:
        release.set()
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_rejects_when_lease_held(tmp_path, monkeypatch) -> None:
    """G3: scheduler (or another manual run) holds the lease → distill_busy."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    calls: list[int] = []

    async def mock_runner(home: str, **kwargs: object) -> DistillRunResult:
        del home, kwargs
        calls.append(1)
        return DistillRunResult(
            job_id="never",
            status="success",
            window_start_ms=0,
            window_end_ms=0,
            message_count=0,
            sampled=False,
        )

    await host.start()
    try:
        await host.configure(dict(_DISTILL_ENABLED_CONFIG))
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(mock_runner)
        home = str(host._home)  # pylint: disable=protected-access
        token = try_claim_distill_lease(
            home, now_ms=int(time.time() * 1000), lease_ms=3_600_000
        )
        assert token is not None

        with pytest.raises(PersonalContext.Error, match="distill_busy"):
            await host.run_distill_now()
        assert calls == []
        assert host._distill_manual_task is None  # pylint: disable=protected-access
        # Rejection is not a run error.
        status = await host.get_distill_status()
        assert status["last_error"] is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_releases_lease_when_create_task_fails(
    tmp_path, monkeypatch
) -> None:
    """§2.1: create_task failure after claim must return the lease."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")

    async def unused_runner(home: str, **kwargs: object) -> DistillRunResult:
        del home, kwargs
        raise AssertionError("runner must not start")

    await host.start()
    try:
        await host.configure(dict(_DISTILL_ENABLED_CONFIG))
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(unused_runner)
        original_create_task = host_module.asyncio.create_task

        def _fail_create_task(coro, *args, **kwargs):
            del args, kwargs
            coro.close()
            raise RuntimeError("no loop")

        monkeypatch.setattr(host_module.asyncio, "create_task", _fail_create_task)
        try:
            with pytest.raises(RuntimeError, match="no loop"):
                await host.run_distill_now()
        finally:
            monkeypatch.setattr(host_module.asyncio, "create_task", original_create_task)
        assert host._distill_manual_task is None  # pylint: disable=protected-access
        assert get_distill_lease(str(host._home)) is None  # pylint: disable=protected-access
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_rejects_duplicate_manual_run(
    tmp_path, monkeypatch
) -> None:
    """G3: second manual request while the first is still running → distill_busy."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    started = asyncio.Event()
    release = asyncio.Event()
    await host.start()
    try:
        await host.configure(dict(_DISTILL_ENABLED_CONFIG))
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(_blocking_runner(started, release))

        assert await host.run_distill_now() == {"accepted": True}
        await asyncio.wait_for(started.wait(), timeout=1.0)
        with pytest.raises(PersonalContext.Error, match="distill_busy"):
            await host.run_distill_now()
        release.set()
        await _wait_manual_distill(host)
    finally:
        release.set()
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_records_failed_result(tmp_path, monkeypatch) -> None:
    """G5: runner returns status=failed → last_error carries result.error."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")

    async def failing_runner(home: str, **kwargs: object) -> DistillRunResult:
        del home
        return DistillRunResult(
            job_id="job-failed",
            status="failed",
            window_start_ms=0,
            window_end_ms=int(kwargs["window_end_ms"]),
            message_count=0,
            sampled=False,
            error="llm returned empty content",
        )

    await host.start()
    try:
        await host.configure(dict(_DISTILL_ENABLED_CONFIG))
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(failing_runner)
        assert await host.run_distill_now() == {"accepted": True}
        await _wait_manual_distill(host)
        status = await host.get_distill_status()
        assert status["last_error"] == "llm returned empty content"
        assert status["lease"] is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_run_distill_now_records_runner_exception(tmp_path, monkeypatch) -> None:
    """G5: runner raises → last_error carries the message; lease released."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")

    async def raising_runner(home: str, **kwargs: object) -> DistillRunResult:
        del home, kwargs
        raise RuntimeError("boom")

    await host.start()
    try:
        await host.configure(dict(_DISTILL_ENABLED_CONFIG))
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(raising_runner)
        assert await host.run_distill_now() == {"accepted": True}
        await _wait_manual_distill(host)
        status = await host.get_distill_status()
        assert status["last_error"] == "boom"
        assert status["lease"] is None
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_stop_cancels_manual_distill_and_releases_lease(
    tmp_path, monkeypatch
) -> None:
    """G6: host.stop() cancels the in-flight manual job; lease is released."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    started = asyncio.Event()
    release = asyncio.Event()
    await host.start()
    await host.configure(dict(_DISTILL_ENABLED_CONFIG))
    core = host._personal_context  # pylint: disable=protected-access
    core.set_distill_runner(_blocking_runner(started, release))

    assert await host.run_distill_now() == {"accepted": True}
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task = host._distill_manual_task  # pylint: disable=protected-access
    assert task is not None

    await host.stop()

    assert task.cancelled()
    assert host._distill_manual_task is None  # pylint: disable=protected-access
    assert get_distill_lease(str(host._home)) is None  # pylint: disable=protected-access
    status = await host.get_distill_status()
    assert status["last_error"] == "distill_cancelled"


@pytest.mark.asyncio
async def test_patch_distill_config_cancels_manual_distill(
    tmp_path, monkeypatch
) -> None:
    """G6: a configuration change that restarts the runtime cancels the manual job."""

    _pin_one_model(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "pc")
    started = asyncio.Event()
    release = asyncio.Event()
    await host.start()
    try:
        await host.configure(dict(_DISTILL_ENABLED_CONFIG))
        core = host._personal_context  # pylint: disable=protected-access
        core.set_distill_runner(_blocking_runner(started, release))

        assert await host.run_distill_now() == {"accepted": True}
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task = host._distill_manual_task  # pylint: disable=protected-access
        assert task is not None

        await host.patch_distill_config({"interval_seconds": 3600})

        assert task.cancelled()
        assert get_distill_lease(str(host._home)) is None  # pylint: disable=protected-access
    finally:
        release.set()
        await host.stop()


@pytest.mark.asyncio
async def test_build_distill_runner_delegates_to_run_distill_job(
    tmp_path, monkeypatch
) -> None:
    """Factory runner forwards DistillRunnerPort kwargs into run_distill_job."""

    seen: dict[str, object] = {}

    async def fake_job(home, **kwargs):
        seen["home"] = home
        seen.update(kwargs)
        return DistillRunResult(
            job_id="x",
            status="success",
            window_start_ms=0,
            window_end_ms=kwargs["window_end_ms"],
            message_count=0,
            sampled=False,
        )

    monkeypatch.setattr(
        "jiuwenswarm.server.personal_context.distill_ports.run_distill_job",
        fake_job,
    )
    corpus = FixtureCorpus(default_fixture_messages())
    runner = build_distill_runner(
        home=tmp_path,
        corpus=corpus,
        resolve_llm=lambda: _FakeLlm(),
    )
    result = await runner(
        str(tmp_path),
        window_end_ms=99,
        learning_since_ms=1,
        max_messages=10,
        force_full_window=True,
    )
    assert result.job_id == "x"
    assert seen["home"] == str(tmp_path)
    assert seen["window_end_ms"] == 99
    assert seen["learning_since_ms"] == 1
    assert seen["max_messages"] == 10
    assert seen["force_full_window"] is True
    assert seen["corpus"] is corpus
    assert "llm" in seen
