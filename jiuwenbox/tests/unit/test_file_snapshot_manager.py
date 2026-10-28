# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Snapshot auditing keeps the existing upload/download vocabulary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenbox.models.common import AuditEventType
from jiuwenbox.models.sandbox import SandboxPhase
from jiuwenbox.server.sandbox_manager import SandboxManager


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,direction", [("read", "download"), ("compare_replace", "upload")])
@pytest.mark.parametrize("ok", [True, False])
async def test_snapshot_audit_direction(tmp_path, monkeypatch, operation, direction, ok):
    response = {"ok": ok, **({} if ok else {"error": "conflict"})}
    runtime = SimpleNamespace(file_snapshot=AsyncMock(return_value=(response, b"read")))
    audit = Mock()
    manager = SandboxManager(runtime=runtime, audit_logger=audit, state_dir=tmp_path, policy_reader=Mock())
    monkeypatch.setattr(manager, "_get_sandbox", Mock(return_value=SimpleNamespace(phase=SandboxPhase.READY)))
    monkeypatch.setattr(manager, "_mark_active", Mock())
    enforce = AsyncMock()
    monkeypatch.setattr(manager, "_enforce_path_policy", enforce)
    payload = {"operation": operation, "path": "/todo.json"}
    content = b"write" if operation == "compare_replace" else b""
    assert await manager.file_snapshot("box", payload, content) == (response, b"read")
    enforce.assert_awaited_once_with("box", "/todo.json", op="write" if content else "read")
    runtime.file_snapshot.assert_awaited_once_with("box", payload, content)
    args, data = audit.log.call_args
    assert args == (AuditEventType.FILE_TRANSFER, "box")
    assert data["direction"] == direction and data["operation"] == operation
    assert data["ok"] is ok and data["path"] == "snapshot_ipc"
