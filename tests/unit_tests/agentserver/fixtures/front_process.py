# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Isolated Front subprocess fixtures and ownership-safe cleanup."""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import time
from pathlib import Path

import psutil


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


def _wait_stopped(child, timeout=5):
    # Grandchildren are not ours to reap with waitpid(). Linux CI containers may
    # retain an orphan zombie until PID 1 reaps it; it has already stopped and
    # cannot execute or hold the test fixture's files/sockets open.
    deadline = time.monotonic() + timeout
    while True:
        try:
            if child.status() == psutil.STATUS_ZOMBIE or not child.is_running():
                return
        except psutil.NoSuchProcess:
            return
        if time.monotonic() >= deadline:
            raise psutil.TimeoutExpired(timeout, pid=child.pid)
        time.sleep(.01)


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
                _wait_stopped(child)
        except psutil.NoSuchProcess:
            pass
