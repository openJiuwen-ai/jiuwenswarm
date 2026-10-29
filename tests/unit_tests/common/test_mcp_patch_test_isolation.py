# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""模拟全量 UT 已安装生产补丁的顺序，防止测试 fixture 再次包装 SDK。"""

import subprocess
import sys
from pathlib import Path


def test_mcp_fixtures_preserve_preinstalled_patch():
    code = """
import pytest

class PreinstalledPatch:
    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session):
        from jiuwenswarm.common import mcp_call_timeout_patch as patch
        from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
        from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import StreamableHttpClient
        from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr
        from openjiuwen.core.foundation.tool.base import Tool
        patch.apply_mcp_call_timeout_patch()
        self.patch = patch
        self.bindings = [(cls, name, cls.__dict__[name])
            for cls in (SseClient, StreamableHttpClient, ToolMgr, Tool)
            for name in ('connect', '_do_connect', 'disconnect', 'call_tool',
                         'list_tools', '_create_client', 'invoke', '__init_subclass__')
            if name in cls.__dict__]
        self.guards = {name: set(getattr(patch, name)) for name in
            ('_wrapped_methods', '_wrapped_connects', '_wrapped_factories')}

    def pytest_sessionfinish(self, session):
        assert self.patch._PATCHED
        assert all(cls.__dict__[name] is value for cls, name, value in self.bindings)
        assert all(getattr(self.patch, name) == value for name, value in self.guards.items())

raise SystemExit(pytest.main([
    'tests/unit_tests/common/test_mcp_call_timeout_recovery.py',
    'tests/unit_tests/common/test_mcp_call_timeout_patch.py',
    'tests/unit_tests/common/test_mcp_remote_lifecycle.py',
    '-k', 'stamped_timeout or unstamped_client or sse_timeout_reports_timeout or '
          '(test_discovery_lifecycle and normal) or toolmgr_owner_reconnect',
    '-o', 'addopts=', '-o', 'log_cli=false', '-q',
], plugins=[PreinstalledPatch()]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
