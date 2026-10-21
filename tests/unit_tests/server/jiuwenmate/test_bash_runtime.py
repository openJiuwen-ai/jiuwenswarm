"""Run the core Bash tool with Invocation-local process defaults."""

import asyncio
import importlib
import os
from types import SimpleNamespace

import pytest


def module():
    try:
        return importlib.import_module("jiuwenswarm.server.jiuwenmate.bash_runtime")
    except ModuleNotFoundError:
        pytest.fail("Persistent Bash provider is not implemented")


def tool(tmp_path, agent="agent-a"):
    from openjiuwen.core.sys_operation import SysOperation, SysOperationCard
    from openjiuwen.harness.schema.build_context import PARENT_SYS_OPERATION_EXTRAS_KEY
    from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import FeishuCliRuntime

    office = tmp_path / agent
    office.mkdir(exist_ok=True)
    session = office / "sessions" / "session-a"
    session.mkdir(parents=True, exist_ok=True)
    (office / "output").mkdir(exist_ok=True)
    context = SimpleNamespace(extras={}, execution=SimpleNamespace(
        namespace_id="local", agent_id=agent, session_id="session-a", execution_id="execution-a"), language="cn")
    context.extras[PARENT_SYS_OPERATION_EXTRAS_KEY] = SysOperation(SysOperationCard(id=agent))
    runtime = FeishuCliRuntime(tmp_path / "jm", config_loader=lambda: {})
    module().bind_bash_runtime(context, office=office, cli=runtime)
    return module().build_bash_tool({}, context), session


@pytest.mark.asyncio
async def test_published_agent_can_execute_bash_in_its_office(tmp_path):
    bash, session = tool(tmp_path)
    result = await bash.invoke({"command": 'python -c "import os; from pathlib import Path; Path(\'marker.txt\').write_text(os.getcwd())"'})
    assert result.success, result
    assert (session / "marker.txt").read_text() == str(session)


@pytest.mark.asyncio
async def test_bash_defaults_to_session_and_exposes_shared_output_directory(tmp_path):
    bash, session = tool(tmp_path)
    command = """python -c "import os; from pathlib import Path; Path('cwd.txt').write_text(os.getcwd()); Path('draft.md').write_text('final'); Path(os.environ['JIUWENMATE_OUTPUT_DIR'], 'final.md').write_text(Path('draft.md').read_text()); Path('output.txt').write_text(os.environ['JIUWENMATE_OUTPUT_DIR'])"""
    result = await bash.invoke({"command": command})
    assert result.success, result
    assert (session / "cwd.txt").read_text() == str(session)
    assert (session / "output.txt").read_text() == str(session.parent.parent / "output")
    assert (session.parent.parent / "output" / "final.md").read_text() == "final"


@pytest.mark.asyncio
async def test_relative_bash_workdir_is_resolved_from_the_current_session(tmp_path):
    bash, session = tool(tmp_path)
    nested = session / "nested"
    nested.mkdir()
    command = """python -c "import os; from pathlib import Path; Path('cwd.txt').write_text(os.getcwd())"""
    result = await bash.invoke({"command": command, "workdir": "nested"})
    assert result.success, result
    assert (nested / "cwd.txt").read_text() == str(nested)


@pytest.mark.asyncio
async def test_concurrent_bash_uses_separate_cli_environment(tmp_path):
    before = dict(os.environ)
    cwd = os.getcwd()
    a, a_dir = tool(tmp_path, "agent-a")
    b, b_dir = tool(tmp_path, "agent-b")
    command = 'python -c "import os; from pathlib import Path; Path(\'env.txt\').write_text(os.environ[\'LARKSUITE_CLI_CONFIG_DIR\'])"'
    results = await asyncio.gather(a.invoke({"command": command}), b.invoke({"command": command}))
    assert all(result.success for result in results), results
    assert "agent-a" in (a_dir / "env.txt").read_text()
    assert "agent-b" in (b_dir / "env.txt").read_text()
    assert dict(os.environ) == before
    assert os.getcwd() == cwd


@pytest.mark.asyncio
async def test_bash_timeout_is_not_transparently_retried(tmp_path):
    bash, office = tool(tmp_path)
    result = await bash.invoke({"command": 'python -c "from pathlib import Path; import time; p=Path(\'attempts\'); p.write_text(p.read_text()+\'x\' if p.exists() else \'x\'); time.sleep(5)"', "timeout": 1})
    assert not result.success
    assert (office / "attempts").read_text() == "x"
    assert bash.card.idempotent is not True


@pytest.mark.asyncio
async def test_timeout_stops_children_before_their_late_side_effect(tmp_path):
    bash, office = tool(tmp_path)
    result = await bash.invoke({"command": 'python -c "from pathlib import Path; import time; time.sleep(2); Path(\'late-side-effect\').touch()"', "timeout": 1})
    assert not result.success
    await asyncio.sleep(1.2)
    assert not (office / "late-side-effect").exists()


