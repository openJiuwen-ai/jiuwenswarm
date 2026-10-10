# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Comparable native prepared-execution timing for baseline and candidate.

The isolated observer timestamps DeepAgent.send_input, after Agent/session
preparation, in both versions. It never replaces Runtime or reports queue ACKs.
The local model protocol fixture makes transport repeatable; external model
validation must be reported separately.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

from issue4689_front import seed, terminate_owned
from issue4689_native import ModelFixture, open_front, payload, port, send

OBSERVER = '''
import importlib.abc, importlib.machinery, json, os, sys, time
class Observer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "openjiuwen.harness.deep_agent":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        original = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                send_input = module.DeepAgent.send_input
                async def timed(instance, request):
                    stamp = time.perf_counter_ns()
                    with open(os.environ["ISSUE4689_ENTRY_LOG"], "a", encoding="utf-8") as file:
                        file.write(json.dumps({"request_id": request.request_id,
                                               "entered_at_ns": stamp, "pid": os.getpid()}) + "\\n")
                    return await send_input(instance, request)
                module.DeepAgent.send_input = timed
        spec.loader = Loader()
        return spec
sys.meta_path.insert(0, Observer())
'''


async def one_request(ws, request_id, mode, entry_log):
    sent = time.perf_counter_ns()
    await send(ws, request_id, "chat.send", "acceptance-0",
               {"query": "[4689-original] Reply resident-worker-ok", "mode": mode}, stream=True)
    while True:
        wire = json.loads(await asyncio.wait_for(ws.recv(), 90))
        if wire.get("request_id") != request_id:
            continue
        event = payload(wire)
        if wire.get("status") == "failed" or (isinstance(event, dict) and event.get("event_type") == "chat.error"):
            raise RuntimeError(f"native request {request_id} failed")
        if wire.get("is_final"):
            break
    rows = [json.loads(line) for line in entry_log.read_text(encoding="utf-8").splitlines()]
    entries = [row for row in rows if row["request_id"] == request_id]
    if len(entries) != 1:
        raise AssertionError(f"expected one real SDK entry for {request_id}, got {len(entries)}")
    entry = entries[0]
    return {"request_id": request_id, "sent_at_ns": sent, **entry,
            "entry_ms": (entry["entered_at_ns"] - sent) / 1e6}


async def process_samples(args, label, iteration, count, server_port, output):
    code = Path(getattr(args, label)).resolve()
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-chat-perf-") as tmp:
        root = Path(tmp)
        data = root / "data"
        seed(data, Path(args.baseline), args.mode)
        config_path = data / "config" / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["models"]["defaults"] = [{"model_id": "mdl_fixture", "is_default": True,
            "model_client_config": {"client_provider": "OpenAI", "api_base": f"http://127.0.0.1:{server_port}/v1",
                                    "api_key": "fixture", "model_name": "fixture-model"},
            "model_config_obj": {"temperature": 0, "max_tokens": 128}}]
        config["progressive_tool_enabled"] = False
        config["permissions"]["enabled"] = False
        config.setdefault("react", {}).update(enable_skill_evolution=False, enable_read_image_multimodal=False)
        config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        observer = root / "observer"
        observer.mkdir()
        (observer / "sitecustomize.py").write_text(OBSERVER, encoding="utf-8")
        entry_log = root / "entries.jsonl"
        env = os.environ.copy()
        env.update(PYTHONPATH=os.pathsep.join((str(observer), str(code))), JIUWENSWARM_DATA_DIR=str(data),
                   JIUWENSWARM_HOME=str(root / "home"), JIUWENSWARM_RUNTIME_PROFILE="desktop",
                   ISSUE4689_ENTRY_LOG=str(entry_log), NO_PROXY="127.0.0.1,localhost")
        env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
        env.pop("PYTHONPROFILEIMPORTTIME", None)
        front_port = port()
        log_path = output.with_suffix(f".{label}-{iteration}.log")
        with log_path.open("wb") as log:
            started = time.perf_counter_ns()
            proc = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(front_port)],
                                    cwd=code, env=env, stdout=log, stderr=log)
            ws = None
            try:
                ws = await open_front(front_port)
                for sample in range(count):
                    row = await one_request(ws, f"{label}-{iteration}-{sample}", args.mode, entry_log)
                    row.update(label=label, iteration=iteration, sample=sample, mode=args.mode,
                               condition="cold" if sample == 0 else "warm", started_at_ns=started,
                               fixture="local-openai-protocol", observation="prepared-DeepAgent.send_input")
                    with output.open("a", encoding="utf-8") as out:
                        out.write(json.dumps(row) + "\n")
                    print(json.dumps({key: row[key] for key in ("label", "iteration", "sample", "condition", "entry_ms")}), flush=True)
            finally:
                if ws is not None:
                    await ws.close()
                terminate_owned(proc, [])


async def main(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"platform": platform.platform(), "python": sys.version, "mode": args.mode,
                "baseline": args.baseline, "candidate": args.candidate,
                "cold_samples": args.cold, "warm_samples": args.warm, "fixture": "local-openai-protocol"}
    output.with_suffix(".environment.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    ModelFixture.release.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for iteration in range(args.cold):
            labels = ("baseline", "candidate") if iteration % 2 == 0 else ("candidate", "baseline")
            for label in labels:
                await process_samples(args, label, iteration, 1 + (args.warm if iteration == 0 else 0),
                                      server.server_port, output)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--mode", choices=("agent", "code.normal"), default="agent")
    parser.add_argument("--cold", type=int, default=30)
    parser.add_argument("--warm", type=int, default=100)
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
