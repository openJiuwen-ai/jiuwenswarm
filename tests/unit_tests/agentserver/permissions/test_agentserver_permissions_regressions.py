# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 6 small test modules)."""

from __future__ import annotations

import pytest
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission import (
    lifecycle,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
    AutoPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    AutoReviewer,
    IsolatedModelReviewerClient,
)
from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import (
    PersistentAuditWriter,
)
from tests.unit_tests.agentserver.permissions.auto_permission_test_support import (
    FakeBaseRail,
)
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from openjiuwen.core.foundation.tool import McpServerConfig
from jiuwenswarm.agents.harness.common.rails.subagent_tool_activity_rail import (
    SubagentToolActivityRail,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
import asyncio
import jiuwenswarm.agents.harness.common.tools.multi_session_toolkits as multi_session_toolkits_module
from jiuwenswarm.agents.harness.common.tools.multi_session_toolkits import (
    MultiSessionToolkit,
    SessionTask,
    Status,
)
import importlib.util
from pathlib import Path
import json
from openjiuwen.core.foundation.llm import AssistantMessage, ToolCall
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from jiuwenswarm.agents.harness.common.rails.interrupt import interrupt_helpers
from tests.unit_tests.agentserver.permissions.test_permission_answer_cutover import _request, answer_host
from tests.unit_tests.agentserver.permissions.test_permission_cold_build import cold
import ast


# --- from test_auto_permission_runtime_reload.py ---

def _auto_config(
    *,
    timeout_ms: int,
    output_format: str = "json_object",
    audit_enabled: bool,
    audit_root: str,
) -> dict[str, object]:
    return {
        "enabled": True,
        "mode": "auto",
        "auto": {
            "reviewer_timeout_ms": timeout_ms,
            "reviewer_output_format": output_format,
            "persistent_audit_enabled": audit_enabled,
            "persistent_audit_root": audit_root,
        },
    }

def test_same_rail_reload_refreshes_reviewer_and_audit_components(
    tmp_path,
) -> None:
    base = FakeBaseRail()
    reviewer = AutoReviewer(
        client=IsolatedModelReviewerClient(model=object()),
        timeout_ms=1000,
    )
    rail = AutoPermissionInterruptRail(
        base_rail=base,
        permission_config=_auto_config(
            timeout_ms=1000,
            audit_enabled=False,
            audit_root=str(tmp_path / "old"),
        ),
        workspace_root=tmp_path,
        auto_reviewer=reviewer,
    )
    updated = _auto_config(
        timeout_ms=4321,
        output_format="prompt",
        audit_enabled=True,
        audit_root=str(tmp_path / "new"),
    )

    rail.update_config(updated)

    assert rail.auto_reviewer is reviewer
    assert reviewer.timeout_seconds == pytest.approx(4.321)
    assert reviewer.client.output_format == "prompt"
    assert isinstance(rail.persistent_audit_writer, PersistentAuditWriter)
    assert rail.persistent_audit_writer.data_root == tmp_path / "new"
    first_writer = rail.persistent_audit_writer

    rail.update_config(updated)
    assert rail.persistent_audit_writer is first_writer

    disabled = _auto_config(
        timeout_ms=2000,
        audit_enabled=False,
        audit_root=str(tmp_path / "new"),
    )
    rail.update_config(disabled)

    assert reviewer.timeout_seconds == pytest.approx(2.0)
    assert reviewer.client.output_format == "json_object"
    assert "reviewer_min_confidence" not in rail.auto_options
    assert rail.persistent_audit_writer is None

def test_runtime_prepare_failure_preserves_installed_components(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = FakeBaseRail()
    reviewer = AutoReviewer(
        client=IsolatedModelReviewerClient(model=object()),
        timeout_ms=1000,
    )
    old_config = _auto_config(
        timeout_ms=1000,
        audit_enabled=False,
        audit_root=str(tmp_path / "old"),
    )
    rail = AutoPermissionInterruptRail(
        base_rail=base,
        permission_config=old_config,
        workspace_root=tmp_path,
        auto_reviewer=reviewer,
    )

    class FailingWriter:
        def __init__(self, *, data_root):
            del data_root
            raise RuntimeError("audit_writer_failed")

    monkeypatch.setattr(lifecycle, "PersistentAuditWriter", FailingWriter)

    with pytest.raises(RuntimeError, match="audit_writer_failed"):
        rail.update_config(
            _auto_config(
                timeout_ms=9000,
                output_format="prompt",
                audit_enabled=True,
                audit_root=str(tmp_path / "new"),
            )
        )

    assert reviewer.timeout_seconds == pytest.approx(1.0)
    assert not hasattr(reviewer, "min_confidence")
    assert reviewer.client.output_format == "json_object"
    assert rail.persistent_audit_writer is None
    assert rail.permission_config["auto"]["reviewer_timeout_ms"] == 1000
    assert base.config_updates == []

# --- from test_browser_runtime_adapter_wiring.py ---

@dataclass(frozen=True)
class _Settings:
    mcp_cfg: McpServerConfig

def _adapter(adapter_type: type[Any]) -> Any:
    adapter = object.__new__(adapter_type)
    adapter._workspace_dir = "/workspace"
    adapter._sys_operation = object()
    adapter._model_cache = {}
    adapter._coding_memory_rail = None
    adapter._browser_runtime_settings = None
    return adapter

def _subagent_config() -> dict[str, object]:
    return {
        "subagents": {
            interface_deep.STATUSLINE_SETUP_AGENT_TYPE: {"enabled": False},
            "general_agent": {"enabled": False},
            "research_agent": {"enabled": False},
            "explore_agent": {"enabled": False},
            "plan_agent": {"enabled": False},
            "code_agent": {"enabled": False},
            "browser_agent": {"enabled": True},
        }
    }

@pytest.mark.parametrize(
    ("adapter_type", "module"),
    (
        (JiuWenSwarmDeepAdapter, interface_deep),
        (JiuwenSwarmCodeAdapter, interface_code),
    ),
)
def test_deep_agent_spec_provider_passes_prepared_browser_settings(
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: type[Any],
    module: Any,
) -> None:
    adapter = _adapter(adapter_type)
    captured: dict[str, object] = {}
    original_cfg = McpServerConfig(
        server_id="browser-original",
        server_name="browser-original",
        server_path="stdio://browser-original",
        client_type="stdio",
        params={},
    )
    guarded_cfg = McpServerConfig(
        server_id="browser-guarded",
        server_name="browser-guarded",
        server_path="stdio://browser-guarded",
        client_type="stdio",
        params={},
    )
    profile = SimpleNamespace(network_guard_enforced=True)

    def build_browser(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return SimpleNamespace(factory_kwargs={"settings": _Settings(original_cfg)})

    monkeypatch.setattr(
        interface_deep,
        "apply_browser_runtime_security_profile",
        lambda config: (guarded_cfg, profile),
    )
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setattr(module, "build_browser_agent_config", build_browser)
    if adapter_type is JiuWenSwarmDeepAdapter:
        monkeypatch.setattr(
            adapter,
            "_sync_mcp_credentials_environment",
            lambda: True,
        )
        monkeypatch.setattr(interface_deep, "_load_custom_subagents", lambda **_: [])

    subagents, _ = adapter._build_configured_subagents(
        object(),
        _subagent_config(),
        {},
    )

    assert subagents is not None
    assert "settings" not in captured
    assert captured["sys_operation"] is adapter._sys_operation
    # Without it the subagent panel shows only thinking (issue #8637).
    assert any(isinstance(rail, SubagentToolActivityRail) for rail in captured["rails"])
    assert subagents[-1].factory_kwargs["settings"].mcp_cfg is guarded_cfg
    assert adapter._browser_runtime_settings.mcp_cfg is guarded_cfg
    if adapter_type is JiuwenSwarmCodeAdapter:
        assert subagents[-1].factory_kwargs["auto_create_workspace"] is False

@pytest.mark.parametrize(
    ("adapter_type", "module"),
    (
        (JiuWenSwarmDeepAdapter, interface_deep),
        (JiuwenSwarmCodeAdapter, interface_code),
    ),
)
def test_default_managed_browser_stays_unverified_and_uses_upstream_settings(
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: type[Any],
    module: Any,
) -> None:
    adapter = _adapter(adapter_type)
    captured_final: dict[str, object] = {}
    settings = SimpleNamespace(
        mcp_cfg=McpServerConfig(
            server_id="playwright_official_stdio",
            server_name="playwright-official",
            server_path="stdio://playwright",
            client_type="stdio",
            params={
                "command": "npx",
                "args": [
                    "-y",
                    "@playwright/mcp@latest",
                    "--caps=pdf,vision,devtools,config,network,storage,testing",
                ],
                "cwd": ".",
                "env": {},
                "timeout_s": 30,
            },
        )
    )

    def build_browser(*args: object, **kwargs: object) -> object:
        captured_final.update(kwargs)
        return SimpleNamespace(factory_kwargs={"settings": settings})

    for name in (
        "BROWSER_DRIVER",
        "PLAYWRIGHT_CDP_URL",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_MCP_CONFIG",
        "PLAYWRIGHT_MCP_EXTENSION",
        "PLAYWRIGHT_MCP_INIT_PAGE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(adapter, "_browser_runtime_enabled", lambda: True)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setattr(adapter, "_resolve_headless_from_config", lambda _: False)
    monkeypatch.setattr(
        adapter, "_resolve_managed_browser_binary_from_config", lambda _: ""
    )
    monkeypatch.setattr(interface_deep, "build_browser_agent_config", build_browser)
    monkeypatch.setattr(module, "build_browser_agent_config", build_browser)
    if adapter_type is JiuWenSwarmDeepAdapter:
        monkeypatch.setattr(
            adapter,
            "_sync_mcp_credentials_environment",
            lambda: True,
        )
        monkeypatch.setattr(interface_deep, "_load_custom_subagents", lambda **_: [])

    subagents, _ = adapter._build_configured_subagents(
        object(),
        _subagent_config(),
        {},
    )

    assert subagents is not None
    assert adapter._browser_runtime_settings is None
    assert adapter._browser_runtime_security_profile.network_guard_enforced is False
    assert (
        adapter._browser_runtime_security_profile.failure_reason
        == "unsupported_browser_driver"
    )
    assert "settings" not in captured_final
    assert captured_final["sys_operation"] is adapter._sys_operation
    assert subagents[-1].factory_kwargs["settings"] is settings

# --- from test_cached_agent_permission_readiness.py ---

class _CachedAgent:
    def __init__(self) -> None:
        self.executions = 0
        self.executed = asyncio.Event()

    async def process_message_stream(self, _request):
        self.executions += 1
        self.executed.set()
        if False:
            yield None

    def get_instance(self):
        return self

    async def ensure_instance(self):
        return self

class _ReadinessManager:
    def __init__(self, agent: _CachedAgent) -> None:
        self.agent = agent
        self.lookups = 0

    async def wait_for_permissions_ready(self) -> None:
        raise AssertionError("ordinary cached execution must not wait for global reload")

    def get_agent_nowait(self, _channel_id: str):
        self.lookups += 1
        return self.agent

def _multi_session_toolkit() -> MultiSessionToolkit:
    toolkit = MultiSessionToolkit(
        session_id="parent-session",
        channel_id="web",
        request_id="request-1",
        sub_agent_config=SimpleNamespace(),
    )
    toolkit.sessions.append(
        SessionTask(
            session_id="child-session",
            description="finished task",
            status=Status.COMPLETED,
            result="done",
        )
    )
    return toolkit

@pytest.mark.asyncio
async def test_multi_session_ordinary_cached_execution_does_not_wait_for_global_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _CachedAgent()
    manager = _ReadinessManager(agent)
    toolkit = _multi_session_toolkit()
    monkeypatch.setattr(
        multi_session_toolkits_module,
        "get_current_agent_manager",
        lambda: manager,
    )

    async def run_agent_streaming(_agent, *, inputs):
        _ = inputs
        agent.executions += 1
        yield SimpleNamespace(type="answer", payload={"output": "summary"})

    from openjiuwen.core.runner import Runner

    monkeypatch.setattr(Runner, "run_agent_streaming", run_agent_streaming)
    await toolkit.notify("child-session", Status.COMPLETED, result="done")
    assert manager.lookups == 1
    assert agent.executions == 1

@pytest.mark.asyncio
async def test_multi_session_without_cached_agent_keeps_raw_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _CachedAgent()
    manager = _ReadinessManager(agent)
    toolkit = _multi_session_toolkit()
    monkeypatch.setattr(
        multi_session_toolkits_module,
        "get_current_agent_manager",
        lambda: manager,
    )

    manager.agent = None
    await toolkit.notify("child-session", Status.COMPLETED, result="done")
    assert manager.lookups == 1
    assert agent.executions == 0

# --- from test_owner_scopes_matchers.py ---

def _load_owner_scopes_match_args():
    import sys

    mod_path = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "rails"
        / "permissions"
        / "owner_scopes.py"
    )
    spec = importlib.util.spec_from_file_location("owner_scopes_under_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod._match_args

def test_match_args_uses_permission_engine_command_matcher() -> None:
    match_args = _load_owner_scopes_match_args()
    assert match_args("ls *", {"command": "ls -la"}) is True
    assert match_args("ls *", {"command": "rm -rf /"}) is False

def test_match_args_uses_permission_engine_path_and_url_matchers() -> None:
    match_args = _load_owner_scopes_match_args()
    assert match_args("/tmp/*", {"path": "/tmp/a.txt"}) is True
    assert match_args("https://example.com/*", {"url": "https://example.com/x"}) is True
    assert match_args("https://example.com/*", {"url": "https://other.example/x"}) is False

# --- from test_permission_call_id_round_trip.py ---

def project(h):
    state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
    assert state and state.interrupted_tools
    interactions = []
    for interrupted in state.interrupted_tools.values():
        interactions.extend({"id": key, "value": value} for key, value in interrupted.interrupt_requests.items())
    queue = h.adapter._root_permission_queue
    if queue is not None and queue.snapshot_scope(root_session_id=h.adapter._parent_session_id):
        snapshot = queue.reconcile(
            {"result_type": "interrupt", "state": interactions, "interrupt_ids": [x["id"] for x in interactions]},
            root_session_id=h.adapter._parent_session_id,
        )
        assert snapshot and snapshot.cards
        first = snapshot.cards[0]
        matching = next(x for x in interactions if x["id"] == first.key.tool_call_id)
        return interrupt_helpers.build_verified_permission_ask_user_question(matching, first)
    return interrupt_helpers.convert_interactions_to_ask_user_question(interactions)

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_actual_host_core_two_cards_then_next_turn_reusing_call_id(answer_host, monkeypatch, mode):
    h = answer_host
    h.raw["permissions"]["mode"] = mode
    await h.create()
    await h.adapter.start_interaction(h.adapter._parent_session_id)
    trace = []
    for turn, calls in [(1, ["call_A", "call_B"]), (2, ["call_A"])]:
        h.script.responses = [
            AssistantMessage(content="", tool_calls=[ToolCall(
                id=call_id, type="function", name=h.tool.card.name,
                arguments=json.dumps({"value": f"turn-{turn}-{call_id}"}),
            ) for call_id in calls]),
            AssistantMessage(content="completed"),
        ]
        result = await _request(h, query=f"Run turn {turn}")
        assert result.ok
        for call_id in calls:
            payload = project(h)
            assert payload["request_id"] == call_id
            question = payload["questions"][0]
            if mode == "auto":
                assert question["card_id"] == call_id
            else:
                assert "card_id" not in question
            trace.append({"turn": turn, "request_id": payload["request_id"], "card_id": question.get("card_id")})
            answer = {"selected_options": ["allow_once"]}
            if mode == "auto":
                answer["card_id"] = question["card_id"]
            result = await _request(h, source=payload["source"], request_id=payload["request_id"], answers=[answer])
            assert result.ok
        state = h.adapter._instance.loop_session.get_state(INTERRUPTION_KEY)
        assert not state or not state.interrupted_tools
    assert h.executions == [{"value": "turn-1-call_A"}, {"value": "turn-1-call_B"}, {"value": "turn-2-call_A"}]
    assert [item["request_id"] for item in trace] == ["call_A", "call_B", "call_A"]

# --- from test_permission_engine_entrypoints.py ---

_REPO = Path(__file__).resolve().parents[4]

_INTERRUPT_HELPERS = (
    _REPO
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "common"
    / "rails"
    / "interrupt"
    / "interrupt_helpers.py"
)

def test_interrupt_helpers_uses_host_permission_rail() -> None:
    source = _INTERRUPT_HELPERS.read_text(encoding="utf-8")
    assert "JiuwenSwarmPermissionInterruptRail" in source
    assert "ToolPermissionHost" in source
    assert "from openjiuwen.harness.security.host" not in source
    # A Host subclass is allowed for Smart callbacks; do not mistake its
    # longer class name for direct construction of the SDK base class.
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PermissionInterruptRail"
        for node in ast.walk(ast.parse(source))
    )

def test_permission_engine_public_factory_import() -> None:
    from openjiuwen.harness.security import (
        PermissionConfirmResponse,
        PermissionEngine,
        ToolPermissionHost,
        build_permission_interrupt_rail,
    )

    assert callable(build_permission_interrupt_rail)
    assert PermissionEngine is not None
    assert ToolPermissionHost is not None
    assert PermissionConfirmResponse.__module__ == (
        "openjiuwen.harness.security.permission_engine.models"
    )