@pytest.mark.asyncio
async def test_stream_uses_the_same_environment(tmp_path):
    bash, office = tool(tmp_path)
    chunks = [chunk async for chunk in bash.stream({"command": 'python -c "from pathlib import Path; Path(\'stream.txt\').write_text(\'ok\')"'})]
    assert chunks[-1].success
    assert (office / "stream.txt").read_text() == "ok"


@pytest.mark.asyncio
async def test_background_process_is_stopped_at_invocation_cleanup(tmp_path):
    bash, office = tool(tmp_path)
    result = await bash.invoke({"command": 'python -c "import time; from pathlib import Path; Path(\'started\').touch(); time.sleep(6); Path(\'leaked\').touch()"', "run_in_background": True})
    assert result.success, result
    for _ in range(100):
        if (office / "started").exists():
            break
        await asyncio.sleep(0.01)
    assert (office / "started").exists()
    await bash.aclose()
    await asyncio.sleep(6.1)
    assert not (office / "leaked").exists()


def test_provider_does_not_change_personal_allowlist(tmp_path):
    from openjiuwen.core.sys_operation import LocalWorkConfig
    before = LocalWorkConfig().shell_allowlist
    tool(tmp_path)
    assert LocalWorkConfig().shell_allowlist == before


def test_bash_publication_includes_webpage_fetch():
    fields = module().with_bash_tool({})
    nested = fields["tools"][0]["params"]["tools"]
    assert nested == [{"type": "core.web_fetch"}]
    upgraded = module().ensure_web_fetch({
        "tools": [{"type": "jiuwenmate.bash", "params": {"tools": [{"type": "demo.plugin"}]}}],
        "system_prompt": "已有提示",
    })
    assert upgraded["tools"][0]["params"]["tools"] == [
        {"type": "demo.plugin"},
        {"type": "core.web_fetch"},
    ]
    assert "fetch_webpage" in upgraded["system_prompt"]
    assert module().ensure_web_fetch(upgraded) == upgraded


def test_duplicate_bash_tool_is_rejected():
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
    with pytest.raises(PersistentAgentError):
        module().with_bash_tool({"tools": [{"type": "jiuwenmate.bash"}, {"type": "jiuwenmate.bash"}]})


def test_plugin_that_builds_a_bash_tool_is_rejected_before_registration(tmp_path, monkeypatch):
    from openjiuwen.harness.schema.deep_agent_spec import BuiltinToolSpec, _TOOL_PROVIDER_REGISTRY
    from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError

    plugin, _ = tool(tmp_path)
    monkeypatch.setitem(_TOOL_PROVIDER_REGISTRY, "test.plugin.bash", lambda _params, _ctx: plugin)
    fields = module().with_bash_tool({"tools": [{"type": "test.plugin.bash"}]})
    from openjiuwen.core.sys_operation import SysOperation, SysOperationCard
    from openjiuwen.harness.schema.build_context import PARENT_SYS_OPERATION_EXTRAS_KEY
    from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import FeishuCliRuntime
    context = SimpleNamespace(extras={PARENT_SYS_OPERATION_EXTRAS_KEY: SysOperation(SysOperationCard(id="parent"))},
        execution=SimpleNamespace(namespace_id="local", agent_id="agent-a", session_id="session-a", execution_id="e-2"),
        language="cn")
    module().bind_bash_runtime(context, office=tmp_path / "agent-a",
                               cli=FeishuCliRuntime(tmp_path, config_loader=lambda: {}))
    with pytest.raises(PersistentAgentError):
        for declaration in fields["tools"]:
            BuiltinToolSpec.model_validate(declaration).build(language="cn", context=context)


@pytest.mark.asyncio
async def test_cleanup_failure_still_settles_budget_and_forgets_candidates(tmp_path, monkeypatch):
    from tests.unit_tests.server.jiuwenmate.test_session_scheduler import Host, NS, BudgetAccountRef

    async def fail_cleanup(_context):
        raise OSError("injected shell cleanup failure")

    host = Host(tmp_path)
    forgotten = []
    monkeypatch.setattr("jiuwenswarm.server.jiuwenmate.execution_driver.close_bash_runtime", fail_cleanup)
    monkeypatch.setattr(host.manager.commit_queue, "forget", lambda *args: forgotten.append(args))
    await host.manager.start()
    try:
        host.core.admit.add("cleanup")
        await host.send("cleanup")
        host.core.release("cleanup")
        await host.scheduler.drain()
        account = host.budgets.account(NS, BudgetAccountRef("agent", "mate_dev"))
        assert account.reserved_tokens == 0
        assert forgotten
    finally:
        await host.manager.aclose()
