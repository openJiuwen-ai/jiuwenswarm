# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib
import io
import json
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import app
from jiuwenswarm.channels.process_cli.client import InProcessRuntimeClient
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.context_compaction import (
    ContextCompactInput,
    ContextCompactResult,
)
from jiuwenswarm.runtime.agent_catalog import (
    AgentCatalogInput,
    AgentCatalogResult,
    AgentDescriptor,
    AgentToolDescriptor,
    AgentToolsResult,
)
from jiuwenswarm.runtime.mcp_catalog import (
    McpCatalogListInput,
    McpCatalogListResult,
    McpCatalogShowInput,
    McpCatalogShowResult,
    McpServerDescriptor,
)
from jiuwenswarm.runtime.model_catalog import (
    ModelCatalogError,
    ModelCatalogResult,
    ModelSelectionResult,
    RuntimeModelDescriptor,
)
from jiuwenswarm.runtime.memory_catalog import (
    MemoryListResult,
    MemoryLocationsResult,
    MemoryScopeInput,
    MemoryStatusResult,
)
from jiuwenswarm.runtime.permission_catalog import (
    PermissionCatalogError,
    PermissionLayerSnapshot,
    PermissionRuleSnapshot,
    PermissionSnapshotInput,
    PermissionSnapshotResult,
    PermissionToolSnapshot,
)
from jiuwenswarm.runtime.request import resolve_request_runtime_mode
from jiuwenswarm.runtime.session_catalog import SessionListResult, SessionSummary
from jiuwenswarm.runtime.session_rewind import (
    SessionRewindAction,
    SessionRewindContextPolicy,
    SessionRewindInput,
    SessionRewindListInput,
    SessionRewindListResult,
    SessionRewindResult,
    SessionRewindTurn,
)
from jiuwenswarm.runtime.session_provisioner import (
    SessionCreateResult,
    SessionDeleteResult,
    SessionDescriptor,
    SessionForkResult,
    SessionProvisionCommitTiming,
    SessionProvisionState,
    SessionSwitchResult,
)


class FakeClient:
    latest: FakeClient | None = None
    delay = 0.0

    def __init__(self) -> None:
        type(self).latest = self
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def create_or_resume_session(
        self,
        *,
        channel_id: str,
        session_id: str | None,
    ) -> str:
        self.calls.append(f"session:{channel_id}:{session_id or ''}")
        return session_id or "runtime-session"

    async def stream(self, request):
        self.calls.append(f"stream:{request.session_id}")
        if self.delay:
            await asyncio.sleep(self.delay)
        yield RuntimeEvent(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            payload={"event_type": "chat.delta", "delta": "hello"},
        )
        yield RuntimeEvent(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            payload={"event_type": "chat.final", "content": "hello"},
            is_complete=True,
        )

    async def answer_interaction(self, request):
        return []

    async def cancel(self, request) -> None:
        self.calls.append(
            f"cancel:{request.session_id}:{request.request_id}:"
            f"{request.params.get('target_request_id')}"
        )

    async def cleanup_session(self, *, channel_id: str, session_id: str) -> bool:
        self.calls.append(f"cleanup:{channel_id}:{session_id}")
        return True

    async def close(self) -> None:
        self.calls.append("close")


class ErrorClient(FakeClient):
    async def stream(self, request):
        self.calls.append(f"stream:{request.session_id}")
        yield RuntimeEvent.error(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            error=RuntimeError("request failed"),
        )


class StartFailureClient(FakeClient):
    async def start(self) -> None:
        self.calls.append("start")
        raise RuntimeError("start failed")


class SlowStartClient(FakeClient):
    async def start(self) -> None:
        self.calls.append("start")
        await asyncio.sleep(10)


class SlowSessionClient(FakeClient):
    async def create_or_resume_session(
        self,
        *,
        channel_id: str,
        session_id: str | None,
    ) -> str:
        self.calls.append(f"session:{channel_id}:{session_id or ''}")
        await asyncio.sleep(10)
        return "unreachable"


class CancelledCleanupClient(FakeClient):
    async def cleanup_session(self, *, channel_id: str, session_id: str) -> bool:
        self.calls.append(f"cleanup:{channel_id}:{session_id}")
        raise asyncio.CancelledError


def _interaction_event(request, question: str) -> RuntimeEvent:
    return RuntimeEvent(
        request_id=request.request_id,
        channel_id=request.channel_id,
        session_id=request.session_id,
        payload={
            "event_type": "chat.ask_user_question",
            "question": question,
            "source": "ask_user",
        },
    )


class InteractionClient(FakeClient):
    async def stream(self, request):
        self.calls.append(f"stream:{request.session_id}")
        yield _interaction_event(request, "first question")


class ConsecutiveInteractionClient(InteractionClient):
    def __init__(self) -> None:
        super().__init__()
        self.answer_count = 0

    async def answer_interaction(self, request):
        self.answer_count += 1
        if self.answer_count == 1:
            return [_interaction_event(request, "second question")]
        return [
            RuntimeEvent(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                payload={"event_type": "chat.final", "content": "done"},
                is_complete=True,
            )
        ]


class SkillsClient(FakeClient):
    async def invoke(self, request):
        self.calls.append(
            f"invoke:{request.req_method.value}:{request.session_id or ''}"
        )
        return [
            RuntimeEvent(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                payload={
                    "skills": [
                        {
                            "name": "demo",
                            "source": "local",
                            "description": "demo skill",
                            "installed": True,
                        }
                    ]
                },
                is_complete=True,
            )
        ]


class SkillsErrorClient(FakeClient):
    async def invoke(self, request):
        self.calls.append(f"invoke:{request.req_method.value}")
        return [
            RuntimeEvent.error(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                error=RuntimeError("skills unavailable"),
            )
        ]


class SkillsRaiseClient(FakeClient):
    async def invoke(self, request):
        self.calls.append(f"invoke:{request.req_method.value}")
        raise RuntimeError("skills crashed")


class SlowSkillsClient(FakeClient):
    async def invoke(self, request):
        self.calls.append(f"invoke:{request.req_method.value}")
        await asyncio.sleep(10)
        return []


class SessionsClient(FakeClient):
    async def list_sessions(self, *, channel_id, limit=20, offset=0):
        self.calls.append(f"list:{channel_id}:{limit}:{offset}")
        return SessionListResult(
            sessions=(
                SessionSummary(
                    session_id="process_cli_current",
                    channel_id="process_cli",
                    title="当前会话",
                    mode="agent.code.normal",
                    work_mode="code",
                ),
            ),
            total=1,
            limit=limit,
            offset=offset,
        )


class ModelsClient(FakeClient):
    descriptor = RuntimeModelDescriptor(
        selection_key="reasoning-model",
        display_name="reasoning-model",
        model_name="provider/model",
        provider="openai",
        reasoning_level="high",
        is_default=True,
        is_current=True,
    )

    async def list_models(
        self,
        *,
        channel_id,
        session_id=None,
        selected_model="",
    ):
        self.calls.append(f"models:{channel_id}:{session_id or ''}:{selected_model}")
        return ModelCatalogResult(
            models=(self.descriptor,),
            current_selection=self.descriptor.selection_key,
            current_display_name=self.descriptor.display_name,
        )

    async def select_model(self, *, channel_id, selection, session_id=None):
        self.calls.append(f"select:{channel_id}:{session_id or ''}:{selection}")
        return ModelSelectionResult(
            model=self.descriptor,
            session_id=session_id or "",
            persisted=bool(session_id),
        )


