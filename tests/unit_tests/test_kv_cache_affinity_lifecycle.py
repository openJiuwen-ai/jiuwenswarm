from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from openjiuwen.core.kv_cache.kv_cache_types import KVCacheIdentity
from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime
from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_model_provider


class _AffinityModel:
    def __init__(self) -> None:
        self.model_client_config = SimpleNamespace(
            client_provider="AscendAffinity",
            api_base="http://127.0.0.1:8000/v1",
            extensions=None,
        )
        self.model_config = SimpleNamespace(model_name="test-model")
        self.calls: list[tuple[str, dict]] = []

    async def prefetch_kvc(self, **kwargs) -> bool:
        self.calls.append(("prefetch", kwargs))
        return True

    async def offload_kvc(self, **kwargs) -> bool:
        self.calls.append(("offload", kwargs))
        return True

    async def evict_kvc(self, **kwargs) -> bool:
        self.calls.append(("evict", kwargs))
        return True


@pytest_asyncio.fixture(autouse=True)
async def _reset_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        kv_cache_model_provider,
        "is_kv_cache_affinity_enabled",
        lambda config=None: True,
    )
    await kv_cache_application_runtime.close_kv_cache_runtime()
    yield
    await kv_cache_application_runtime.close_kv_cache_runtime()


def test_application_runtime_is_not_created_when_affinity_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kv_cache_model_provider,
        "is_kv_cache_affinity_enabled",
        lambda config=None: False,
    )
    monkeypatch.setattr(
        kv_cache_application_runtime,
        "KVCacheRuntime",
        lambda **_kwargs: pytest.fail("disabled KVC must not create a runtime"),
    )

    assert kv_cache_application_runtime.get_kv_cache_runtime() is None
    assert kv_cache_application_runtime.get_kv_cache_runtime() is None


def test_application_runtime_gate_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kv_cache_model_provider,
        "is_kv_cache_affinity_enabled",
        lambda config=None: (_ for _ in ()).throw(RuntimeError("broken config")),
    )

    assert kv_cache_application_runtime.get_kv_cache_runtime() is None


def test_application_runtime_initialization_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_initialization_error(**_kwargs):
        raise RuntimeError("broken runtime")

    monkeypatch.setattr(
        kv_cache_application_runtime,
        "KVCacheRuntime",
        _raise_initialization_error,
    )

    assert kv_cache_application_runtime.get_kv_cache_runtime() is None


@pytest.mark.asyncio
async def test_ambiguous_cold_session_waits_for_live_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _entry(model_name: str, api_base: str) -> dict:
        return {
            "model_client_config": {
                "api_base": api_base,
                "model_name": model_name,
                "client_provider": "OpenAI",
                "extensions": {"kv_cache": {"mode": "affinity"}},
            },
            "model_config_obj": {},
            "is_default": model_name == "model-a",
        }

    models = [
        _entry("model-a", "http://model-a/v1"),
        _entry("model-b", "http://model-b/v1"),
    ]
    monkeypatch.setattr(kv_cache_model_provider, "get_config", lambda: {})
    monkeypatch.setattr(
        kv_cache_model_provider,
        "get_default_models",
        lambda _config: models,
    )

    assert kv_cache_model_provider.create_default_kv_cache_model() is None

    runtime = kv_cache_application_runtime.get_kv_cache_runtime()
    identity = KVCacheIdentity("session-b", "session-b")
    assert await runtime.prepare(identity) is False

    # The actual model call establishes the binding without a default guess.
    offload_started = asyncio.Event()

    class _LiveModel(_AffinityModel):
        async def offload_kvc(self, **kwargs) -> bool:
            result = await super().offload_kvc(**kwargs)
            offload_started.set()
            return result

    model = _LiveModel()
    model.model_config.model_name = "model-b"
    lease = await runtime.begin_inference(identity, model)
    assert lease is not None
    await runtime.end_inference(lease, succeeded=True)
    assert await runtime.suspend(identity) is True
    await asyncio.wait_for(offload_started.wait(), timeout=1)
    assert [action for action, _ in model.calls] == ["offload"]
    assert await runtime.prepare(identity) is True
    assert await runtime.release(identity) is True
    assert [action for action, _ in model.calls] == ["offload", "prefetch", "evict"]
    assert all(kwargs["model"] == "model-b" for _, kwargs in model.calls)


