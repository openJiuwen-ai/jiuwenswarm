from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
    supports_phase_auto_root,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import (
    RootPermissionQueueError,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.request import prepare_chat_turn
from jiuwenswarm.server.runtime import agent_manager as agent_manager_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session import session_metadata as session_metadata_module


def _request(tmp_path, **params):
    return SimpleNamespace(
        channel_id="web",
        session_id="session-a",
        params={"mode": "agent", **params},
        metadata={},
    )


class _CapturingManager(AgentManager):
    def __init__(self) -> None:
        super().__init__()
        self.project_dirs: list[str | None] = []
        self.selected_modes: list[str | None] = []
        self.agent = _PreparedAgent()
        self.owner_lock = _ObservedLock()

    async def get_agent(self, **kwargs):
        self.project_dirs.append(kwargs.get("project_dir"))
        self.selected_modes.append(kwargs.get("mode"))
        self.agents.setdefault("web", {})["agent::captured"] = self.agent
        return self.agent

    def _get_agent_create_lock(self, channel_key: str, cache_key: str):
        if cache_key.startswith("auto-session:"):
            return self.owner_lock
        return super()._get_agent_create_lock(channel_key, cache_key)


class _ObservedLock:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.contender_waiting = asyncio.Event()

    async def __aenter__(self):
        if self.lock.locked():
            self.contender_waiting.set()
        await self.lock.acquire()
        return self

    async def __aexit__(self, *_exc) -> None:
        self.lock.release()


class _PreparedAgent:
    def __init__(self) -> None:
        self.prepare_started = asyncio.Event()
        self.allow_prepare = asyncio.Event()
        self.allow_prepare.set()
        self.sessions: set[str] = set()
        self.permission_root: Path | None = None

    async def prepare_session(
        self,
        *,
        session_id: str,
        project_dir: str | None = None,
        **_kwargs,
    ) -> None:
        self.prepare_started.set()
        await self.allow_prepare.wait()
        self.permission_root = (
            Path(project_dir).resolve(strict=False) if project_dir else None
        )
        self.sessions.add(session_id)

    def has_session_runtime(self, session_id=None) -> bool:
        return session_id in self.sessions

    def has_auto_permission_session(self, session_id=None) -> bool:
        return session_id in self.sessions

    def validate_auto_permission_workspace_request(self, request) -> None:
        raw = request.params.get("project_dir") or request.params.get("workspace_dir")
        if raw and Path(raw).resolve(strict=False) != self.permission_root:
            raise RootPermissionQueueError("workspace_changed")


class _SessionOwner:
    def __init__(self, *, auto: bool = False) -> None:
        self.auto = auto

    def has_session_runtime(self, session_id=None) -> bool:
        return session_id == "session-a"

    def has_auto_permission_session(self, session_id=None) -> bool:
        return self.auto and session_id == "session-a"

    def validate_auto_permission_workspace_request(self, request) -> None:
        if self.auto and request.params.get("project_dir") == "reject":
            raise RootPermissionQueueError("workspace_changed")


@pytest.fixture
def auto_config(monkeypatch, internal_auto_mode):
    monkeypatch.setattr(
        agent_manager_module,
        "get_config",
        lambda: {"permissions": {"enabled": True, "mode": "auto"}},
    )


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"mode": "agent", "work_mode": "work"}, True),
        ({"mode": "agent.plan", "work_mode": "work"}, True),
        ({"mode": "agent", "work_mode": "code"}, False),
        ({"mode": "code.normal"}, False),
        ({"mode": "team"}, False),
        ({"mode": "auto_harness"}, False),
    ],
)
def test_auto_permission_support_matrix(
    params: dict[str, str],
    expected: bool,
) -> None:
    assert supports_phase_auto_root(params) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["workspace_dir", "project_dir"])
