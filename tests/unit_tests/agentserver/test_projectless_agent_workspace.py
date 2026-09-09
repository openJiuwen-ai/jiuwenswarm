import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.sys_operation.cwd import (
    get_cwd,
    get_project_root,
    get_workspace,
    init_cwd,
    set_cwd,
)
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager
from openjiuwen.harness.tools import BashTool

from jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail import (
    RuntimePromptRail,
)
from jiuwenswarm.common import projectless_workspace
from jiuwenswarm.common.projectless_workspace import get_projectless_task_workspace
from jiuwenswarm.common.runtime_workspace import resolve_runtime_workspace_paths
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.agent_ws_server import (
    _uses_projectless_task_workspace,
    resolve_request_project_dir,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_manager import AgentManager


class _FakeAgent:
    def __init__(self, builder: SystemPromptBuilder) -> None:
        self.system_prompt_builder = builder
        self.prompt_attachment_manager = PromptAttachmentManager()


class _RecordingRuntimeRail:
    def __init__(self) -> None:
        self.runtime_paths: dict[str, object] = {}
        self.execution_paths: dict[str, str] = {}

    def set_runtime_paths(self, **kwargs) -> None:
        self.runtime_paths = kwargs

    def set_execution_paths(self, **kwargs) -> None:
        self.execution_paths = kwargs

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def test_projectless_task_workspace_has_stable_task_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))

    workspace = get_projectless_task_workspace("session-1", "生成月度报告")
    resumed = get_projectless_task_workspace("session-1", "改写后的标题")

    assert resumed == workspace
    assert workspace.root_dir.parent.name.count("-") == 2
    assert workspace.root_dir.name == "chat-1"
    assert workspace.work_dir == workspace.root_dir / "work"
    assert workspace.outputs_dir == workspace.root_dir / "outputs"
    assert workspace.work_dir.is_dir()
    assert workspace.outputs_dir.is_dir()
    metadata = json.loads(
        (workspace.root_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["chat_id"] == "chat-1"
    assert metadata["session_id"] == "session-1"
    assert metadata["query"] == "生成月度报告"
    assert metadata["title"] == "生成月度报告"
    assert not (tmp_path / "Documents" / ".jiuwenswarm").exists()


def test_projectless_task_workspace_uses_session_creation_date_on_first_use(
    tmp_path, monkeypatch
):
    tasks_dir = tmp_path / "Documents"
    registry_dir = tmp_path / "registry"
    sessions_dir = tmp_path / "agent" / "sessions"
    session_id = "session-created-yesterday"
    session_start = datetime.now().astimezone() - timedelta(days=1)
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "metadata.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "created_at": session_start.timestamp(),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tasks_dir))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(registry_dir))
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir",
        lambda: sessions_dir,
    )

    workspace = get_projectless_task_workspace(session_id, "跨天首次使用")
    resumed = resolve_runtime_workspace_paths(
        internal_workspace_dir=tmp_path / "internal",
        project_dir=None,
        workspace_dir=None,
        cwd=None,
        session_id=session_id,
        task_name="第二天继续对话",
        bind_request=True,
    )

    expected_date = session_start.strftime("%Y-%m-%d")
    assert workspace.root_dir.parent.name == expected_date
    assert resumed.runtime_workspace_root == workspace.root_dir
    assert resumed.cwd == workspace.root_dir / "work"


def test_projectless_task_workspace_preserves_full_request_in_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))

    workspace = get_projectless_task_workspace(
        "session-long",
        "这是一个很长的用户请求摘要，用来验证任务目录名称会被限制在合理长度范围内，避免路径过长",
    )

    assert workspace.root_dir.name == "chat-1"
    metadata = json.loads(
        (workspace.root_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["query"] == (
        "这是一个很长的用户请求摘要，用来验证任务目录名称会被限制在合理长度范围内，避免路径过长"
    )


def test_projectless_task_workspace_adds_numeric_suffix_for_same_day_collision(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))

    first = get_projectless_task_workspace("session-a", "生成报告")
    second = get_projectless_task_workspace("session-b", "生成报告")

    assert first.root_dir.name == "chat-1"
    assert second.root_dir.name == "chat-2"


def test_linux_documents_dir_honors_xdg_user_dirs(tmp_path, monkeypatch):
    config_dir = tmp_path / "xdg"
    config_dir.mkdir()
    documents_dir = tmp_path / "文档"
    (config_dir / "user-dirs.dirs").write_text(
        f'XDG_DOCUMENTS_DIR="{documents_dir}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("JIUWENSWARM_TASKS_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    monkeypatch.setattr(projectless_workspace.sys, "platform", "linux")

    assert (
        projectless_workspace.get_projectless_tasks_dir()
        == (documents_dir / "JiuwenSwarm").resolve()
    )


def test_windows_documents_dir_honors_known_folder(tmp_path, monkeypatch):
    documents_dir = tmp_path / "Moved Documents"
    monkeypatch.delenv("JIUWENSWARM_TASKS_DIR", raising=False)
    monkeypatch.setattr(projectless_workspace.sys, "platform", "win32")
    monkeypatch.setattr(
        projectless_workspace,
        "_get_windows_documents_dir",
        lambda: documents_dir,
    )

    assert (
        projectless_workspace.get_projectless_tasks_dir()
        == (documents_dir / "JiuwenSwarm").resolve()
    )


def test_macos_documents_dir_defaults_to_home_documents(tmp_path, monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_TASKS_DIR", raising=False)
    monkeypatch.setattr(projectless_workspace.sys, "platform", "darwin")
    monkeypatch.setattr(projectless_workspace.Path, "home", lambda: tmp_path)

    assert (
        projectless_workspace.get_projectless_tasks_dir()
        == (tmp_path / "Documents" / "JiuwenSwarm").resolve()
    )


def test_runtime_workspace_resolver_uses_same_projectless_layout_for_agent_and_code(
    tmp_path, monkeypatch
):
    internal_dir = tmp_path / "internal"
    internal_dir.mkdir()
    caller_cwd = tmp_path / "caller-cwd"
    caller_cwd.mkdir()
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))

    paths = resolve_runtime_workspace_paths(
        internal_workspace_dir=internal_dir,
        project_dir=None,
        workspace_dir=None,
        cwd=str(caller_cwd),
        session_id="session-code",
        task_name="检查代码",
        bind_request=True,
    )

    assert paths.internal_workspace_dir == internal_dir.resolve()
    assert paths.runtime_workspace_root.name == "chat-1"
    assert paths.project_root == paths.runtime_workspace_root
    assert paths.cwd == paths.runtime_workspace_root / "work"
    assert paths.outputs_dir == paths.runtime_workspace_root / "outputs"
    assert paths.cwd != caller_cwd
    assert paths.is_projectless


def test_runtime_workspace_resolver_keeps_explicit_project_as_boundary(tmp_path):
    internal_dir = tmp_path / "internal"
    project_dir = tmp_path / "project"
    project_cwd = project_dir / "src"
    outside_cwd = tmp_path / "outside"
    for directory in (internal_dir, project_cwd, outside_cwd):
        directory.mkdir(parents=True)

    inside = resolve_runtime_workspace_paths(
        internal_workspace_dir=internal_dir,
        project_dir=str(project_dir),
        workspace_dir=None,
        cwd=str(project_cwd),
        session_id="session-project",
        task_name=None,
        bind_request=True,
    )
    outside = resolve_runtime_workspace_paths(
        internal_workspace_dir=internal_dir,
        project_dir=str(project_dir),
        workspace_dir=None,
        cwd=str(outside_cwd),
        session_id="session-project",
        task_name=None,
        bind_request=True,
    )

    assert inside.runtime_workspace_root == project_dir.resolve()
    assert inside.cwd == project_cwd.resolve()
    assert outside.cwd == project_dir.resolve()
    assert not inside.is_projectless


def test_request_task_name_uses_raw_query_instead_of_rendered_envelope():
    request = AgentRequest(
        request_id="req-summary",
        channel_id="web",
        params={"query": "整理本周销售数据并生成报告", "mode": "agent"},
    )
    inputs = {
        "query": (
            '你收到一条消息：{"source":"web",'
            '"timestamp":"2026-08-27 15:00:00",'
            '"content":"整理本周销售数据并生成报告"}'
        )
    }

    assert (
        JiuWenSwarmDeepAdapter._resolve_request_task_name(request, inputs)
        == "整理本周销售数据并生成报告"
    )


@pytest.mark.asyncio
async def test_runtime_prompt_describes_projectless_agent_task_dirs(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "2026-08-27" / "report"
    work_dir = task_root / "work"
    outputs_dir = task_root / "outputs"
    work_dir.mkdir(parents=True)
    outputs_dir.mkdir()
    agent_dir = tmp_path / "agent-workspace"
    agent_dir.mkdir()
    config_dir = tmp_path / "config"

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_agent_workspace_dir",
        lambda: agent_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_user_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_runtime_state_path",
        lambda _session_id: config_dir / "runtime.yaml",
    )

    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    rail = RuntimePromptRail(language="en", channel="web")
    rail.init(agent)
    rail.set_runtime_paths(
        cwd=str(work_dir),
        project_dir=None,
        task_workspace_root=str(task_root),
        task_work_dir=str(work_dir),
        task_outputs_dir=str(outputs_dir),
    )
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=SimpleNamespace(get_session_id=lambda: "session-1"),
        extra={},
    )

    await rail.before_model_call(ctx)

    prompt = builder.build()
    assert "# Directory and File-Operation Boundaries" in prompt
    assert "## Current Task Directories" in prompt
    assert f"Current task root: `{task_root}`" in prompt
    assert f"Temporary working directory: `{work_dir}`" in prompt
    assert f"Final deliverables directory: `{outputs_dir}`" in prompt
    assert (
        "Resolve relative paths against the temporary working directory."
        in prompt
    )
    assert "## Current Project Directory" not in prompt
    assert str(agent_dir) in prompt


