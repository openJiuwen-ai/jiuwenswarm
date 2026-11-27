import asyncio
from collections.abc import Callable
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import threading
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import (
    DeliveryMode,
    HarnessCapability,
    HarnessCheckpoint,
    HarnessState,
    ResumePolicy,
    SendReceipt,
)
from openjiuwen.harness_providers.io_adapter import TURN_LIFECYCLE

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime import extension_package_manager as equipment
from jiuwenswarm.server.runtime.agent_adapter import external_harness_helpers
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_session_metadata,
    init_session_metadata,
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CLI preservation")
def test_preserved_cli_paths_work_for_host_and_manifest_configuration(tmp_path, monkeypatch):
    from jiuwenswarm.common import external_cli_runtime, windows_cli_paths

    root = windows_cli_paths.windows_npm_root()
    old = tmp_path / "old-app" / "runtime" / "node-runtime"
    active = root / "installed" / "snapshot" / "prefix"
    active.mkdir(parents=True)
    (active / "host.cmd").touch()
    (active / "manifest.cmd").touch()
    mapping = root / "preserved" / "snapshot" / "mapping.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({"schema": 1, "source": str(old), "target": str(active)}))
    monkeypatch.setattr(external_cli_runtime, "activate_external_cli_runtime_paths", lambda: None)
    monkeypatch.setattr(external_harness_helpers, "_host_cli_agent_entry", lambda _: {"cli_path": str(old / "host.cmd")})
    host = external_harness_helpers._merge_host_external_cli_config("codex", {"model": "selected"})
    assert host["cli_path"] == str(active / "host.cmd")
    manifest = external_harness_helpers._merge_host_external_cli_config("codex", {"cli_path": str(old / "manifest.cmd"), "model": "selected"})
    assert manifest["cli_path"] == str(active / "manifest.cmd")
    assert manifest["model"] == "selected"
    monkeypatch.setattr(external_harness_helpers, "_host_cli_agent_entry", lambda _: {"cli_agent": "codex"})
    native = external_harness_helpers._merge_host_external_cli_config(
        "codex", {"codex_bin": str(old / "manifest.cmd"), "model": "selected"},
    )
    assert native["codex_bin"] == str(active / "manifest.cmd")
    assert "cli_path" not in native
    custom = tmp_path / "user-codex.cmd"
    custom.touch()
    result = external_harness_helpers._merge_host_external_cli_config("codex", {"cli_path": str(custom)})
    assert result["cli_path"] == str(custom)
    native_custom = external_harness_helpers._merge_host_external_cli_config("codex", {"codex_bin": str(custom)})
    assert native_custom["codex_bin"] == str(custom)


def test_merge_host_codex_turn_idle_invalid_host_config_falls_back(monkeypatch):
    from jiuwenswarm.common import external_cli_runtime

    monkeypatch.setattr(external_cli_runtime, "activate_external_cli_runtime_paths", lambda: None)
    monkeypatch.setattr(
        external_harness_helpers,
        "_host_cli_agent_entry",
        lambda _: {"cli_agent": "codex", "codex_turn_idle_timeout_s": "5min"},
    )
    merged = external_harness_helpers._merge_host_external_cli_config("codex", {})
    assert merged["turn_idle_timeout_s"] == 300.0


@pytest.fixture(autouse=True)
def enable_host_external_cli_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        external_harness_helpers,
        "_host_external_cli_agents",
        lambda: [{"cli_agent": "claude"}, {"cli_agent": "codex"}],
    )


@pytest.mark.asyncio
async def test_external_subagent_usage_snapshot_read_is_offloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.session import session_token_usage

    loop_thread = threading.get_ident()
    read_threads: list[int] = []
    session_id = "external-parent-session"

    def _read_usage(session: str) -> dict[str, Any]:
        read_threads.append(threading.get_ident())
        return {
            "schema_version": 1,
            "revision": 1,
            "session_id": session,
            "main": {
                "input_tokens": 10,
                "output_tokens": 2,
                "input_calls": 1,
                "output_calls": 1,
                "calls_total": 1,
            },
            "others": {
                "input_tokens": 5,
                "output_tokens": 1,
                "input_calls": 1,
                "output_calls": 1,
                "calls_total": 1,
            },
        }

    monkeypatch.setattr(session_token_usage, "get_session_token_usage", _read_usage)
    monkeypatch.setattr(interface_deep, "append_history_record", lambda **_kwargs: None)
    chunk = OutputSchema(
        type="subagent_updated",
        index=1,
        payload={
            "subagent_updated": {
                "parent_session_id": session_id,
                "subagent_id": "external-child",
                "status": "running",
                "task_description": "Review the requested changes",
            }
        },
    )

    payload = await external_harness_helpers._parse_external_chunk(
        chunk,
        has_streamed_answer=False,
    )

    assert payload is not None
    assert payload["session_usage"]["session_id"] == session_id
    assert read_threads and all(thread_id != loop_thread for thread_id in read_threads)