async def test_first_auto_request_selects_declared_root(
    auto_config,
    tmp_path,
    field: str,
) -> None:
    manager = _CapturingManager()
    root = tmp_path / "project"

    await manager.get_agent_for_request(_request(tmp_path, **{field: str(root)}))

    assert manager.project_dirs == [str(root.resolve())]


@pytest.mark.asyncio
async def test_existing_session_owner_wins_over_late_root(auto_config, tmp_path) -> None:
    manager = _CapturingManager()
    owner = _SessionOwner(auto=True)
    manager.agents["web"] = {"agent::original": owner}

    selected = await manager.get_agent_for_request(
        _request(tmp_path, project_dir=str(tmp_path / "other"))
    )

    assert selected is owner
    assert manager.project_dirs == []


@pytest.mark.asyncio
async def test_existing_non_auto_owner_does_not_capture_deep_auto_request(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()
    owner = _SessionOwner(auto=False)
    manager.agents["web"] = {"code::original": owner}

    selected = await manager.get_agent_for_request(
        _request(tmp_path, project_dir=str(tmp_path / "project"))
    )

    assert selected is manager.agent
    assert selected is not owner
    assert manager.selected_modes == ["agent"]


@pytest.mark.asyncio
async def test_auto_owner_lookup_skips_earlier_non_auto_runtime(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()
    manager.agents["web"] = {"code::first": _SessionOwner(auto=False)}
    request = _request(tmp_path, project_dir=str(tmp_path / "project"))

    first = await manager.get_agent_for_request(request)
    second = await manager.get_agent_for_request(request)

    assert first is second is manager.agent
    assert manager.project_dirs == [str((tmp_path / "project").resolve())]
    assert (
        manager.get_auto_permission_agent_for_session_nowait("web", "session-a")
        is manager.agent
    )


@pytest.mark.asyncio
async def test_conflicting_first_roots_fail_before_owner_creation(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()

    with pytest.raises(ValueError, match="workspace_conflict"):
        await manager.get_agent_for_request(
            _request(
                tmp_path,
                workspace_dir=str(tmp_path / "scratch"),
                project_dir=str(tmp_path / "project"),
            )
        )

    assert manager.project_dirs == []


@pytest.mark.asyncio
async def test_first_concurrent_requests_establish_one_session_owner(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()
    manager.agent.allow_prepare.clear()
    request = _request(tmp_path, project_dir=str(tmp_path / "project"))

    first = asyncio.create_task(manager.get_agent_for_request(request))
    await manager.agent.prepare_started.wait()
    second = asyncio.create_task(manager.get_agent_for_request(request))
    await manager.owner_lock.contender_waiting.wait()

    assert manager.project_dirs == [str((tmp_path / "project").resolve())]
    manager.agent.allow_prepare.set()
    first_owner, second_owner = await asyncio.gather(first, second)

    assert first_owner is second_owner is manager.agent
    assert manager.project_dirs == [str((tmp_path / "project").resolve())]


@pytest.mark.asyncio
async def test_conflicting_concurrent_request_cannot_mutate_winner_metadata(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()
    manager.agent.allow_prepare.clear()
    first_root = str(tmp_path / "first")
    second_root = str(tmp_path / "second")
    metadata_writes: list[str] = []

    def admit(root: str):
        def callback() -> str:
            metadata_writes.append(root)
            return root

        return callback

    first = asyncio.create_task(
        manager.get_agent_for_request(
            _request(tmp_path, project_dir=first_root),
            admit_request=admit(first_root),
        )
    )
    await manager.agent.prepare_started.wait()
    second = asyncio.create_task(
        manager.get_agent_for_request(
            _request(tmp_path, project_dir=second_root),
            admit_request=admit(second_root),
        )
    )
    await manager.owner_lock.contender_waiting.wait()

    manager.agent.allow_prepare.set()
    assert await first is manager.agent
    with pytest.raises(RootPermissionQueueError, match="workspace_changed"):
        await second

    assert metadata_writes == [first_root]
    assert manager.project_dirs == [str(Path(first_root).resolve(strict=False))]


@pytest.mark.asyncio
async def test_admitted_metadata_root_is_revalidated_before_owner_handoff(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()
    owner = _PreparedAgent()
    owner.permission_root = (tmp_path / "first").resolve(strict=False)
    owner.sessions.add("session-a")
    manager.agents["web"] = {"agent::original": owner}
    request = _request(tmp_path)

    def admit_request() -> str:
        admitted = str(tmp_path / "other")
        request.params["project_dir"] = admitted
        request.metadata["project_dir"] = admitted
        return admitted

    with pytest.raises(RootPermissionQueueError, match="workspace_changed"):
        await manager.get_agent_for_request(request, admit_request=admit_request)

    assert manager.project_dirs == []


@pytest.mark.asyncio
async def test_cold_persisted_root_cannot_replace_explicit_request_root(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()
    request = _request(tmp_path, project_dir=str(tmp_path / "requested"))
    callback_calls = 0

    def admit_request() -> str:
        nonlocal callback_calls
        callback_calls += 1
        persisted = str(tmp_path / "persisted")
        request.params["project_dir"] = persisted
        request.metadata["project_dir"] = persisted
        return persisted

    with pytest.raises(RootPermissionQueueError, match="new_session_required"):
        await manager.get_agent_for_request(request, admit_request=admit_request)

    assert callback_calls == 1
    assert manager.project_dirs == []
    assert manager.agent.prepare_started.is_set() is False


@pytest.mark.asyncio
async def test_cold_root_mismatch_rejects_before_metadata_store_changes(
    auto_config,
    monkeypatch,
    tmp_path,
) -> None:
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "session-a"
    session_dir.mkdir(parents=True)
    persisted_root = tmp_path / "persisted"
    original_metadata = {
        "session_id": "session-a",
        "project_dir": str(persisted_root),
        "mode": "agent",
        "work_mode": "work",
        "model": "original-model",
        "last_message_at": 100.0,
        "last_user_message_at": 100.0,
    }
    metadata_file = session_dir / "metadata.json"
    metadata_file.write_text(
        json.dumps(original_metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    original_bytes = metadata_file.read_bytes()
    monkeypatch.setattr(
        session_metadata_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    session_metadata_module.remove_session_metadata_cache("session-a")
    request = AgentRequest(
        request_id="request-a",
        channel_id="web",
        session_id="session-a",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "query": "continue",
            "mode": "agent",
            "work_mode": "work",
            "project_dir": str(tmp_path / "other"),
            "model_name": "replacement-model",
        },
    )

    with pytest.raises(RootPermissionQueueError, match="new_session_required"):
        await prepare_chat_turn(_CapturingManager(), request, "web")

    assert metadata_file.read_bytes() == original_bytes
    session_metadata_module.remove_session_metadata_cache("session-a")
    assert session_metadata_module.get_session_metadata(
        "session-a",
        cache_bust=True,
        enable_writeback=False,
    )["model"] == "original-model"


@pytest.mark.asyncio
async def test_permission_resume_without_owner_fails_before_creation(
    auto_config,
    tmp_path,
) -> None:
    manager = _CapturingManager()
    callback_calls = 0
    request = _request(
        tmp_path,
        project_dir=str(tmp_path / "project"),
        source="permission_interrupt",
        request_id="permission-1",
        answers=[{"value": "allow_once"}],
    )

    def admit_request() -> str:
        nonlocal callback_calls
        callback_calls += 1
        return str(tmp_path / "project")

    with pytest.raises(RootPermissionQueueError, match="resume_owner_missing"):
        await manager.get_agent_for_request(request, admit_request=admit_request)

    assert callback_calls == 0
    assert manager.project_dirs == []


@pytest.mark.asyncio
async def test_non_auto_keeps_project_identity_separate_from_workspace(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        agent_manager_module,
        "get_config",
        lambda: {"permissions": {"enabled": True, "mode": "manual"}},
    )
    manager = _CapturingManager()
    workspace = tmp_path / "scratch"

    await manager.get_agent_for_request(
        _request(
            tmp_path,
            workspace_dir=str(workspace),
            project_dir=str(tmp_path / "project"),
        )
    )

    assert manager.project_dirs == [str(tmp_path / "project")]


@pytest.mark.asyncio
async def test_non_auto_permission_resume_keeps_generic_admission(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        agent_manager_module,
        "get_config",
        lambda: {"permissions": {"enabled": True, "mode": "manual"}},
    )
    manager = _CapturingManager()
    request = _request(
        tmp_path,
        source="permission_interrupt",
        request_id="permission-1",
        answers=[{"value": "allow_once"}],
    )
    callback_calls = 0

    def admit_request() -> str:
        nonlocal callback_calls
        callback_calls += 1
        return str(tmp_path / "generic")

    selected = await manager.get_agent_for_request(
        request,
        admit_request=admit_request,
    )

    assert selected is manager.agent
    assert callback_calls == 1
    assert manager.project_dirs == [str(tmp_path / "generic")]


@pytest.mark.asyncio
async def test_existing_auto_owner_survives_transition_to_manual(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        agent_manager_module,
        "get_config",
        lambda: {"permissions": {"enabled": True, "mode": "manual"}},
    )
    manager = _CapturingManager()
    owner = _SessionOwner(auto=True)
    manager.agents["web"] = {"agent::original": owner}

    selected = await manager.get_agent_for_request(_request(tmp_path))

    assert selected is owner
    assert manager.project_dirs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected_mode"),
    [
        ({"mode": "team"}, "team"),
        ({"mode": "code.normal"}, "code"),
        ({"mode": "auto_harness"}, "auto_harness"),
        ({"mode": "agent", "work_mode": "code"}, "code"),
    ],
)
async def test_existing_auto_owner_does_not_capture_excluded_mode(
    auto_config,
    tmp_path,
    params: dict[str, str],
    expected_mode: str,
) -> None:
    manager = _CapturingManager()
    owner = _SessionOwner(auto=True)
    manager.agents["web"] = {"agent::original": owner}

    selected = await manager.get_agent_for_request(_request(tmp_path, **params))

    assert selected is manager.agent
    assert selected is not owner
    assert manager.selected_modes == [expected_mode]


def test_session_owner_accepts_equivalent_or_omitted_root(tmp_path) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("session-a")
    adapter._enable_auto_permission = True
    adapter._permission_workspace_root = (tmp_path / "project").resolve()

    adapter.validate_auto_permission_workspace_request(_request(tmp_path))
    adapter.validate_auto_permission_workspace_request(
        _request(tmp_path, project_dir=str(tmp_path / "project" / "."))
    )


@pytest.mark.parametrize("mode", ["team", "code.normal", "auto_harness"])
def test_session_owner_ignores_workspace_changes_for_excluded_mode(
    tmp_path,
    mode: str,
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("session-a")
    adapter._enable_auto_permission = True
    adapter._permission_workspace_root = (tmp_path / "project").resolve()

    adapter.validate_auto_permission_workspace_request(
        _request(tmp_path, mode=mode, project_dir=str(tmp_path / "other"))
    )


@pytest.mark.parametrize(
    "params",
    [
        {"project_dir": "other"},
        {"workspace_dir": "scratch", "project_dir": "project"},
    ],
)
def test_session_owner_rejects_workspace_change_before_runtime(
    tmp_path,
    params: dict[str, str],
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("session-a")
    adapter._enable_auto_permission = True
    adapter._permission_workspace_root = (tmp_path / "project").resolve()
    absolute = {key: str(tmp_path / value) for key, value in params.items()}

    with pytest.raises(RootPermissionQueueError, match="new_session_required"):
        adapter.validate_auto_permission_workspace_request(
            _request(tmp_path, **absolute)
        )
