# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from unittest.mock import Mock

import psutil
import pytest

from jiuwenswarm.server.supervisor import shutdown


@pytest.mark.parametrize("disappears_at", ["cmdline", "ppid"])
def test_short_lived_child_does_not_skip_front_detach(monkeypatch, disappears_at):
    def process(pid, parent, module):
        return Mock(pid=pid, ppid=Mock(return_value=parent),
                    cmdline=Mock(return_value=["python", "-m", module]))

    front_module = "jiuwenswarm.server.app_agentserver"
    worker_module = "jiuwenswarm.server.worker.main"
    launcher = process(10, 1, front_module)
    front = process(11, 10, front_module)
    worker_launcher = process(12, 11, worker_module)
    worker = process(13, 12, worker_module)
    tool = process(14, 13, "tool")
    exiting = process(15, 13, "short-lived-tool")
    getattr(exiting, disappears_at).side_effect = psutil.NoSuchProcess(15)
    exiting.terminate.side_effect = psutil.NoSuchProcess(15)
    launcher.children.return_value = [front, worker_launcher, worker, tool, exiting]
    monkeypatch.setattr(psutil, "Process", Mock(return_value=launcher))

    shutdown.detach_front_process(10)

    launcher.terminate.assert_called_once_with()
    front.terminate.assert_called_once_with()
    worker_launcher.terminate.assert_not_called()
    worker.terminate.assert_not_called()
    tool.terminate.assert_not_called()


def test_detach_without_front_refuses_to_terminate_anything(monkeypatch):
    parent = Mock(pid=10, ppid=Mock(return_value=1), cmdline=Mock(return_value=["unrelated"]))
    parent.children.return_value = []
    monkeypatch.setattr(psutil, "Process", Mock(return_value=parent))
    with pytest.raises(RuntimeError, match="without an AgentServer"):
        shutdown.detach_front_process(10)
    parent.terminate.assert_not_called()