@pytest.fixture
def install_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[[str, str | None], Path]:
    packages: dict[str, Path] = {}

    def _install(name: str, provider_name: str | None) -> Path:
        package_dir = tmp_path / "agent_templates" / name
        package_dir.mkdir(parents=True)
        runtime = (
            {"provider_name": provider_name, "config": {"model": "test-model"}}
            if provider_name is not None
            else None
        )
        manifest: dict[str, Any] = {
            "package_type": "agent_template",
            "name": name,
            "description": f"{name} test expert",
        }
        if runtime is not None:
            manifest["runtime"] = runtime
        (package_dir / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        packages[name] = package_dir
        return package_dir

    monkeypatch.setattr(
        equipment,
        "resolve_equipment_runtime_id",
        lambda kind, identifier: str(identifier),
    )
    monkeypatch.setattr(
        equipment,
        "resolve_agent_template_dir",
        lambda name: packages[name],
    )
    return _install


def test_unbound_deep_agent_does_not_create_runtime_binding(
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / ".jiuwenswarm",
    )
    install_template("plain-expert", None)
    init_session_metadata(session_id="deep-agent-session")

    result = asyncio.run(external_harness_helpers.resolve_external_harness_binding(
        "deep-agent-session",
        {"agent_template_name": "plain-expert"},
    ))

    assert result == {"kind": "deep_agent"}
    assert "session_runtime_binding" not in get_session_metadata(
        "deep-agent-session",
        cache_bust=True,
    )


def test_first_external_resolve_returns_runtime_without_persisting(
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / ".jiuwenswarm",
    )
    package_dir = install_template("codex-expert", "codex")
    init_session_metadata(session_id="external-session")

    result = asyncio.run(external_harness_helpers.resolve_external_harness_binding(
        "external-session",
        {"agent_template_name": "codex-expert"},
    ))

    assert result["kind"] == "external_harness"
    assert result["agent_template_name"] == "codex-expert"
    assert result["provider_name"] == "codex"
    assert result["runtime"].provider_name == "codex"
    assert result["package_path"] == package_dir
    assert "session_runtime_binding" not in get_session_metadata(
        "external-session",
        cache_bust=True,
    )


@pytest.mark.parametrize("requested_template", [None, "claude-expert"])
def test_bound_external_session_rejects_runtime_or_template_switch(
    requested_template: str | None,
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / f".jiuwenswarm-{requested_template}",
    )
    install_template("codex-expert", "codex")
    install_template("claude-expert", "claudecode")
    session_id = f"bound-session-{requested_template}"
    init_session_metadata(session_id=session_id)
    external_harness_helpers.persist_external_harness_binding(
        session_id,
        {
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
        },
    )
    params = (
        {}
        if requested_template is None
        else {"agent_template_name": requested_template}
    )

    with pytest.raises(ValueError, match="新建会话"):
        asyncio.run(external_harness_helpers.resolve_external_harness_binding(
            session_id,
            params,
        ))


@pytest.mark.asyncio
async def test_root_returns_chat_error_before_child_creation_on_binding_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = False
    adapter._session_adapter_key = lambda session_id: str(session_id)
    child_requested = False

    async def _get_child(*args: Any, **kwargs: Any) -> Any:
        nonlocal child_requested
        child_requested = True
        raise AssertionError("child must not be created")

    adapter._get_session_adapter_for_request = _get_child
    monkeypatch.setattr(
        interface_deep,
        "resolve_external_harness_binding",
        lambda session_id, params: (_ for _ in ()).throw(
            ValueError("请新建会话后切换专家")
        ),
    )
    request = AgentRequest(
        request_id="request-2",
        channel_id="web",
        session_id="session-2",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
    )

    chunks = [
        chunk
        async for chunk in adapter._process_message_stream_impl(
            request,
            {"query": "hello"},
        )
    ]

    assert child_requested is False
    assert len(chunks) == 1
    assert chunks[0].payload == {
        "event_type": "chat.error",
        "error": "请新建会话后切换专家",
    }


@pytest.mark.asyncio
async def test_external_child_creation_persists_runtime_binding(
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / ".jiuwenswarm",
    )
    install_template("codex-expert", "codex")
    init_session_metadata(session_id="external-session")
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    child = interface_deep.JiuWenSwarmDeepAdapter()
    binding = await external_harness_helpers.resolve_external_harness_binding(
        "external-session",
        {"agent_template_name": "codex-expert"},
    )
    monkeypatch.setattr(
        parent,
        "_new_session_scoped_adapter",
        lambda session_id: child,
    )

    await parent._get_or_create_session_adapter(
        "external-session",
        external_harness_binding=binding,
        reserve_activity=True,
    )

    assert get_session_metadata("external-session", cache_bust=True)[
        "session_runtime_binding"
    ] == {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
    }
    child._unregister_session_agent_task("external-session")


@pytest.mark.asyncio
async def test_deep_agent_child_rejects_external_without_persisting_binding(
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / ".jiuwenswarm",
    )
    install_template("codex-expert", "codex")
    init_session_metadata(session_id="deep-then-external")
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child.mark_as_session_scoped("deep-then-external")
    child.mark_session_mcp_reconcile_started()
    parent._session_adapters["deep-then-external"] = child
    binding = await external_harness_helpers.resolve_external_harness_binding(
        "deep-then-external",
        {"agent_template_name": "codex-expert"},
    )

    with pytest.raises(
        external_harness_helpers.ExternalHarnessBindingError,
        match="新建会话",
    ):
        await parent._get_or_create_session_adapter(
            "deep-then-external",
            external_harness_binding=binding,
        )

    assert "session_runtime_binding" not in get_session_metadata(
        "deep-then-external",
        cache_bust=True,
    )


@pytest.mark.asyncio
async def test_external_child_creation_skips_deep_agent_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    child = interface_deep.JiuWenSwarmDeepAdapter()
    binding = {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
        "runtime": object(),
        "package_path": Path("codex-expert"),
    }
    lifecycle_calls: list[str] = []

    async def _unexpected(*args: Any, **kwargs: Any) -> None:
        lifecycle_calls.append("called")

    child.create_instance = _unexpected
    child.start_interaction = _unexpected
    monkeypatch.setattr(
        parent,
        "_new_session_scoped_adapter",
        lambda session_id: child,
    )
    monkeypatch.setattr(parent, "_reload_session_adapter_if_stale", _unexpected)

    selected = await parent._get_or_create_session_adapter(
        "external-child-session",
        external_harness_binding=binding,
        reserve_activity=True,
    )

    assert selected is child
    assert child._instance is None
    assert child._external_harness_binding is binding
    assert lifecycle_calls == []
    child._unregister_session_agent_task("external-child-session")


@pytest.mark.asyncio
async def test_child_creation_without_binding_uses_persisted_external_runtime(
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / ".jiuwenswarm",
    )
    install_template("codex-expert", "codex")
    init_session_metadata(session_id="restart-session")
    external_harness_helpers.persist_external_harness_binding(
        "restart-session",
        {
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
        },
    )
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child.create_instance = AsyncMock(
        side_effect=AssertionError("persisted External session created a DeepAgent")
    )
    monkeypatch.setattr(
        parent,
        "_new_session_scoped_adapter",
        lambda session_id: child,
    )

    selected = await parent._get_or_create_session_adapter("restart-session")

    assert selected is child
    binding = child._external_harness_binding
    assert binding["kind"] == "external_harness"
    assert binding["agent_template_name"] == "codex-expert"


@pytest.mark.asyncio
async def test_cached_session_child_rejects_runtime_kind_mismatch() -> None:
    parent = interface_deep.JiuWenSwarmDeepAdapter()
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child.mark_as_session_scoped("cached-session")
    child._external_harness_binding = {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
    }
    parent._session_adapters["cached-session"] = child

    with pytest.raises(
        external_harness_helpers.ExternalHarnessBindingError,
        match="新建会话",
    ):
        await parent._get_or_create_session_adapter(
            "cached-session",
            external_harness_binding={"kind": "deep_agent"},
        )


@pytest.mark.asyncio
async def test_external_equipment_admission_persists_without_native_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child._external_harness_binding = {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
    }
    saved: list[dict[str, Any]] = []

    async def _unexpected_native_load(params: dict[str, Any]) -> None:
        raise AssertionError("External child must not use DeepAgent equipment APIs")

    monkeypatch.setattr(child, "_marketplace_equipment_gate", lambda params: None)
    monkeypatch.setattr(child, "_connector_equipment_gate", lambda params: None)
    child._load_agent_template_for_request = _unexpected_native_load
    monkeypatch.setattr(
        session_metadata,
        "save_session_equipment",
        lambda session_id, **kwargs: saved.append(
            {"session_id": session_id, **kwargs}
        ),
    )
    request = AgentRequest(
        request_id="equipment-request",
        session_id="equipment-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "agent_template_name": "codex-expert",
            "plugin_names": [],
            "mcp": ["connector-a"],
        },
    )

    assert await child._ensure_chat_extensions(request) is None
    assert saved[0]["agent_template_name"] == "codex-expert"


