# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Probe examples must produce trees accepted by the current component contract."""

import pytest
from openjiuwen.core.events.ops_impl.common import OpsError
from openjiuwen.core.events.ops_impl.fs.models import FsAncestorsOutput, FsListOutput, FsStatus, FsWriteOutput
from openjiuwen.core.events.ops_impl.mcp import McpCallOutput, McpConnectOutput, McpStatus, McpToolDescriptor

from jiuwenswarm.extensions_v2.examples.fs_probe.fs_probe.state import ProbeSnapshot
from jiuwenswarm.extensions_v2.examples.fs_probe.fs_probe.views import panel_tree as fs_tree
from jiuwenswarm.extensions_v2.examples.mcp_probe.mcp_probe.state import ProbeState
from jiuwenswarm.extensions_v2.examples.mcp_probe.mcp_probe.views import panel_tree as mcp_tree
from jiuwenswarm.extensions_v2.webui.schema import validate_root


@pytest.mark.parametrize("status", [None, FsStatus.OK, FsStatus.ERROR])
def test_fs_probe_trees_follow_current_props(tmp_path, status):
    snapshot = None if status is None else ProbeSnapshot(
        cwd=str(tmp_path),
        refreshed_at="2026-11-04",
        listed=FsListOutput(status),
        exists_status=status,
        checks=(),
        preview=None,
        preview_stat=None,
        ancestors=FsAncestorsOutput(status),
        write=FsWriteOutput(status, str(tmp_path / "marker.md")),
    )
    report = validate_root(fs_tree(snapshot))
    assert report.ok, report.message


@pytest.mark.parametrize("status", [None, McpStatus.OK, McpStatus.ERROR])
def test_mcp_probe_trees_follow_current_props(status):
    error = OpsError("probe_error", "Probe failed") if status is McpStatus.ERROR else None
    state = None if status is None else ProbeState(
        connect=McpConnectOutput(
            status,
            connected=status is McpStatus.OK,
            tools=(McpToolDescriptor("echo", "Return input", {}),),
            error=error,
        ),
        register_error="Registration failed" if error else "",
        last_call=McpCallOutput(status, result="Example result", error=error),
        last_tool="echo",
    )
    report = validate_root(mcp_tree(state))
    assert report.ok, report.message
