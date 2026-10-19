"""WEB-03 distill control-plane API (dedicated distill.* ; D7 kept on runtime.patch)."""

from __future__ import annotations

from pathlib import Path

import pytest
from openjiuwen.harness.personal_context import PersonalContext
from openjiuwen.harness.personal_context.distill import DistillRunResult

from jiuwenswarm.server.personal_context import host_api as host_module
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


def _pin_models(monkeypatch: pytest.MonkeyPatch) -> None:
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


class _FakeSnapshot:
    state = "STOPPED"


class FakeDistillCore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.configured: object | None = None
        self.active = False
        self.snapshot_state = "STOPPED"
        self._distill_corpus: object | None = "corpus"
        self._distill_runner: object | None = None
        self._distill_task = None

    def set_distill_corpus(self, corpus: object | None) -> None:
        self._distill_corpus = corpus

    def set_distill_runner(self, runner: object | None) -> None:
        self._distill_runner = runner

    def _set_embedding_configuration(self, **_kwargs: object) -> None:
        self.calls.append(("_set_embedding_configuration", None))

    async def snapshot(self) -> object:
        snap = _FakeSnapshot()
        snap.state = self.snapshot_state
        return snap

    async def set_configuration(self, config: object) -> None:
        self.configured = config
        self.calls.append(("set_configuration", None))

    async def activate_runtime(self) -> None:
        self.active = True
        self.calls.append(("activate_runtime", None))

    async def deactivate_runtime(self, *, timeout_seconds: float = 30.0) -> None:
        self.active = False
        self.calls.append(("deactivate_runtime", timeout_seconds))


@pytest.fixture
def distill_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[PersonalContextHostAPI, FakeDistillCore]:
    _pin_models(monkeypatch)
    host = PersonalContextHostAPI(home=tmp_path / "personal_context")
    fake = FakeDistillCore()

    async def mock_runner(
        home: str,
        *,
        window_end_ms: int,
        learning_since_ms: int | None = None,
        max_messages: int = 800,
        force_full_window: bool = False,
    ) -> DistillRunResult:
        del home, learning_since_ms, max_messages, force_full_window
        return DistillRunResult(
            job_id="job-rpc",
            status="success",
            window_start_ms=0,
            window_end_ms=window_end_ms,
            message_count=3,
            sampled=False,
        )

    fake._distill_runner = mock_runner
    monkeypatch.setattr(host, "_personal_context", fake)
    return host, fake


@pytest.mark.asyncio
async def test_d7_runtime_patch_still_rejects_distill(
    distill_host: tuple[PersonalContextHostAPI, FakeDistillCore],
) -> None:
    host, _fake = distill_host
    await host.configure(
        {
            "collection_enabled": False,
            "agent_use_enabled": False,
            "strategy_profile": "rules",
            "model_index": 0,
            "fetch_services": [],
        }
    )
    with pytest.raises(PersonalContext.Error, match="unsupported fields"):
        await host.patch_runtime_config({"distill": {"enabled": True}})


@pytest.mark.asyncio
async def test_patch_distill_config_writes_and_returns(
    distill_host: tuple[PersonalContextHostAPI, FakeDistillCore],
) -> None:
    host, fake = distill_host
    await host.configure(
        {
            "collection_enabled": False,
            "agent_use_enabled": False,
            "strategy_profile": "rules",
            "model_index": 0,
            "fetch_services": [],
            "distill": {"enabled": False, "interval_seconds": 86400},
        }
    )
    result = await host.patch_distill_config(
        {"enabled": True, "interval_seconds": 3600, "message_threshold": 10}
    )
    assert result["enabled"] is True
    assert result["interval_seconds"] == 3600
    assert result["message_threshold"] == 10
    assert ("set_configuration", None) in fake.calls
    cfg = await host.get_distill_config()
    assert cfg["enabled"] is True
    assert cfg["interval_seconds"] == 3600


@pytest.mark.asyncio
async def test_patch_distill_rejects_unknown_keys(
    distill_host: tuple[PersonalContextHostAPI, FakeDistillCore],
) -> None:
    host, _fake = distill_host
    await host.configure(
        {
            "collection_enabled": False,
            "agent_use_enabled": False,
            "strategy_profile": "rules",
            "model_index": 0,
            "fetch_services": [],
        }
    )
    with pytest.raises(PersonalContext.Error, match="unsupported fields"):
        await host.patch_distill_config({"max_messages": 100})


@pytest.mark.asyncio
async def test_get_distill_status_reports_wired(
    distill_host: tuple[PersonalContextHostAPI, FakeDistillCore],
) -> None:
    host, _fake = distill_host
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
    status = await host.get_distill_status()
    assert status["wired"] is True
    assert status["enabled"] is True
    assert status["collection_enabled"] is False
    assert "last_error" in status


@pytest.mark.asyncio
async def test_run_distill_now_rpc_success(
    distill_host: tuple[PersonalContextHostAPI, FakeDistillCore],
) -> None:
    host, fake = distill_host
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

    async def mock_runner(
        home: str,
        *,
        window_end_ms: int,
        learning_since_ms: int | None = None,
        max_messages: int = 800,
        force_full_window: bool = False,
    ) -> DistillRunResult:
        del home, learning_since_ms, max_messages, force_full_window
        return DistillRunResult(
            job_id="job-rpc",
            status="success",
            window_start_ms=0,
            window_end_ms=window_end_ms,
            message_count=3,
            sampled=False,
        )

    # configure 会经 _ensure_distill_ports 重写 runner；测试再钉回 mock。
    fake.set_distill_corpus("corpus")
    fake.set_distill_runner(mock_runner)
    result = await host.run_distill_now()
    assert result == {"accepted": True}
    task = host._distill_manual_task  # pylint: disable=protected-access
    assert task is not None
    await task
    status = await host.get_distill_status()
    assert status["last_error"] is None
    assert status["lease"] is None


@pytest.mark.asyncio
async def test_run_distill_now_fails_when_disabled(
    distill_host: tuple[PersonalContextHostAPI, FakeDistillCore],
) -> None:
    host, _fake = distill_host
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
    status = await host.get_distill_status()
    assert status["last_error"] == "distill_disabled"
