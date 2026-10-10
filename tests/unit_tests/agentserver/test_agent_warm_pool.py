from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_warm_pool import AgentWarmPool
from jiuwenswarm.server.runtime.agent_manager import AgentManager


class _FakeRootAgent:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.prepared: list[str] = []
        self.cleaned: list[str] = []

    async def prepare_session(self, *, session_id: str, **_kwargs) -> None:
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("prepare failed")
        self.prepared.append(session_id)

    async def cleanup_session_runtime(self, session_id: str) -> bool:
        self.cleaned.append(session_id)
        return True


class _ControlledRootAgent(_FakeRootAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}

    async def prepare_session(self, *, session_id: str, **_kwargs) -> None:
        gate = asyncio.Event()
        self.gates[session_id] = gate
        self.started.append(session_id)
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(session_id)
            raise
        self.prepared.append(session_id)


class _FakeManager:
    def __init__(self, agent: _FakeRootAgent) -> None:
        self.agent = agent
        self.pins = 0
        self.get_agent_calls: list[dict] = []

    async def get_agent(self, **kwargs):
        self.get_agent_calls.append(kwargs)
        return self.agent

    def pin_agent(self, _agent) -> None:
        self.pins += 1

    def unpin_agent(self, _agent) -> None:
        self.pins -= 1