@pytest.mark.asyncio
async def test_external_equipment_admission_rejects_plugins() -> None:
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child._external_harness_binding = {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
    }
    request = AgentRequest(
        request_id="plugin-request",
        session_id="plugin-session",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "agent_template_name": "codex-expert",
            "plugin_names": ["plugin-a"],
        },
    )

    response = await child._ensure_chat_extensions(request)

    assert response is not None
    assert response.payload["event_type"] == "chat.error"
    assert "plugin" in response.payload["error"]


@pytest.mark.asyncio
async def test_start_external_harness_uses_manifest_factory_and_session_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = SimpleNamespace()
    io = SimpleNamespace(start=AsyncMock())
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        external_harness_helpers,
        "create_harness",
        lambda **kwargs: captured.update(create=kwargs) or harness,
    )
    monkeypatch.setattr(
        external_harness_helpers,
        "build_harness_context",
        lambda **kwargs: captured.update(context=kwargs)
        or SimpleNamespace(agent_name=kwargs["agent_name"]),
    )
    monkeypatch.setattr(
        external_harness_helpers,
        "HarnessIOAdapter",
        lambda received_harness, **kwargs: captured.update(io=kwargs) or io,
    )

    result = await external_harness_helpers.start_external_harness(
        session_id="external-session",
        agent_template_name="codex-expert",
        provider_name="codex",
        runtime={"config": {"model": "test-model"}},
        package_path=tmp_path / "codex-expert",
        language="zh",
        cwd=tmp_path / "workspace",
        env={"TEST_TOKEN": "secret"},
    )

    assert result is io
    assert captured["create"]["provider"] == "codex"
    assert captured["context"]["host_session_id"] == "external-session"
    assert captured["context"]["resume_policy"] is ResumePolicy.NEW
    io.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_external_harness_requires_host_cli_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        external_harness_helpers,
        "_host_external_cli_agents",
        lambda: [],
    )

    with pytest.raises(ValueError, match="请先在配置界面启用 Claude"):
        await external_harness_helpers.start_external_harness(
            session_id="missing-host-cli",
            agent_template_name="claude-expert",
            provider_name="claudecode",
            runtime={"config": {}},
            package_path=tmp_path / "claude-expert",
            language="zh",
            cwd=tmp_path / "workspace",
            env={},
        )


