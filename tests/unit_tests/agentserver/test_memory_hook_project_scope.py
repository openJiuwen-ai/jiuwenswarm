from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.session import session_metadata


def test_memory_hook_scope_falls_back_to_session_project_metadata(monkeypatch) -> None:
    calls: list[str] = []

    def _metadata(session_id: str) -> dict[str, str]:
        calls.append(session_id)
        return {"project_id": "proj-42", "project_dir": "C:/workspace/project-42"}

    monkeypatch.setattr(session_metadata, "get_session_metadata", _metadata)
    request = SimpleNamespace(session_id="session-1", params={"query": "hello"})

    extra = interface_module._memory_hook_extra(request)

    assert extra == {
        "query": "hello",
        "project_id": "proj-42",
        "project_dir": "C:/workspace/project-42",
    }
    assert calls == ["session-1"]
    assert request.params == {"query": "hello"}


def test_memory_hook_scope_keeps_explicit_request_project_id(monkeypatch) -> None:
    def _unexpected_lookup(_session_id: str) -> dict[str, str]:
        raise AssertionError("explicit project_id must not read session metadata")

    monkeypatch.setattr(session_metadata, "get_session_metadata", _unexpected_lookup)
    request = SimpleNamespace(
        session_id="session-1",
        params={
            "query": "hello",
            "project_id": "proj-explicit",
            "project_dir": "C:/workspace/project-explicit",
        },
    )

    assert interface_module._memory_hook_extra(request) == {
        "query": "hello",
        "project_id": "proj-explicit",
        "project_dir": "C:/workspace/project-explicit",
    }