async def _wait_until(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_persist_session_is_create_identity_but_not_warm_key() -> None:
    class WarmPool:
        def __init__(self):
            self.claim_calls = 0

        @staticmethod
        def make_key(**kwargs):
            return AgentWarmPool.make_key(**kwargs)

        async def claim(self, _key):
            self.claim_calls += 1
            return object()

    warm_pool = WarmPool()
    manager = object.__new__(AgentManager)
    manager.warm_pool = warm_pool
    manager._session_create_token_lock = asyncio.Lock()
    manager._session_create_tokens = {}
    params = {
        "channel_id": "web",
        "project_id": "default",
        "project_dir": "",
        "work_mode": "work",
        "is_swarm": False,
        "prewarm_eligible": True,
        "create_token": "stable-token",
    }

    first = await manager.claim_prewarmed_session(**params, persist_session=True)
    second = await manager.claim_prewarmed_session(**params, persist_session=True)

    assert first is second
    assert warm_pool.claim_calls == 1
    with pytest.raises(ValueError, match="different session parameters"):
        await manager.claim_prewarmed_session(**params, persist_session=False)
    assert warm_pool.claim_calls == 1


@pytest.fixture
def isolated_pool(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.project_store.list_projects",
        lambda **_kwargs: [],
    )

    def factory(agent: _FakeRootAgent) -> AgentWarmPool:
        # Prewarming is off by default; stay explicit so a developer environment
        # that opts in cannot silently turn these cases into no-ops.
        return AgentWarmPool(_FakeManager(agent), max_concurrency=4, enabled=True)

    yield factory


def test_prewarm_is_disabled_unless_the_environment_opts_in(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )

    def build() -> AgentWarmPool:
        return AgentWarmPool(_FakeManager(_FakeRootAgent()))

    monkeypatch.delenv("JIUWENSWARM_AGENT_PREWARM", raising=False)
    assert build()._enabled is False

    monkeypatch.setenv("JIUWENSWARM_AGENT_PREWARM", " OFF ")
    assert build()._enabled is False

    monkeypatch.setenv("JIUWENSWARM_AGENT_PREWARM", "1")
    assert build()._enabled is True


@pytest.mark.asyncio
async def test_disabled_pool_never_warms_and_always_bypasses(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setenv("JIUWENSWARM_AGENT_PREWARM", "0")
    agent = _FakeRootAgent()
    pool = AgentWarmPool(_FakeManager(agent))

    stats = await pool.sync(["web"], config={"model": "a"})
    assert stats == {"target": 0, "ready": 0, "warming": 0, "failed": 0, "stale": 0}

    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    claim = await pool.claim(key)
    assert claim.prewarm_hit is False
    assert claim.prewarm_status == "bypassed"
    assert claim.session_id.startswith("web_")
    assert not pool._slots
    assert not pool._tasks
    assert agent.prepared == []
    await pool.wait_for_session(claim.session_id)
    await pool.close()


def test_warm_key_normalizes_project_directory(tmp_path: Path) -> None:
    key = AgentWarmPool.make_key(
        channel_id=" web ",
        project_id="project-a",
        project_dir=str(tmp_path / ".." / tmp_path.name),
        work_mode="CODE",
    )
    assert key.channel_id == "web"
    assert key.project_dir == str(tmp_path.resolve()).lower()
    assert key.agent_mode == "code"
    assert key.agent_sub_mode == "normal"


def test_new_session_id_preserves_channel_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.time.time",
        lambda: 1_787_629_558.0,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.secrets.token_hex",
        lambda _size: "123456789abc",
    )
    channel_id = (
        "bench_20260901T101256Z_0037_"
        "scikit-learn__scikit-learn-12973"
    )

    session_id = AgentWarmPool._new_session_id(channel_id)

    assert session_id == (
        f"{channel_id}_{int(1_787_629_558.0 * 1000):x}_123456789abc"
    )


@pytest.mark.asyncio
async def test_code_prewarm_uses_same_manager_cache_identity_as_code_chat(
    isolated_pool,
) -> None:
    pool = isolated_pool(_FakeRootAgent())
    key = pool.make_key(
        channel_id="web",
        project_id="project-code",
        project_dir="/tmp/code-project",
        work_mode="code",
    )

    claim = await pool.claim(key)
    await pool.wait_for_session(claim.session_id)

    assert pool._manager.get_agent_calls == [
        {
            "channel_id": "web",
            "mode": "code",
            "project_dir": key.project_dir,
            "sub_mode": "normal",
        }
    ]
    await pool.end_foreground()
    await pool.close()


@pytest.mark.asyncio
async def test_pool_keeps_one_ready_slot_total_and_claim_is_atomic(isolated_pool) -> None:
    agent = _FakeRootAgent()
    pool = isolated_pool(agent)
    stats = await pool.sync(["web"], config={"model": "a"})
    assert stats["target"] == 2
    await _wait_until(lambda: len(pool._slots) == 1)

    work_key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    claims = await asyncio.gather(pool.claim(work_key), pool.claim(work_key))
    assert len({claim.session_id for claim in claims}) == 2
    assert sum(claim.prewarm_hit for claim in claims) == 1
    assert {claim.prewarm_status for claim in claims} == {"ready", "warming"}
    await pool.end_foreground()
    await _wait_until(lambda: work_key in pool._slots)
    assert len([key for key in pool._slots if key == work_key]) == 1
    assert len(pool._slots) == 1
    await pool.close()


@pytest.mark.asyncio
async def test_sync_filters_non_user_protocol_channels(isolated_pool) -> None:
    pool = isolated_pool(_FakeRootAgent())
    stats = await pool.sync(["web", "acp", "a2a"], config={"model": "a"})
    assert stats["target"] == 2
    assert pool._enabled_channels == {"web"}
    await pool.close()


@pytest.mark.asyncio
async def test_foreground_bypasses_background_and_pauses_lazy_dispatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.project_store.list_projects",
        lambda **_kwargs: [],
    )
    agent = _ControlledRootAgent()
    pool = AgentWarmPool(_FakeManager(agent), max_concurrency=1, enabled=True)
    await pool.sync(["web"], config={"model": "a"})
    await _wait_until(lambda: len(agent.started) == 1)
    assert len(pool._tasks) == 1
    assert len(pool._pending) == 1

    await pool.begin_foreground()
    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    claim = await pool.claim(key)
    assert claim.prewarm_hit is False
    await _wait_until(lambda: len(agent.started) == 2)

    background_id, foreground_id = agent.started
    await _wait_until(lambda: background_id in agent.cancelled)
    assert len(agent.started) == 2

    agent.gates[foreground_id].set()
    await pool.wait_for_session(foreground_id)
    assert len(agent.started) == 2

    await pool.end_foreground()
    await _wait_until(lambda: len(agent.started) == 3)
    for gate in agent.gates.values():
        gate.set()
    await pool.close()