@pytest.mark.asyncio
async def test_checkpoint_provider_restores_matching_session_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkpoint = HarnessCheckpoint(
        provider="claude-code",
        schema_version="1",
        agent_id="external_harness:claudecode:claude-expert",
        host_session_id="checkpoint-session",
        checkpoint_id="checkpoint-1",
        sequence=3,
        data={"session_id": "thread-1"},
        provider_session_id="thread-1",
    )
    harness = SimpleNamespace(
        card=SimpleNamespace(
            supports=lambda capability: capability is HarnessCapability.CHECKPOINT
        )
    )
    io = SimpleNamespace(start=AsyncMock())
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        external_harness_helpers,
        "create_harness",
        lambda **kwargs: harness,
    )
    monkeypatch.setattr(
        external_harness_helpers,
        "get_session_metadata",
        lambda session_id, **kwargs: {
            "session_harness_checkpoint": {
                "agent_template_name": "claude-expert",
                "provider_name": "claudecode",
                "checkpoint": {
                    "provider": checkpoint.provider,
                    "schema_version": checkpoint.schema_version,
                    "agent_id": checkpoint.agent_id,
                    "host_session_id": checkpoint.host_session_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "sequence": checkpoint.sequence,
                    "data": {"session_id": "thread-1"},
                    "provider_session_id": checkpoint.provider_session_id,
                    "revision": None,
                },
            }
        },
    )
    monkeypatch.setattr(
        external_harness_helpers,
        "build_harness_context",
        lambda **kwargs: captured.update(kwargs)
        or SimpleNamespace(agent_name=kwargs["agent_name"]),
    )
    monkeypatch.setattr(
        external_harness_helpers,
        "HarnessIOAdapter",
        lambda received_harness, **kwargs: io,
    )

    await external_harness_helpers.start_external_harness(
        session_id="checkpoint-session",
        agent_template_name="claude-expert",
        provider_name="claudecode",
        runtime={"config": {}},
        package_path=tmp_path / "claude-expert",
        language="zh",
        cwd=tmp_path / "workspace",
        env={},
    )

    assert captured["resume_policy"] is ResumePolicy.RESUME_IF_AVAILABLE
    assert captured["checkpoint"] == checkpoint


def _finished_external_io() -> SimpleNamespace:
    async def _outputs():
        yield OutputSchema(
            type=TURN_LIFECYCLE,
            index=0,
            payload={"turn_id": "turn-1", "kind": "started"},
        )
        yield OutputSchema(
            type=TURN_LIFECYCLE,
            index=1,
            payload={
                "turn_id": "turn-1",
                "kind": "finished",
                "final_output": "answer",
            },
        )

    return SimpleNamespace(
        state=HarnessState.IDLE,
        has_pending_interrupt=lambda: False,
        send=AsyncMock(
            return_value=SendReceipt(
                message_id="message-1",
                turn_id="turn-1",
                accepted_mode=DeliveryMode.AUTO,
            )
        ),
        outputs=_outputs,
    )


