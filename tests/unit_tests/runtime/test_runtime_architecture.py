# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


RUNTIME_DIR = Path(__file__).parents[3] / "jiuwenswarm" / "runtime"
PROJECT_ROOT = Path(__file__).parents[3]
FORBIDDEN_IMPORT_PREFIXES = (
    "jiuwenswarm.gateway",
    "jiuwenswarm.server.agent_ws_server",
    "websockets",
)
FORBIDDEN_RUNTIME_TYPES = {"CliAgentPool", "CoreAgentBackend"}


def _runtime_sources() -> list[Path]:
    return sorted(RUNTIME_DIR.rglob("*.py"))


def _runtime_dependency_sources() -> list[Path]:
    package = PROJECT_ROOT / "jiuwenswarm"
    sources = list(_runtime_sources())
    for folder in ("cron", "xiaoyi_phone_tools"):
        sources.extend(
            (package / "agents" / "harness" / "common" / "tools" / folder).glob("*.py")
        )
    sources.append(
        package / "server" / "runtime" / "agent_adapter" / "interface_deep.py"
    )
    sources.extend(
        [
            package
            / "agents"
            / "harness"
            / "common"
            / "tools"
            / "send_file_to_user.py",
            package
            / "agents"
            / "harness"
            / "common"
            / "tools"
            / "multi_session_toolkits.py",
            package / "server" / "runtime" / "agent_adapter" / "team_helpers.py",
            package / "agents" / "harness" / "team" / "remote_member_bootstrap.py",
            package / "server" / "runtime" / "proactive_adapter.py",
        ]
    )
    return sorted(set(sources))


def test_runtime_public_api_has_no_transport_imports() -> None:
    violations: list[str] = []
    for source in _runtime_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for module in imported:
                if module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{source.name}:{node.lineno}: {module}")

    assert violations == []


def test_runtime_host_services_cold_import_does_not_load_runtime_core() -> None:
    script = """
import sys
import jiuwenswarm.runtime.host_services

unexpected = sorted(
    name for name in sys.modules
    if name == 'jiuwenswarm.runtime.service'
    or name == 'jiuwenswarm.server.runtime.agent_manager'
)
print('UNEXPECTED_RUNTIME_CORE=' + repr(unexpected))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "UNEXPECTED_RUNTIME_CORE=[]" in result.stdout


def test_runtime_lazy_public_exports_remain_discoverable() -> None:
    import jiuwenswarm.runtime as runtime

    assert set(runtime.__all__) <= set(dir(runtime))
    assert runtime.AgentRuntime.__name__ == "AgentRuntime"


def test_runtime_execution_dependencies_have_no_transport_imports() -> None:
    violations: list[str] = []
    for source in _runtime_dependency_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith(FORBIDDEN_IMPORT_PREFIXES) or module.startswith(
                    "jiuwenswarm.server.gateway_push"
                ):
                    violations.append(f"{source}:{node.lineno}: {module}")
    assert violations == []


def test_runtime_start_does_not_load_gateway_or_agentserver_modules() -> None:
    script = """
import asyncio
import sys
from jiuwenswarm.runtime import AgentRuntime

async def main():
    runtime = AgentRuntime()
    try:
        await runtime.start()
        names = sorted(
            name for name in sys.modules
            if name.startswith('jiuwenswarm.gateway')
            or name.startswith('jiuwenswarm.server.agent_ws_server')
        )
        print('TRANSPORT_MODULES=' + repr(names))
    finally:
        await runtime.close()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "TRANSPORT_MODULES=[]" in result.stdout


def test_runtime_does_not_reintroduce_alternate_agent_implementations() -> None:
    declared: set[str] = set()
    for source in _runtime_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        declared.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        )

    assert declared.isdisjoint(FORBIDDEN_RUNTIME_TYPES)


def test_agentserver_injects_runtime_manager_into_teammate_daemon() -> None:
    """The control-plane daemon runs outside a request ContextVar."""
    source = PROJECT_ROOT / "jiuwenswarm" / "server" / "app_agentserver.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    daemon_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_teammate_bootstrap_daemon"
    ]

    assert len(daemon_calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in daemon_calls[0].keywords}
    manager = keywords.get("agent_manager")
    assert isinstance(manager, ast.Call)
    assert isinstance(manager.func, ast.Attribute)
    assert manager.func.attr == "get_agent_manager"


def test_agentserver_session_lifecycle_uses_runtime_public_api() -> None:
    source = PROJECT_ROOT / "jiuwenswarm" / "server" / "agent_ws_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    forbidden = {
        "cancel_all_inflight_work",
        "cleanup_session_runtime",
        "create_session",
    }
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr == "_agent_manager"
            and node.func.attr in forbidden
        ):
            violations.append(f"{node.lineno}: {node.func.attr}")

    assert violations == []


def test_agentserver_does_not_bypass_runtime_for_global_team_cancel() -> None:
    source = PROJECT_ROOT / "jiuwenswarm" / "server" / "agent_ws_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    helper_name = "cancel_all_team_stream_tasks_across_managers"
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(helper_name):
                    violations.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == helper_name:
                    violations.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == helper_name:
                violations.append(f"{node.lineno}: call {helper_name}")
            elif isinstance(node.func, ast.Attribute) and node.func.attr == helper_name:
                violations.append(f"{node.lineno}: call {helper_name}")

    assert violations == []


def test_agentserver_session_delete_is_transport_only() -> None:
    source = PROJECT_ROOT / "jiuwenswarm" / "server" / "agent_ws_server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_handle_session_delete"
    ]
    assert len(handlers) == 1
    handler = handlers[0]
    runtime_delete_calls = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "delete_session"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Attribute)
        and isinstance(node.func.value.func.value, ast.Name)
        and node.func.value.func.value.id == "self"
        and node.func.value.func.attr == "_execution_runtime"
    ]
    assert len(runtime_delete_calls) == 1

    forbidden_symbols = {
        "Runner",
        "_agent_manager",
        "_heartbeat_runtime",
        "_plan_active_sessions",
        "_plan_exited_sessions",
        "_resolve_adapter",
        "abort_session_delete",
        "abort_trajectory_session_delete",
        "adapter",
        "begin_session_delete",
        "begin_trajectory_session_delete",
        "cleanup_session",
        "cleanup_session_runtime",
        "commit_session_delete",
        "commit_trajectory_session_delete",
        "delete_session_runtime",
        "evict_plan_session",
        "get_agent_nowait",
        "get_agent_sessions_dir",
        "get_session_metadata",
        "get_team_binding_store",
        "get_team_manager",
        "mark_session_deleted",
        "release_subagent_runtime_for_session",
        "remove_session_metadata_cache",
        "resolve_session_dir",
        "restore_session_after_failed_delete",
        "rmtree",
        "session_dir",
        "shutil",
        "team_manager",
        "unbind_session",
    }
    forbidden_import_prefixes = (
        "openjiuwen.core.runner",
        "jiuwenswarm.agents.harness.team",
        "jiuwenswarm.observability.session_delete",
        "jiuwenswarm.server.runtime.session",
        "jiuwenswarm.server.runtime.team_binding_store",
        "shutil",
    )
    violations: list[str] = []

    for node in ast.walk(handler):
        if isinstance(node, ast.Name) and node.id in forbidden_symbols:
            violations.append(f"{node.lineno}: name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_symbols:
            violations.append(f"{node.lineno}: attribute {node.attr}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(forbidden_import_prefixes):
                    violations.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(forbidden_import_prefixes):
                violations.append(f"{node.lineno}: import {node.module}")

    assert violations == []
