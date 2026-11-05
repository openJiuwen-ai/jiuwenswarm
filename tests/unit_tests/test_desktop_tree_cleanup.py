# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Best-effort desktop tree cleanup must preserve unproven Worker branches."""
import ast
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest


def test_service_tree_rechecks_recorded_pid_before_signalling(monkeypatch):
    from jiuwenswarm.common.process_supervision import signal_service_tree

    process = Mock(pid=100)
    process.create_time.return_value = 200.0
    monkeypatch.setattr(psutil, "Process", Mock(return_value=process))
    with pytest.raises(RuntimeError, match="identity changed"):
        signal_service_tree(100, expected_created=100.0)
    process.children.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


@pytest.mark.parametrize("failure", ["cmdline", "ppid", "terminate", "children"])
@pytest.mark.parametrize("force", [False, True])
def test_cleanup_continues_after_one_process_cannot_be_inspected_or_stopped(monkeypatch, failure, force):
    source = Path(__file__).resolve().parents[2] / "jiuwenswarm/channels/desktop/desktop_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_psutil_terminate")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    parent = Mock(pid=100)
    uncertain = Mock(pid=101)
    descendant = Mock(pid=102)
    sibling = Mock(pid=103)
    worker = Mock(pid=104)
    worker_child = Mock(pid=105)
    for child, ppid in [(uncertain, 100), (descendant, 101), (sibling, 100), (worker, 100), (worker_child, 104)]:
        child.cmdline.return_value = ["python", "-m", "service"]
        child.ppid.return_value = ppid
    worker.cmdline.return_value = ["python", "-m", "jiuwenswarm.server.worker.main"]
    parent.children.return_value = [uncertain, descendant, sibling, worker, worker_child]
    denied = psutil.AccessDenied(101)
    if failure == "children":
        parent.children.side_effect = psutil.AccessDenied(100)
    elif failure == "terminate":
        (uncertain.kill if force else uncertain.terminate).side_effect = denied
    else:
        getattr(uncertain, failure).side_effect = denied
    monkeypatch.setattr(psutil, "Process", Mock(return_value=parent))
    with pytest.raises(RuntimeError):
        namespace["_psutil_terminate"](100, force=force)
    (parent.kill if force else parent.terminate).assert_called_once()
    if failure != "children":
        (sibling.kill if force else sibling.terminate).assert_called_once()
    for child in [worker, worker_child]:
        child.kill.assert_not_called()
        child.terminate.assert_not_called()
    if failure in {"cmdline", "ppid"}:
        for child in [uncertain, descendant]:
            child.kill.assert_not_called()
            child.terminate.assert_not_called()