@pytest.mark.asyncio
async def test_external_session_reuses_started_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_batches = [
        [
            OutputSchema(
                type=TURN_LIFECYCLE,
                index=0,
                payload={"turn_id": f"turn-{index}", "kind": "started"},
            ),
            OutputSchema(
                type=TURN_LIFECYCLE,
                index=1,
                payload={
                    "turn_id": f"turn-{index}",
                    "kind": "finished",
                    "final_output": f"answer-{index}",
                },
            ),
        ]
        for index in (1, 2)
    ]

    async def _outputs():
        for chunk in output_batches.pop(0):
            yield chunk

    io = SimpleNamespace(
        state=HarnessState.IDLE,
        has_pending_interrupt=lambda: False,
        send=AsyncMock(
            side_effect=[
                SendReceipt(
                    message_id=f"message-{index}",
                    turn_id=f"turn-{index}",
                    accepted_mode=DeliveryMode.AUTO,
                )
                for index in (1, 2)
            ]
        ),
        outputs=_outputs,
    )
    start = AsyncMock(return_value=io)
    monkeypatch.setattr(
        external_harness_helpers,
        "start_external_harness",
        start,
    )
    adapter = SimpleNamespace(
        _external_harness=None,
        _external_harness_binding={
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
            "runtime": {"config": {}},
            "package_path": "codex-expert",
        },
        _parent_session_id="reuse-session",
        _project_dir="",
        _resolve_runtime_language=lambda: "zh",
    )

    payloads: list[dict[str, Any] | None] = []
    for index in (1, 2):
        request = AgentRequest(
            request_id=f"reuse-request-{index}",
            channel_id="web",
            session_id="reuse-session",
            req_method=ReqMethod.CHAT_SEND,
            params={"query": f"question-{index}"},
        )
        payloads.extend(
            [
                chunk.payload
                async for chunk in external_harness_helpers.process_external_harness_stream(
                    adapter,
                    request,
                    {"query": f"question-{index}"},
                )
            ]
        )

    assert payloads == [
        {"event_type": "chat.final", "content": "answer-1"},
        {"event_type": "chat.final", "content": "answer-2"},
    ]
    start.assert_awaited_once()
    assert io.send.await_count == 2


@pytest.mark.asyncio
async def test_process_external_harness_stream_projects_terminal_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        OutputSchema(
            type=TURN_LIFECYCLE,
            index=0,
            payload={"turn_id": "turn-1", "kind": "started"},
        ),
        OutputSchema(type="llm_output", index=1, payload={"content": "delta"}),
        OutputSchema(
            type=TURN_LIFECYCLE,
            index=2,
            payload={
                "turn_id": "turn-1",
                "kind": "finished",
                "final_output": "full answer",
            },
        ),
    ]

    async def _outputs():
        for chunk in chunks:
            yield chunk

    io = SimpleNamespace(
        state=HarnessState.IDLE,
        has_pending_interrupt=lambda: False,
        send=AsyncMock(
            return_value=SendReceipt(
                message_id="message-1",
                turn_id="turn-1",
                accepted_mode=DeliveryMode.AUTO,
            )
        ),
        outputs=_outputs,
    )
    adapter = SimpleNamespace(
        _external_harness=io,
        _external_harness_binding={
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
        },
    )
    request = AgentRequest(
        request_id="request-1",
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
    )

    result = [
        chunk
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter,
            request,
            {"query": "hello"},
        )
    ]

    assert [chunk.payload for chunk in result] == [
        {"event_type": "chat.delta", "content": "delta"},
        {"event_type": "chat.final", "content": "full answer"},
    ]
    assert result[-1].is_complete is True


@pytest.mark.asyncio
async def test_external_error_boundaries_sanitize_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-errorboundarysecret"

    async def _empty_outputs():
        if False:
            yield

    io = SimpleNamespace(
        state=HarnessState.IDLE,
        has_pending_interrupt=lambda: False,
        send=AsyncMock(
            side_effect=RuntimeError(f"input rejected api_key={secret}"),
        ),
        outputs=_empty_outputs,
    )
    adapter = SimpleNamespace(
        _external_harness=io,
        _external_harness_binding={
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
        },
    )
    request = AgentRequest(
        request_id="send-error-request",
        channel_id="web",
        session_id="send-error-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
    )

    result = [
        chunk
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter,
            request,
            {"query": "hello"},
        )
    ]

    assert result[0].payload["event_type"] == "chat.error"
    assert secret not in result[0].payload["error"]


@pytest.mark.asyncio
async def test_external_running_rejects_new_plain_message() -> None:
    io = SimpleNamespace(
        state=HarnessState.RUNNING,
        has_pending_interrupt=lambda: False,
        send=AsyncMock(),
    )
    adapter = SimpleNamespace(
        _external_harness=io,
        _external_harness_binding={"kind": "external_harness"},
    )
    request = AgentRequest(
        request_id="running-request",
        channel_id="web",
        session_id="running-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "new task"},
    )

    result = [
        chunk
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter,
            request,
            {"query": "new task"},
        )
    ]

    assert "任务正在进行中" in result[0].payload["error"]
    io.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_pending_interaction_resumes_same_turn() -> None:
    answer = InteractiveInput(raw_inputs="continue")
    chunks = [
        OutputSchema(
            type=TURN_LIFECYCLE,
            index=0,
            payload={"turn_id": "turn-1", "kind": "finished", "final_output": "done"},
        )
    ]

    async def _outputs():
        for chunk in chunks:
            yield chunk

    io = SimpleNamespace(
        state=HarnessState.RUNNING,
        is_pending_interrupt_resume_valid=lambda value: value is answer,
        send=AsyncMock(return_value=None),
        outputs=_outputs,
    )
    adapter = SimpleNamespace(
        _external_harness=io,
        _external_harness_binding={"kind": "external_harness"},
    )
    request = AgentRequest(
        request_id="resume-request",
        channel_id="web",
        session_id="resume-session",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )

    result = [
        chunk
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter,
            request,
            {"query": answer},
        )
    ]

    assert result[0].payload == {"event_type": "chat.final", "content": "done"}
    io.send.assert_awaited_once_with(answer)


