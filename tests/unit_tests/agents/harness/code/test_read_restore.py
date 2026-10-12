# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ReadRestoreRail through a real DeepAgent, across a simulated process restart.

agent-core's read-before-edit registry lives in process memory. The headless
implement driver starts a new process for every continuation round, so a file
read in round 1 is refused for editing in round 2 ("File must be read before
editing"). These tests run agent-core's real ReAct loop with its real
``read_file`` / ``edit_file`` / ``write_file`` tools (``SysOperationRail``),
end round 1, clear the process state the way a new process would start, build
a fresh agent and run round 2 in the same session. Only the model is scripted.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    BaseModelClient,
    ModelClientConfig,
    ModelRequestConfig,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
from openjiuwen.harness.tools import filesystem as agent_core_fs

from jiuwenswarm.agents.harness.code import read_restore
from jiuwenswarm.agents.harness.code.rails import read_restore_rail
from jiuwenswarm.agents.harness.code.rails.read_restore_rail import ReadRestoreRail
from jiuwenswarm.agents.harness.code.read_restore import (
    SessionReadStore,
    read_denied_reason,
)

_TEMPLATE = (
    Path(read_restore.__file__).resolve().parents[3] / "resources" / "config.yaml"
)
_PROJECT_POLICY_MODULE = (
    "jiuwenswarm.agents.harness.common.rails.permissions.project_policy"
)


class _ScriptedClient(BaseModelClient):
    __client_name__ = "read-restore-test"

    def __init__(self, responses: list[AssistantMessage]) -> None:
        super().__init__(
            model_config=ModelRequestConfig(model_name="read-restore-test"),
            model_client_config=ModelClientConfig(
                client_provider="OpenAI",
                api_key="test",
                api_base="http://test.invalid",
                verify_ssl=False,
            ),
        )
        self.responses, self.index = responses, 0

    async def invoke(self, *args: Any, **kwargs: Any) -> AssistantMessage:
        result = self.responses[self.index]
        self.index += 1
        return result

    async def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        if False:
            yield None

    async def generate_image(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def generate_speech(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def generate_video(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError


class _RuntimeModel:
    def __init__(self, client: _ScriptedClient) -> None:
        self.client = client
        self.model_client_config = client.model_client_config
        self.model_config = client.model_config

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return await self.client.invoke(*args, **kwargs)


def _call(call_id: str, name: str, args: dict[str, Any]) -> AssistantMessage:
    # call_goal is what the stream event rail strips in production.
    return AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(id=call_id, type="function", name=name, arguments=json.dumps(args))
        ],
        usage_metadata=UsageMetadata(model_name="read-restore-test"),
    )


def _done() -> AssistantMessage:
    return AssistantMessage(
        content="done", usage_metadata=UsageMetadata(model_name="read-restore-test")
    )


class _Recorder(DeepAgentRail):
    """Records the real tool results the agent got (lowest priority: sees final results)."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, Any]] = []

    async def after_tool_call(self, ctx: Any) -> None:
        name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if name in ("read_file", "edit_file", "write_file"):
            self.results.append((name, ctx.inputs.tool_result))


def _new_process() -> None:
    """What a fresh process starts with: agent-core's registry and our mirror empty."""
    agent_core_fs._FILE_READ_REGISTRY.clear()
    read_restore_rail._PROCESS_SEEN.clear()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "Proj"
    root.mkdir()
    (root / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _new_process()
    yield root
    _new_process()


@pytest.fixture()
def store(tmp_path: Path) -> SessionReadStore:
    return SessionReadStore(tmp_path / "agent-root" / "read_restore")


async def _run(
    project: Path,
    script: list[AssistantMessage],
    *,
    store: SessionReadStore,
    conversation: str = "conv-1",
    with_rail: bool = True,
    config: dict[str, Any] | None = None,
) -> tuple[list[tuple[str, Any]], ReadRestoreRail | None]:
    client = _ScriptedClient(script + [_done()])
    recorder = _Recorder()
    rails: list[Any] = [SysOperationRail(), recorder]
    rail = None
    if with_rail:
        cfg = config or {}
        rail = ReadRestoreRail(config_getter=lambda: cfg, store=store)
        rails.append(rail)
    agent = create_deep_agent(
        model=cast(Any, _RuntimeModel(client)),
        rails=rails,
        workspace=str(project),
        enable_read_image_multimodal=False,
        auto_create_workspace=False,
        enable_task_loop=False,
        max_iterations=8,
    )
    await agent.invoke({"query": "work on app.py", "conversation_id": conversation})
    return recorder.results, rail


def _edit(path: str, old: str, new: str, call_id: str) -> AssistantMessage:
    return _call(
        call_id, "edit_file", {"file_path": path, "old_string": old, "new_string": new}
    )


def _last(results: list[tuple[str, Any]], name: str) -> Any:
    found = [out for tool, out in results if tool == name]
    assert found, f"{name} never ran"
    return found[-1]


async def _round1_read_and_edit(
    project: Path, store: SessionReadStore, **kwargs: Any
) -> None:
    app = str(project / "app.py")
    results, _ = await _run(
        project,
        [
            _call("r1", "read_file", {"file_path": app}),
            _edit(app, "x = 1", "x = 3", "e1"),
        ],
        store=store,
        **kwargs,
    )
    assert _last(results, "edit_file").success


# ─── the refusal itself (control) and the restore ────────────────


@pytest.mark.asyncio
async def test_without_the_rail_round_two_edit_is_refused(project: Path, store) -> None:
    await _round1_read_and_edit(project, store, with_rail=False)
    _new_process()
    app = str(project / "app.py")
    results, _ = await _run(
        project, [_edit(app, "x = 3", "x = 4", "e2")], store=store, with_rail=False
    )
    out = _last(results, "edit_file")
    assert not out.success and "must be read before editing" in str(out.error)
    assert "x = 3" in (project / "app.py").read_text()


@pytest.mark.asyncio
async def test_round_two_edit_succeeds_after_restore(project: Path, store) -> None:
    await _round1_read_and_edit(project, store)
    _new_process()
    app = str(project / "app.py")
    results, rail = await _run(
        project, [_edit(app, "x = 3", "x = 4", "e2")], store=store
    )
    assert _last(results, "edit_file").success, _last(results, "edit_file").error
    assert (project / "app.py").read_text() == "x = 4\ny = 2\n"
    assert rail is not None and rail.stats.get("restored") == 1
    # The model made no read_file call in round 2; the restore read is the rail's.
    assert not [out for tool, out in results if tool == "read_file"]


@pytest.mark.asyncio
async def test_read_only_round_one_then_edit(project: Path, store) -> None:
    app = str(project / "app.py")
    await _run(project, [_call("r1", "read_file", {"file_path": app})], store=store)
    _new_process()
    results, rail = await _run(
        project, [_edit(app, "x = 1", "x = 9", "e2")], store=store
    )
    assert _last(results, "edit_file").success
    assert rail.stats.get("restored") == 1


@pytest.mark.asyncio
async def test_file_changed_outside_the_session_is_not_restored(
    project: Path, store
) -> None:
    await _round1_read_and_edit(project, store)
    _new_process()
    (project / "app.py").write_text("x = 3\ny = 2\nz = 5\n", encoding="utf-8")
    app = str(project / "app.py")
    results, rail = await _run(
        project, [_edit(app, "x = 3", "x = 4", "e2")], store=store
    )
    out = _last(results, "edit_file")
    assert not out.success and "must be read before editing" in str(out.error)
    assert rail.stats.get("stale") == 1 and not rail.stats.get("restored")
    assert "x = 3" in (project / "app.py").read_text()


@pytest.mark.asyncio
async def test_other_session_is_not_restored(project: Path, store) -> None:
    await _round1_read_and_edit(project, store, conversation="conv-a")
    _new_process()
    app = str(project / "app.py")
    results, rail = await _run(
        project,
        [_edit(app, "x = 3", "x = 4", "e2")],
        store=store,
        conversation="conv-b",
    )
    assert not _last(results, "edit_file").success
    assert rail.stats.get("no_record") == 1


@pytest.mark.asyncio
async def test_disabled_config_is_a_no_op(project: Path, store) -> None:
    off = {"modes": {"code": {"read_restore": {"enabled": False}}}}
    await _round1_read_and_edit(project, store, config=off)
    assert not list(store.root().glob("*.json"))  # nothing recorded
    _new_process()
    app = str(project / "app.py")
    results, rail = await _run(
        project, [_edit(app, "x = 3", "x = 4", "e2")], store=store, config=off
    )
    assert not _last(results, "edit_file").success
    assert not rail.stats


@pytest.mark.asyncio
async def test_relative_read_then_absolute_edit(project: Path, store) -> None:
    await _run(
        project, [_call("r1", "read_file", {"file_path": "app.py"})], store=store
    )
    _new_process()
    app = str((project / "app.py").resolve())
    results, rail = await _run(
        project, [_edit(app, "x = 1", "x = 8", "e2")], store=store
    )
    assert _last(results, "edit_file").success, _last(results, "edit_file").error
    assert rail.stats.get("restored") == 1


@pytest.mark.asyncio
async def test_read_then_edit_under_another_spelling(project: Path, store) -> None:
    from openjiuwen.core.sys_operation.cwd import get_cwd

    app = str(project / "app.py")
    await _run(project, [_call("r1", "read_file", {"file_path": app})], store=store)
    _new_process()
    # Round 2 uses another spelling of the same file (realpath of a symlinked tmp).
    other = str(Path(app).resolve())
    if other == app:
        link = project.parent / "ProjLink"
        link.symlink_to(project)
        other = str(link / "app.py")
    results, rail = await _run(
        project, [_edit(other, "x = 1", "x = 7", "e2")], store=store
    )
    assert _last(results, "edit_file").success, _last(results, "edit_file").error
    assert rail.stats.get("restored") == 1
    assert get_cwd() is not None


# ─── write_file mirrors agent-core's whole-file rule ──────────────


@pytest.mark.asyncio
async def test_write_after_full_read_is_restored(project: Path, store) -> None:
    app = str(project / "app.py")
    await _run(project, [_call("r1", "read_file", {"file_path": app})], store=store)
    _new_process()
    results, rail = await _run(
        project,
        [_call("w2", "write_file", {"file_path": app, "content": "x = 0\n"})],
        store=store,
    )
    assert _last(results, "write_file").success, _last(results, "write_file").error
    assert (project / "app.py").read_text() == "x = 0\n"
    assert rail.stats.get("restored") == 1


@pytest.mark.asyncio
async def test_write_after_partial_read_is_not_restored(project: Path, store) -> None:
    app = str(project / "app.py")
    await _run(
        project,
        [_call("r1", "read_file", {"file_path": app, "offset": 0, "limit": 1})],
        store=store,
    )
    _new_process()
    results, rail = await _run(
        project,
        [_call("w2", "write_file", {"file_path": app, "content": "x = 0\n"})],
        store=store,
    )
    assert not _last(results, "write_file").success
    assert rail.stats.get("partial_for_write") == 1
    assert (project / "app.py").read_text() == "x = 1\ny = 2\n"


# ─── read policy ─────────────────────────────────────────────────


@dataclass
class _Deny:
    raw: str


class _StubProjectPolicy:
    """Stand-in for the shell-policy package's loader (not merged on this branch)."""

    def __init__(self, root: str, denies: list[str]) -> None:
        self.root, self.error = root, None
        self.deny_paths = [_Deny(d) for d in denies]

    def path_denied(self, path: Any, axis: str) -> _Deny | None:
        text = Path(path).as_posix()
        for d in self.deny_paths:
            if axis == "read" and text.startswith(f"{self.root}/{d.raw}"):
                return d
        return None


@pytest.mark.asyncio
async def test_project_policy_deny_read_blocks_the_restore(
    project: Path, store, monkeypatch
) -> None:
    await _round1_read_and_edit(project, store)
    module = types.ModuleType(_PROJECT_POLICY_MODULE)
    module.load_project_policy = lambda root, config=None: _StubProjectPolicy(
        str(Path(root)), ["app.py"]
    )
    monkeypatch.setitem(sys.modules, _PROJECT_POLICY_MODULE, module)
    _new_process()
    app = str(project / "app.py")
    results, rail = await _run(
        project, [_edit(app, "x = 3", "x = 4", "e2")], store=store
    )
    assert not _last(results, "edit_file").success
    assert rail.stats.get("denied") == 1


def test_permission_engine_read_deny_blocks_the_restore(project: Path) -> None:
    perms = dict(yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))["permissions"])
    perms["enabled"] = True
    guard = dict(perms["file_guard"])
    guard["paths"] = list(guard.get("paths") or []) + [
        {
            "path": "**/app.py",
            "match": "glob",
            "read": "deny",
            "write": "deny",
            "exec": "deny",
        }
    ]
    perms["file_guard"] = guard
    app = str(project / "app.py")
    assert read_denied_reason(
        app, project_dir=str(project), permissions=perms, session_id="s"
    )
    perms["enabled"] = False
    assert (
        read_denied_reason(
            app, project_dir=str(project), permissions=perms, session_id="s"
        )
        is None
    )


# ─── store ───────────────────────────────────────────────────────


def test_store_survives_corrupt_files_and_caps_entries(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionReadStore(tmp_path / "rr")
    store.root().mkdir(parents=True)
    (store.root() / "s1.json").write_text("{not json", encoding="utf-8")
    assert store.load("s1") == {}
    monkeypatch.setattr(read_restore, "MAX_FILES_PER_SESSION", 3)
    for i in range(5):
        f = tmp_path / f"f{i}.txt"
        f.write_text(f"{i}\n", encoding="utf-8")
        read_restore.record_read(store, "s1", str(f), offset=0, line_count=1)
    files = store.load("s1")
    assert len(files) == 3 and str(tmp_path / "f4.txt") in files
    # a record from another session id is never returned
    assert store.load("s2") == {}


def test_store_is_outside_the_project_by_default() -> None:
    root = read_restore.default_store_root()
    assert root.name == "read_restore"
