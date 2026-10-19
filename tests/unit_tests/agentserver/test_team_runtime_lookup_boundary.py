# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


async def test_single_agent_cancel_terminates_existing_cross_mode_team_owner(monkeypatch):
    manager = SimpleNamespace(terminate_session_runtime=AsyncMock(return_value=True))
    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.team_manager",
                        SimpleNamespace(_team_manager=manager))
    assert await JiuWenSwarm._cancel_team_work_for_session(
        object(), "same-session", "web", log_prefix="cross-mode: "
    )
    manager.terminate_session_runtime.assert_awaited_once_with("same-session", reason="cross-mode: ")


def test_empty_cancel_cannot_import_or_construct_team_runtime(tmp_path):
    code = """
import asyncio, importlib.abc, sys
class NoTeam(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('jiuwenswarm.agents.harness.team', 'openjiuwen.agent_teams')):
            raise AssertionError('unused Team capability activated: ' + fullname)
sys.meta_path.insert(0, NoTeam())
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
assert not asyncio.run(JiuWenSwarm._cancel_team_work_for_session(object(), 'empty-session', 'web'))
assert 'jiuwenswarm.agents.harness.team.team_manager' not in sys.modules
assert 'openjiuwen.agent_teams' not in sys.modules
"""
    root = Path(__file__).parents[3]
    environment = dict(os.environ, PYTHONPATH=str(root), PYTHONUTF8="1",
                       JIUWENSWARM_HOME=str(tmp_path / "home"),
                       JIUWENSWARM_DATA_DIR=str(tmp_path / "data"))
    process = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment,
                             capture_output=True, text=True, encoding="utf-8", timeout=40)
    assert process.returncode == 0, process.stdout + process.stderr
