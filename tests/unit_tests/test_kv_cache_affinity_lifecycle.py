from __future__ import annotations

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
