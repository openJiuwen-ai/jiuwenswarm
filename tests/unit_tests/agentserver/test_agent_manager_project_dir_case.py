"""The runtime project/workspace value must keep the caller's path casing.

``os.path.normcase`` and ``str.casefold`` are comparison-key transforms: on
Windows ``normcase`` lowercases the whole path (drive letter included) and on
every platform ``casefold`` rewrites non-ASCII names (``Straße`` -> ``strasse``).
Applying them to the value that becomes ``adapter._project_dir`` corrupts
user-visible paths and breaks the case-sensitive string equality used by
persisted session metadata and the Web project list, so those transforms stay
limited to cache keys.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime import agent_manager


def _folding_normcase(path: Any) -> str:
    """Stand-in for ``ntpath.normcase``: folds case and uses backslashes."""

    return str(path).lower().replace("/", "\\")


async def test_auto_workspace_keeps_caller_casing(monkeypatch, tmp_path):
    """The Auto workspace feeds prepare_session()/create_instance() directly."""

    declared = tmp_path / "Selected Project"
    request = SimpleNamespace(
        params={"mode": "agent", "project_dir": str(declared)},
        metadata={},
    )
    monkeypatch.setattr(
        agent_manager,
        "get_config",
        lambda: {"permissions": {"enabled": True, "mode": "auto"}},
    )
    monkeypatch.setattr(
        agent_manager, "is_auto_permission_enabled", lambda _config: True
    )
    monkeypatch.setattr(agent_manager, "supports_phase_auto_root", lambda _params: True)
    monkeypatch.setattr(
        agent_manager,
        "resolve_declared_auto_workspace",
        lambda _params, _metadata: declared,
    )
    # Simulate Windows, where normcase() folds the path (drive letter included).
    monkeypatch.setattr(agent_manager.os.path, "normcase", _folding_normcase)

    assert agent_manager._auto_permission_request_workspace(request) == str(declared)


async def test_create_agent_keeps_project_dir_casing(monkeypatch, tmp_path):
    """The cached Agent must receive the caller's path, not a folded cache key."""

    captured: dict[str, Any] = {}

    class _RecordingAgent:
        async def create_instance(self, config, **_kwargs):
            captured["config"] = config

        def set_heartbeat_service(self, _service):
            pass

        def set_personal_context_runtime_enabled(self, _enabled):
            pass

        def set_permissions_changed_notifier(self, _notifier):
            pass

        def set_permissions_external_input_context_builder(self, _builder):
            pass

    facade = SimpleNamespace(JiuWenSwarm=_RecordingAgent)
    monkeypatch.setattr(
        agent_manager,
        "importlib",
        SimpleNamespace(import_module=lambda _name: facade),
    )

    # Mixed case plus a non-ASCII character: normcase() folds the first and
    # casefold() rewrites the second on every platform.
    project = tmp_path / "Straße Projekt"
    manager = agent_manager.AgentManager()

    agent = await manager._create_agent(
        "default", mode="agent", config={"project_dir": str(project)}
    )

    assert isinstance(agent, _RecordingAgent)
    assert captured["config"]["project_dir"] == str(project)


async def test_create_agent_still_uses_a_case_insensitive_cache_key(monkeypatch, tmp_path):
    """Case folding must survive where it is correct: the cache key."""

    class _RecordingAgent:
        async def create_instance(self, _config, **_kwargs):
            pass

        def set_heartbeat_service(self, _service):
            pass

        def set_personal_context_runtime_enabled(self, _enabled):
            pass

        def set_permissions_changed_notifier(self, _notifier):
            pass

        def set_permissions_external_input_context_builder(self, _builder):
            pass

    facade = SimpleNamespace(JiuWenSwarm=_RecordingAgent)
    monkeypatch.setattr(
        agent_manager,
        "importlib",
        SimpleNamespace(import_module=lambda _name: facade),
    )

    project = tmp_path / "Project"
    manager = agent_manager.AgentManager()
    key = agent_manager._make_agent_cache_key("agent", "normal", str(project))
    first = await manager._create_agent(
        "default", mode="agent", config={"project_dir": str(project)}, cache_key=key
    )

    # A different spelling of the same directory resolves to the same slot.
    assert agent_manager._normalize_project_dir(str(project)) == (
        agent_manager._normalize_project_dir(str(project).upper())
    )
    assert manager.agents["default"][key] is first
    assert first._jiuwenswarm_agent_project_dir == agent_manager._normalize_project_dir(
        str(project)
    )


@pytest.mark.parametrize(
    "raw",
    ["", "   ", None],
)
def test_normalize_project_dir_stays_a_key_helper(raw):
    """Empty declarations stay empty instead of becoming the cwd."""

    assert agent_manager._normalize_project_dir(raw) == ""