@pytest.mark.asyncio
async def test_claim_promotes_matching_background_task_without_duplicate_prepare(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.project_store.list_projects",
        lambda **_kwargs: [],
    )
    agent = _ControlledRootAgent()
    pool = AgentWarmPool(_FakeManager(agent), max_concurrency=1, enabled=True)
    await pool.sync(["web"], config={"model": "a"})
    await _wait_until(lambda: len(agent.started) == 1)
    background_id = agent.started[0]
    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )

    claim = await pool.claim(key)

    assert claim.session_id == background_id
    assert claim.prewarm_hit is False
    assert claim.prewarm_status == "warming"
    assert len(agent.started) == 1
    agent.gates[background_id].set()
    await pool.wait_for_session(background_id)
    assert len(agent.started) == 1
    await pool.close()


@pytest.mark.asyncio
async def test_config_revision_replaces_unclaimed_slots(isolated_pool) -> None:
    agent = _FakeRootAgent()
    pool = isolated_pool(agent)
    await pool.sync(["web"], config={"model": "old"})
    await _wait_until(lambda: len(pool._slots) == 1)
    old_ids = {slot.session_id for slot in pool._slots.values()}

    await pool.sync(["web"], config={"model": "new"})
    await _wait_until(
        lambda: len(pool._slots) == 1
        and not old_ids.intersection(slot.session_id for slot in pool._slots.values())
    )
    assert old_ids.issubset(set(agent.cleaned))
    await pool.close()


@pytest.mark.asyncio
async def test_slow_older_sync_cannot_overwrite_newer_revision(
    isolated_pool, monkeypatch
) -> None:
    pool = isolated_pool(_FakeRootAgent())
    original_desired_keys = pool._desired_keys
    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def _controlled_desired_keys(channels):
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=5)
        return original_desired_keys(channels)

    monkeypatch.setattr(pool, "_desired_keys", _controlled_desired_keys)
    older = asyncio.create_task(pool.sync(["web"], config={"model": "old"}))
    await asyncio.wait_for(asyncio.to_thread(first_started.wait), timeout=1)
    await pool.sync(["web"], config={"model": "new"})
    release_first.set()
    await older

    assert pool._revision.config_fingerprint == pool.config_fingerprint(
        {"model": "new"}
    )
    await pool.close()


@pytest.mark.asyncio
async def test_failed_prepare_never_becomes_ready(isolated_pool) -> None:
    pool = isolated_pool(_FakeRootAgent(fail=True))
    await pool.sync(["web"], config={"model": "broken"})
    await _wait_until(lambda: not pool._tasks)
    stats = await pool.stats()
    assert stats["ready"] == 0
    assert stats["failed"] == 1
    assert stats["warming"] == 1
    await pool.close()


@pytest.mark.asyncio
async def test_claim_marker_survives_until_metadata_activation(isolated_pool) -> None:
    pool = isolated_pool(_FakeRootAgent())
    await pool.sync(["web"], config={"model": "ready"})
    await _wait_until(lambda: len(pool._slots) == 1)
    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )

    claim = await pool.claim(key)
    marker = pool._marker_path(claim.session_id)
    assert claim.prewarm_hit is True
    assert marker.is_file()

    pool.clear_marker(claim.session_id)
    assert not marker.exists()
    await pool.close()


def test_new_boot_cleans_only_metadata_less_marked_workspace(
    isolated_pool, tmp_path: Path
) -> None:
    old_pool = isolated_pool(_FakeRootAgent())
    key = old_pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    stale_id = "web_stale"
    persisted_id = "web_persisted"
    for session_id in (stale_id, persisted_id):
        (tmp_path / session_id).mkdir()
        old_pool._write_marker(session_id, key)
    (tmp_path / persisted_id / "metadata.json").write_text("{}", encoding="utf-8")

    isolated_pool(_FakeRootAgent())

    assert not (tmp_path / stale_id).exists()
    assert (tmp_path / persisted_id / "metadata.json").is_file()
