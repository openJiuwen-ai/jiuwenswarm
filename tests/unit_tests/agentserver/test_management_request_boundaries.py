# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Management capabilities must not be prepared by an ordinary chat lookup."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface
from jiuwenswarm.server.runtime.agent_adapter.management_requests import ManagementRequests


def test_plain_facade_and_chat_routes_do_not_load_management_provider(tmp_path):
    """A fresh interpreter guards against transitive and constructor imports."""
    code = r'''
import asyncio
import importlib.abc
import sys

blocked = (
    "jiuwenswarm.server.runtime.agent_adapter.management_requests",
    "jiuwenswarm.server.runtime.session_assets",
)
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("ordinary chat imported optional management: " + fullname)

sys.meta_path.insert(0, Guard())
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm, compute_chat_send_mcp_needed
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
facade = JiuWenSwarm()
request = AgentRequest(request_id="plain", channel_id="web", req_method=ReqMethod.CHAT_SEND,
                       params={"query": "hello"})
async def check():
    for method in ("_handle_skilldev_request", "_handle_skills_request", "_handle_session_asset_request",
                   "_handle_plugins_request", "_handle_package_catalog_request"):
        assert await getattr(facade, method)(request) is None
asyncio.run(check())
assert facade._management_requests is None
assert compute_chat_send_mcp_needed({"mcp": [" one ", "one", None, "two"]}) == ["one", "two"]
assert not any(name in sys.modules for name in blocked)
print("MANAGEMENT_BOUNDARY_OK")
'''
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ, PYTHONPATH=str(root), JIUWENSWARM_DATA_DIR=str(tmp_path / 'data'),
               JIUWENSWARM_HOME=str(tmp_path / 'home'), PYTHONUTF8='1')
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'MANAGEMENT_BOUNDARY_OK' in result.stdout


@pytest.mark.asyncio
async def test_asset_rpc_preserves_session_scope_and_reuses_provider(monkeypatch):
    facade = interface.JiuWenSwarm.__new__(interface.JiuWenSwarm)
    manager = SimpleNamespace(handle_session_assets_list=AsyncMock(return_value={'assets': []}))
    from jiuwenswarm.server.runtime import session_assets

    calls = []
    monkeypatch.setattr(session_assets, 'get_session_asset_manager', lambda: calls.append(True) or manager)
    request = AgentRequest(request_id='assets', channel_id='web', session_id='session-a',
                           req_method=ReqMethod.SESSION_ASSETS_LIST, params={})
    first = await facade._handle_session_asset_request(request)
    provider = facade._get_management_requests()
    second = await facade._handle_session_asset_request(request)
    assert first.ok and second.ok
    assert calls == [True]
    assert provider is facade._get_management_requests()
    assert request.params == {}
    assert manager.handle_session_assets_list.await_args.args == ({'session_id': 'session-a'},)


def test_optional_service_failure_is_not_cached(monkeypatch):
    from jiuwenswarm.server.runtime import session_assets

    expected = object()
    attempts = []

    def create():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError('injected preparation failure')
        return expected

    monkeypatch.setattr(session_assets, 'get_session_asset_manager', create)
    provider = ManagementRequests(SimpleNamespace())
    with pytest.raises(RuntimeError, match='injected'):
        _ = provider.session_asset_manager
    assert provider.session_asset_manager is expected
    assert provider.session_asset_manager is expected
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_skill_sync_rejection_does_not_create_execution_adapter():
    facade = interface.JiuWenSwarm.__new__(interface.JiuWenSwarm)
    facade._ensure_adapter = lambda **kwargs: pytest.fail('rejected sync RPC created an adapter')
    method = next(iter(interface._SKILL_SYNC_WS_REJECTED))
    response = await facade._handle_skills_request(AgentRequest(
        request_id='reject-sync', channel_id='web', req_method=method, params={}))
    assert not response.ok
    assert response.payload['code'] == 'SKILL_SYNC_WS_UNSUPPORTED'