@pytest.mark.asyncio
async def test_runtime_prompt_binds_execution_paths_in_round_task(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "task"
    work_dir = task_root / "work"
    work_dir.mkdir(parents=True)
    config_dir = tmp_path / "config"

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_runtime_state_path",
        lambda _session_id: config_dir / "runtime.yaml",
    )
    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    rail = RuntimePromptRail(language="en", channel="web")
    rail.init(agent)
    rail.set_execution_paths(
        cwd=str(work_dir),
        project_root=str(task_root),
        workspace=str(task_root),
    )
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=SimpleNamespace(get_session_id=lambda: "session-execution"),
        extra={},
    )

    await rail.before_invoke(ctx)

    assert Path(get_cwd()) == work_dir
    assert Path(get_project_root()) == task_root
    assert Path(get_workspace()) == task_root


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "code"])
async def test_agent_and_code_rounds_rebind_bash_cwd_inside_long_lived_task(
    tmp_path, monkeypatch, mode
):
    internal_dir = tmp_path / "internal"
    project_dir = tmp_path / "project"
    internal_dir.mkdir()
    project_dir.mkdir()
    config_dir = tmp_path / "config"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_runtime_state_path",
        lambda _session_id: config_dir / "runtime.yaml",
    )

    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    rail = RuntimePromptRail(language="en", channel="web")
    rail.init(agent)
    rail.set_mode(mode)

    shell = SimpleNamespace(
        execute_cmd=AsyncMock(
            return_value=SimpleNamespace(
                code=0,
                message="ok",
                data=SimpleNamespace(
                    exit_code=0,
                    stdout=str(project_dir),
                    stderr="",
                ),
            )
        )
    )
    bash_tool = BashTool(SimpleNamespace(shell=lambda: shell), language="en")
    run_round = asyncio.Event()

    init_cwd(
        str(internal_dir),
        project_root=str(internal_dir),
        workspace=str(internal_dir),
    )

    async def long_lived_round() -> str:
        await run_round.wait()
        await rail.before_model_call(
            AgentCallbackContext(
                agent=agent,
                inputs=None,
                session=SimpleNamespace(get_session_id=lambda: "session-agent"),
                extra={},
            )
        )
        await bash_tool.invoke({"command": "pwd"})
        return shell.execute_cmd.await_args.kwargs["cwd"]

    # Capture the internal cwd before request-specific paths are available,
    # matching the long-lived DeepAgent supervisor task.
    round_task = asyncio.create_task(long_lived_round())
    await asyncio.sleep(0)
    rail.set_execution_paths(
        cwd=str(project_dir),
        project_root=str(project_dir),
        workspace=str(project_dir),
    )
    run_round.set()

    assert Path(await round_task) == project_dir