@pytest.mark.asyncio
async def test_external_interaction_without_pending_request_is_rejected() -> None:
    answer = InteractiveInput(raw_inputs="continue")
    io = SimpleNamespace(
        state=HarnessState.RUNNING,
        is_pending_interrupt_resume_valid=lambda value: False,
        send=AsyncMock(),
    )
    adapter = SimpleNamespace(
        _external_harness=io,
        _external_harness_binding={"kind": "external_harness"},
    )
    request = AgentRequest(
        request_id="invalid-resume-request",
        channel_id="web",
        session_id="invalid-resume-session",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )

    result = [
        chunk
        async for chunk in external_harness_helpers.process_external_harness_stream(
            adapter,
            request,
            {"query": answer},
        )
    ]

    assert result[0].payload["event_type"] == "chat.error"
    io.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_cancel_requests_graceful_abort() -> None:
    abort = AsyncMock()
    io = SimpleNamespace(
        state=HarnessState.RUNNING,
        abort=abort,
        stop=AsyncMock(),
    )
    adapter = SimpleNamespace(_external_harness=io)
    request = AgentRequest(
        request_id="cancel-request",
        channel_id="web",
        session_id="cancel-session",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": "cancel"},
    )

    response = await external_harness_helpers.process_external_harness_interrupt(
        adapter,
        request,
    )

    assert response.payload["success"] is True
    abort.assert_awaited_once_with(immediate=False)


@pytest.mark.asyncio
async def test_stop_external_harness_is_idempotent() -> None:
    io = SimpleNamespace(stop=AsyncMock())
    adapter = SimpleNamespace(_external_harness=io)

    await external_harness_helpers.stop_external_harness(adapter)
    await external_harness_helpers.stop_external_harness(adapter)

    io.stop.assert_awaited_once()
    assert adapter._external_harness is None


@pytest.mark.asyncio
async def test_terminal_and_stop_persist_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external_harness:codex:codex-expert",
        host_session_id="checkpoint-save-session",
        checkpoint_id="checkpoint-save-1",
        sequence=4,
        data={"thread_id": "thread-1"},
        provider_session_id="thread-1",
    )
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        external_harness_helpers,
        "update_session_metadata",
        lambda **kwargs: persisted.append(kwargs),
    )

    async def _outputs():
        yield OutputSchema(
            type=TURN_LIFECYCLE,
            index=0,
            payload={"turn_id": "turn-1", "kind": "started"},
        )
        yield OutputSchema(
            type=TURN_LIFECYCLE,
            index=1,
            payload={"turn_id": "turn-1", "kind": "finished"},
        )

    harness = SimpleNamespace(
        card=SimpleNamespace(
            supports=lambda capability: capability is HarnessCapability.CHECKPOINT
        ),
        export_checkpoint=AsyncMock(return_value=checkpoint),
    )
    io = SimpleNamespace(
        harness=harness,
        state=HarnessState.IDLE,
        has_pending_interrupt=lambda: False,
        send=AsyncMock(
            return_value=SendReceipt(
                message_id="message-1",
                turn_id="turn-1",
                accepted_mode=DeliveryMode.AUTO,
            )
        ),
        outputs=_outputs,
        stop=AsyncMock(),
    )
    adapter = SimpleNamespace(
        _external_harness=io,
        _external_harness_binding={
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
        },
        _parent_session_id="checkpoint-save-session",
    )
    request = AgentRequest(
        request_id="checkpoint-save-request",
        channel_id="web",
        session_id="checkpoint-save-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
    )

    async for _ in external_harness_helpers.process_external_harness_stream(
        adapter,
        request,
        {"query": "hello"},
    ):
        pass
    await external_harness_helpers.stop_external_harness(adapter)

    assert len(persisted) >= 1
    assert persisted[0]["session_harness_checkpoint"]["agent_template_name"] == "codex-expert"


