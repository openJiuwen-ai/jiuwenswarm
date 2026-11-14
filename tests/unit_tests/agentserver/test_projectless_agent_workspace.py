import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.sys_operation import OperationMode
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
    resolve_request_project_dir,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
    build_filesystem_policy,
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


def test_projectless_task_workspace_keeps_long_session_ids_separate(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))

    shared_prefix = "session-" + "x" * 64
    first = get_projectless_task_workspace(shared_prefix + "-a")
    second = get_projectless_task_workspace(shared_prefix + "-b")

    assert first.root_dir != second.root_dir
    assert get_projectless_task_workspace(shared_prefix + "-a") == first
    assert get_projectless_task_workspace(shared_prefix + "-b") == second


def test_projectless_task_workspace_keeps_case_variant_sessions_separate(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))

    upper = get_projectless_task_workspace("Chat-A")
    lower = get_projectless_task_workspace("chat-a")

    assert upper.root_dir != lower.root_dir
    assert get_projectless_task_workspace("Chat-A") == upper


def test_projectless_task_workspace_reuses_legacy_long_session_binding(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))
    session_id = "session-" + "x" * 64
    legacy_key = projectless_workspace._slugify(session_id, fallback="default")
    legacy_root = projectless_workspace._allocate_task_root(
        projectless_workspace.get_projectless_tasks_dir(), "2026-01-01", legacy_key
    )
    projectless_workspace._write_task_metadata(
        legacy_root, session_id=session_id, task_name="existing task"
    )
    projectless_workspace._write_registered_root(
        projectless_workspace.get_projectless_tasks_dir(), legacy_key, legacy_root
    )

    assert get_projectless_task_workspace(session_id).root_dir == legacy_root


def test_projectless_agent_code_task_files_stay_in_their_own_workspace(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "Documents"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))
    internal_dir = tmp_path / "internal"
    internal_dir.mkdir()

    def paths(session_id, task_name):
        return resolve_runtime_workspace_paths(
            internal_workspace_dir=internal_dir,
            project_dir=None,
            workspace_dir=None,
            cwd=str(internal_dir),
            session_id=session_id,
            task_name=task_name,
            bind_request=True,
        )

    agent = paths("agent-session", "创建报告")
    code = paths("code-session", "实现页面")
    (agent.cwd / "draft.txt").write_text("agent draft", encoding="utf-8")
    (agent.outputs_dir / "report.txt").write_text("agent result", encoding="utf-8")
    (code.cwd / "draft.txt").write_text("code draft", encoding="utf-8")

    assert agent.runtime_workspace_root != code.runtime_workspace_root
    assert paths("agent-session", "标题已更改") == agent
    assert (agent.outputs_dir / "report.txt").read_text(encoding="utf-8") == "agent result"
    assert (code.cwd / "draft.txt").read_text(encoding="utf-8") == "code draft"
    assert not (code.outputs_dir / "report.txt").exists()
    assert not (internal_dir / "draft.txt").exists()


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
async def test_projectless_sessions_keep_their_own_execution_paths(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.runtime_prompt_rail.get_runtime_state_path",
        lambda _session_id: config_dir / "runtime.yaml",
    )
    builder = SystemPromptBuilder(language="en")
    agent = _FakeAgent(builder)
    rail = RuntimePromptRail(language="en", channel="web")
    rail.init(agent)
    first_bound = asyncio.Event()
    second_bound = asyncio.Event()

    async def run(session_id: str, own_event: asyncio.Event, other_event: asyncio.Event) -> tuple[Path, Path, str]:
        task_root = tmp_path / session_id
        work_dir = task_root / "work"
        outputs_dir = task_root / "outputs"
        work_dir.mkdir(parents=True)
        outputs_dir.mkdir()
        rail.set_session_id(session_id)
        rail.set_runtime_paths(
            task_workspace_root=str(task_root),
            task_work_dir=str(work_dir),
            task_outputs_dir=str(outputs_dir),
        )
        rail.set_execution_paths(cwd=str(work_dir), project_root=str(task_root), workspace=str(task_root))
        own_event.set()
        await other_event.wait()
        ctx = AgentCallbackContext(
            agent=agent,
            inputs=None,
            session=SimpleNamespace(get_session_id=lambda: session_id),
            extra={},
        )
        await rail.before_model_call(ctx)
        return Path(get_cwd()), Path(get_project_root()), builder.build()

    first, second = await asyncio.gather(
        run("session-a", first_bound, second_bound),
        run("session-b", second_bound, first_bound),
    )
    assert first[0] == tmp_path / "session-a" / "work"
    assert first[1] == tmp_path / "session-a"
    assert second[0] == tmp_path / "session-b" / "work"
    assert second[1] == tmp_path / "session-b"
    # Prompt builders are isolated by the manager's per-session Agent cache.


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
    from jiuwenswarm.server.runtime.agent_adapter import interface_code

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
    adapter._project_memory_rail = MagicMock()
    adapter._code_agent_rail = None
    adapter._permission_rail = MagicMock()
    adapter._eternal_conversation_rail = None
    adapter._force_english_runtime_prompt = False
    sandbox_patch = AsyncMock()
    monkeypatch.setattr(interface_code, "get_sandbox_runtime", lambda: {"enabled": True})
    monkeypatch.setattr(adapter, "apply_sandbox_runtime_patch", sandbox_patch)
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
    adapter._permission_rail.set_trusted_dirs.assert_called_with([str(task_root)])
    adapter._project_memory_rail.set_workspace_path.assert_called_with(str(task_root))
    assert adapter._sandbox_active_workspace_root == str(task_root)
    sandbox_patch.assert_awaited_with({"enabled": True}, files_changed=True)

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
    adapter._project_memory_rail.set_workspace_path.assert_called_with(
        str(explicit_project)
    )
    assert adapter._sandbox_active_workspace_root == str(explicit_project)
    assert sandbox_patch.await_count == 2


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


