"""A copy (session.fork) of an External Harness session forks the CLI session."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Self
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


@pytest.fixture
def launch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The expert's runtime configuration, with the host's CLI settings merged as is."""
    runtime_config: dict[str, Any] = {}
    monkeypatch.setattr(
        external_harness_helpers, "load_persisted_external_harness_binding",
        AsyncMock(return_value={"runtime": {"config": runtime_config}}),
    )
    monkeypatch.setattr(
        external_harness_helpers, "_merge_host_external_cli_config", lambda provider, config: config,
    )
    return runtime_config


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
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _bind(metadata, "claudecode", "coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "claudecode", "coder", "source", {"session_id": "claude-source", "resumed": True},
    )
    calls: list[tuple[Any, ...]] = []

    async def fork(source_session: str, source_cwd: Path, target_cwd: Path, env: dict[str, str]) -> str:
        calls.append((source_session, source_cwd, target_cwd))
        return "claude-forked"

    monkeypatch.setattr(external_harness_helpers, "_fork_claude_session", fork)

    assert await external_harness_helpers.fork_external_harness_session("source", "target") is True

    assert calls == [("claude-source", Path("/work/source"), (tmp_path / "target-work").resolve())]
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
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(metadata, "codex", "codex-coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "codex", "codex-coder", "source",
        {"thread_id": "thread-source", "resumed": True, "dynamic_tools": ["t"]},
    )
    fork = AsyncMock(return_value="thread-forked")
    monkeypatch.setattr(external_harness_helpers, "_fork_codex_thread", fork)
    launch["codex_bin"] = "/opt/codex"

    assert await external_harness_helpers.fork_external_harness_session("source", "target")

    assert fork.await_args.args[0] == "thread-source"
    assert fork.await_args.args[1].codex_bin == "/opt/codex"
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
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(metadata, "claudecode", "coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "claudecode", "coder", "source", {"session_id": "claude-source"},
    )

    with pytest.raises(ValueError):
        await external_harness_helpers.fork_external_harness_session("source", "target", at_message=True)

    async def broken(*_args: Any) -> str:
        raise FileNotFoundError("Session claude-source not found")

    monkeypatch.setattr(external_harness_helpers, "_fork_claude_session", broken)
    with pytest.raises(ValueError, match="not found"):
        await external_harness_helpers.fork_external_harness_session("source", "target")
    assert "session_runtime_binding" not in metadata["target"]