class MemoryClient(FakeClient):
    async def list_memory_sources(self, memory_input):
        self.calls.append(f"memory:list:{memory_input.session_id or ''}")
        return MemoryListResult(
            files=(),
            mode=memory_input.mode,
            project_dir=memory_input.project_dir,
        )

    async def get_memory_status(self, memory_input):
        self.calls.append(f"memory:status:{memory_input.session_id or ''}")
        return MemoryStatusResult(
            current_mode=memory_input.mode,
            storage_mode="local",
            engine="builtin",
            enabled=True,
            proactive=False,
            forbidden_enabled=False,
            auto_memory_enabled=True,
            auto_coding_memory=True,
        )

    async def get_memory_locations(self, memory_input):
        self.calls.append(f"memory:open:{memory_input.session_id or ''}")
        return MemoryLocationsResult(
            agent_memory_dir="D:/memory/agent",
            project_dir=memory_input.project_dir,
            project_memory_dir=memory_input.project_dir,
            coding_memory_dir="D:/memory/coding",
            user_memory_dir="D:/memory/user",
        )


class CapabilityCatalogClient(FakeClient):
    agent = AgentDescriptor(
        name="reviewer",
        description="Review changes",
        source="project",
        model=None,
        tools=("read_file",),
        disallowed_tools=(),
        color=None,
        permission_mode="ask",
        memory_scope=None,
        shadowed_by=None,
        enabled=True,
        when_to_use="Before merge",
        max_iterations=3,
        skills=("review",),
    )
    mcp_server = McpServerDescriptor(
        name="demo",
        transport="stdio",
        default_enabled=True,
        connection_state="configured",
    )
    permission_layer = PermissionLayerSnapshot(
        enabled=True,
        tools=(PermissionToolSnapshot(name="bash", level="ask"),),
        rules=(
            PermissionRuleSnapshot(
                rule_id="deny-delete",
                tools=("bash",),
                pattern="rm *",
                action="deny",
            ),
        ),
    )

    async def list_agent_definitions(self, catalog_input):
        self.calls.append(f"agents:list:{catalog_input.session_id or ''}")
        return AgentCatalogResult(agents=(self.agent,))

    async def get_agent_definition(self, catalog_input, *, name):
        self.calls.append(f"agents:get:{catalog_input.session_id or ''}:{name}")
        return self.agent

    async def list_agent_definition_tools(self, catalog_input):
        self.calls.append(f"agents:tools:{catalog_input.session_id or ''}")
        return AgentToolsResult(
            tools=(
                AgentToolDescriptor(
                    name="Read",
                    internal_name="read_file",
                    description="Read a file",
                    group="filesystem",
                ),
            ),
            groups=("filesystem",),
            disallowed_for_subagents=(),
        )

    async def list_mcp_servers(self, catalog_input=None):
        enabled_only = bool(catalog_input and catalog_input.enabled_only)
        self.calls.append(f"mcp:list:{enabled_only}")
        return McpCatalogListResult(servers=(self.mcp_server,))

    async def show_mcp_server(self, catalog_input):
        self.calls.append(f"mcp:show:{catalog_input.name}")
        return McpCatalogShowResult(server=self.mcp_server)

    async def get_permission_snapshot(self, snapshot_input):
        self.calls.append(
            f"permissions:{snapshot_input.channel_id}:{snapshot_input.session_id or ''}"
        )
        return PermissionSnapshotResult(
            scope="session" if snapshot_input.session_id else "host",
            session_id=snapshot_input.session_id or "",
            global_layer=self.permission_layer,
            user_layer=PermissionLayerSnapshot(enabled=None, tools=(), rules=()),
            session_layer=PermissionLayerSnapshot(enabled=None, tools=(), rules=()),
            effective=self.permission_layer,
        )


class _PreparedSession:
    def __init__(self, kind: str, result) -> None:
        self.kind = kind
        self.result = result
        self.state = SessionProvisionState.PREPARED


class SessionOperationClient(FakeClient):
    async def describe_session(self, *, session_id: str):
        self.calls.append(f"describe:{session_id}")
        if session_id in {"missing", "bad/../id"}:
            return None
        if session_id == "process_cli_target":
            mode = "team.code.normal"
            work_mode = "code"
            project_dir = "D:/target-project"
        else:
            mode = "agent.work.normal"
            work_mode = "work"
            project_dir = "D:/source-project"
        return SessionDescriptor(
            session_id=session_id,
            channel_id="process_cli",
            mode=mode,
            work_mode=work_mode,
            project_dir=project_dir,
        )

    async def prepare_session_create(self, provision_input):
        self.calls.append(
            "prepare:create:"
            f"{provision_input.previous_session_id}:"
            f"{provision_input.persist_session}:"
            f"{provision_input.mode}"
        )
        return _PreparedSession(
            "create",
            SessionCreateResult(
                channel_id="process_cli",
                session_id="process_cli_created",
                project_id="default",
                project_dir=provision_input.project_dir,
                work_mode=provision_input.work_mode or "code",
                persist_session=provision_input.persist_session,
                prewarm_hit=False,
                prewarm_status="bypassed",
                created=True,
                canonical_mode="agent.work.normal",
            ),
        )

    async def prepare_session_switch(self, provision_input):
        self.calls.append(
            f"prepare:switch:{provision_input.previous_session_id}:"
            f"{provision_input.target_session_id}"
        )
        return _PreparedSession(
            "switch",
            SessionSwitchResult(
                channel_id="process_cli",
                session_id=provision_input.target_session_id,
                mode="team.code.normal",
            ),
        )

    async def prepare_session_fork(self, provision_input):
        self.calls.append(
            f"prepare:fork:{provision_input.source_session_id}:{provision_input.title}"
        )
        return _PreparedSession(
            "fork",
            SessionForkResult(
                channel_id="process_cli",
                source_session_id=provision_input.source_session_id,
                session_id="process_cli_forked",
                title=provision_input.title,
            ),
        )

    async def commit_session_provision(self, prepared, *, timing, context=None):
        self.calls.append(f"commit:{prepared.kind}:{timing.value}")
        prepared.state = SessionProvisionState.COMMITTED
        return prepared.result

    async def abort_session_provision(self, prepared) -> None:
        self.calls.append(f"abort:{prepared.kind}")
        prepared.state = SessionProvisionState.ABORTED

    async def delete_session(self, *, channel_id: str, session_id: str):
        self.calls.append(f"delete:{channel_id}:{session_id}")
        return SessionDeleteResult(
            ok=True,
            session_id=session_id,
            channel_id=channel_id,
        )


class PersistentTeamClient(FakeClient):
    async def stream(self, request):
        self.calls.append(f"stream:{request.session_id}")
        try:
            yield RuntimeEvent(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                payload={"event_type": "chat.final", "content": "team result"},
            )
            yield RuntimeEvent(
                request_id=request.request_id,
                channel_id=request.channel_id,
                session_id=request.session_id,
                payload={
                    "event_type": "chat.processing_status",
                    "is_processing": False,
                    "is_complete": True,
                },
            )
            await asyncio.Event().wait()
        finally:
            self.calls.append("stream_closed")


class StatusThenFinalClient(FakeClient):
    async def stream(self, request):
        self.calls.append(f"stream:{request.session_id}")
        yield RuntimeEvent(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            payload={
                "event_type": "chat.processing_status",
                "is_processing": False,
                "is_complete": True,
            },
        )
        yield RuntimeEvent(
            request_id=request.request_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            payload={"event_type": "chat.final", "content": "single result"},
            is_complete=True,
        )


