"""Real core Bash + host SQLite; no external Feishu account or model call."""

import json
import os
from pathlib import Path
import sys

import pytest

from tests.integration_tests.jiuwenmate.test_web_real_core import (
    Server, ScriptedToolModel, FailingToolIntentRepository, read_execution_events_after_restart,
)
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.budget_repository import BudgetRepository
from jiuwenswarm.server.jiuwenmate.ports.window_backend import WindowBackendStore
from openjiuwen.harness.persistent_agent import EventType
from openjiuwen.harness.schema.deep_agent_spec import ModelSpec
from tests.unit_tests.server.jiuwenmate.test_lark_cli_runtime import setup_cli


@pytest.mark.asyncio
async def test_published_agent_can_execute_bash_in_its_office(tmp_path, monkeypatch):
    model = ScriptedToolModel([("bash", {"command": "python fake-cli.py"})])
    monkeypatch.setattr(ModelSpec, "build", lambda _: model)
    server = Server(tmp_path)
    colleague = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]
    office = Path(colleague["workspace_dir"])
    # A controlled CLI process observes the durable intent before any side effect.
    (office / "fake-cli.py").write_text(
        "import json, sqlite3, os\nfrom pathlib import Path\n"
        f"db = {str(server.db)!r}\n"
        "with sqlite3.connect(db) as conn:\n"
        "    events = conn.execute(\"SELECT event_json FROM execution_events WHERE event_type='tool.intent'\").fetchall()\n"
        "assert any(json.loads(row[0])['payload']['tool_name']=='bash' for row in events)\n"
        "Path('created.txt').write_text(os.getcwd(), encoding='utf-8')\n"
        "print('https://example.invalid/doc/test-document')\n", encoding="utf-8",
    )
    try:
        request_id = await server.send(colleague["agent_id"], "create-document")
        result = await server.ended(request_id)
        assert result["outcome"] == "completed", result
        assert (office / "created.txt").read_text(encoding="utf-8") == str(office)
        assert "https://example.invalid/doc/test-document" in result["replies"][-1]
        history = (await server.call(ReqMethod.JIUWENMATE_CHAT_HISTORY,
            agent_id=colleague["agent_id"], conversation_id="conv-1"))["requests"]
        assert "https://example.invalid/doc/test-document" in history[-1]["replies"][-1]
        events = read_execution_events_after_restart(server.db, request_id)
        assert len([event for event in events if event.event_type == EventType.TOOL_INTENT]) == 1
    finally:
        await server.adapter.aclose()


@pytest.mark.asyncio
async def test_bash_intent_failure_prevents_process_start(tmp_path, monkeypatch):
    model = ScriptedToolModel([("bash", {"command": 'python -c "from pathlib import Path; Path(\'marker\').write_text(\'created\')"'})])
    monkeypatch.setattr(ModelSpec, "build", lambda _: model)
    db = tmp_path / "agents.sqlite3"

    def manager_factory(service, on_event):
        return AgentManager(service, FailingToolIntentRepository(db), WindowBackendStore(db),
                            BudgetRepository(db), on_event=on_event)

    server = Server(tmp_path, manager_factory=manager_factory)
    colleague = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]
    try:
        result = await server.ask(colleague["agent_id"], "write-marker")
        assert result["outcome"] == "failed"
        assert not (Path(colleague["workspace_dir"]) / "marker").exists()
    finally:
        await server.adapter.aclose()


@pytest.mark.asyncio
async def test_prepared_private_cli_runs_through_published_bash(tmp_path, monkeypatch, setup_cli):
    if os.name != "nt":
        pytest.skip("Windows private launcher integration")
    from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import FeishuCliRuntime, _run
    from tests.unit_tests.server.jiuwenmate.test_lark_cli_runtime import config

    model = ScriptedToolModel([("bash", {"command": "lark-cli docs +create --as bot --doc-format markdown --content test"})])
    monkeypatch.setattr(ModelSpec, "build", lambda _: model)
    root = tmp_path / "中文 运行目录"
    server = Server(root)
    colleague = (await server.call(ReqMethod.JIUWENMATE_COLLEAGUE_ENSURE_DEFAULT))["colleague"]
    host = server.adapter._colleagues()
    package_loader, _, _ = setup_cli

    async def run(argv, stdin, env):
        if "install" not in argv:
            return await _run(argv, stdin, env)
        prefix = Path(argv[argv.index("--prefix") + 1])
        prefix.mkdir(parents=True, exist_ok=True)
        script = prefix / "fake-cli.py"
        script.write_text(
            "import os, sys, json\nfrom pathlib import Path\nargs=sys.argv[1:]\n"
            "if '--version' in args:\n    print('lark-cli version 1.0.94')\n"
            "else:\n"
            "    assert args[:2]==['docs','+create']\n"
            "    assert args[args.index('--as')+1]=='bot'\n"
            "    p=Path(os.environ['LARKSUITE_CLI_CONFIG_DIR'])/'config.json'\n"
            "    app=json.loads(p.read_text(encoding='utf-8'))['apps'][0]\n"
            "    assert app['appId']=='app-a' and app['defaultAs']=='bot'\n"
            "    assert app['appSecret']['source']=='file'\n"
            "    assert Path(app['appSecret']['id']).read_text(encoding='utf-8')=='test-secret'\n"
            "    Path('cli-used.txt').write_text(str(p), encoding='utf-8')\n"
            "    print('https://example.invalid/doc/private-cli-document')\n", encoding="utf-8",
        )
        (prefix / "lark-cli.cmd").write_text(
            f'@echo off\n"{sys.executable}" "%~dp0fake-cli.py" %*\n', encoding="utf-8",
        )
        return "installed"

    host.cli_runtime = FeishuCliRuntime(root, config_loader=lambda: config(colleague["agent_id"]),
                                       package_loader=package_loader, runner=run)
    try:
        assert (await host.cli_runtime.prepare("local", colleague["agent_id"]))["status"] == "ready"
        host.refresh_feishu_capability("local", colleague["agent_id"],
            skill_directories=host.cli_runtime.skill_directories("local", colleague["agent_id"]))
        result = await server.ask(colleague["agent_id"], "create-private-document")
        assert result["outcome"] == "completed", result
        office = Path(colleague["workspace_dir"])
        assert "runtime" in (office / "cli-used.txt").read_text(encoding="utf-8")
        assert "private-cli-document" in result["replies"][-1]
        assert "test-secret" not in host.current_snapshot("local", colleague["agent_id"]).spec_json
    finally:
        await server.adapter.aclose()