@pytest.mark.asyncio
async def test_code_round_does_not_overwrite_worktree_cwd_within_request(
    tmp_path, monkeypatch
):
    project_dir = tmp_path / "project"
    next_project_dir = tmp_path / "next-project"
    worktree_dir = tmp_path / "worktree"
    project_dir.mkdir()
    next_project_dir.mkdir()
    worktree_dir.mkdir()
    config_dir = tmp_path / "config"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_runtime_state_path",
        lambda _session_id: config_dir / "runtime.yaml",
    )

    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    rail = RuntimePromptRail(language="en", channel="web")
    rail.init(agent)
    rail.set_mode("code")
    rail.set_execution_paths(
        cwd=str(project_dir),
        project_root=str(project_dir),
        workspace=str(project_dir),
    )
    ctx = AgentCallbackContext(
        agent=agent,
        inputs=None,
        session=SimpleNamespace(get_session_id=lambda: "session-code"),
        extra={},
    )

    await rail.before_model_call(ctx)
    set_cwd(str(worktree_dir))
    # A following request in the same session supplies the same paths. It must
    # not advance the binding revision or reset the active worktree.
    rail.set_execution_paths(
        cwd=str(project_dir),
        project_root=str(project_dir),
        workspace=str(project_dir),
    )
    await rail.before_model_call(ctx)
    assert Path(get_cwd()) == worktree_dir

    rail.set_execution_paths(
        cwd=str(next_project_dir),
        project_root=str(next_project_dir),
        workspace=str(next_project_dir),
    )
    await rail.before_model_call(ctx)
    assert Path(get_cwd()) == next_project_dir