def test_project_resolution_preserves_legacy_workspace_for_team(tmp_path):
    workspace = tmp_path / "legacy-team-workspace"
    request = AgentRequest(
        request_id="req-team",
        channel_id="web",
        params={
            "mode": "team",
            "workspace_dir": str(workspace),
            "cwd": str(tmp_path / "caller-cwd"),
        },
    )

    assert resolve_request_project_dir(request) == str(workspace)
    assert resolve_request_project_dir(request, include_legacy_fallbacks=False) is None


def test_projectless_sandbox_mounts_task_root_instead_of_server_cwd(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "task"
    task_root.mkdir()
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    server_cwd = tmp_path / "server"
    server_cwd.mkdir()
    monkeypatch.chdir(server_cwd)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._sandbox_active_workspace_root = str(task_root)
    adapter._project_dir = None
    adapter._instance_overrides = {}

    project_dir = adapter._resolve_project_dir_for_sandbox()
    policy, _ = build_filesystem_policy(
        {}, project_dir=project_dir, shared_dir=agent_root,
        startup_mode="external",
    )
    mounts = policy["filesystem_policy"]["bind_mounts"]
    assert any(
        mount["host_path"] == str(task_root)
        and mount["sandbox_path"] == str(task_root)
        and mount["mode"] == "rw"
        for mount in mounts
    )
    assert not any(mount["host_path"] == str(server_cwd) for mount in mounts)


@pytest.mark.asyncio
async def test_sandbox_recreation_is_scoped_to_current_isolation(monkeypatch):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    recreate = AsyncMock(side_effect=["sandbox-a-new", "sandbox-b-new"])
    monkeypatch.setattr(jiuwenbox, "force_recreate_jiuwenbox_sandbox", recreate)
    monkeypatch.setattr(interface_deep, "is_enterprise", lambda: False)
    monkeypatch.setattr(
        interface_deep, "build_filesystem_policy", lambda *_args, **_kwargs: ({}, []),
    )
    monkeypatch.setattr(
        interface_deep, "_sys_operation_isolation_key", lambda card: card.isolation_key,
    )

    adapters = []
    for isolation_key in ("session-a", "session-b"):
        adapter = object.__new__(JiuWenSwarmDeepAdapter)
        adapter._sys_operation_card = SimpleNamespace(
            mode=OperationMode.SANDBOX,
            isolation_key=isolation_key,
            gateway_config=SimpleNamespace(
                isolation=SimpleNamespace(container_scope="custom"),
                launcher_config=SimpleNamespace(
                    base_url="http://sandbox/", sandbox_type="jiuwenbox", extra_params={},
                ),
            ),
        )
        adapter._workspace_dir = None
        adapter._is_code_agent = False
        adapter._resolve_project_dir_for_sandbox = lambda: None
        adapters.append(adapter)

    await asyncio.gather(
        *(adapter.apply_sandbox_runtime_patch({"files": {}}, files_changed=True)
          for adapter in adapters)
    )

    assert sorted(call.kwargs["shared_key"] for call in recreate.await_args_list) == [
        "http://sandbox|session-a", "http://sandbox|session-b",
    ]


@pytest.mark.asyncio
async def test_sandbox_recreation_without_isolation_key_does_not_clear_endpoint(
    monkeypatch, caplog,
):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    recreate = AsyncMock()
    monkeypatch.setattr(jiuwenbox, "force_recreate_jiuwenbox_sandbox", recreate)
    monkeypatch.setattr(interface_deep, "is_enterprise", lambda: False)
    monkeypatch.setattr(
        interface_deep, "build_filesystem_policy", lambda *_args, **_kwargs: ({}, []),
    )
    monkeypatch.setattr(interface_deep, "_sys_operation_isolation_key", lambda _card: None)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._sys_operation_card = SimpleNamespace(
        mode=OperationMode.SANDBOX,
        gateway_config=SimpleNamespace(
            launcher_config=SimpleNamespace(
                base_url="http://sandbox", sandbox_type="jiuwenbox", extra_params={},
            ),
        ),
    )
    adapter._workspace_dir = None
    adapter._is_code_agent = False
    adapter._resolve_project_dir_for_sandbox = lambda: None

    await adapter.apply_sandbox_runtime_patch({"files": {}}, files_changed=True)

    recreate.assert_not_awaited()
    assert "files policy change was not applied" in caplog.text


@pytest.mark.asyncio
async def test_sandbox_recreation_skips_session_scope_template(monkeypatch, caplog):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    recreate = AsyncMock()
    monkeypatch.setattr(jiuwenbox, "force_recreate_jiuwenbox_sandbox", recreate)
    monkeypatch.setattr(interface_deep, "is_enterprise", lambda: False)
    monkeypatch.setattr(
        interface_deep, "build_filesystem_policy", lambda *_args, **_kwargs: ({}, []),
    )
    monkeypatch.setattr(
        interface_deep, "_sys_operation_isolation_key", lambda _card: "{session_id}",
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._sys_operation_card = SimpleNamespace(
        mode=OperationMode.SANDBOX,
        gateway_config=SimpleNamespace(
            isolation=SimpleNamespace(container_scope="session"),
            launcher_config=SimpleNamespace(
                base_url="http://sandbox", sandbox_type="jiuwenbox", extra_params={},
            ),
        ),
    )
    adapter._workspace_dir = None
    adapter._is_code_agent = False
    adapter._resolve_project_dir_for_sandbox = lambda: None

    await adapter.apply_sandbox_runtime_patch({"files": {}}, files_changed=True)

    recreate.assert_not_awaited()
    assert "only CUSTOM isolation scope is supported" in caplog.text


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
