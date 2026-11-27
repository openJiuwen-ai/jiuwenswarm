"""A copy (session.fork) of an External Harness session forks the CLI session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
import uuid

import pytest

from jiuwenswarm.runtime.session_provisioner import SessionForkInput
from jiuwenswarm.server.runtime.agent_adapter import external_harness_helpers


def _checkpoint(provider: str, template: str, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_template_name": template,
        "provider_name": provider,
        "checkpoint": {
            "provider": provider,
            "schema_version": "1",
            "agent_id": f"external_harness:{provider}:{template}",
            "host_session_id": session_id,
            "checkpoint_id": "checkpoint-7",
            "sequence": 7,
            "data": data,
            "provider_session_id": None,
            "revision": "rev-7",
        },
    }


@pytest.fixture
def metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, dict[str, Any]]:
    store: dict[str, dict[str, Any]] = {
        "target": {"project_dir": str(tmp_path / "target-work")},
    }

    def get_session_metadata(session_id: str, cache_bust: bool = False) -> dict[str, Any]:
        return dict(store.get(session_id, {}))

    def update_session_metadata(*, session_id: str, **fields: Any) -> None:
        for key in ("touch_last_message_at", "cache_bust", "sync_write"):
            fields.pop(key, None)
        store.setdefault(session_id, {}).update(fields)

    monkeypatch.setattr(external_harness_helpers, "get_session_metadata", get_session_metadata)
    monkeypatch.setattr(external_harness_helpers, "update_session_metadata", update_session_metadata)
    return store


def _bind(store: dict[str, dict[str, Any]], provider: str, template: str) -> None:
    store["source"] = {
        "project_dir": "/work/source",
        "session_runtime_binding": {
            "kind": "external_harness",
            "agent_template_name": template,
            "provider_name": provider,
        },
    }


@pytest.mark.asyncio
async def test_claude_code_copy_resumes_from_a_fork_of_the_cli_session(
    metadata: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _bind(metadata, "claudecode", "coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "claudecode", "coder", "source", {"session_id": "claude-source", "resumed": True},
    )
    calls: list[tuple[Any, ...]] = []

    def fork(source_session: str, source_cwd: Path, target_cwd: Path) -> str:
        calls.append((source_session, source_cwd, target_cwd))
        return "claude-forked"

    monkeypatch.setattr(external_harness_helpers, "_fork_claude_session", fork)

    assert await external_harness_helpers.fork_external_harness_session("source", "target") is True

    assert calls == [
        ("claude-source", Path("/work/source").resolve(), (tmp_path / "target-work").resolve())
    ]
    target = metadata["target"]
    assert target["session_runtime_binding"] == {
        "kind": "external_harness",
        "agent_template_name": "coder",
        "provider_name": "claudecode",
    }
    stored = target["session_harness_checkpoint"]
    assert stored["agent_template_name"] == "coder"
    checkpoint = stored["checkpoint"]
    assert checkpoint["host_session_id"] == "target"
    assert checkpoint["agent_id"] == "external_harness:claudecode:coder"
    assert checkpoint["data"] == {"session_id": "claude-forked", "resumed": False}
    assert checkpoint["provider_session_id"] == "claude-forked"
    assert checkpoint["sequence"] == 0
    assert checkpoint["revision"] is None
    # The source keeps its own CLI session.
    assert metadata["source"]["session_harness_checkpoint"]["checkpoint"]["data"]["session_id"] == "claude-source"
    loaded = external_harness_helpers._load_external_harness_checkpoint(
        session_id="target", agent_template_name="coder", provider_name="claudecode",
    )
    assert loaded is not None and loaded.data["session_id"] == "claude-forked"


@pytest.mark.asyncio
async def test_codex_copy_resumes_from_a_forked_thread(
    metadata: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(metadata, "codex", "codex-coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "codex", "codex-coder", "source",
        {"thread_id": "thread-source", "resumed": True, "dynamic_tools": ["t"]},
    )
    fork = AsyncMock(return_value="thread-forked")
    monkeypatch.setattr(external_harness_helpers, "_fork_codex_thread", fork)
    monkeypatch.setattr(
        external_harness_helpers, "load_persisted_external_harness_binding", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        external_harness_helpers, "_merge_host_external_cli_config",
        lambda provider, config: {**config, "codex_bin": "/opt/codex"},
    )

    assert await external_harness_helpers.fork_external_harness_session("source", "target")

    assert fork.await_args.args[:2] == ("thread-source", "/opt/codex")
    target = metadata["target"]
    assert target["session_runtime_binding"]["provider_name"] == "codex"
    assert target["session_harness_checkpoint"]["checkpoint"]["data"] == {
        "thread_id": "thread-forked", "resumed": False, "dynamic_tools": ["t"],
    }


@pytest.mark.asyncio
async def test_copy_of_a_session_with_no_turn_is_bound_without_a_cli_session(
    metadata: dict[str, dict[str, Any]],
) -> None:
    _bind(metadata, "claudecode", "coder")

    assert await external_harness_helpers.fork_external_harness_session("source", "target")

    assert metadata["target"]["session_runtime_binding"]["agent_template_name"] == "coder"
    assert "session_harness_checkpoint" not in metadata["target"]


@pytest.mark.asyncio
async def test_copy_of_a_session_of_swarms_agent_is_left_alone(
    metadata: dict[str, dict[str, Any]],
) -> None:
    metadata["source"] = {}

    assert await external_harness_helpers.fork_external_harness_session("source", "target") is False
    assert await external_harness_helpers.fork_external_harness_session(
        "source", "target", at_message=True,
    ) is False
    assert "session_runtime_binding" not in metadata["target"]


@pytest.mark.asyncio
async def test_copy_is_refused_at_a_message_or_after_a_failed_fork(
    metadata: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(metadata, "claudecode", "coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "claudecode", "coder", "source", {"session_id": "claude-source"},
    )

    with pytest.raises(ValueError):
        await external_harness_helpers.fork_external_harness_session("source", "target", at_message=True)

    def broken(*_args: Any) -> str:
        raise FileNotFoundError("Session claude-source not found")

    monkeypatch.setattr(external_harness_helpers, "_fork_claude_session", broken)
    with pytest.raises(ValueError, match="not found"):
        await external_harness_helpers.fork_external_harness_session("source", "target")
    assert "session_runtime_binding" not in metadata["target"]


def test_claude_fork_lands_in_the_target_folders_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import project_key_for_directory

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    source_cwd = tmp_path / "source"
    target_cwd = tmp_path / "target"
    source_cwd.mkdir()
    source_id = str(uuid.uuid4())
    user_id, reply_id = str(uuid.uuid4()), str(uuid.uuid4())
    project = tmp_path / "claude" / "projects" / project_key_for_directory(source_cwd)
    project.mkdir(parents=True)
    lines = [
        {"type": "user", "uuid": user_id, "parentUuid": None, "sessionId": source_id,
         "cwd": str(source_cwd), "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "uuid": reply_id, "parentUuid": user_id, "sessionId": source_id,
         "cwd": str(source_cwd), "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    ]
    (project / f"{source_id}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    forked = external_harness_helpers._fork_claude_session(source_id, source_cwd, target_cwd)

    assert forked != source_id
    target_file = tmp_path / "claude" / "projects" / project_key_for_directory(target_cwd) / f"{forked}.jsonl"
    assert target_file.is_file()
    assert not (project / f"{forked}.jsonl").exists()
    assert (project / f"{source_id}.jsonl").is_file()
    entries = [json.loads(line) for line in target_file.read_text().splitlines() if line.strip()]
    assert {entry.get("sessionId") for entry in entries if "sessionId" in entry} == {forked}


@pytest.mark.asyncio
async def test_failed_cli_fork_removes_the_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service
    from jiuwenswarm.runtime.session_provisioner import RuntimeSessionProvisioner, SessionProvisionError
    import jiuwenswarm.common.utils as common_utils

    monkeypatch.setattr(common_utils, "get_agent_sessions_dir", lambda: tmp_path)

    def fork_session(**kwargs: Any) -> dict[str, str]:
        (tmp_path / kwargs["target_session_id"]).mkdir()
        return {"session_id": kwargs["target_session_id"], "source_session_id": "s", "title": "copy"}

    seen: dict[str, Any] = {}

    async def fork_cli(
        source: str, target: str, *, at_message: bool, session_equipment_override: Any = None,
    ) -> bool:
        seen.update(
            source=source, target=target, at_message=at_message,
            override=session_equipment_override,
        )
        raise ValueError("Forking the CLI session failed: boom")

    monkeypatch.setattr(session_ops_service, "fork_session", fork_session)
    monkeypatch.setattr(external_harness_helpers, "fork_external_harness_session", fork_cli)
    provisioner = RuntimeSessionProvisioner(
        agent_manager=SimpleNamespace(get_agent_nowait=lambda _channel: None),
        plan_controller=SimpleNamespace(),
    )

    with pytest.raises(SessionProvisionError) as raised:
        await provisioner.prepare_session_fork(
            SessionForkInput(
                channel_id="web", source_session_id="s", target_session_id="t",
                session_equipment_override={"agent_template_name": "other"},
            )
        )

    assert raised.value.code == "BAD_REQUEST"
    assert seen == {
        "source": "s", "target": "t", "at_message": False,
        "override": {"agent_template_name": "other"},
    }
    assert not (tmp_path / "t").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [{"agent_template_name": "other"}, {"agent_template_name": ""}, {"agent_template_name": None}],
)
async def test_copy_is_refused_when_the_override_switches_or_clears_the_expert(
    metadata: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, override: dict[str, Any],
) -> None:
    _bind(metadata, "claudecode", "coder")
    monkeypatch.setattr(
        external_harness_helpers.equipment, "resolve_equipment_runtime_id", lambda _kind, name: name,
    )

    with pytest.raises(ValueError, match="coder"):
        await external_harness_helpers.fork_external_harness_session(
            "source", "target", session_equipment_override=override,
        )
    assert "session_runtime_binding" not in metadata["target"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [None, {}, {"plugin_names": ["p"], "mcp": []}, {"agent_template_name": " hub-uuid "}],
)
async def test_copy_keeps_its_expert_when_the_override_does(
    metadata: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, override: dict[str, Any] | None,
) -> None:
    _bind(metadata, "claudecode", "coder")
    # A Hub asset UUID resolves to the bound local package id.
    monkeypatch.setattr(
        external_harness_helpers.equipment, "resolve_equipment_runtime_id",
        lambda _kind, name: "coder" if name == "hub-uuid" else name,
    )

    assert await external_harness_helpers.fork_external_harness_session(
        "source", "target", session_equipment_override=override,
    )
    assert metadata["target"]["session_runtime_binding"]["agent_template_name"] == "coder"


def _fake_claude_sdk(
    monkeypatch: pytest.MonkeyPatch, events: list[str], *, forked: str = "claude-forked",
) -> None:
    from jiuwenswarm.common import external_cli_runtime

    monkeypatch.setattr(
        external_cli_runtime, "activate_external_cli_runtime_paths", lambda: events.append("activate"),
    )
    sdk = ModuleType("claude_agent_sdk")

    def fork_session(_session: str, *, directory: str) -> SimpleNamespace:
        events.append("fork")
        return SimpleNamespace(session_id=forked)

    sdk.fork_session = fork_session  # type: ignore[attr-defined]
    sdk.project_key_for_directory = lambda path: Path(path).name  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)


def test_claude_fork_exposes_managed_sdk_paths_before_importing_the_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    events: list[str] = []
    _fake_claude_sdk(monkeypatch, events)
    original_import = __import__

    def tracking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "claude_agent_sdk":
            events.append("import")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", tracking_import)
    same_folder = tmp_path / "work"

    assert external_harness_helpers._fork_claude_session("s", same_folder, same_folder) == "claude-forked"
    assert events == ["activate", "import", "fork"]


@pytest.mark.asyncio
async def test_missing_forked_transcript_reports_where_it_looked(
    metadata: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    events: list[str] = []
    _fake_claude_sdk(monkeypatch, events)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude" / "projects").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="claude-forked.jsonl not found under"):
        external_harness_helpers._fork_claude_session("s", tmp_path / "source", tmp_path / "target")

    # Across asyncio.to_thread the copy is refused with that reason, not a StopIteration.
    _bind(metadata, "claudecode", "coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "claudecode", "coder", "source", {"session_id": "claude-source"},
    )
    with pytest.raises(ValueError, match="not found under"):
        await external_harness_helpers.fork_external_harness_session("source", "target")
    assert events[:2] == ["activate", "fork"]
    assert "session_runtime_binding" not in metadata["target"]


@pytest.mark.asyncio
async def test_codex_fork_gives_up_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    closed: list[bool] = []

    class HangingCodex:
        def __init__(self, *, config: Any) -> None:
            self.config = config

        async def __aenter__(self) -> "HangingCodex":
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            closed.append(True)

        async def thread_fork(self, _thread: str, *, cwd: str) -> Any:
            await asyncio.Event().wait()

    codex = ModuleType("openai_codex")
    codex.AsyncCodex = HangingCodex  # type: ignore[attr-defined]
    codex.CodexConfig = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai_codex", codex)
    monkeypatch.setattr(external_harness_helpers, "_CODEX_FORK_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError, match="did not finish within"):
        await external_harness_helpers._fork_codex_thread("thread", None, tmp_path)
    # The short-lived App Server is shut down on timeout.
    assert closed == [True]