def _write_claude_transcript(projects: Path, cwd: Path) -> str:
    from claude_agent_sdk import project_key_for_directory

    session_id = str(uuid.uuid4())
    user_id, reply_id = str(uuid.uuid4()), str(uuid.uuid4())
    project = projects / project_key_for_directory(cwd)
    project.mkdir(parents=True)
    lines = [
        {"type": "user", "uuid": user_id, "parentUuid": None, "sessionId": session_id,
         "cwd": str(cwd), "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "uuid": reply_id, "parentUuid": user_id, "sessionId": session_id,
         "cwd": str(cwd), "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    ]
    (project / f"{session_id}.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return session_id


@pytest.mark.asyncio
async def test_claude_fork_lands_in_the_target_folders_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import project_key_for_directory

    # The CLI's environment names the config folder; the process's does not.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    source_cwd = tmp_path / "source"
    target_cwd = tmp_path / "target"
    source_cwd.mkdir()
    source_id = _write_claude_transcript(tmp_path / "claude" / "projects", source_cwd)
    project = tmp_path / "claude" / "projects" / project_key_for_directory(source_cwd)

    forked = await external_harness_helpers._fork_claude_session(source_id, source_cwd, target_cwd, env)

    assert forked != source_id
    target_file = tmp_path / "claude" / "projects" / project_key_for_directory(target_cwd) / f"{forked}.jsonl"
    assert target_file.is_file()
    assert not (project / f"{forked}.jsonl").exists()
    assert (project / f"{source_id}.jsonl").is_file()
    entries = [json.loads(line) for line in target_file.read_text().splitlines() if line.strip()]
    assert {entry.get("sessionId") for entry in entries if "sessionId" in entry} == {forked}
    assert [entry["message"]["role"] for entry in entries if "message" in entry] == ["user", "assistant"]
    assert not (tmp_path / "home").exists()


@pytest.mark.asyncio
async def test_claude_copy_forks_in_the_experts_config_folder(
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import project_key_for_directory

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    expert_projects = tmp_path / "expert-claude" / "projects"
    launch["env"] = {"CLAUDE_CONFIG_DIR": str(tmp_path / "expert-claude")}
    _bind(metadata, "claudecode", "coder")
    source_cwd = tmp_path / "source"
    metadata["source"]["project_dir"] = str(source_cwd)
    source_id = _write_claude_transcript(expert_projects, source_cwd)
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "claudecode", "coder", "source", {"session_id": source_id},
    )

    assert await external_harness_helpers.fork_external_harness_session("source", "target")

    forked = metadata["target"]["session_harness_checkpoint"]["checkpoint"]["data"]["session_id"]
    target_key = project_key_for_directory((tmp_path / "target-work").resolve())
    assert (expert_projects / target_key / f"{forked}.jsonl").is_file()
    assert not (tmp_path / "home").exists()


class _RecordingCodex:
    """``openai_codex`` with an App Server that records how it is started."""

    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []
        self.forks: list[tuple[str, str | None]] = []
        recorder = self

        class CodexConfig:
            def __init__(self, **kwargs: Any) -> None:
                recorder.configs.append(kwargs)

        class AsyncCodex:
            def __init__(self, *, config: Any) -> None:
                pass

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def thread_fork(self, thread_id: str, *, cwd: str | None = None) -> Any:
                recorder.forks.append((thread_id, cwd))
                return SimpleNamespace(id="thread-forked")

        self.module = SimpleNamespace(CodexConfig=CodexConfig, AsyncCodex=AsyncCodex)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host_codex_home", "expected"),
    [(None, "/expert/codex-home"), ("/host/codex-home", "/host/codex-home")],
    ids=["provider-only", "host-set"],
)
async def test_codex_fork_runs_with_the_environment_of_a_normal_start(
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    host_codex_home: str | None, expected: str,
) -> None:
    codex = _RecordingCodex()
    monkeypatch.setitem(sys.modules, "openai_codex", codex.module)
    if host_codex_home is None:
        monkeypatch.delenv("CODEX_HOME", raising=False)
    else:
        monkeypatch.setenv("CODEX_HOME", host_codex_home)
    launch.update(
        env={"CODEX_HOME": "/expert/codex-home", "EXPERT_ONLY": "1"},
        codex_bin="/opt/codex",
        config_overrides=["model_provider=\"local\""],
    )
    _bind(metadata, "codex", "codex-coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "codex", "codex-coder", "source", {"thread_id": "thread-source"},
    )

    assert await external_harness_helpers.fork_external_harness_session("source", "target")

    [config] = codex.configs
    target_cwd = str((tmp_path / "target-work").resolve())
    # A normal start merges the process env, the provider's env, then the
    # context env (the process env again), so a host value wins.
    assert config["env"]["CODEX_HOME"] == expected
    assert config["env"]["EXPERT_ONLY"] == "1"
    assert config["env"]["PATH"] == os.environ["PATH"]
    assert config["codex_bin"] == "/opt/codex"
    assert config["config_overrides"] == ('model_provider="local"',)
    assert config["cwd"] == target_cwd
    assert codex.forks == [("thread-source", target_cwd)]
    assert metadata["target"]["session_harness_checkpoint"]["checkpoint"]["data"]["thread_id"] == "thread-forked"


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

    async def fork_session_via_store(store: Any, session: str, *, directory: str) -> SimpleNamespace:
        events.append("fork")
        if await store.load({"project_key": Path(directory).name, "session_id": session}) is None:
            raise FileNotFoundError(f"Session {session} not found")
        return SimpleNamespace(session_id=forked)

    sdk.fork_session_via_store = fork_session_via_store  # type: ignore[attr-defined]
    sdk.project_key_for_directory = lambda path: Path(path).name  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)


@pytest.mark.asyncio
async def test_claude_fork_exposes_managed_sdk_paths_before_importing_the_sdk(
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
    work = tmp_path / "work"
    (tmp_path / "claude" / "projects" / "work").mkdir(parents=True)
    (tmp_path / "claude" / "projects" / "work" / "s.jsonl").write_text("{}\n")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}

    assert await external_harness_helpers._fork_claude_session("s", work, work, env) == "claude-forked"
    assert events == ["activate", "import", "fork"]


@pytest.mark.asyncio
async def test_missing_source_transcript_reports_where_it_looked(
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    events: list[str] = []
    _fake_claude_sdk(monkeypatch, events)
    (tmp_path / "claude" / "projects").mkdir(parents=True)
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}

    with pytest.raises(FileNotFoundError, match="claude-source.jsonl not found under"):
        await external_harness_helpers._fork_claude_session(
            "claude-source", tmp_path / "source", tmp_path / "target", env,
        )

    # The copy is refused with that reason.
    launch["env"] = env
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

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            closed.append(True)

        async def thread_fork(self, _thread: str, *, cwd: str) -> Any:
            await asyncio.Event().wait()

    codex = ModuleType("openai_codex")
    codex.AsyncCodex = HangingCodex  # type: ignore[attr-defined]
    codex.CodexConfig = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai_codex", codex)
    monkeypatch.setattr(external_harness_helpers, "_CODEX_FORK_TIMEOUT_SECONDS", 0.05)
    harness_config = SimpleNamespace(codex_bin=None, config_overrides=())

    with pytest.raises(TimeoutError, match="did not finish within"):
        await external_harness_helpers._fork_codex_thread("thread", harness_config, {}, tmp_path)
    # The short-lived App Server is shut down on timeout.
    assert closed == [True]


@pytest.mark.asyncio
async def test_a_copy_at_a_message_is_refused_before_anything_is_copied(
    metadata: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common import session_ops_service
    from jiuwenswarm.runtime.session_provisioner import (
        RuntimeSessionProvisioner,
        SessionProvisionError,
    )

    _bind(metadata, "claudecode", "coder")
    copied: list[Any] = []
    monkeypatch.setattr(session_ops_service, "fork_session", lambda **kwargs: copied.append(kwargs))
    created = AsyncMock(return_value="t")
    provisioner = RuntimeSessionProvisioner(
        agent_manager=SimpleNamespace(get_agent_nowait=lambda _channel: None, create_session=created),
        plan_controller=SimpleNamespace(),
    )

    with pytest.raises(SessionProvisionError) as raised:
        await provisioner.prepare_session_fork(
            SessionForkInput(channel_id="web", source_session_id="source", cutoff_message_id="m-3")
        )

    assert raised.value.code == "BAD_REQUEST"
    assert copied == []
    created.assert_not_awaited()


def test_a_cleanup_that_fails_is_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    import jiuwenswarm.common.utils as common_utils
    from jiuwenswarm.runtime import session_provisioner

    monkeypatch.setattr(common_utils, "get_agent_sessions_dir", lambda: tmp_path)
    (tmp_path / "t").mkdir()

    def refuse(path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        raise PermissionError("busy")

    monkeypatch.setattr(session_provisioner.shutil, "rmtree", refuse)
    with caplog.at_level("WARNING"):
        session_provisioner.RuntimeSessionProvisioner._remove_created_session_dirs(["t", "missing"])

    assert [record.getMessage() for record in caplog.records] == [
        "Failed to remove half-created session dir t: busy",
    ]


@pytest.mark.asyncio
async def test_a_claude_fork_no_copy_is_bound_to_is_removed(
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import project_key_for_directory

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    projects = tmp_path / "claude" / "projects"
    launch["env"] = {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    _bind(metadata, "claudecode", "coder")
    source_cwd = tmp_path / "source"
    metadata["source"]["project_dir"] = str(source_cwd)
    source_id = _write_claude_transcript(projects, source_cwd)
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "claudecode", "coder", "source", {"session_id": source_id},
    )

    def broken_write(**_fields: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(external_harness_helpers, "update_session_metadata", broken_write)

    with pytest.raises(OSError, match="disk full"):
        await external_harness_helpers.fork_external_harness_session("source", "target")

    target_project = projects / project_key_for_directory((tmp_path / "target-work").resolve())
    assert list(target_project.glob("*.jsonl")) == []
    assert (projects / project_key_for_directory(source_cwd) / f"{source_id}.jsonl").is_file()


@pytest.mark.asyncio
async def test_a_codex_fork_no_copy_is_bound_to_is_archived(
    metadata: dict[str, dict[str, Any]], launch: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = _RecordingCodex()
    archived: list[str] = []

    async def thread_archive(_self: Any, thread_id: str) -> None:
        archived.append(thread_id)

    codex.module.AsyncCodex.thread_archive = thread_archive
    monkeypatch.setitem(sys.modules, "openai_codex", codex.module)
    _bind(metadata, "codex", "codex-coder")
    metadata["source"]["session_harness_checkpoint"] = _checkpoint(
        "codex", "codex-coder", "source", {"thread_id": "thread-source"},
    )

    def broken_write(**_fields: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(external_harness_helpers, "update_session_metadata", broken_write)

    with pytest.raises(OSError, match="disk full"):
        await external_harness_helpers.fork_external_harness_session("source", "target")

    assert archived == ["thread-forked"]
    assert [config["env"]["PATH"] for config in codex.configs] == [os.environ["PATH"]] * 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
@pytest.mark.asyncio
async def test_a_forked_transcript_is_private_and_never_an_existing_file(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        store = external_harness_helpers._ClaudeProjectFiles(tmp_path, "target")
        await store.append({"project_key": "target", "session_id": "fork-1"}, [{"type": "user"}])
        await store.append({"project_key": "target", "session_id": "fork-1"}, [{"type": "assistant"}])
        written = tmp_path / "target" / "fork-1.jsonl"
        assert written.stat().st_mode & 0o777 == 0o600
        assert [json.loads(line)["type"] for line in written.read_text().splitlines()] == ["user", "assistant"]

        # A file or link already at the fork's path is neither appended to nor followed.
        (tmp_path / "target" / "fork-2.jsonl").write_text("kept\n")
        outside = tmp_path / "outside.jsonl"
        outside.write_text("outside\n")
        (tmp_path / "target" / "fork-3.jsonl").symlink_to(outside)
        fresh = external_harness_helpers._ClaudeProjectFiles(tmp_path, "target")
        for session in ("fork-2", "fork-3"):
            with pytest.raises(FileExistsError):
                await fresh.append({"project_key": "target", "session_id": session}, [{"type": "user"}])
        assert (tmp_path / "target" / "fork-2.jsonl").read_text() == "kept\n"
        assert outside.read_text() == "outside\n"
    finally:
        os.umask(previous)
