# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regression coverage for Computer Use packaging probes and helper files."""

import ast
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_bundle_probe_runs_as_plain_python(monkeypatch, capsys):
    """The synchronous SDK engine compiles probes without top-level await flags."""
    module = ast.parse((ROOT / "scripts/verify_computer_use_bundle.py").read_text(encoding="utf-8"))
    probe = next(ast.literal_eval(node.value) for node in module.body
                 if isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id == "PROBE" for target in node.targets))
    events = []

    class Connection:
        def __init__(self, callback):
            pass

        async def connect(self):
            events.append("connect")
            return {"platform": "windows"}

        def close(self):
            events.append("close")

        async def wait_closed(self):
            events.append("wait_closed")

    connection_module = ModuleType("computer_use.native.connection")
    connection_module.Connection = Connection
    paths_module = ModuleType("computer_use.paths")
    paths_module.native_driver = lambda: {"command": "bundled-driver.exe"}
    monkeypatch.setitem(sys.modules, connection_module.__name__, connection_module)
    monkeypatch.setitem(sys.modules, paths_module.__name__, paths_module)
    namespace = {}
    exec(compile(probe, "computer-use-agent.py", "exec", dont_inherit=True), namespace)
    assert json.loads(capsys.readouterr().out) == {"platform": "windows", "driver": "bundled-driver.exe"}
    assert events == ["connect", "close", "wait_closed"]
    exec("observed = packaged_value + 1", namespace)
    assert namespace["observed"] == 42


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_driver_exec_collected_only_on_macos(platform):
    """The file-path helper must be collected as Python data only on macOS."""
    module = ast.parse((ROOT / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8"))
    blocks = [node for node in module.body if isinstance(node, ast.If)
              and any(isinstance(child, ast.Constant) and child.value == "native/driver_exec.py"
                      for child in ast.walk(node))]
    assert len(blocks) == 1
    calls = []

    def collect_data_files(package, **options):
        calls.append((package, options))
        return [("sdk/native/driver_exec.py", "computer_use/native")]

    namespace = {"sys": SimpleNamespace(platform=platform), "datas": [],
                 "collect_data_files": collect_data_files}
    exec(compile(ast.Module(body=blocks, type_ignores=[]), "jiuwenswarm.spec", "exec"), namespace)
    if platform == "darwin":
        assert calls == [("computer_use", {"include_py_files": True, "includes": ["native/driver_exec.py"]})]
        assert namespace["datas"] == [("sdk/native/driver_exec.py", "computer_use/native")]
    else:
        assert calls == []
        assert namespace["datas"] == []
