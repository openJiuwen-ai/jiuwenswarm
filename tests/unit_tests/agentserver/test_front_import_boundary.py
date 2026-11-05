# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Front control queries with a native warming Worker and module inspection."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from websockets.legacy.client import connect

from tests.unit_tests.agentserver.fixtures.front_process import free_port, receive, seed, terminate_owned

_OBSERVER = '''
import importlib.abc
import importlib.machinery
import json
import os
import sys
class Observer(importlib.abc.MetaPathFinder):
    baseline = set()
    def find_spec(self, name, path=None, target=None):
        if name != "jiuwenswarm.server.front.router":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        original = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                dispatch = module.MethodRouter._dispatch_control
                async def observed(instance, ws, request, lock):
                    await dispatch(instance, ws, request, lock)
                    prefixes = ("openjiuwen", "mcp", "jiuwenswarm.agents.harness",
                        "jiuwenswarm.server.runtime.agent_adapter",
                        "jiuwenswarm.server.runtime.skill.skill_manager", "jiuwenswarm.runtime.project_lsp",
                        "jiuwenswarm.server.runtime.session.kv_cache")
                    hits = {name for name in sys.modules if any(name == p or name.startswith(p + ".") for p in prefixes)}
                    if not Observer.baseline:
                        Observer.baseline.update(hits)
                    delta = sorted(hits - Observer.baseline)
                    with open(os.environ["ISSUE4689_MODULE_OBSERVER"], "a", encoding="utf-8") as file:
                        file.write(json.dumps({"pid": os.getpid(), "method": request.req_method.value,
                            "forbidden": delta, "loaded_modules": sorted(sys.modules)}) + "\\n")
                module.MethodRouter._dispatch_control = observed
        spec.loader = Loader()
        return spec
sys.meta_path.insert(0, Observer())
'''


@pytest.mark.asyncio
async def test_native_front_control_queries_do_not_load_runtime_dependencies(tmp_path: Path):
    root = Path(__file__).resolve().parents[3]
    data = tmp_path / "data"
    seed(data, root, "agent")
    observer = tmp_path / "observer"
    observer.mkdir()
    (observer / "sitecustomize.py").write_text(_OBSERVER, encoding="utf-8")
    observations = tmp_path / "modules.jsonl"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(observer), str(root))),
           "JIUWENSWARM_DATA_DIR": str(data), "ISSUE4689_MODULE_OBSERVER": str(observations)}
    env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
    env.pop("JIUWENSWARM_RUNTIME_STUB", None)
    port = free_port()
    with (tmp_path / "front.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(port)],
                                   cwd=root, env=env, stdout=log, stderr=log)
        ws = None
        try:
            for _ in range(400):
                try:
                    ws = await connect(f"ws://127.0.0.1:{port}")
                    acknowledgement = json.loads(await ws.recv())
                    assert acknowledgement.get("event") == "connection.ack"
                    break
                except OSError:
                    await asyncio.sleep(.01)
            assert ws is not None
            for method, params in (("session.list", {}), ("project.list", {}), ("config.get", {}),
                                   ("history.get", {"session_id": "acceptance-0", "page_idx": 1})):
                await ws.send(json.dumps({"request_id": method, "channel_id": "web", "req_method": method,
                                          "session_id": "acceptance-0", "params": params}))
                await receive(ws, method)
            # The observer appends each ~7.7KB JSON line via a buffered write;
            # splitlines() counts a torn (mid-flush) line as complete, so a
            # count-based wait can race the writer and hand json.loads a
            # truncated string. Retry until the whole file parses cleanly into
            # the expected row count instead.
            rows: list[dict] = []
            for _ in range(400):
                if observations.exists():
                    try:
                        rows = [json.loads(line) for line in observations.read_text(encoding="utf-8").splitlines()]
                    except json.JSONDecodeError:
                        rows = []
                    if len(rows) == 4:
                        break
                await asyncio.sleep(.01)
            assert len(rows) == 4
            assert len({row["pid"] for row in rows}) == 1
            assert all(row["forbidden"] == [] for row in rows)
        finally:
            if ws is not None:
                await ws.close()
            terminate_owned(process, [])
