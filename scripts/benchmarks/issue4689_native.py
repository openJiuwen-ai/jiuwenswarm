# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Front/Worker/Model transport smoke in a disposable workspace.

The local OpenAI protocol fixture makes process and tool behavior repeatable;
it is explicitly separate from validation against an external real model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
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

_DELIVERY_FAULT = '''
import importlib.abc
import importlib.machinery
import sys
import os
import json
class Fault(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name not in {"jiuwenswarm.server.front.event_forwarder", "jiuwenswarm.runtime.service"}:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        loader = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return loader.create_module(spec)
            def exec_module(self, module):
                loader.exec_module(module)
                if name == "jiuwenswarm.runtime.service":
                    for operation in ("commit", "abort"):
                        original = getattr(module.AgentRuntime, operation + "_session_provision")
                        def observe(original, operation):
                            async def wrapped(instance, prepared, *args, **kwargs):
                                result = await original(instance, prepared, *args, **kwargs)
                                with open(os.environ["ISSUE4689_CREATE_LIFECYCLE"], "a", encoding="utf-8") as file:
                                    file.write(json.dumps({"operation": operation,
                                        "session_id": prepared.result.session_id,
                                        "state": prepared.state.value}) + "\\n")
                                return result
                            return wrapped
                        setattr(module.AgentRuntime, operation + "_session_provision", observe(original, operation))
                    return
                send = module.EventForwarder.send
                dropped = set()
                async def fail_once(instance, payload, **kwargs):
                    rid = payload.get("request_id")
                    if rid == "failed-create" and payload.get("status") == "succeeded" and rid not in dropped:
                        dropped.add(rid)
                        with open(os.environ["ISSUE4689_FAILED_DELIVERY"], "w", encoding="utf-8") as file:
                            json.dump(payload, file)
                        return False
                    return await send(instance, payload, **kwargs)
                module.EventForwarder.send = fail_once
        spec.loader = Loader()
        return spec
sys.meta_path.insert(0, Fault())
'''


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
            elif fixture.main_calls == 1 and fixture.scenario == "mcp":
                name = next(name for name in fixture.names if "resident_echo" in name)
                tool = {"index": 0, "id": "fixture-mcp-effect", "type": "function", "function": {
                    "name": name, "arguments": json.dumps({"value": "once"})}}
            elif fixture.main_calls == 1 and fixture.scenario == "interaction":
                tool = {"index": 0, "id": "fixture-question", "type": "function", "function": {
                    "name": "ask_user", "arguments": json.dumps({"query": "Continue?"})}}
            elif fixture.main_calls == 1 and fixture.scenario == "cross-session":
                tool = {"index": 0, "id": "fixture-cross-session", "type": "function", "function": {
                    "name": "session_send_message", "arguments": json.dumps({"target_session_id": "target-session",
                        "message": "[4689-original] Cross-session input once", "input_mode": "follow_up"})}}
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
    if module == "jiuwenswarm.server.app_agentserver":
        from jiuwenswarm.server.supervisor.shutdown import detach_front_process
        detach_front_process(proc.pid)
        if proc.poll() is None:
            proc.wait(timeout=5)
        return
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
        result.update(data_dir=str(data), profile=args.profile, scenario=args.scenario, gateway=GATEWAY)
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
        if args.scenario == "mcp":
            mcp_fixture = Path(tmp) / "mcp_fixture.py"
            mcp_fixture.write_text('''
import os, sys, time
from pathlib import Path
from mcp.server.fastmcp import FastMCP
Path(sys.argv[2]).write_text(str(os.getpid()))
server = FastMCP("resident-fixture")
@server.tool()
def resident_echo(value: str) -> str:
    with Path(sys.argv[1]).open("a") as file:
        file.write(value + "\\n")
    time.sleep(3)
    return value
server.run(transport="stdio")
''', encoding="utf-8")
            config["mcp"] = {"servers": [{"name": "resident-fixture", "enabled": True,
                "transport": "stdio", "command": sys.executable,
                "args": [str(mcp_fixture), str(data / "side-effects.txt"), str(data / "mcp.pid")]}]}
        (config_dir / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        (config_dir / ".env").write_text("# isolated protocol fixture\n", encoding="utf-8")
        session = data / "agent" / "sessions" / "resident-session"
        session.mkdir(parents=True)
        (session / "metadata.json").write_text(json.dumps({"session_id": "resident-session", "mode": args.mode,
            "channel_id": "web", "user_id": "acceptance-owner", "title": "Resident acceptance", "work_mode": "code" if args.mode.startswith("code") else "work",
            "project_dir": str(data / "agent" / "workspace"), "created_at": time.time()}), encoding="utf-8")
        if args.scenario == "cross-session":
            target = session.parent / "target-session"
            target.mkdir()
            (target / "metadata.json").write_text(json.dumps({"session_id": "target-session", "mode": args.mode,
                "user_id": "acceptance-owner", "channel_id": "web", "title": "Target acceptance", "work_mode": "work",
                "project_dir": str(data / "agent" / "workspace"), "created_at": time.time()}), encoding="utf-8")
        env = os.environ.copy()
        env.update(PYTHONPATH=str(code), JIUWENSWARM_DATA_DIR=str(data), JIUWENSWARM_HOME=str(Path(tmp) / "home"),
                   JIUWENSWARM_RUNTIME_PROFILE=args.profile, NO_PROXY="127.0.0.1,localhost")
        env.pop("PYTHONPROFILEIMPORTTIME", None)
        if args.scenario == "failed-create":
            observer = Path(tmp) / "observer"
            observer.mkdir()
            (observer / "sitecustomize.py").write_text(_DELIVERY_FAULT, encoding="utf-8")
            env["PYTHONPATH"] = os.pathsep.join((str(observer), str(code)))
            env["ISSUE4689_FAILED_DELIVERY"] = str(Path(tmp) / "failed-delivery.json")
            env["ISSUE4689_CREATE_LIFECYCLE"] = str(Path(tmp) / "create-lifecycle.jsonl")
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
                       {"query": "[4689-original] Say resident-worker-ok", "mode": args.mode,
                        **({"mcp": ["resident-fixture"]} if args.scenario == "mcp" else {})}, stream=True)
            deadline = time.monotonic() + 60
            if args.scenario == "warming-cancel":
                started_cancel = time.perf_counter_ns()
                result["warming_cancel"] = await rpc(ws, "warming-cancel", "chat.interrupt", "resident-session",
                    {"intent": "cancel", "request_id": "original-chat"})
                result["warming_cancel_ms"] = (time.perf_counter_ns() - started_cancel) / 1e6
                if not result["warming_cancel"].get("success"):
                    raise AssertionError("warming request was not cancelled")
            while not ModelFixture.entered.is_set() and args.scenario != "warming-cancel":
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
            if args.scenario in {"tool", "mcp"}:
                while not ModelFixture.counter.exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("native tool did not write its side effect")
                    await asyncio.sleep(.05)
                if args.scenario == "mcp":
                    result["mcp_pid_before"] = int((data / "mcp.pid").read_text())
            if args.scenario in {"queue", "steer"}:
                await send(ws, "supplemental", "chat.send", "resident-session",
                           {"query": "[4689-original] Supplemental input for the same execution", "mode": args.mode,
                            **({"input_mode": "steer"} if args.scenario == "steer" else {})}, stream=True)
                await asyncio.sleep(.15)
            snapshot = await rpc(ws, "before", "session.get_metadata", "resident-session")
            runtime = snapshot.get("runtime", {})
            entered = next((item for item in runtime.get("dispatches", []) if item["request_id"] == "original-chat"), {})
            if not entered.get("entered_at_ns") and args.scenario != "warming-cancel":
                raise AssertionError("restarting before the prepared native execution entry")
            result["before"] = runtime
            children = psutil.Process(proc.pid).children(recursive=True)
            candidates = [child for child in children if "jiuwenswarm.server.worker.main" in " ".join(child.cmdline())]
            if not candidates:
                raise RuntimeError("native Worker PID not found")
            worker = (candidates[-1].pid, candidates[-1].create_time())
            result["worker_pid"] = worker[0]
            if args.scenario == "cross-session":
                for attempt in range(100):
                    target_before = await rpc(ws, f"target-before-{attempt}", "session.get_metadata", "target-session")
                    target_entered = next((item for item in target_before.get("runtime", {}).get("dispatches", [])
                                           if item.get("entered_at_ns")), None)
                    if target_entered:
                        break
                    await asyncio.sleep(.05)
                if not target_entered:
                    raise AssertionError("target execution did not enter before transport restart")
                result["target_before"] = target_before["runtime"]
            if args.scenario == "failed-create":
                existing_sessions = {path.name for path in session.parent.iterdir() if path.is_dir()}
                await send(ws, "failed-create", "session.create", None,
                    {"mode": args.mode, "work_mode": "work", "project_id": "default",
                     "create_token": "acceptance-failed-delivery"})
                failure = await response(ws, "failed-create")
                result["create_failure"] = failure
                result["undelivered_creation"] = json.loads(Path(env["ISSUE4689_FAILED_DELIVERY"]).read_text(encoding="utf-8"))
                await asyncio.sleep(.3)
                result["sessions_after_failed_delivery"] = sorted(path.name for path in session.parent.iterdir() if path.is_dir())
                result["sessions_before_failed_delivery"] = sorted(existing_sessions)
                result["create_lifecycle"] = [json.loads(line) for line in
                    Path(env["ISSUE4689_CREATE_LIFECYCLE"]).read_text(encoding="utf-8").splitlines()]
                if [event["operation"] for event in result["create_lifecycle"]] != ["abort"]:
                    raise AssertionError("failed business delivery did not abort the prepared transaction")
                if failure.get("status") != "failed" and failure.get("ok") is not False:
                    raise AssertionError("delivery failure was not reported as a business error")
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
            if args.scenario not in {"crash", "warming-cancel"}:
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
            finished_requests = set()
            expected_requests = {"original-chat", "supplemental"} if args.scenario == "queue" else {
                "answer" if args.scenario == "interaction" else "original-chat"}
            while args.scenario not in {"cancel", "crash", "warming-cancel"}:
                wire = json.loads(await asyncio.wait_for(ws.recv(), 30))
                event = payload(wire)
                if isinstance(event, dict) and event.get("event_type") == "chat.error":
                    raise RuntimeError(str(event.get("message") or event.get("error")))
                final_id = wire.get("request_id") or wire.get("id") or (event.get("request_id") if isinstance(event, dict) else None)
                complete = wire.get("is_final") or (GATEWAY and wire.get("event") == "chat.final")
                if final_id in expected_requests and complete and event.get("final_mode") != "patch_segment":
                    finished_requests.add(final_id)
                if finished_requests == expected_requests:
                    break
            final = await rpc(ws, "final-state", "session.get_metadata", "resident-session")
            result["final"] = final.get("runtime", {})
            if args.scenario == "cross-session":
                target_history = target / "history.jsonl"
                for attempt in range(300):
                    if target_history.exists() and "resident-worker-ok" in target_history.read_text(encoding="utf-8"):
                        break
                    await asyncio.sleep(.05)
                result["target_history_complete"] = target_history.exists() and "resident-worker-ok" in target_history.read_text(encoding="utf-8")
                result["target_snapshot"] = await rpc(ws, "target-state", "session.get_metadata", "target-session",
                    {"runtime_resume": {"after": 0, "worker_instance": runtime["worker_instance"]}})
                if not result["target_history_complete"]:
                    raise AssertionError("cross-session target did not complete after transport restart")
                if not result["target_snapshot"].get("runtime", {}).get("dispatches"):
                    raise AssertionError("cross-session target is missing from its scoped execution journal")
                target_after = next(item for item in result["target_snapshot"]["runtime"]["dispatches"]
                                    if item["dispatch_id"] == target_entered["dispatch_id"])
                if target_after["execution_id"] != target_entered["execution_id"]:
                    raise AssertionError("cross-session target execution was replaced")
                result["same_target_execution"] = True
            history = session / "history.jsonl"
            result["history_records"] = len(history.read_text(encoding="utf-8").splitlines()) if history.exists() else 0
            if args.scenario in {"queue", "steer"}:
                result["supplemental_persisted"] = "Supplemental input" in history.read_text(encoding="utf-8")
                if not result["supplemental_persisted"]:
                    raise AssertionError("supplemental input absent from durable history")
            if args.scenario == "crash":
                same_worker = False
            else:
                same_worker = psutil.Process(worker[0]).create_time() == worker[1]
            result.update(model_calls=ModelFixture.calls, main_model_calls=ModelFixture.main_calls,
                          side_effects=ModelFixture.counter.read_text().splitlines() if ModelFixture.counter.exists() else [],
                          tool_names=ModelFixture.names,
                          same_worker=same_worker,
                          same_instance=result["before"].get("worker_instance") == result["after"].get("worker_instance"))
            if args.scenario == "warming-cancel" and ModelFixture.main_calls:
                raise AssertionError("cancelled warming input entered the model")
            if args.scenario != "crash" and (not result["same_worker"] or not result["same_instance"]):
                raise AssertionError("Worker was replaced on transport restart")
            if args.scenario in {"tool", "mcp"} and result["side_effects"] != ["once"]:
                raise AssertionError("tool side effect missing or duplicated")
            if args.scenario == "mcp":
                result["mcp_pid_after"] = int((data / "mcp.pid").read_text())
                if result["mcp_pid_before"] != result["mcp_pid_after"]:
                    raise AssertionError("MCP server was restarted by Front restart")
            # End through the explicit IPC lifecycle authority, testing the
            # shared shutdown budget, persistence barrier and final resource close.
            workers = module_processes(proc, "jiuwenswarm.server.worker.main")
            worker_processes += [(child.pid, child.create_time()) for child in workers]
            active_worker = workers[-1] if workers else psutil.Process(worker[0])
            await ws.close()
            ws = None
            if gateway_proc is not None:
                stop_front(gateway_proc, "jiuwenswarm.gateway.app_gateway")
            stop_front(proc)
            shutdown_started = time.perf_counter_ns()
            helper = await asyncio.create_subprocess_exec(sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown",
                "--shutdown-worker", "--timeout", "30", cwd=code, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(helper.communicate(), 33)
            if helper.returncode:
                raise AssertionError(f"Application shutdown helper failed: {stderr.decode(errors='replace')[-1200:]}")
            done = json.loads(stdout.splitlines()[-1])
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
            logging.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", default="agent", choices=("agent", "code.normal"))
    parser.add_argument("--scenario", default="restart", choices=("restart", "tool", "mcp", "interaction", "offline", "cancel", "crash", "queue", "steer", "cross-session", "warming-cancel", "failed-create"))
    parser.add_argument("--profile", default="desktop", choices=("desktop", "agentos"))
    parser.add_argument("--gateway", action="store_true")
    asyncio.run(run(parser.parse_args()))