@pytest.mark.parametrize("extra_model", [None, "OpenAI", "AscendAffinity"])
def test_default_binding_requires_a_single_affinity_model(
    monkeypatch: pytest.MonkeyPatch, extra_model: str | None,
) -> None:
    import openjiuwen.core.foundation.llm as llm

    config = {
        "models": {"defaults": [{
            "model_client_config": {
                "client_provider": "AscendAffinity",
                "model_name": "model-a",
                "api_base": "http://model-a/v1",
            },
        }]},
    }
    if extra_model:
        # AgentOS models are candidates even though they are not the default.
        config["models"]["agentos"] = [{
            "model_client_config": {
                "client_provider": extra_model,
                "model_name": "model-b",
                "api_base": "http://model-b/v1",
            },
        }]
    monkeypatch.setattr(kv_cache_model_provider, "get_config", lambda: config)
    built = []

    def _build_model(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(supports_kv_cache_affinity=lambda: True)

    monkeypatch.setattr(llm, "Model", _build_model)
    result = kv_cache_model_provider.create_default_kv_cache_model()
    if extra_model == "AscendAffinity":
        assert result is None
        assert not built
    else:
        assert result is not None
        assert built[0]["model_config"].model_name == "model-a"


def test_application_runtime_is_shared_until_closed() -> None:
    first = kv_cache_application_runtime.get_kv_cache_runtime()
    second = kv_cache_application_runtime.get_kv_cache_runtime()

    assert first is second


@pytest.mark.asyncio
async def test_close_replaces_application_runtime() -> None:
    first = kv_cache_application_runtime.get_kv_cache_runtime()

    await kv_cache_application_runtime.close_kv_cache_runtime()
    second = kv_cache_application_runtime.get_kv_cache_runtime()

    assert first.closed is True
    assert second is not first


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_kvc_close_failure_is_not_server_cancellation(monkeypatch, error_type):
    class BrokenRuntime:
        async def close(self):
            raise error_type("KVC cleanup failed")

    monkeypatch.setattr(kv_cache_application_runtime, "_runtime", BrokenRuntime())
    await kv_cache_application_runtime.close_kv_cache_runtime()
    assert kv_cache_application_runtime._runtime is None


@pytest.mark.asyncio
async def test_shutdown_budget_includes_cancellation_resistant_cleanup(monkeypatch) -> None:
    release = asyncio.Event()
    cancelled = asyncio.Event()
    tasks = []

    class SlowRuntime:
        async def close(self):
            tasks.append(asyncio.current_task())
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise RuntimeError("late cleanup failure")

    monkeypatch.setattr(kv_cache_application_runtime, "_runtime", SlowRuntime())
    monkeypatch.setattr(
        kv_cache_application_runtime, "KVC_TERMINAL_CLEANUP_TIMEOUT_SECONDS", 0.01
    )
    try:
        await asyncio.wait_for(
            kv_cache_application_runtime.close_kv_cache_runtime(), timeout=0.5
        )
        await asyncio.wait_for(cancelled.wait(), timeout=0.5)
        assert kv_cache_application_runtime._runtime is None
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_historical_session_uses_cached_default_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _AffinityModel()
    builds: list[None] = []

    def _build_model() -> _AffinityModel:
        builds.append(None)
        return model

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider."
        "create_default_kv_cache_model",
        _build_model,
    )
    runtime = kv_cache_application_runtime.get_kv_cache_runtime()
    identity = KVCacheIdentity("session-a", "session-a")

    assert await runtime.prepare(identity) is True
    assert await runtime.release(identity) is True

    assert builds == [None]
    assert [action for action, _ in model.calls] == ["prefetch", "evict"]


@pytest.mark.asyncio
async def test_runtime_action_failure_does_not_escape_session_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenModel(_AffinityModel):
        async def evict_kvc(self, **kwargs) -> bool:
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_model_provider."
        "create_default_kv_cache_model",
        _BrokenModel,
    )
    from openjiuwen.core.session.agent import create_agent_session

    session = create_agent_session(
        session_id="session-a",
        kv_cache_runtime=kv_cache_application_runtime.get_kv_cache_runtime(),
    )

    assert await session.release_kvc() is False
