# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Front/Worker/Model transport smoke in a disposable workspace.

The local OpenAI protocol fixture makes process and tool behavior repeatable;
it is explicitly separate from validation against an external real model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
import yaml
from websockets.legacy.client import connect

GATEWAY = False


class ModelFixture(BaseHTTPRequestHandler):
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    main_calls = 0
    names = []
    scenario = "restart"
    counter = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        type(self).calls += 1
        type(self).names = [tool.get("function", {}).get("name") for tool in request.get("tools", [])]
        fixture = type(self)
        main = "[4689-original]" in json.dumps(request.get("messages", []))
        tool = None
        if main:
            fixture.main_calls += 1
            fixture.entered.set()
            if fixture.main_calls == 1 and fixture.scenario == "tool":
                target = str(fixture.counter).replace("'", "''")
                tool = {"index": 0, "id": "fixture-side-effect", "type": "function", "function": {
                    "name": "powershell", "arguments": json.dumps({"command":
                        f"Add-Content -LiteralPath '{target}' -Value once; Start-Sleep -Seconds 3"})}}
            elif fixture.main_calls == 1 and fixture.scenario == "interaction":
                tool = {"index": 0, "id": "fixture-question", "type": "function", "function": {
                    "name": "ask_user", "arguments": json.dumps({"query": "Continue?"})}}
            else:
                fixture.release.wait(timeout=60)
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            content = {"role": "assistant", "tool_calls": [tool]} if tool else {
                "role": "assistant", "content": "resident-worker-ok"}
            for delta, finish in ((content, None), ({}, "tool_calls" if tool else "stop")):
                chunk = {"id": "fixture-response", "object": "chat.completion.chunk", "created": int(time.time()),
                         "model": "fixture-model", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps({"id": "fixture-response", "object": "chat.completion", "created": int(time.time()),
                               "model": "fixture-model", "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "resident-worker-ok"}}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def payload(wire):
    if wire.get("type") in {"event", "res"} and "payload" in wire:
        body = wire["payload"]
        if wire.get("event") and isinstance(body, dict):
            return {"event_type": wire["event"], **body}
        return body
    body = wire.get("body") or wire.get("payload") or {}
    return body.get("result") or body.get("delta") or body


async def response(ws, request_id):
    while True:
        wire = json.loads(await asyncio.wait_for(ws.recv(), 90))
        if (wire.get("request_id") or wire.get("id")) == request_id:
            return wire


async def send(ws, request_id, method, session_id, params=None, *, stream=False):
    if GATEWAY:
        await ws.send(json.dumps({"type": "req", "id": request_id, "method": method,
            "params": {"session_id": session_id, **(params or {})}, "is_stream": stream}))
        return
    await ws.send(json.dumps({"request_id": request_id, "channel_id": "web", "user_id": "acceptance-owner",
                             "session_id": session_id, "req_method": method, "is_stream": stream,
                             "params": {"session_id": session_id, **(params or {})}}))


async def rpc(ws, request_id, method, session_id, params=None):
    await send(ws, request_id, method, session_id, params)
    return payload(await response(ws, request_id))


async def open_front(front_port, suffix=""):
    deadline = time.monotonic() + 30
    while True:
        try:
            ws = await connect(f"ws://127.0.0.1:{front_port}{suffix}", max_size=32 * 1024 * 1024)
            await ws.recv()
            return ws
        except (OSError, EOFError):
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(.025)


def module_processes(proc, module):
    result = []
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in children:
        try:
            argv = child.cmdline()
            if "-m" in argv and argv[argv.index("-m") + 1] == module:
                result.append(child)
        except psutil.NoSuchProcess:
            pass
    return result


def stop_front(proc, module="jiuwenswarm.server.app_agentserver"):
    # Windows venv python.exe is a launcher; killing just Popen.pid leaves the
    # actual Front alive. Retain Worker descendants but stop both Front PIDs.
    children = module_processes(proc, module)
    for child in reversed(children):
        try:
            argv = child.cmdline()
            if "-m" in argv and argv[argv.index("-m") + 1] == module:
                child.kill()
                child.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)


async def run(args):
    global GATEWAY
    GATEWAY = args.gateway
    code = Path(args.code).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    thread.start()
    result = {}
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-native-chat-") as tmp:
        data = Path(tmp) / "data"
        config_dir = data / "config"
        config_dir.mkdir(parents=True)
        config = yaml.safe_load((code / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8"))
        config["models"]["defaults"] = [{"model_id": "mdl_fixture", "is_default": True,
            "model_client_config": {"client_provider": "OpenAI", "api_base": f"http://127.0.0.1:{model_server.server_port}/v1",
                                    "api_key": "local-fixture", "model_name": "fixture-model"},
            "model_config_obj": {"temperature": 0, "max_tokens": 128}}]
        config["progressive_tool_enabled"] = False
        config.setdefault("react", {})["enable_skill_evolution"] = False
        config["react"]["enable_read_image_multimodal"] = False
        config["permissions"]["enabled"] = False
        (config_dir / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        (config_dir / ".env").write_text("# isolated protocol fixture\n", encoding="utf-8")
        session = data / "agent" / "sessions" / "resident-session"
        session.mkdir(parents=True)
        (session / "metadata.json").write_text(json.dumps({"session_id": "resident-session", "mode": args.mode,
            "title": "Resident acceptance", "work_mode": "code" if args.mode.startswith("code") else "work",
            "project_dir": str(data / "agent" / "workspace"), "created_at": time.time()}), encoding="utf-8")
        env = os.environ.copy()
        env.update(PYTHONPATH=str(code), JIUWENSWARM_DATA_DIR=str(data), JIUWENSWARM_HOME=str(Path(tmp) / "home"),
                   JIUWENSWARM_RUNTIME_PROFILE=args.profile, NO_PROXY="127.0.0.1,localhost")
        env.pop("PYTHONPROFILEIMPORTTIME", None)
        front_port = port()
        gateway_port = port()
        ModelFixture.scenario = args.scenario
        ModelFixture.counter = data / "side-effects.txt"
        log = output.with_suffix(".log").open("wb")
        def launch():
            return subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(front_port)],
                                    cwd=code, env=env, stdout=log, stderr=log)
        proc = launch()
        gateway_proc = None
        def launch_gateway():
            return subprocess.Popen([sys.executable, "-m", "jiuwenswarm.gateway.app_gateway", "--host", "127.0.0.1",
                "--port", str(gateway_port), "--agent-server-url", f"ws://127.0.0.1:{front_port}", "--web-path", "/ws"],
                cwd=code, env=env, stdout=log, stderr=log)
        ws = None
        worker = None
        worker_processes = []
        try:
            ws = await open_front(front_port)
            if GATEWAY:
                await ws.close()
                gateway_proc = launch_gateway()
                ws = await open_front(gateway_port, "/ws?user_id=acceptance-owner")
            await send(ws, "original-chat", "chat.send", "resident-session",
                       {"query": "[4689-original] Say resident-worker-ok", "mode": args.mode}, stream=True)
            deadline = time.monotonic() + 60
            while not ModelFixture.entered.is_set():
                worker_processes = [(child.pid, child.create_time()) for child in
                    module_processes(proc, "jiuwenswarm.server.worker.main")]
                if time.monotonic() > deadline:
                    raise TimeoutError("native execution did not reach model transport")
                await asyncio.sleep(.05)
            interaction = None
            if args.scenario == "interaction":
                while interaction is None:
                    wire = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    event = payload(wire)
                    if isinstance(event, dict) and event.get("event_type") == "chat.ask_user_question":
                        interaction = event
                        result["interaction"] = interaction
            if args.scenario == "tool":
                while not ModelFixture.counter.exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("native tool did not write its side effect")
                    await asyncio.sleep(.05)
            snapshot = await rpc(ws, "before", "session.get_metadata", "resident-session")
            runtime = snapshot.get("runtime", {})
            entered = next(item for item in runtime["dispatches"] if item["request_id"] == "original-chat")
            if not entered["entered_at_ns"]:
                raise AssertionError("restarting before the prepared native execution entry")
            result["before"] = runtime
            children = psutil.Process(proc.pid).children(recursive=True)
            candidates = [child for child in children if "jiuwenswarm.server.worker.main" in " ".join(child.cmdline())]
            if not candidates:
                raise RuntimeError("native Worker PID not found")
            worker = (candidates[-1].pid, candidates[-1].create_time())
            result["worker_pid"] = worker[0]
            await ws.close()
            ws = None
            if gateway_proc is not None:
                stop_front(gateway_proc, "jiuwenswarm.gateway.app_gateway")
            stop_front(proc)
            if args.scenario == "offline":
                ModelFixture.release.set()
                history = session / "history.jsonl"
                deadline = time.monotonic() + 30
                while not history.exists() or "resident-worker-ok" not in history.read_text(encoding="utf-8"):
                    if time.monotonic() > deadline:
                        raise TimeoutError("offline execution did not persist its result")
                    await asyncio.sleep(.05)
                result["offline_history_complete"] = True
            if args.scenario == "crash":
                for worker_pid, created in reversed(worker_processes):
                    try:
                        child = psutil.Process(worker_pid)
                        if child.create_time() == created:
                            child.kill()
                            child.wait(timeout=5)
                    except psutil.NoSuchProcess:
                        pass
            proc = launch()
            if GATEWAY:
                gateway_proc = launch_gateway()
                ws = await open_front(gateway_port, "/ws?user_id=acceptance-owner")
            else:
                ws = await open_front(front_port)
            for attempt in range(80):
                recovered = await rpc(ws, f"after-{attempt}", "session.get_metadata", "resident-session",
                                      {"runtime_resume": {"after": 0, "worker_instance": runtime.get("worker_instance")}})
                if recovered.get("runtime", {}).get("worker_instance"):
                    break
                await asyncio.sleep(.05)
            result["after"] = recovered.get("runtime", {})
            if args.scenario != "crash":
                restored = next(item for item in result["after"]["dispatches"]
                                if item["request_id"] == "original-chat")
                if entered["execution_id"] != restored["execution_id"]:
                    raise AssertionError("restart replaced the original execution")
                result["same_execution"] = True
            if args.scenario == "interaction":
                result["restored_interactions"] = [item for dispatch in result["after"]["dispatches"]
                                                    for item in dispatch["pending_interactions"]]
                if not result["restored_interactions"]:
                    raise AssertionError("pending interaction missing from restored snapshot")
                await send(ws, "answer", "chat.send", "resident-session", {
                    "mode": args.mode, "source": "ask_user_interrupt", "request_id": interaction["request_id"],
                    "query": "yes", "answers": [{"question": "Continue?", "selected_options": ["yes"]}]}, stream=True)
            if args.scenario == "cancel":
                cancelled = await rpc(ws, "cancel", "chat.interrupt", "resident-session", {"intent": "cancel"})
                if not cancelled.get("success"):
                    raise AssertionError("reconnected execution was not cancelled")
                result["cancelled"] = cancelled
            if args.scenario == "crash" and not result["after"].get("interrupted"):
                raise AssertionError("crashed Worker execution was not reported interrupted")
            ModelFixture.release.set()
            while args.scenario not in {"cancel", "crash"}:
                wire = json.loads(await asyncio.wait_for(ws.recv(), 30))
                event = payload(wire)
                if isinstance(event, dict) and event.get("event_type") == "chat.error":
                    raise RuntimeError(str(event.get("message") or event.get("error")))
                terminal_request = "answer" if args.scenario == "interaction" else "original-chat"
                final_id = wire.get("request_id") or wire.get("id") or (event.get("request_id") if isinstance(event, dict) else None)
                complete = wire.get("is_final") or (GATEWAY and wire.get("event") == "chat.final")
                if final_id == terminal_request and complete and event.get("final_mode") != "patch_segment":
                    break
            final = await rpc(ws, "final-state", "session.get_metadata", "resident-session")
            result["final"] = final.get("runtime", {})
            history = session / "history.jsonl"
            result["history_records"] = len(history.read_text(encoding="utf-8").splitlines()) if history.exists() else 0
            if args.scenario == "crash":
                same_worker = False
            else:
                same_worker = psutil.Process(worker[0]).create_time() == worker[1]
            result.update(model_calls=ModelFixture.calls, main_model_calls=ModelFixture.main_calls,
                          side_effects=ModelFixture.counter.read_text().splitlines() if ModelFixture.counter.exists() else [],
                          tool_names=ModelFixture.names,
                          same_worker=same_worker,
                          same_instance=result["before"].get("worker_instance") == result["after"].get("worker_instance"))
            if args.scenario != "crash" and (not result["same_worker"] or not result["same_instance"]):
                raise AssertionError("Worker was replaced on transport restart")
            if args.scenario == "tool" and result["side_effects"] != ["once"]:
                raise AssertionError("tool side effect missing or duplicated")
            # End through the explicit IPC lifecycle authority, testing the
            # shared shutdown budget, persistence barrier and final resource close.
            workers = module_processes(proc, "jiuwenswarm.server.worker.main")
            worker_processes += [(child.pid, child.create_time()) for child in workers]
            active_worker = workers[-1] if workers else psutil.Process(worker[0])
            argv = active_worker.cmdline()
            socket_path = argv[argv.index("--socket") + 1]
            await ws.close()
            ws = None
            if gateway_proc is not None:
                stop_front(gateway_proc, "jiuwenswarm.gateway.app_gateway")
            stop_front(proc)
            from jiuwenswarm.server.ipc.client import connect as ipc_connect
            shutdown_started = time.perf_counter_ns()
            ipc = await asyncio.wait_for(ipc_connect(socket_path), 5)
            await asyncio.wait_for(ipc.recv(), 5)
            await ipc.send({"kind": "shutdown"})
            while True:
                done = await asyncio.wait_for(ipc.recv(), 31)
                if done.get("kind") == "shutdown.done":
                    break
            await ipc.close()
            result["shutdown"] = done
            result["shutdown_ms"] = (time.perf_counter_ns() - shutdown_started) / 1e6
            if done.get("ok") is not True:
                raise AssertionError(f"Worker final shutdown failed: {done}")
            await asyncio.to_thread(active_worker.wait, 5)
            output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({key: result[key] for key in ("worker_pid", "same_worker", "same_instance", "model_calls")}))
        finally:
            ModelFixture.release.set()
            if ws is not None:
                await ws.close()
            if gateway_proc is not None:
                stop_front(gateway_proc, "jiuwenswarm.gateway.app_gateway")
            worker_processes += [(child.pid, child.create_time()) for child in
                                module_processes(proc, "jiuwenswarm.server.worker.main")]
            stop_front(proc)
            if worker is not None and worker not in worker_processes:
                worker_processes.append(worker)
            for worker_pid, worker_created in reversed(worker_processes):
                try:
                    child = psutil.Process(worker_pid)
                    if child.create_time() == worker_created:
                        child.kill()
                        child.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
            log.close()
            model_server.shutdown()
            model_server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", default="agent", choices=("agent", "code.normal"))
    parser.add_argument("--scenario", default="restart", choices=("restart", "tool", "interaction", "offline", "cancel", "crash"))
    parser.add_argument("--profile", default="desktop", choices=("desktop", "agentos"))
    parser.add_argument("--gateway", action="store_true")
    asyncio.run(run(parser.parse_args()))