@pytest.mark.asyncio
async def test_external_template_unload_stops_and_invalidates_child() -> None:
    child = interface_deep.JiuWenSwarmDeepAdapter()
    child.mark_as_session_scoped("unload-session")
    child._external_harness_binding = {
        "kind": "external_harness",
        "agent_template_name": "codex-expert",
        "provider_name": "codex",
    }
    io = SimpleNamespace(stop=AsyncMock())
    child._external_harness = io

    await child.unload_equipment_if_loaded("agent_templates", "codex-expert")

    io.stop.assert_awaited_once()
    assert child._external_harness is None
    assert child._external_harness_binding is None


def test_runtime_binding_reads_only_the_manifest_card(
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The provider comes from openjiuwen's card; the package is parsed once, in create_harness."""
    from openjiuwen.harness.resources import extension_loader

    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / ".jiuwenswarm",
    )
    install_template("claude-expert", "claudecode")
    init_session_metadata(session_id="card-session")

    def _no_full_load(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the full AgentTemplate loader must not run while binding")

    # The async loader delegates to this module global, so one patch covers both.
    monkeypatch.setattr(extension_loader, "load_agent_template_package", _no_full_load)
    binding = asyncio.run(external_harness_helpers.resolve_external_harness_binding(
        "card-session",
        {"agent_template_name": "claude-expert"},
    ))
    assert binding["provider_name"] == "claudecode"
    assert binding["runtime"].config == {"model": "test-model"}


def test_runtime_provider_outside_the_host_allowlist_keeps_its_error(
    install_template: Callable[[str, str | None], Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.paths._workspace_base_dir",
        tmp_path / ".jiuwenswarm",
    )
    install_template("odd-expert", "unknown-provider")
    init_session_metadata(session_id="odd-session")
    with pytest.raises(
        ValueError,
        match=r"runtime.provider_name must be one of: claudecode, codex; got 'unknown-provider'",
    ):
        asyncio.run(external_harness_helpers.resolve_external_harness_binding(
            "odd-session",
            {"agent_template_name": "odd-expert"},
        ))


@pytest.mark.asyncio
async def test_external_user_input_question_carries_the_interrupt_id() -> None:
    chunk = OutputSchema(
        type="__interaction__",
        index=0,
        payload={"id": "ext-q", "value": {"kind": "user_input", "prompt": "Which?"}},
    )

    parsed = await external_harness_helpers._parse_external_chunk(chunk, has_streamed_answer=False)

    assert parsed["request_id"] == "ext-q"
    assert parsed["interrupt_id"] == "ext-q"
    assert parsed["kind"] == "ask_user"


_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git is not installed")


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """Keep the runner's global Git configuration and ``GIT_*`` variables out of the tests."""
    home = tmp_path / "home"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _git(folder: Path, *args: str) -> str:
    return subprocess.run(
        [_GIT, "-C", str(folder), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _repository(folder: Path) -> Path:
    folder.mkdir(parents=True)
    _git(folder, "init", "-q")
    (folder / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(folder, "add", "-A")
    _git(
        folder,
        "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
        "commit", "-qm", "first",
    )
    return folder


def _sessions_in(monkeypatch, sessions: Path) -> None:
    monkeypatch.setattr(
        external_harness_helpers,
        "get_default_project_session_workspace_dir",
        lambda session_id: sessions / session_id,
    )


def _worktree_adapter(project: Path) -> SimpleNamespace:
    adapter = SimpleNamespace(_project_dir=str(project))
    external_harness_helpers._bind_external_workspace(adapter, {"project_worktree": True})
    return adapter


@needs_git
def test_project_worktree_works_in_a_linked_worktree_of_the_project(
    tmp_path, monkeypatch, isolated_git
):
    project = _repository(tmp_path / "repo")
    sessions = tmp_path / "sessions"
    _sessions_in(monkeypatch, sessions)
    # The session's project is bound when its adapter is made; the request asks for a worktree.
    adapter = SimpleNamespace(_project_dir=str(project))
    external_harness_helpers._bind_external_workspace(adapter, {"project_worktree": True})
    folder = external_harness_helpers._external_workspace(adapter, "s1")
    assert folder == (sessions / "s1").resolve()
    assert (folder / "README.md").read_text(encoding="utf-8") == "fixture\n"
    common = Path(folder, _git(folder, "rev-parse", "--git-common-dir")).resolve()
    assert common == (project / ".git").resolve()
    # A later start of the same session finds the worktree made.
    again = SimpleNamespace(_project_dir="")
    external_harness_helpers._bind_external_workspace(
        again, {"project_dir": str(project), "project_worktree": True}
    )
    assert external_harness_helpers._external_workspace(again, "s1") == folder


@needs_git
def test_project_without_worktree_or_git_is_used_as_it_is(tmp_path, monkeypatch, isolated_git):
    plain = tmp_path / "plain"
    plain.mkdir()
    _sessions_in(monkeypatch, tmp_path / "sessions")
    adapter = SimpleNamespace(_project_dir="")
    external_harness_helpers._bind_external_workspace(
        adapter, {"project_dir": str(plain), "project_worktree": True}
    )
    assert external_harness_helpers._external_workspace(adapter, "s2") == plain.resolve()
    repo = _repository(tmp_path / "repo")
    other = SimpleNamespace(_project_dir="")
    external_harness_helpers._bind_external_workspace(other, {"project_dir": str(repo)})
    assert external_harness_helpers._external_workspace(other, "s3") == repo.resolve()


def _worktrees(project: Path) -> list[str]:
    listing = _git(project, "worktree", "list", "--porcelain")
    return [line for line in listing.splitlines() if line.startswith("worktree ")]


@needs_git
def test_project_worktree_refuses_a_session_folder_with_other_files(
    tmp_path, monkeypatch, isolated_git
):
    project = _repository(tmp_path / "repo")
    sessions = tmp_path / "sessions"
    _sessions_in(monkeypatch, sessions)
    (sessions / "s1").mkdir(parents=True)
    (sessions / "s1" / "notes.txt").write_text("kept\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        external_harness_helpers._external_workspace(_worktree_adapter(project), "s1")
    assert (sessions / "s1" / "notes.txt").read_text(encoding="utf-8") == "kept\n"
    assert len(_worktrees(project)) == 1


@needs_git
def test_project_worktree_is_made_again_after_its_folder_was_deleted(
    tmp_path, monkeypatch, isolated_git
):
    project = _repository(tmp_path / "repo")
    sessions = tmp_path / "sessions"
    _sessions_in(monkeypatch, sessions)
    folder = external_harness_helpers._external_workspace(_worktree_adapter(project), "s1")
    shutil.rmtree(folder)
    again = external_harness_helpers._external_workspace(_worktree_adapter(project), "s1")
    assert again == folder
    assert (folder / "README.md").read_text(encoding="utf-8") == "fixture\n"
    assert len(_worktrees(project)) == 2


@needs_git
def test_project_worktree_needs_a_commit(tmp_path, monkeypatch, isolated_git):
    project = tmp_path / "empty"
    project.mkdir()
    _git(project, "init", "-q")
    _sessions_in(monkeypatch, tmp_path / "sessions")
    with pytest.raises(ValueError, match="no commit"):
        external_harness_helpers._external_workspace(_worktree_adapter(project), "s1")


def test_project_worktree_reports_a_missing_git(tmp_path, monkeypatch):
    _sessions_in(monkeypatch, tmp_path / "sessions")
    monkeypatch.setattr(external_harness_helpers.shutil, "which", lambda _name: None)
    with pytest.raises(ValueError, match="git was not found"):
        external_harness_helpers._external_workspace(_worktree_adapter(tmp_path), "s1")


def test_project_worktree_reports_a_project_git_cannot_read(tmp_path, monkeypatch):
    _sessions_in(monkeypatch, tmp_path / "sessions")

    def refuse(project, args, timeout):
        return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership")

    monkeypatch.setattr(external_harness_helpers, "_run_project_git", refuse)
    with pytest.raises(ValueError, match="dubious ownership"):
        external_harness_helpers._external_workspace(_worktree_adapter(tmp_path), "s1")


@needs_git
@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_a_failed_worktree_add_raises_and_leaves_no_worktree(
    tmp_path, monkeypatch, isolated_git, failure
):
    project = _repository(tmp_path / "repo")
    sessions = tmp_path / "sessions"
    _sessions_in(monkeypatch, sessions)
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if "add" in command:
            # Let git register and fill the worktree, then report the failure.
            real_run(command, *args, **kwargs)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return subprocess.CompletedProcess(command, 128, "", "fatal: disk full")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(external_harness_helpers.subprocess, "run", run)
    expected = "disk full" if failure == "exit" else "did not finish"
    with pytest.raises(ValueError, match=expected):
        external_harness_helpers._external_workspace(_worktree_adapter(project), "s1")
    monkeypatch.setattr(external_harness_helpers.subprocess, "run", real_run)
    assert len(_worktrees(project)) == 1
    assert not (sessions / "s1").exists()


async def test_ensure_external_harness_finds_the_workspace_off_the_event_loop(
    tmp_path, monkeypatch
):
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def workspace(_adapter, _session_id):
        threads.append(threading.get_ident())
        return tmp_path

    monkeypatch.setattr(external_harness_helpers, "_external_workspace", workspace)
    io = SimpleNamespace(state=HarnessState.IDLE)
    start = AsyncMock(return_value=io)
    monkeypatch.setattr(external_harness_helpers, "start_external_harness", start)
    adapter = SimpleNamespace(
        _external_harness=None,
        _external_harness_binding={
            "kind": "external_harness",
            "agent_template_name": "codex-expert",
            "provider_name": "codex",
            "runtime": {"config": {}},
            "package_path": "codex-expert",
        },
        _parent_session_id="s1",
        _project_dir="",
        _resolve_runtime_language=lambda: "zh",
    )
    request = AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="s1",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "hello"},
    )
    assert await external_harness_helpers._ensure_external_harness(adapter, request) is io
    assert threads and threads[0] != loop_thread
    assert start.await_args.kwargs["cwd"] == tmp_path