def _args(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = {
        "prompt": "say hello",
        "session": None,
        "cwd": str(tmp_path),
        "project_dir": str(tmp_path),
        "trusted_dir": [],
        "mode": "code.normal",
        "work_mode": "code",
        "output": "jsonl",
        "timeout": None,
        "show_reasoning": False,
        "show_tools": False,
        "model": None,
        "_interactive_worker": False,
        "_session_result_file": None,
        "_worker_result_file": None,
        "_operation": "chat",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.asyncio
async def test_runtime_client_invoke_delegates_to_shared_runtime() -> None:
    expected = [
        RuntimeEvent(
            request_id="request",
            channel_id="process_cli",
            session_id=None,
            payload={"skills": []},
        )
    ]

    class RuntimeStub:
        async def invoke(self, request):
            assert request.req_method == ReqMethod.SKILLS_LIST
            return expected

    request = AgentRequest(
        request_id="request",
        channel_id="process_cli",
        req_method=ReqMethod.SKILLS_LIST,
    )
    client = InProcessRuntimeClient(RuntimeStub())

    assert await client.invoke(request) is expected


@pytest.mark.asyncio
async def test_runtime_client_session_methods_only_delegate_to_runtime() -> None:
    calls: list[tuple[str, object]] = []
    prepared = object()
    deleted = SessionDeleteResult(ok=True, session_id="process_cli_target")

    class RuntimeStub:
        async def describe_session(self, *, session_id):
            calls.append(("describe", session_id))
            return "descriptor"

        async def list_sessions(self, *, channel_id, limit, offset):
            calls.append(("list", (channel_id, limit, offset)))
            return "sessions"

        async def list_models(
            self,
            *,
            channel_id,
            session_id,
            selected_model,
        ):
            calls.append(("models", (channel_id, session_id, selected_model)))
            return "models"

        async def select_model(self, *, channel_id, selection, session_id):
            calls.append(("select_model", (channel_id, selection, session_id)))
            return "selected"

        async def compact_context(self, compact_input):
            calls.append(("compact", compact_input))
            return "compacted"

        async def list_rewind_turns(self, rewind_input):
            calls.append(("rewind_list", rewind_input))
            return "rewind turns"

        async def rewind_session(self, rewind_input):
            calls.append(("rewind", rewind_input))
            return "rewound"

        async def prepare_session_create(self, value):
            calls.append(("create", value))
            return prepared

        async def prepare_session_switch(self, value):
            calls.append(("switch", value))
            return prepared

        async def prepare_session_fork(self, value):
            calls.append(("fork", value))
            return prepared

        async def commit_session_provision(self, value, *, timing, context):
            calls.append(("commit", (value, timing, context)))
            return "committed"

        async def abort_session_provision(self, value):
            calls.append(("abort", value))

        async def delete_session(self, *, channel_id, session_id):
            calls.append(("delete", (channel_id, session_id)))
            return deleted

    client = InProcessRuntimeClient(RuntimeStub())
    create_input = object()
    switch_input = object()
    fork_input = object()

    assert await client.describe_session(session_id="process_cli_target") == (
        "descriptor"
    )
    assert (
        await client.list_sessions(
            channel_id="process_cli",
            limit=10,
            offset=2,
        )
        == "sessions"
    )
    assert (
        await client.list_models(
            channel_id="process_cli",
            session_id="process_cli_target",
            selected_model="model-a",
        )
        == "models"
    )
    assert (
        await client.select_model(
            channel_id="process_cli",
            selection="model-b",
            session_id="process_cli_target",
        )
        == "selected"
    )
    compact_input = ContextCompactInput(
        request_id="compact-request",
        channel_id="process_cli",
        session_id="process_cli_target",
    )
    assert await client.compact_context(compact_input) == "compacted"
    rewind_list_input = object()
    rewind_input = object()
    assert await client.list_rewind_turns(rewind_list_input) == "rewind turns"
    assert await client.rewind_session(rewind_input) == "rewound"
    assert await client.prepare_session_create(create_input) is prepared
    assert await client.prepare_session_switch(switch_input) is prepared
    assert await client.prepare_session_fork(fork_input) is prepared
    assert (
        await client.commit_session_provision(
            prepared,
            timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )
        == "committed"
    )
    await client.abort_session_provision(prepared)
    assert (
        await client.delete_session(
            channel_id="process_cli",
            session_id="process_cli_target",
        )
        is deleted
    )
    assert [name for name, _value in calls] == [
        "describe",
        "list",
        "models",
        "select_model",
        "compact",
        "rewind_list",
        "rewind",
        "create",
        "switch",
        "fork",
        "commit",
        "abort",
        "delete",
    ]


@pytest.mark.asyncio
async def test_runtime_client_close_drains_team_work_before_runtime() -> None:
    calls: list[str] = []

    class RuntimeStub:
        async def cancel_all_team_stream_tasks(self, *, reason):
            calls.append(f"team:{reason}")

        async def close(self):
            calls.append("close")

    client = InProcessRuntimeClient(RuntimeStub())

    await client.close()

    assert calls == [
        "team:[process CLI close] ",
        "close",
    ]


@pytest.mark.asyncio
async def test_runtime_client_close_attempts_every_stage_after_cleanup_failure() -> (
    None
):
    calls: list[str] = []

    class RuntimeStub:
        async def cancel_all_team_stream_tasks(self, *, reason):
            calls.append("team")
            raise RuntimeError("team cleanup failed")

        async def close(self):
            calls.append("close")

    client = InProcessRuntimeClient(RuntimeStub())

    with pytest.raises(RuntimeError, match="team cleanup failed"):
        await client.close()

    assert calls == ["team", "close"]


@pytest.mark.parametrize(
    ("mode", "work_mode", "expected"),
    [
        ("agent.work.normal", "code", ("agent", None)),
        ("agent.code.normal", "work", ("code", "normal")),
        ("team.work.normal", "code", ("team", None)),
        ("team.code.normal", "work", ("code", "team")),
    ],
)
def test_process_cli_canonical_mode_controls_runtime_route(
    tmp_path: Path,
    mode: str,
    work_mode: str,
    expected: tuple[str, str | None],
) -> None:
    """The mode's environment segment wins over orthogonal project identity."""
    request = app._build_request(
        _args(tmp_path, mode=mode, work_mode=work_mode),
        session_id="runtime-session",
        request_id="runtime-request",
    )

    resolved = resolve_request_runtime_mode(request, work_mode=work_mode)

    assert (resolved.manager_mode, resolved.sub_mode) == expected
    assert resolved.canonical_mode == mode
    assert request.params["work_mode"] == mode.split(".")[1]


@pytest.mark.asyncio
async def test_one_command_owns_one_runtime_lifecycle(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    FakeClient.delay = 0.0
    monkeypatch.setattr(app, "InProcessRuntimeClient", FakeClient)

    result = await app.run(_args(tmp_path))

    client = FakeClient.latest
    assert result == 0
    assert client is not None
    assert client.calls == [
        "start",
        "session:process_cli:",
        "stream:runtime-session",
        "cleanup:process_cli:runtime-session",
        "close",
    ]
    output = capsys.readouterr().out
    assert '"event_type": "chat.delta"' in output
    assert '"event_type": "chat.final"' in output


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team.work.normal", "team.code.normal"])
async def test_team_round_returns_on_terminal_status_and_closes_persistent_stream(
    monkeypatch,
    tmp_path: Path,
    mode: str,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", PersistentTeamClient)
    stdout = io.StringIO()

    result = await asyncio.wait_for(
        app.run(
            _args(tmp_path, mode=mode, output="json"),
            stdout=stdout,
            stderr=io.StringIO(),
        ),
        timeout=1,
    )

    client = PersistentTeamClient.latest
    document = json.loads(stdout.getvalue())
    assert result == 0
    assert document["ok"] is True
    assert [event["payload"]["event_type"] for event in document["events"]] == [
        "chat.final",
        "chat.processing_status",
    ]
    assert client is not None
    assert client.calls == [
        "start",
        "session:process_cli:",
        "stream:runtime-session",
        "stream_closed",
        "cleanup:process_cli:runtime-session",
        "close",
    ]


@pytest.mark.asyncio
async def test_single_agent_does_not_treat_team_status_shape_as_stream_boundary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", StatusThenFinalClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(tmp_path, mode="agent.work.normal", output="json"),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert [event["payload"]["event_type"] for event in document["events"]] == [
        "chat.processing_status",
        "chat.final",
    ]
    assert document["events"][-1]["payload"]["content"] == "single result"


@pytest.mark.asyncio
async def test_timeout_precisely_cancels_then_cleans_up(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeClient.delay = 0.1
    monkeypatch.setattr(app, "InProcessRuntimeClient", FakeClient)

    result = await app.run(_args(tmp_path, timeout=0.01))

    client = FakeClient.latest
    assert result == 124
    assert client is not None
    assert client.calls[0:3] == [
        "start",
        "session:process_cli:",
        "stream:runtime-session",
    ]
    cancel_call = next(call for call in client.calls if call.startswith("cancel:"))
    _kind, session_id, request_id, target_request_id = cancel_call.split(":")
    assert session_id == "runtime-session"
    assert request_id == target_request_id
    assert client.calls[-2:] == [
        "cleanup:process_cli:runtime-session",
        "close",
    ]


@pytest.mark.asyncio
async def test_timeout_keeps_jsonl_error_machine_compatible(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeClient.delay = 0.1
    monkeypatch.setattr(app, "InProcessRuntimeClient", FakeClient)
    stdout = io.StringIO()

    try:
        result = await app.run(
            _args(tmp_path, timeout=0.01),
            stdout=stdout,
            stderr=io.StringIO(),
        )
    finally:
        FakeClient.delay = 0.0

    document = json.loads(stdout.getvalue().strip())
    assert result == 124
    assert document["payload"]["event_type"] == "runtime.error"
    assert document["payload"]["error"] == "process CLI execution timed out"
    assert "\033[" not in stdout.getvalue()


@pytest.mark.asyncio
async def test_timeout_covers_runtime_startup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SlowStartClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(tmp_path, timeout=0.01, output="json"),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    client = SlowStartClient.latest
    assert result == 124
    assert document["ok"] is False
    assert document["events"][-1]["payload"]["event_type"] == "runtime.error"
    assert client is not None
    assert client.calls == ["start", "close"]


@pytest.mark.asyncio
async def test_timeout_covers_session_creation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SlowSessionClient)

    result = await app.run(
        _args(tmp_path, timeout=0.01),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    client = SlowSessionClient.latest
    assert result == 124
    assert client is not None
    assert client.calls == ["start", "session:process_cli:", "close"]


@pytest.mark.asyncio
async def test_runtime_error_returns_failure_and_cleans_up(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", ErrorClient)

    result = await app.run(_args(tmp_path))

    client = ErrorClient.latest
    assert result == 1
    assert client is not None
    assert client.calls[-2:] == [
        "cleanup:process_cli:runtime-session",
        "close",
    ]


@pytest.mark.asyncio
async def test_cancelled_session_cleanup_still_closes_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", CancelledCleanupClient)

    with pytest.raises(asyncio.CancelledError):
        await app.run(_args(tmp_path))

    client = CancelledCleanupClient.latest
    assert client is not None
    assert client.calls[-2:] == [
        "cleanup:process_cli:runtime-session",
        "close",
    ]


@pytest.mark.asyncio
async def test_task_cancellation_precisely_cancels_then_cleans_up(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeClient.delay = 10.0
    FakeClient.latest = None
    monkeypatch.setattr(app, "InProcessRuntimeClient", FakeClient)
    try:
        task = asyncio.create_task(app.run(_args(tmp_path)))
        while FakeClient.latest is None or not any(
            call.startswith("stream:") for call in FakeClient.latest.calls
        ):
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        client = FakeClient.latest
        assert client is not None
        cancel_call = next(call for call in client.calls if call.startswith("cancel:"))
        _kind, session_id, request_id, target_request_id = cancel_call.split(":")
        assert session_id == "runtime-session"
        assert request_id == target_request_id
        assert client.calls[-2:] == [
            "cleanup:process_cli:runtime-session",
            "close",
        ]
    finally:
        FakeClient.delay = 0.0


@pytest.mark.asyncio
async def test_human_task_cancellation_shows_interrupted_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeClient.delay = 10.0
    FakeClient.latest = None
    monkeypatch.setattr(app, "InProcessRuntimeClient", FakeClient)
    monkeypatch.setenv("NO_COLOR", "1")
    output = io.StringIO()
    try:
        task = asyncio.create_task(
            app.run(
                _args(tmp_path, output="human"),
                stdout=output,
                stderr=output,
            )
        )
        while FakeClient.latest is None or not any(
            call.startswith("stream:") for call in FakeClient.latest.calls
        ):
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "! 已中断" in output.getvalue()
    finally:
        FakeClient.delay = 0.0


@pytest.mark.asyncio
async def test_start_failure_still_closes_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", StartFailureClient)

    stdout = io.StringIO()
    result = await app.run(
        _args(tmp_path),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    client = StartFailureClient.latest
    event = json.loads(stdout.getvalue())
    assert result == 1
    assert event["ok"] is False
    assert event["payload"]["error"] == "start failed"
    assert client is not None
    assert client.calls == ["start", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["json", "jsonl"])
async def test_noninteractive_interaction_is_an_explicit_machine_failure(
    monkeypatch,
    tmp_path: Path,
    output_format: str,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", InteractionClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(tmp_path, output=output_format),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert result == 4
    if output_format == "json":
        document = json.loads(stdout.getvalue())
        assert document["ok"] is False
        events = document["events"]
    else:
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert events[-1]["ok"] is False
    assert events[-1]["payload"]["event_type"] == "runtime.error"
    assert "interactive input is unavailable" in events[-1]["payload"]["error"]


@pytest.mark.asyncio
async def test_answer_interaction_handles_consecutive_questions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", ConsecutiveInteractionClient)
    monkeypatch.setattr(app, "_interaction_answer", lambda _payload, _stream: ("y", []))

    result = await app.run(
        _args(tmp_path, output="human", _interactive_worker=True),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    client = ConsecutiveInteractionClient.latest
    assert result == 0
    assert client is not None
    assert client.answer_count == 2


@pytest.mark.asyncio
async def test_interactive_worker_reports_real_runtime_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    FakeClient.delay = 0.0
    monkeypatch.setattr(app, "InProcessRuntimeClient", FakeClient)
    result_file = tmp_path / "session.txt"

    result = await app.run(
        _args(
            tmp_path,
            output="human",
            _interactive_worker=True,
            _session_result_file=str(result_file),
        )
    )

    assert result == 0
    assert result_file.read_text(encoding="utf-8") == "runtime-session"


@pytest.mark.asyncio
async def test_skills_list_uses_stateless_runtime_invoke_lifecycle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SkillsClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="/skills list",
            session="existing-session",
            output="human",
            _operation="skills.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    client = SkillsClient.latest
    assert result == 0
    assert client is not None
    assert client.calls == [
        "start",
        "invoke:skills.list:existing-session",
        "close",
    ]
    assert "已安装技能（1）" in stdout.getvalue()
    assert "demo [local] · demo skill" in stdout.getvalue()


@pytest.mark.asyncio
async def test_skills_list_json_preserves_runtime_payload(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SkillsClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="/skills list",
            output="json",
            _operation="skills.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert document["ok"] is True
    assert document["events"][0]["payload"] == {
        "skills": [
            {
                "name": "demo",
                "source": "local",
                "description": "demo skill",
                "installed": True,
            }
        ]
    }


@pytest.mark.asyncio
async def test_skills_list_preserves_runtime_error_event_and_closes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SkillsErrorClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="/skills list",
            output="json",
            _operation="skills.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    client = SkillsErrorClient.latest
    document = json.loads(stdout.getvalue())
    assert result == 1
    assert client is not None
    assert client.calls == ["start", "invoke:skills.list", "close"]
    assert document["ok"] is False
    assert document["events"][0]["payload"] == {
        "event_type": "runtime.error",
        "error": "skills unavailable",
    }


@pytest.mark.asyncio
async def test_skills_list_converts_invoke_exception_and_closes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SkillsRaiseClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="/skills list",
            output="jsonl",
            _operation="skills.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    client = SkillsRaiseClient.latest
    event = json.loads(stdout.getvalue())
    assert result == 1
    assert client is not None
    assert client.calls == ["start", "invoke:skills.list", "close"]
    assert event["ok"] is False
    assert event["payload"]["error"] == "skills crashed"


@pytest.mark.asyncio
async def test_skills_list_timeout_closes_without_chat_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SlowSkillsClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="/skills list",
            output="json",
            timeout=0.01,
            _operation="skills.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    client = SlowSkillsClient.latest
    document = json.loads(stdout.getvalue())
    assert result == 124
    assert client is not None
    assert client.calls == ["start", "invoke:skills.list", "close"]
    assert document["events"][0]["payload"] == {
        "event_type": "runtime.error",
        "error": "process CLI execution timed out",
    }


@pytest.mark.asyncio
async def test_sessions_list_uses_runtime_query_without_chat_session_or_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", SessionsClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            session="process_cli_current",
            output="json",
            _operation="session.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    client = SessionsClient.latest
    document = json.loads(stdout.getvalue())
    assert result == 0
    assert client is not None
    assert client.calls == ["start", "list:process_cli:20:0", "close"]
    assert document["events"][0]["payload"] == {
        "event_type": "session.listed",
        "sessions": [
            {
                "session_id": "process_cli_current",
                "channel_id": "process_cli",
                "title": "当前会话",
                "mode": "agent.code.normal",
                "work_mode": "code",
                "project_id": "",
                "project_dir": "",
                "model": "",
                "created_at": 0.0,
                "last_message_at": 0.0,
                "message_count": 0,
            }
        ],
        "total": 1,
        "limit": 20,
        "offset": 0,
        "current_session_id": "process_cli_current",
    }


@pytest.mark.asyncio
async def test_model_list_uses_runtime_query_and_never_exposes_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app, "InProcessRuntimeClient", ModelsClient)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            session="process_cli_current",
            model="reasoning-model",
            output="json",
            _operation="model.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    client = ModelsClient.latest
    document = json.loads(stdout.getvalue())
    serialized = json.dumps(document, ensure_ascii=False)
    assert result == 0
    assert client is not None
    assert client.calls == [
        "start",
        "models:process_cli:process_cli_current:reasoning-model",
        "close",
    ]
    assert document["events"][0]["payload"]["event_type"] == "model.listed"
    assert document["events"][0]["payload"]["current_selection"] == ("reasoning-model")
    assert "api_key" not in serialized
    assert "api_base" not in serialized
    assert not any(call.startswith("cleanup:") for call in client.calls)


@pytest.mark.asyncio
async def test_runtime_client_memory_methods_only_delegate_to_runtime() -> None:
    calls: list[tuple[str, MemoryScopeInput]] = []
    memory_input = MemoryScopeInput(
        channel_id="process_cli",
        project_dir="D:/project",
        trusted_dirs=("D:/project",),
    )

    class RuntimeStub:
        async def list_memory_sources(self, value):
            calls.append(("list", value))
            return "listed"

        async def get_memory_status(self, value):
            calls.append(("status", value))
            return "status"

        async def get_memory_locations(self, value):
            calls.append(("open", value))
            return "locations"

    client = InProcessRuntimeClient(RuntimeStub())

    assert await client.list_memory_sources(memory_input) == "listed"
    assert await client.get_memory_status(memory_input) == "status"
    assert await client.get_memory_locations(memory_input) == "locations"
    assert calls == [
        ("list", memory_input),
        ("status", memory_input),
        ("open", memory_input),
    ]


@pytest.mark.asyncio
async def test_runtime_client_capability_catalog_methods_only_delegate() -> None:
    calls: list[tuple[str, object]] = []
    agent_input = AgentCatalogInput(
        channel_id="process_cli",
        project_dir="D:/project",
        trusted_dirs=("D:/project",),
    )
    mcp_list_input = McpCatalogListInput(enabled_only=True)
    mcp_show_input = McpCatalogShowInput(name="demo")
    permission_input = PermissionSnapshotInput(channel_id="process_cli")

    class RuntimeStub:
        async def list_agent_definitions(self, value):
            calls.append(("agents.list", value))
            return "agents"

        async def get_agent_definition(self, value, *, name):
            calls.append(("agents.get", (value, name)))
            return "agent"

        async def list_agent_definition_tools(self, value):
            calls.append(("agents.tools", value))
            return "tools"

        async def list_mcp_servers(self, value=None):
            calls.append(("mcp.list", value))
            return "mcp-servers"

        async def show_mcp_server(self, value):
            calls.append(("mcp.show", value))
            return "mcp-server"

        async def get_permission_snapshot(self, value):
            calls.append(("permissions.show", value))
            return "permissions"

    client = InProcessRuntimeClient(RuntimeStub())

    assert await client.list_agent_definitions(agent_input) == "agents"
    assert await client.get_agent_definition(agent_input, name="reviewer") == "agent"
    assert await client.list_agent_definition_tools(agent_input) == "tools"
    assert await client.list_mcp_servers(mcp_list_input) == "mcp-servers"
    assert await client.show_mcp_server(mcp_show_input) == "mcp-server"
    assert await client.get_permission_snapshot(permission_input) == "permissions"
    assert calls == [
        ("agents.list", agent_input),
        ("agents.get", (agent_input, "reviewer")),
        ("agents.tools", agent_input),
        ("mcp.list", mcp_list_input),
        ("mcp.show", mcp_show_input),
        ("permissions.show", permission_input),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "prompt", "expected_event", "expected_call"),
    [
        ("agents.list", "", "agents.listed", "agents:list:process_cli_current"),
        (
            "agents.get",
            "reviewer",
            "agents.detail",
            "agents:get:process_cli_current:reviewer",
        ),
        (
            "agents.tools",
            "",
            "agents.tools",
            "agents:tools:process_cli_current",
        ),
        ("mcp.list", "", "mcp.listed", "mcp:list:False"),
        ("mcp.show", "demo", "mcp.detail", "mcp:show:demo"),
        ("mcp.show", "", "mcp.listed", "mcp:list:True"),
        (
            "permissions.show",
            "",
            "permissions.snapshot",
            "permissions:process_cli:process_cli_current",
        ),
    ],
)
async def test_capability_catalog_commands_use_one_runtime_without_chat_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    prompt: str,
    expected_event: str,
    expected_call: str,
) -> None:
    client = CapabilityCatalogClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt=prompt,
            session="process_cli_current",
            output="json",
            _operation=operation,
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert client.calls == ["start", expected_call, "close"]
    assert document["events"][0]["payload"]["event_type"] == expected_event
    assert not any(
        call.startswith(("session:", "cleanup:", "stream:")) for call in client.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "prompt"),
    [
        ("agents.list", "unexpected"),
        ("agents.get", ""),
        ("agents.tools", "unexpected"),
        ("mcp.list", "unexpected"),
        ("mcp.show", "too many"),
        ("permissions.show", "deny bash"),
    ],
)
async def test_capability_catalog_rejects_bad_syntax_before_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    prompt: str,
) -> None:
    client = CapabilityCatalogClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt=prompt,
            output="json",
            _operation=operation,
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert client.calls == ["start", "close"]
    assert document["events"][0]["payload"]["event_type"] == "runtime.error"
    assert document["events"][0]["metadata"]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_permission_snapshot_has_human_output_without_chat_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = CapabilityCatalogClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            output="human",
            _operation="permissions.show",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    text = stdout.getvalue()
    assert result == 0
    assert "权限快照（只读）" in text
    assert "bash: ask" in text
    assert "deny-delete: deny" in text
    assert "执行完成" not in text
    assert client.calls == ["start", "permissions:process_cli:", "close"]


@pytest.mark.asyncio
async def test_capability_query_error_preserves_code_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class PermissionFailureClient(CapabilityCatalogClient):
        async def get_permission_snapshot(self, snapshot_input):
            self.calls.append("permissions:failed")
            raise PermissionCatalogError(
                "failed to read permissions",
                code="READ_FAILED",
            )

    client = PermissionFailureClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            output="json",
            _operation="permissions.show",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert client.calls == ["start", "permissions:failed", "close"]
    assert document["events"][0]["metadata"]["code"] == "READ_FAILED"
    assert not any(call.startswith("cleanup:") for call in client.calls)


@pytest.mark.asyncio
async def test_capability_query_timeout_closes_without_cancel_or_chat_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class SlowMcpClient(CapabilityCatalogClient):
        async def list_mcp_servers(self, catalog_input=None):
            self.calls.append("mcp:list:slow")
            await asyncio.sleep(10)
            return McpCatalogListResult(servers=())

    client = SlowMcpClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            output="jsonl",
            timeout=0.01,
            _operation="mcp.list",
        ),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert result == 124
    assert client.calls == ["start", "mcp:list:slow", "close"]
    assert not any(call.startswith(("cancel:", "cleanup:")) for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_event", "expected_call"),
    [
        ("memory.list", "memory.listed", "memory:list:process_cli_current"),
        ("memory.status", "memory.status", "memory:status:process_cli_current"),
        ("memory.open", "memory.locations", "memory:open:process_cli_current"),
    ],
)
async def test_memory_commands_use_fresh_runtime_without_chat_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    expected_event: str,
    expected_call: str,
) -> None:
    client = MemoryClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            session="process_cli_current",
            output="json",
            _operation=operation,
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert client.calls == ["start", expected_call, "close"]
    assert document["events"][0]["payload"]["event_type"] == expected_event
    assert not any(
        call.startswith(("session:", "cleanup:", "stream:")) for call in client.calls
    )


@pytest.mark.asyncio
async def test_memory_status_has_human_output_without_chat_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = MemoryClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            output="human",
            _operation="memory.status",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert result == 0
    assert "builtin" in stdout.getvalue()
    assert client.calls == ["start", "memory:status:", "close"]


@pytest.mark.asyncio
async def test_model_select_publishes_only_committed_local_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = ModelsClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()
    worker_result = tmp_path / "worker-result.json"

    result = await app.run(
        _args(
            tmp_path,
            prompt="reasoning-model",
            session="process_cli_current",
            output="json",
            _operation="model.select",
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    state = json.loads(worker_result.read_text(encoding="utf-8"))
    assert result == 0
    assert client.calls == [
        "start",
        "select:process_cli:process_cli_current:reasoning-model",
        "close",
    ]
    assert state == {
        "operation": "model.select",
        "session_id": "process_cli_current",
        "mode": "code.normal",
        "work_mode": "code",
        "project_dir": str(tmp_path),
        "model_name": "reasoning-model",
    }
    assert not any(call.startswith("cleanup:") for call in client.calls)


def test_chat_request_carries_selected_model_without_transport_fields(
    tmp_path: Path,
) -> None:
    request = app._build_request(
        _args(tmp_path, model="reasoning-model"),
        session_id="process_cli_current",
        request_id="request-model",
    )

    assert request.params["model_name"] == "reasoning-model"
    assert "api_key" not in request.params
    assert "api_base" not in request.params


@pytest.mark.asyncio
async def test_chat_rejects_invalid_model_before_session_or_agent_work(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class InvalidModelClient(FakeClient):
        async def select_model(self, *, channel_id, selection, session_id=None):
            self.calls.append(f"select:{channel_id}:{session_id or ''}:{selection}")
            raise ModelCatalogError("model not found", code="NOT_FOUND")

    client = InvalidModelClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(tmp_path, model="missing-model", output="json"),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert client.calls == [
        "start",
        "select:process_cli::missing-model",
        "close",
    ]
    assert document["events"][0]["metadata"] == {"code": "NOT_FOUND"}


@pytest.mark.asyncio
async def test_chat_rejects_foreign_explicit_session_before_resume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class ForeignSessionClient(FakeClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            return SessionDescriptor(
                session_id=session_id,
                channel_id="web",
                mode="agent.work.normal",
                work_mode="work",
            )

    client = ForeignSessionClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            session="web-session",
            model=None,
            output="json",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert client.calls == ["start", "describe:web-session", "close"]
    assert document["events"][0]["metadata"] == {"code": "NOT_FOUND"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    (
        "agent.work.normal",
        "agent.code.normal",
        "team.work.normal",
        "team.code.normal",
    ),
)
async def test_validated_model_key_reaches_all_supported_chat_modes(
    monkeypatch,
    tmp_path: Path,
    mode: str,
) -> None:
    class CapturingModelClient(ModelsClient):
        async def stream(self, request):
            self.request = request
            async for event in super().stream(request):
                yield event

    client = CapturingModelClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)

    result = await app.run(
        _args(tmp_path, mode=mode, model="reasoning-model"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert result == 0
    assert client.request.params["mode"] == mode
    assert client.request.params["model_name"] == "reasoning-model"
    assert client.calls == [
        "start",
        "select:process_cli::reasoning-model",
        "session:process_cli:",
        "stream:runtime-session",
        "cleanup:process_cli:runtime-session",
        "close",
    ]


@pytest.mark.asyncio
async def test_cold_session_model_is_validated_and_forwarded_to_chat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class ColdSessionModelClient(ModelsClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            return SessionDescriptor(
                session_id=session_id,
                channel_id="process_cli",
                mode="team.code.normal",
                work_mode="code",
                model="reasoning-model",
            )

        async def stream(self, request):
            self.request = request
            async for event in super().stream(request):
                yield event

    client = ColdSessionModelClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)

    result = await app.run(
        _args(
            tmp_path,
            session="process_cli_existing",
            mode="team.code.normal",
            model=None,
        ),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert result == 0
    assert client.request.params["model_name"] == "reasoning-model"
    assert client.calls == [
        "start",
        "describe:process_cli_existing",
        "select:process_cli::reasoning-model",
        "session:process_cli:process_cli_existing",
        "stream:process_cli_existing",
        "cleanup:process_cli:process_cli_existing",
        "close",
    ]


@pytest.mark.asyncio
async def test_compact_uses_runtime_public_api_without_chat_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    compact_state = RuntimeEvent.control(
        request_id="compact-request",
        channel_id="process_cli",
        session_id="process_cli_existing",
        payload={
            "event_type": "context.compression_state",
            "status": "completed",
        },
    )

    class CompactClient(FakeClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            return SessionDescriptor(
                session_id=session_id,
                channel_id="process_cli",
                mode="team.code.normal",
                work_mode="code",
                project_dir="D:/persisted-project",
            )

        async def compact_context(self, compact_input):
            self.calls.append("compact")
            self.compact_input = compact_input
            return ContextCompactResult(
                result="compressed",
                stats={"raw_total_tokens": 1000, "total_tokens": 300},
                summary="compact summary",
                events=(compact_state,),
            )

    client = CompactClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            session="process_cli_existing",
            mode="agent.work.normal",
            output="json",
            _operation="context.compact",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert client.compact_input.channel_id == "process_cli"
    assert client.compact_input.session_id == "process_cli_existing"
    assert client.compact_input.mode == "team.code.normal"
    assert client.compact_input.project_dir == "D:/persisted-project"
    assert client.calls == [
        "start",
        "describe:process_cli_existing",
        "compact",
        "close",
    ]
    assert [event["payload"]["event_type"] for event in document["events"]] == [
        "context.compression_state",
        "context.compact.result",
    ]
    assert document["events"][1]["payload"]["compact_summary"] == ("compact summary")


@pytest.mark.asyncio
async def test_compact_requires_owned_current_session_before_runtime_work(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = FakeClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            session=None,
            output="json",
            _operation="context.compact",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert client.calls == ["start", "close"]
    assert document["events"][0]["metadata"] == {"code": "BAD_REQUEST"}


@pytest.mark.asyncio
async def test_rewind_list_uses_runtime_api_without_chat_session_or_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class RewindListClient(FakeClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            return SessionDescriptor(
                session_id=session_id,
                channel_id="process_cli",
                mode="agent.code.normal",
                work_mode="code",
                project_dir="D:/persisted-project",
            )

        async def list_rewind_turns(self, rewind_input):
            self.calls.append("rewind:list")
            self.rewind_input = rewind_input
            return SessionRewindListResult(
                turns=(
                    SessionRewindTurn(
                        turn_index=2,
                        content_preview="second prompt",
                        files_changed=1,
                    ),
                ),
                total=2,
            )

    client = RewindListClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            session="process_cli_existing",
            output="json",
            _operation="session.rewind.list",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert client.calls == [
        "start",
        "describe:process_cli_existing",
        "rewind:list",
        "close",
    ]
    assert client.rewind_input == SessionRewindListInput(
        channel_id="process_cli",
        session_id="process_cli_existing",
        project_dir="D:/persisted-project",
    )
    assert document["events"][0]["payload"] == {
        "event_type": "session.rewind.turns",
        "turns": [
            {
                "turn_index": 2,
                "content_preview": "second prompt",
                "timestamp": 0,
                "id": "",
                "request_id": "",
                "stats": {
                    "filesChanged": 1,
                    "linesAdded": 0,
                    "linesRemoved": 0,
                },
                "files": [],
            }
        ],
        "total": 2,
    }
    assert not any(call.startswith("cleanup:") for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expected_action", "require_context"),
    [
        ("2 conversation", SessionRewindAction.CONVERSATION, True),
        ("2 all", SessionRewindAction.CONVERSATION_AND_FILES, True),
        ("2 files", SessionRewindAction.FILES_ONLY, False),
    ],
)
async def test_rewind_mutation_uses_persisted_runtime_context_policy(
    monkeypatch,
    tmp_path: Path,
    prompt: str,
    expected_action: SessionRewindAction,
    require_context: bool,
) -> None:
    class RewindClient(FakeClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            return SessionDescriptor(
                session_id=session_id,
                channel_id="process_cli",
                mode="agent.code.normal",
                work_mode="code",
            )

        async def rewind_session(self, rewind_input):
            self.calls.append("rewind")
            self.rewind_input = rewind_input
            return SessionRewindResult(
                action=rewind_input.action,
                session_id=rewind_input.session_id,
                turn_index=rewind_input.turn_index,
                context_rebuilt=(
                    None
                    if rewind_input.action is SessionRewindAction.FILES_ONLY
                    else True
                ),
            )

    client = RewindClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt=prompt,
            session="process_cli_existing",
            output="json",
            _operation="session.rewind",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert client.calls == [
        "start",
        "describe:process_cli_existing",
        "rewind",
        "close",
    ]
    assert isinstance(client.rewind_input, SessionRewindInput)
    assert client.rewind_input.action is expected_action
    assert client.rewind_input.turn_index == 2
    assert (
        client.rewind_input.context_policy
        is SessionRewindContextPolicy.ENSURE_PERSISTED
    )
    assert client.rewind_input.require_context is require_context
    assert document["events"][0]["payload"]["event_type"] == "session.rewound"
    assert document["events"][0]["payload"]["action"] == expected_action.value
    assert not any(call.startswith("cleanup:") for call in client.calls)


@pytest.mark.asyncio
async def test_rewind_rejects_invalid_input_before_runtime_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class RewindClient(FakeClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            return SessionDescriptor(
                session_id=session_id,
                channel_id="process_cli",
                mode="agent.code.normal",
                work_mode="code",
            )

        async def rewind_session(self, rewind_input):
            pytest.fail(f"invalid input reached Runtime mutation: {rewind_input}")

    client = RewindClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="not-a-turn all",
            session="process_cli_existing",
            output="json",
            _operation="session.rewind",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert client.calls == [
        "start",
        "describe:process_cli_existing",
        "close",
    ]
    assert document["events"][0]["metadata"] == {"code": "BAD_REQUEST"}


@pytest.mark.asyncio
async def test_session_create_delivers_result_before_after_delivery_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    stdout = io.StringIO()
    worker_result = tmp_path / "worker-result.json"
    session_result = tmp_path / "session.txt"

    class CreateOrderClient(SessionOperationClient):
        async def prepare_session_create(self, provision_input):
            assert provision_input.project_dir == ""
            assert provision_input.cwd == str(tmp_path)
            assert provision_input.previous_mode == "agent.work.normal"
            return await super().prepare_session_create(provision_input)

        async def commit_session_provision(self, prepared, *, timing, context=None):
            assert timing is SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY
            assert '"event_type": "session.created"' in stdout.getvalue()
            assert json.loads(worker_result.read_text(encoding="utf-8")) == {
                "operation": "session.create",
                "session_id": "process_cli_created",
                "mode": "agent.work.normal",
                "work_mode": "work",
                "project_dir": str(tmp_path),
            }
            return await super().commit_session_provision(
                prepared,
                timing=timing,
                context=context,
            )

    client = CreateOrderClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)

    result = await app.run(
        _args(
            tmp_path,
            prompt="--persist",
            session="process_cli_previous",
            mode="agent.work.normal",
            _operation="session.create",
            _session_result_file=str(session_result),
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert result == 0
    assert session_result.read_text(encoding="utf-8") == "process_cli_created"
    assert client.calls == [
        "start",
        "describe:process_cli_previous",
        "prepare:create:process_cli_previous:True:agent.work.normal",
        "commit:create:after_result_delivery",
        "close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "current_mode",
        "current_work_mode",
        "target_mode",
        "target_work_mode",
        "target_is_team",
    ),
    [
        (
            "team.work.normal",
            "work",
            "agent.code.normal",
            "code",
            False,
        ),
        (
            "agent.work.normal",
            "work",
            "team.code.normal",
            "code",
            True,
        ),
    ],
)
async def test_session_switch_uses_persisted_modes_not_current_cli_mode(
    monkeypatch,
    tmp_path: Path,
    current_mode: str,
    current_work_mode: str,
    target_mode: str,
    target_work_mode: str,
    target_is_team: bool,
) -> None:
    worker_result = tmp_path / "worker-result.json"

    class CrossModeClient(SessionOperationClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            if session_id == "process_cli_current":
                mode, work_mode, project_dir = (
                    current_mode,
                    current_work_mode,
                    "D:/current-project",
                )
            else:
                mode, work_mode, project_dir = (
                    target_mode,
                    target_work_mode,
                    "D:/target-project",
                )
            return SessionDescriptor(
                session_id=session_id,
                channel_id="process_cli",
                mode=mode,
                work_mode=work_mode,
                project_dir=project_dir,
            )

        async def prepare_session_switch(self, provision_input):
            assert provision_input.mode == target_mode
            assert provision_input.previous_mode == current_mode
            assert provision_input.team_hint is target_is_team
            return await super().prepare_session_switch(provision_input)

    client = CrossModeClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)

    result = await app.run(
        _args(
            tmp_path,
            prompt="process_cli_target",
            session="process_cli_current",
            mode=current_mode,
            work_mode=current_work_mode,
            _operation="session.switch",
            _worker_result_file=str(worker_result),
        ),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    state = json.loads(worker_result.read_text(encoding="utf-8"))
    assert result == 0
    assert state == {
        "operation": "session.switch",
        "session_id": "process_cli_target",
        "mode": target_mode,
        "work_mode": target_work_mode,
        "project_dir": "D:/target-project",
        "model_name": "",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_error", [RuntimeError("commit failed"), asyncio.CancelledError()]
)
async def test_session_create_post_delivery_failure_aborts_only_prepared_lease(
    monkeypatch,
    tmp_path: Path,
    commit_error: BaseException,
) -> None:
    worker_result = tmp_path / "worker-result.json"

    class FailingCommitClient(SessionOperationClient):
        async def commit_session_provision(self, prepared, *, timing, context=None):
            self.calls.append(f"commit:{prepared.kind}:{timing.value}")
            raise commit_error

    client = FailingCommitClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="",
            _operation="session.create",
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert result == 0
    assert '"event_type": "session.created"' in stdout.getvalue()
    assert '"event_type": "runtime.error"' not in stdout.getvalue()
    assert worker_result.is_file()
    assert client.calls[-2:] == ["abort:create", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "prompt", "expected_session", "event_type"),
    [
        (
            "session.switch",
            "process_cli_target",
            "process_cli_target",
            "session.switched",
        ),
        (
            "session.fork",
            "branch title",
            "process_cli_forked",
            "session.forked",
        ),
    ],
)
async def test_switch_and_fork_commit_before_result_delivery(
    monkeypatch,
    tmp_path: Path,
    operation: str,
    prompt: str,
    expected_session: str,
    event_type: str,
) -> None:
    stdout = io.StringIO()
    worker_result = tmp_path / "worker-result.json"

    class BeforeDeliveryClient(SessionOperationClient):
        async def commit_session_provision(self, prepared, *, timing, context=None):
            assert timing is SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
            assert stdout.getvalue() == ""
            assert not worker_result.exists()
            return await super().commit_session_provision(
                prepared,
                timing=timing,
                context=context,
            )

    client = BeforeDeliveryClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)

    result = await app.run(
        _args(
            tmp_path,
            prompt=prompt,
            session="process_cli_source",
            _operation=operation,
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    state = json.loads(worker_result.read_text(encoding="utf-8"))
    assert result == 0
    assert document["payload"]["event_type"] == event_type
    assert state["session_id"] == expected_session
    assert not any(call.startswith("cleanup:") for call in client.calls)
    assert client.calls[-1] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "prompt"),
    [
        ("session.switch", "process_cli_target"),
        ("session.fork", "branch title"),
    ],
)
async def test_switch_and_fork_commit_failure_aborts_without_publishing_state(
    monkeypatch,
    tmp_path: Path,
    operation: str,
    prompt: str,
) -> None:
    class FailingCommitClient(SessionOperationClient):
        async def commit_session_provision(self, prepared, *, timing, context=None):
            self.calls.append(f"commit:{prepared.kind}:{timing.value}")
            raise RuntimeError("commit failed")

    client = FailingCommitClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()
    worker_result = tmp_path / "worker-result.json"

    result = await app.run(
        _args(
            tmp_path,
            prompt=prompt,
            session="process_cli_source",
            output="json",
            _operation=operation,
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert document["events"][0]["payload"]["event_type"] == "runtime.error"
    assert not worker_result.exists()
    assert client.calls[-2:] == [f"abort:{operation.removeprefix('session.')}", "close"]


@pytest.mark.asyncio
async def test_resume_missing_session_preserves_state_and_never_prepares(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = SessionOperationClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()
    worker_result = tmp_path / "worker-result.json"

    result = await app.run(
        _args(
            tmp_path,
            prompt="missing",
            session="process_cli_current",
            output="json",
            _operation="session.switch",
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert document["events"][0]["metadata"] == {"code": "NOT_FOUND"}
    assert not worker_result.exists()
    assert client.calls == ["start", "describe:missing", "close"]


@pytest.mark.asyncio
async def test_session_delete_uses_runtime_and_never_runs_chat_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = SessionOperationClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()
    worker_result = tmp_path / "worker-result.json"

    result = await app.run(
        _args(
            tmp_path,
            prompt="process_cli_target",
            session="process_cli_current",
            _operation="session.delete",
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    event = json.loads(stdout.getvalue())
    assert result == 0
    assert event["payload"]["event_type"] == "session.deleted"
    assert json.loads(worker_result.read_text(encoding="utf-8"))["session_id"] == (
        "process_cli_current"
    )
    assert client.calls == [
        "start",
        "describe:process_cli_target",
        "delete:process_cli:process_cli_target",
        "close",
    ]


@pytest.mark.asyncio
async def test_session_delete_accepts_owned_custom_session_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    client = SessionOperationClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt="custom_process_session",
            session="process_cli_current",
            output="json",
            _operation="session.delete",
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 0
    assert document["events"][0]["payload"]["event_type"] == "session.deleted"
    assert client.calls == [
        "start",
        "describe:custom_process_session",
        "delete:process_cli:custom_process_session",
        "close",
    ]


@pytest.mark.asyncio
async def test_session_delete_failure_does_not_publish_parent_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FailingDeleteClient(SessionOperationClient):
        async def delete_session(self, *, channel_id: str, session_id: str):
            self.calls.append(f"delete:{channel_id}:{session_id}")
            return SessionDeleteResult.failure(
                session_id,
                code="SESSION_BUSY",
                message="session is busy",
            )

    client = FailingDeleteClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    stdout = io.StringIO()
    worker_result = tmp_path / "worker-result.json"

    result = await app.run(
        _args(
            tmp_path,
            prompt="custom_process_session",
            session="process_cli_current",
            output="json",
            _operation="session.delete",
            _worker_result_file=str(worker_result),
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert document["events"][0]["metadata"] == {"code": "SESSION_BUSY"}
    assert not worker_result.exists()
    assert client.calls[-1] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["session.switch", "session.fork", "session.delete"],
)
async def test_session_operations_reject_foreign_owned_metadata(
    monkeypatch,
    tmp_path: Path,
    operation: str,
) -> None:
    class ForeignSessionClient(SessionOperationClient):
        async def describe_session(self, *, session_id: str):
            self.calls.append(f"describe:{session_id}")
            return SessionDescriptor(
                session_id=session_id,
                channel_id="tui",
                mode="agent.code.normal",
                work_mode="code",
            )

    client = ForeignSessionClient()
    monkeypatch.setattr(app, "InProcessRuntimeClient", lambda: client)
    target = "process_cli_foreign"
    stdout = io.StringIO()

    result = await app.run(
        _args(
            tmp_path,
            prompt=(target if operation != "session.fork" else "branch"),
            session=(target if operation == "session.fork" else "process_cli_current"),
            output="json",
            _operation=operation,
        ),
        stdout=stdout,
        stderr=io.StringIO(),
    )

    document = json.loads(stdout.getvalue())
    assert result == 1
    assert document["events"][0]["metadata"] == {"code": "NOT_FOUND"}
    assert not any(
        call.startswith(("prepare:", "commit:", "delete:")) for call in client.calls
    )


def test_interactive_worker_enables_runtime_interactions(tmp_path: Path) -> None:
    request = app._build_request(
        _args(
            tmp_path,
            output="human",
            _interactive_worker=True,
        ),
        session_id="runtime-session",
        request_id="runtime-request",
    )

    assert request.params["supports_user_interaction"] is True


def test_process_cli_has_no_server_or_transport_dependencies() -> None:
    package = Path(app.__file__).resolve().parent
    forbidden = (
        "jiuwenswarm.gateway",
        "jiuwenswarm.server.agent_ws_server",
        "jiuwenswarm.channels.tui",
        "websockets",
        "http.server",
    )
    violations: list[str] = []
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden):
                        violations.append(f"{source.name}:{node.lineno}:{alias.name}")
            if module.startswith(forbidden):
                violations.append(f"{source.name}:{node.lineno}:{module}")
    assert violations == []


def test_runtime_has_no_process_cli_dependency() -> None:
    runtime_package = Path(app.__file__).parents[2] / "runtime"
    violations: list[str] = []
    for source in runtime_package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("jiuwenswarm.channels.process_cli"):
                    violations.append(f"{source.name}:{node.lineno}:{module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("jiuwenswarm.channels.process_cli"):
                        violations.append(f"{source.name}:{node.lineno}:{alias.name}")
    assert violations == []


def test_new_entry_does_not_replace_existing_remote_cli() -> None:
    pyproject = Path(app.__file__).parents[3] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert 'jiuwenswarm = "jiuwenswarm.channels.cli.main:main"' in text
    assert 'jiuwenswarm-process = "jiuwenswarm.channels.process_cli.main:main"' in text


@pytest.mark.parametrize(
    "module_name",
    ["chat", "events", "gateway_client", "render", "_terminal"],
)
def test_historical_remote_cli_imports_alias_migrated_modules(module_name: str) -> None:
    historical = importlib.import_module(f"jiuwenswarm.cli.{module_name}")
    migrated = importlib.import_module(f"jiuwenswarm.channels.cli.{module_name}")

    assert historical is migrated
