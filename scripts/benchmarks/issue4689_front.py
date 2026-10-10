# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows source Front/control benchmark; parent-spawn timestamps, native Worker.

Use the same interpreter and seeded workspace for baseline and candidate. No
business payloads or credentials are saved. This is not a chat entry benchmark.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil
from websockets.legacy.client import connect as ws_connect


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def receive(ws, request_id):
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if message.get("request_id") == request_id and message.get("is_final", True):
            if message.get("status") == "failed" or message.get("ok") is False:
                raise RuntimeError(f"control {request_id} failed: {message.get('response_kind')}")
            return message


def seed(root: Path, code: Path, mode: str):
    config = root / "config"
    config.mkdir(parents=True)
    shutil.copy2(code / "jiuwenswarm/resources/config.yaml", config / "config.yaml")
    (config / ".env").write_text("# isolated acceptance workspace\n", encoding="utf-8")
    sessions = root / "agent" / "sessions"
    for i in range(20):
        directory = sessions / f"acceptance-{i}"
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(json.dumps({
            "session_id": f"acceptance-{i}", "mode": mode, "title": f"Acceptance {i}",
            "work_mode": "code" if mode.startswith("code") else "work",
            "created_at": 1770000000, "round_id": 12,
        }), encoding="utf-8")
        (directory / "history.jsonl").write_text("\n".join(json.dumps({
            "role": "user" if n % 2 == 0 else "assistant", "content": f"Persisted message {n}",
            "timestamp": 1770000000 + n,
        }) for n in range(24)) + "\n", encoding="utf-8")


def terminate_owned(proc, children):
    # The parent handle plus creation time protects against unrelated/reused PIDs.
    try:
        children += [(p.pid, p.create_time()) for p in psutil.Process(proc.pid).children(recursive=True)]
    except psutil.Error:
        pass
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)
    for pid, created in set(children):
        try:
            child = psutil.Process(pid)
            if child.create_time() == created:
                child.kill()
                child.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass


async def sample(label, code, python, root, output, iteration):
    port = free_port()
    env = os.environ.copy()
    env.update(PYTHONPATH=str(code), JIUWENSWARM_DATA_DIR=str(root),
               JIUWENSWARM_HOME=str(root.parent / "home"), AGENT_SERVER_HOST="127.0.0.1")
    env.pop("PYTHONPROFILEIMPORTTIME", None)
    env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
    started = time.perf_counter_ns()
    proc = subprocess.Popen([python, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(port)],
                            cwd=code, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    children = []
    try:
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.02):
                    listening = time.perf_counter_ns()
                break
            except OSError:
                if proc.poll() is not None or time.perf_counter_ns() - started > 10_000_000_000:
                    raise RuntimeError("Front did not listen")
                await asyncio.sleep(.001)
        controls = {}
        async with ws_connect(f"ws://127.0.0.1:{port}", max_size=16 * 1024 * 1024) as ws:
            await ws.recv()
            for method, params in (
                ("session.list", {"limit": 20}), ("project.list", {}),
                ("config.get", {}), ("history.get", {"session_id": "acceptance-0", "page_idx": 1}),
            ):
                request_id = f"{label}-{iteration}-{method}"
                sent = time.perf_counter_ns()
                await ws.send(json.dumps({"request_id": request_id, "channel_id": "web",
                                          "req_method": method, "params": params,
                                          "session_id": "acceptance-0"}))
                await receive(ws, request_id)
                received = time.perf_counter_ns()
                controls[method] = {"sent_at_ns": sent, "received_at_ns": received,
                                    "response_ms": (received - sent) / 1e6,
                                    "available_from_spawn_ms": (received - started) / 1e6}
        row = {"label": label, "iteration": iteration, "started_at_ns": started,
               "listening_at_ns": listening, "listen_ms": (listening - started) / 1e6,
               "controls": controls}
        with output.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row) + "\n")
        print(json.dumps({"label": label, "iteration": iteration, "listen_ms": row["listen_ms"]}), flush=True)
    finally:
        terminate_owned(proc, children)


async def main(args):
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"platform": platform.platform(), "python": sys.version,
                "baseline": args.baseline, "candidate": args.candidate, "samples": args.samples,
                "mode": args.mode}
    output.with_suffix(".environment.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-front-") as tmp:
        root = Path(tmp) / "data"
        seed(root, Path(args.baseline), args.mode)
        # Alternate versions to reduce filesystem-cache/order effects.
        for i in range(args.samples):
            for label in ("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline"):
                await sample(label, Path(getattr(args, label)), args.python, root, output, i)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--mode", choices=("agent", "code.normal"), default="agent")
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
