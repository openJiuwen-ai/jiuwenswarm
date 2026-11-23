# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 2 small test modules)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs
from openjiuwen.harness.prompts import PromptAttachmentKind, PromptAttachmentManager
from openjiuwen.harness.rails.personal_context import PersonalContextRail
import ast


# --- from test_host_rail_smoke.py ---

@pytest.mark.asyncio
async def test_real_host_core_and_rail_connection(tmp_path: Path) -> None:
    """A configured Host starts Core and the Rail reads the same fixed Context root."""

    home = tmp_path / ".jiuwenswarm" / ".personal_context"
    source_root = tmp_path / "source"
    source_root.mkdir()
    context_root = home / "workspace" / "context"
    context_root.mkdir(parents=True)
    (context_root / "description.md").write_text("smoke context", encoding="utf-8")

    host = PersonalContextHostAPI(home=home)
    config = {
        "collection_enabled": True,
        "agent_use_enabled": True,
        "strategy_profile": "rules",
        "fetch_services": [
            {
                "service_id": "smoke-local",
                "provider": "local_files",
                "enabled": False,
                "time_range": {"mode": "all"},
                "source": {"root_dir": str(source_root)},
                "credentials": {},
            }
        ],
    }

    await host.configure(config)
    try:
        saved = yaml.safe_load(
            (home / "personal_context.yaml").read_text(encoding="utf-8")
        )
        assert saved["fetch_services"][0]["interval_seconds"] == 10_800.0

        status = await host.get_status()
        assert status.configured is True
        assert status.state == "RUNNING"
        assert status.pipeline_running is True

        manager = PromptAttachmentManager()
        agent = SimpleNamespace(prompt_attachment_manager=manager)
        rail = PersonalContextRail(home)
        rail.init(agent)
        ctx = AgentCallbackContext(
            agent=agent,
            inputs=ModelCallInputs(messages=[AssistantMessage(content="hello")]),
            session=SimpleNamespace(session_id="smoke-session"),
        )

        await rail.before_model_call(ctx)
        attachments = await manager.collect_for_session("smoke-session")
        assert len(attachments) == 1
        assert attachments[0].section == "personal_context"
        assert attachments[0].kind == PromptAttachmentKind.RUNTIME
        assert "smoke context" in (attachments[0].content or "")
        await rail.after_model_call(ctx)
        assert await manager.collect_for_session("smoke-session") == []

        await host.set_agent_use_enabled(False)
        await rail.before_model_call(ctx)
        assert await manager.collect_for_session("smoke-session") == []

        await host.set_agent_use_enabled(True)
        await rail.before_model_call(ctx)
        attachments = await manager.collect_for_session("smoke-session")
        assert len(attachments) == 1
        assert "smoke context" in (attachments[0].content or "")
        await rail.after_model_call(ctx)
        assert await manager.collect_for_session("smoke-session") == []
    finally:
        await host.stop(timeout_seconds=5)

    assert (await host.get_status()).state == "STOPPED"

# --- from test_personal_context_cancellation_static_rules.py ---

ROOT = Path(__file__).parents[3]

TARGETS = (
    (
        ROOT / "jiuwenswarm/server/personal_context/ws_handler.py",
        "_notify_runtime_enabled",
    ),
    (
        ROOT / "jiuwenswarm/server/runtime/agent_manager.py",
        "set_personal_context_runtime_enabled",
    ),
    (
        ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py",
        "refresh_personal_context_rail",
    ),
)

@pytest.mark.parametrize(
    ("path", "function_name"),
    TARGETS,
    ids=[function_name for _, function_name in TARGETS],
)
def test_cancelled_error_is_not_raised_inside_except_handler(
    path: Path,
    function_name: str,
) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    assert len(functions) == 1, (path, function_name)

    handlers = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Attribute)
        and isinstance(node.type.value, ast.Name)
        and node.type.value.id == "asyncio"
        and node.type.attr == "CancelledError"
    ]

    assert handlers, (path, function_name)
    assert all(
        not any(isinstance(statement, ast.Raise) for statement in handler.body)
        for handler in handlers
    ), (path, function_name)

