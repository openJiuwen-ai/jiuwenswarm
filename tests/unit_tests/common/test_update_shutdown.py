# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Update helpers require the original desktop shutdown outcome, not absence."""
import ast
import asyncio
import json
import logging
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.update_shutdown import create_shutdown_receipt, complete_shutdown_receipt, require_clean_shutdown


def test_receipts_are_unique_and_failed_shutdown_cannot_become_success(tmp_path):
    first = create_shutdown_receipt(tmp_path)
    second = create_shutdown_receipt(tmp_path)
    assert first != second
    assert first.read_bytes() == b"pending\n"
    with pytest.raises(RuntimeError, match="Update refused"):
        require_clean_shutdown(first)
    complete_shutdown_receipt(first, ok=False)
    with pytest.raises(RuntimeError, match="cannot be marked successful"):
        complete_shutdown_receipt(first, ok=True)
    complete_shutdown_receipt(second, ok=True)
    require_clean_shutdown(second)
    assert second.read_bytes() == b"ok\n"


@pytest.mark.parametrize("state", ["pending", "failed", "missing"])
def test_update_cli_refuses_failed_attempt_before_an_absent_worker_can_override_it(tmp_path, monkeypatch, state):
    from jiuwenswarm.server.supervisor import shutdown

    receipt = create_shutdown_receipt(tmp_path)
    if state == "failed":
        complete_shutdown_receipt(receipt, ok=False)
    elif state == "missing":
        receipt.unlink()
    lifecycle = AsyncMock(return_value={"ok": True, "absent": True})
    monkeypatch.setattr(shutdown, "shutdown_resident_worker", lifecycle)
    monkeypatch.setattr(sys, "argv", ["shutdown", "--shutdown-worker", "--shutdown-receipt", str(receipt)])
    with pytest.raises(RuntimeError, match="Update refused"):
        shutdown.main()
    lifecycle.assert_not_awaited()


def test_successful_update_cli_may_verify_the_worker_is_absent(tmp_path, monkeypatch, capsys):
    from jiuwenswarm.server.supervisor import shutdown

    receipt = create_shutdown_receipt(tmp_path)
    complete_shutdown_receipt(receipt, ok=True)
    lifecycle = AsyncMock(return_value={"ok": True, "absent": True})
    monkeypatch.setattr(shutdown, "shutdown_resident_worker", lifecycle)
    monkeypatch.setattr(sys, "argv", ["shutdown", "--shutdown-worker", "--shutdown-receipt", str(receipt)])
    shutdown.main()
    lifecycle.assert_awaited_once()
    assert json.loads(capsys.readouterr().out) == {"ok": True, "absent": True}


@pytest.mark.parametrize("failure", ["exception", "non_ok", "invalid_result", "missing_receipt", "front_cleanup"])
def test_python_desktop_records_persistence_failure_after_worker_disappears(tmp_path, monkeypatch, failure):
    from jiuwenswarm.server.supervisor import shutdown

    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / "jiuwenswarm/channels/desktop/desktop_app.py").read_text(encoding="utf-8"))
    runtime_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DesktopRuntime")
    method = next(node for node in runtime_class.body if isinstance(node, ast.FunctionDef) and node.name == "shutdown")
    namespace = {"time": time, "asyncio": asyncio, "logger": logging.getLogger(__name__),
                 "complete_shutdown_receipt": complete_shutdown_receipt,
                 "_terminate_process_tree": Mock(), "_kill_process_tree": Mock()}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "desktop-shutdown", "exec"), namespace)
    receipt = create_shutdown_receipt(tmp_path)
    runtime = SimpleNamespace(_lock=threading.Lock(), _is_shutting_down=False, _startup_cancelled=threading.Event(),
        _abort_all_blob_saves=Mock(), processes={"agent": SimpleNamespace(poll=lambda: 0)},
        _update_shutdown_receipt=receipt)
    if failure == "missing_receipt":
        receipt.unlink()
    lifecycle = (AsyncMock(side_effect=RuntimeError("persistence incomplete")) if failure == "exception"
                 else AsyncMock(return_value=None if failure == "invalid_result" else {"ok": False}))
    if failure == "front_cleanup":
        runtime.processes["agent"].poll = Mock(side_effect=[None, 0, 0])
        namespace["_terminate_process_tree"].side_effect = RuntimeError("Front ownership unknown")
        lifecycle = AsyncMock(return_value={"ok": True, "absent": True})
    monkeypatch.setattr(shutdown, "shutdown_resident_worker", lifecycle)
    namespace["shutdown"](runtime)
    assert runtime.processes == {}
    if failure == "missing_receipt":
        assert not receipt.exists()
    else:
        assert receipt.read_bytes() == b"failed\n"
    with pytest.raises(RuntimeError, match="Update refused"):
        require_clean_shutdown(receipt)


@pytest.mark.parametrize("result", [{"ok": False}, None])
def test_python_update_helper_rejects_non_ok_shutdown(monkeypatch, result):
    from jiuwenswarm.server.supervisor import shutdown

    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / "jiuwenswarm/channels/desktop/desktop_app.py").read_text(encoding="utf-8"))
    method = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_shutdown_runtime_before_update")
    namespace = {"asyncio": asyncio}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "update-preflight", "exec"), namespace)
    monkeypatch.setattr(shutdown, "shutdown_resident_worker", AsyncMock(return_value=result))
    with pytest.raises(RuntimeError, match="Worker shutdown incomplete"):
        namespace[method.name]()


def test_python_linux_updater_forwards_its_attempt_receipt_before_replacing_files(tmp_path):
    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / "jiuwenswarm/channels/desktop/desktop_app.py").read_text(encoding="utf-8"))
    runtime_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DesktopRuntime")
    method = next(node for node in runtime_class.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_launch_linux_install_helper")
    namespace = {"Path": Path, "os": os, "shlex": shlex, "EXECUTABLE_NAME": "workswarm",
                 "get_user_workspace_dir": lambda: tmp_path, "logger": logging.getLogger(__name__),
                 "subprocess": SimpleNamespace(DEVNULL=-3, Popen=Mock())}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "linux-updater", "exec"), namespace)
    receipt = create_shutdown_receipt(tmp_path / "attempt with spaces")
    runtime = SimpleNamespace(_update_shutdown_receipt=receipt)
    assert namespace[method.name](runtime, tmp_path / "update.tar.gz", tmp_path / "app/workswarm")
    helper = (tmp_path / ".updates/_install_helper.sh").read_text(encoding="utf-8")
    assert f"--shutdown-receipt {shlex.quote(str(receipt))}" in helper
    assert helper.index("--shutdown-receipt") < helper.index("mv ")