@pytest.mark.asyncio
async def test_code_runtime_config_uses_projectless_task_workspace(
    tmp_path, monkeypatch
):
    internal_dir = tmp_path / "internal"
    internal_dir.mkdir()
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))

    adapter = JiuwenSwarmCodeAdapter()
    runtime_rail = _RecordingRuntimeRail()
    deep_config = SimpleNamespace(cwd=None, project_root=None)
    adapter._instance = SimpleNamespace(
        ability_manager=SimpleNamespace(add=lambda *_args, **_kwargs: None),
        deep_config=deep_config,
    )
    adapter._agent_workspace_dir = str(internal_dir)
    adapter._project_dir = None
    adapter._workspace_dir = str(internal_dir)
    adapter._runtime_prompt_rail = runtime_rail
    adapter._subagent_rail = None
    adapter._project_memory_rail = None
    adapter._code_agent_rail = None
    adapter._permission_rail = None
    adapter._eternal_conversation_rail = None
    adapter._force_english_runtime_prompt = False
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setattr(adapter, "_resolve_output_language", lambda: "en")
    monkeypatch.setattr(adapter, "_resolve_prompt_channel", lambda _sid: "web")
    monkeypatch.setattr(adapter, "_resolve_model_name", lambda: "model")
    monkeypatch.setattr(adapter, "_write_runtime_state", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_update_rails_for_mode", AsyncMock())
    monkeypatch.setattr(adapter, "_set_user_interaction_enabled", AsyncMock())
    monkeypatch.setattr(adapter, "_update_tools_for_mode", AsyncMock())
    monkeypatch.setattr(adapter, "_update_session_tools", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_acp_runtime_tools", lambda *_args: None)
    monkeypatch.setattr(adapter, "_update_prompt_for_mode", lambda *_args: None)
    monkeypatch.setattr(adapter, "_register_shared_tool", lambda *_args: None)

    await adapter._update_runtime_config(
        adapter._RuntimeConfig(
            session_id="code-session",
            mode="code",
            request_id="request-1",
            channel_id="web",
            cwd=str(tmp_path / "ignored-cwd"),
            task_name="实现登录接口",
        )
    )

    task_root = Path(runtime_rail.execution_paths["workspace"])
    assert task_root.name == "chat-1"
    assert runtime_rail.execution_paths == {
        "cwd": str(task_root / "work"),
        "project_root": str(task_root),
        "workspace": str(task_root),
    }
    assert runtime_rail.runtime_paths["workspace_dir"] == str(internal_dir)
    assert runtime_rail.runtime_paths["task_outputs_dir"] == str(task_root / "outputs")
    assert deep_config.cwd == str(task_root / "work")
    assert deep_config.project_root == str(task_root)

    explicit_project = tmp_path / "explicit-project"
    explicit_project.mkdir()
    await adapter._update_runtime_config(
        adapter._RuntimeConfig(
            session_id="code-session",
            mode="code",
            request_id="request-2",
            channel_id="web",
            project_dir=str(explicit_project),
            cwd=str(tmp_path / "outside-project"),
            task_name="调研当前项目",
        )
    )

    assert runtime_rail.execution_paths == {
        "cwd": str(explicit_project),
        "project_root": str(explicit_project),
        "workspace": str(explicit_project),
    }
    assert deep_config.cwd == str(explicit_project)
    assert deep_config.project_root == str(explicit_project)


def test_agent_projectless_resolution_does_not_promote_legacy_cwd(tmp_path):
    request = AgentRequest(
        request_id="req-agent",
        channel_id="web",
        params={
            "mode": "agent",
            "cwd": str(tmp_path / "caller-cwd"),
            "trusted_dirs": [str(tmp_path / "trusted")],
        },
    )

    assert resolve_request_project_dir(request, include_legacy_fallbacks=False) is None
    assert resolve_request_project_dir(request) == str(tmp_path / "caller-cwd")


def test_projectless_task_workspace_detection_includes_agent_and_code_not_team():
    assert _uses_projectless_task_workspace(
        {"mode": "agent", "work_mode": "work"}, "tui"
    )
    assert _uses_projectless_task_workspace(
        {"mode": "agent", "work_mode": "code"}, "tui"
    )
    assert _uses_projectless_task_workspace({"mode": "code"}, "tui")
    assert not _uses_projectless_task_workspace(
        {"mode": "code.normal", "cwd": "C:/workspace/project"}, "tui"
    )
    assert not _uses_projectless_task_workspace(
        {
            "mode": "code.normal",
            "project_dir": "C:/workspace/project",
            "cwd": "C:/workspace/project",
        },
        "tui",
    )
    assert not _uses_projectless_task_workspace({"mode": "team"}, "tui")
    assert JiuWenSwarmDeepAdapter._is_projectless_agent_mode("agent")
    assert not JiuWenSwarmDeepAdapter._is_projectless_agent_mode("code")
    assert not JiuWenSwarmDeepAdapter._is_projectless_agent_mode("team")


@pytest.mark.asyncio
async def test_agent_manager_uses_project_dir_not_workspace_dir_for_identity(
    monkeypatch,
):
    manager = object.__new__(AgentManager)
    captured: dict[str, object] = {}

    async def get_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(process_message=AsyncMock(return_value="ok"))

    monkeypatch.setattr(manager, "wait_for_session_prewarm", AsyncMock())
    monkeypatch.setattr(manager, "get_agent", get_agent)
    request = SimpleNamespace(
        session_id="session-manager",
        channel_id="web",
        params={
            "mode": "code",
            "project_dir": "C:/projects/pi",
            "workspace_dir": "C:/internal/workspace",
        },
    )

    assert await manager.process_message(request) == "ok"
    assert captured["mode"] == "code"
    assert captured["project_dir"] == "C:/projects/pi"

    request.params = {
        "mode": "code",
        "workspace_dir": "C:/internal/workspace",
    }
    assert await manager.process_message(request) == "ok"
    assert captured["project_dir"] is None
