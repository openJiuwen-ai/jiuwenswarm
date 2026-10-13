# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Two real AgentOS Workers isolate identical caller/session/request identities.

Uses the real Front, SDK, history and native PowerShell tool. Only the external
model HTTP protocol is a deterministic local fixture; no SDK method is patched.
Run source/frozen acceptance separately from external real-model validation.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path

import psutil
import yaml

import issue4689_native as native
from issue4689_shutdown import NativeProcessHandle

SESSION_ID = "shared-agentos-session"
REQUEST_ID = "shared-agentos-request"
USER_ID = "acceptance-owner"  # native.send preserves this same caller on both.


class IsolationModelFixture(native.ModelFixture):
    def do_POST(self):
        fixture = type(self)
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        text = json.dumps(request.get("messages", []), ensure_ascii=False)
        main = "[4689-original]" in text
        with fixture.lock:
            fixture.calls += 1
            fixture.names = [tool.get("function", {}).get("name") for tool in request.get("tools", [])]
            if fixture.other_marker in text:
                fixture.violations.append("model received the other data directory's marker")
            if main:
                fixture.main_calls += 1
                call = fixture.main_calls
                if call == 1:
                    fixture.restored_seed = fixture.seed_marker in text
            else:
                call = 0
        tool = None
        if call == 1:
            if "powershell" not in fixture.names:
                fixture.violations.append("native PowerShell tool absent")
            target = str(fixture.counter).replace("'", "''")
            tool = {"index": 0, "id": "isolated-native-tool", "type": "function", "function": {
                "name": "powershell", "arguments": json.dumps({"command":
                    f"Add-Content -LiteralPath '{target}' -Value '{fixture.marker}'"})}}
        elif call == 2:
            fixture.entered.set()
            if not fixture.release.wait(120):
                fixture.violations.append("model gate timed out before isolation assertions")
        elif call > 2:
            fixture.violations.append("original input unexpectedly repeated a model execution")
        content = {"role": "assistant", "tool_calls": [tool]} if tool else {
            "role": "assistant", "content": f"{fixture.marker}-reply"}
        try:
            self.send_response(200)
            if request.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for delta, finish in ((content, None), ({}, "tool_calls" if tool else "stop")):
                    chunk = {"id": "isolated-model-response", "object": "chat.completion.chunk",
                        "created": int(time.time()), "model": "fixture-model",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                body = json.dumps({"id": "isolated-model-response", "object": "chat.completion",
                    "created": int(time.time()), "model": "fixture-model", "choices": [
                        {"index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": content}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            fixture.cancelled_connections += 1


def fixture_for(marker, other_marker, counter):
    return type(f"Fixture_{marker}", (IsolationModelFixture,), {
        "marker": marker, "other_marker": other_marker, "seed_marker": f"seed-{marker}",
        "counter": counter, "calls": 0, "main_calls": 0, "names": [], "violations": [],
        "restored_seed": False, "cancelled_connections": 0, "lock": threading.Lock(),
        "entered": threading.Event(), "release": threading.Event(),
    })


def fixture_observation(deployment):
    return {"model_calls": deployment.fixture.calls, "main_model_calls": deployment.fixture.main_calls,
        "restored_seed": deployment.fixture.restored_seed, "violations": list(deployment.fixture.violations),
        "model_gate_entered": deployment.fixture.entered.is_set(),
        "side_effects": deployment.fixture.counter.read_text().splitlines()
            if deployment.fixture.counter.exists() else []}


@dataclass
class Deployment:
    marker: str
    data: Path
    home: Path
    fixture: type
    model: ThreadingHTTPServer
    env: dict
    log: object
    front_port: int
    front: subprocess.Popen | None = None
    ws: object = None
    worker: object = None
    worker_created: float | None = None
    process_handle: object = None
    seed: str = ""
    frames: list = field(default_factory=list)
    owned_workers: list = field(default_factory=list)
    closed: bool = False

    @property
    def session(self):
        return self.data / "agent/sessions" / SESSION_ID

    @property
    def lease_path(self):
        return self.data / "agent/lifecycle/resources" / f"session_{SESSION_ID}.json"


def prepare(code, temporary, marker, other_marker, output):
    data, home = temporary / marker / "data", temporary / marker / "home"
    home.mkdir(parents=True)
    fixture = fixture_for(marker, other_marker, data / "native-side-effects.txt")
    model = ThreadingHTTPServer(("127.0.0.1", 0), fixture)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    config_dir = data / "config"
    config_dir.mkdir(parents=True)
    config = yaml.safe_load((code / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8"))
    config["models"]["defaults"] = [{"model_id": "mdl_fixture", "is_default": True,
        "model_client_config": {"client_provider": "OpenAI",
            "api_base": f"http://127.0.0.1:{model.server_port}/v1", "api_key": "local-fixture",
            "model_name": "fixture-model"}, "model_config_obj": {"temperature": 0, "max_tokens": 128}}]
    config["progressive_tool_enabled"] = False
    config["permissions"]["enabled"] = False
    config.setdefault("react", {}).update(enable_skill_evolution=False, enable_read_image_multimodal=False)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    (config_dir / ".env").write_text("# isolated AgentOS model fixture\n", encoding="utf-8")
    session = data / "agent/sessions" / SESSION_ID
    session.mkdir(parents=True)
    (session / "metadata.json").write_text(json.dumps({"session_id": SESSION_ID, "mode": "agent",
        "work_mode": "work", "channel_id": "web", "user_id": USER_ID, "title": marker,
        "project_dir": str(data / "agent/workspace"), "created_at": time.time()}), encoding="utf-8")
    seed = "\n".join(json.dumps(row) for row in [
        {"role": "user", "request_id": "same-seed-request", "content": fixture.seed_marker},
        {"role": "assistant", "request_id": "same-seed-request", "event_type": "chat.final",
         "content": f"seed-response-{marker}"},
    ]) + "\n"
    (session / "history.jsonl").write_text(seed, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(code), "JIUWENSWARM_DATA_DIR": str(data),
        "JIUWENSWARM_HOME": str(home), "USERPROFILE": str(home), "HOME": str(home),
        "JIUWENSWARM_RUNTIME_PROFILE": "agentos", "NO_PROXY": "127.0.0.1,localhost"}
    env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
    env.pop("PYTHONPROFILEIMPORTTIME", None)
    return Deployment(marker, data, home, fixture, model, env,
        output.with_name(f"{output.stem}-{marker}.log").open("wb"), native.port(), seed=seed)


async def rpc(deployment, request_id, method, params=None):
    await native.send(deployment.ws, request_id, method, SESSION_ID, params)
    while True:
        wire = json.loads(await asyncio.wait_for(deployment.ws.recv(), 120))
        deployment.frames.append(wire)
        if (wire.get("request_id") or wire.get("id")) == request_id:
            assert wire.get("status") != "failed" and wire.get("ok") is not False, wire
            return native.payload(wire)


def retain_worker(deployment):
    lease = json.loads(deployment.lease_path.read_text(encoding="utf-8"))
    owner = lease["runtime_owner"]
    worker = psutil.Process(owner["pid"])
    assert abs(worker.create_time() - owner["started_at"]) < .01
    descendants = psutil.Process(deployment.front.pid).children(recursive=True)
    assert worker.pid in {child.pid for child in descendants}, "lease owner is not this Front's Worker"
    assert "jiuwenswarm.server.worker.main" in " ".join(worker.cmdline())
    deployment.worker, deployment.worker_created = worker, worker.create_time()
    deployment.owned_workers.append((worker.pid, deployment.worker_created))
    if sys.platform == "win32":
        deployment.process_handle = NativeProcessHandle(worker.pid)
    return lease


async def finish(deployment):
    while True:
        wire = json.loads(await asyncio.wait_for(deployment.ws.recv(), 120))
        deployment.frames.append(wire)
        body = native.payload(wire)
        if isinstance(body, dict) and body.get("event_type") == "chat.error":
            raise AssertionError(body)
        if ((wire.get("request_id") or wire.get("id")) == REQUEST_ID and wire.get("is_final")
                and isinstance(body, dict) and body.get("final_mode") != "patch_segment"):
            assert wire.get("status") != "failed" and wire.get("ok") is not False, wire
            final_content = [native.payload(frame) for frame in deployment.frames
                if isinstance(native.payload(frame), dict)
                and native.payload(frame).get("event_type") == "chat.final"]
            # E2A completion can carry an empty result after the actual final
            # content event. Require that event as well as terminal completion.
            assert any(deployment.fixture.marker + "-reply" in json.dumps(value)
                for value in final_content), deployment.frames
            return wire


async def durable_history(deployment):
    deadline = time.monotonic() + 30
    while True:
        try:
            text = (deployment.session / "history.jsonl").read_text(encoding="utf-8")
            records = [json.loads(line) for line in text.splitlines() if line]
            if any(row.get("event_type") == "chat.final" and
                    deployment.marker + "-reply" in row.get("content", "") for row in records):
                return text, records
        except (OSError, json.JSONDecodeError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("completed SDK reply was not durably written to its own history")
        await asyncio.sleep(.025)


async def close_owned(deployment, code, *, timeout=30):
    if deployment.ws is not None:
        await deployment.ws.close()
        deployment.ws = None
    if deployment.front is not None:
        deployment.owned_workers.extend((child.pid, child.create_time()) for child in
            native.module_processes(deployment.front, "jiuwenswarm.server.worker.main"))
        native.stop_front(deployment.front)
    started = time.monotonic()
    helper = await asyncio.create_subprocess_exec(sys.executable, "-m",
        "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker", "--timeout", str(timeout),
        cwd=code, env=deployment.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(helper.communicate(), timeout + 3)
    assert helper.returncode == 0, stderr.decode(errors="replace")[-1500:]
    result = json.loads(stdout.splitlines()[-1])
    assert result.get("ok") is True, result
    if deployment.worker is not None:
        assert not result.get("absent"), result
    if deployment.process_handle is not None:
        result["native_exit_code"] = await asyncio.to_thread(deployment.process_handle.wait, 5)
        assert result["native_exit_code"] == 0, result
    elif deployment.worker is not None:
        await asyncio.to_thread(deployment.worker.wait, 5)
    result["elapsed_ms"] = (time.monotonic() - started) * 1000
    assert result["elapsed_ms"] <= timeout * 1000, "the owner shutdown budget includes helper startup"
    deployment.closed = True
    return result


async def run(args):
    code, output = Path(args.code).resolve(), Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    native.GATEWAY = False
    result = {"passed": False, "profile": "agentos", "transport": "real Front/Worker/SDK/native tool",
        "model": "local deterministic HTTP protocol fixture; external real model not claimed",
        "same_user_id": USER_ID, "same_session_id": SESSION_ID, "same_request_id": REQUEST_ID,
        "front_executable": args.front_executable, "deployments": {}, "cleanup_errors": []}
    deployments = []
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-agentos-isolation-", ignore_cleanup_errors=True) as temporary:
        try:
            markers = ["tenant-left-4689", "tenant-right-4689"]
            for index, marker in enumerate(markers):
                deployments.append(prepare(code, Path(temporary), marker, markers[1 - index], output))
            for deployment in deployments:
                deployment.front = subprocess.Popen([args.front_executable or sys.executable, "-m",
                    "jiuwenswarm.server.app_agentserver", "--port", str(deployment.front_port)],
                    cwd=code, env=deployment.env, stdout=deployment.log, stderr=deployment.log)
            for deployment in deployments:
                deployment.ws = await native.open_front(deployment.front_port)
                await native.send(deployment.ws, REQUEST_ID, "chat.send", SESSION_ID, {
                    "query": f"[4689-original] {deployment.marker}: run the native tool once and confirm isolation",
                    "mode": "agent"}, stream=True)
            deadline = time.monotonic() + 150
            while not all(deployment.fixture.entered.is_set() for deployment in deployments):
                if time.monotonic() >= deadline:
                    raise TimeoutError("both real SDK executions did not reach the post-tool model gate")
                await asyncio.sleep(.05)
            identities = []
            for deployment in deployments:
                assert deployment.fixture.main_calls == 2 and deployment.fixture.restored_seed
                assert not deployment.fixture.violations, deployment.fixture.violations
                assert deployment.fixture.counter.read_text().splitlines() == [deployment.marker]
                before = (await rpc(deployment, "same-before-query", "session.get_metadata"))["runtime"]
                dispatch = next(item for item in before["dispatches"] if item["request_id"] == REQUEST_ID)
                execution = next(item for item in before["executions"]
                    if item.get("request_id") == REQUEST_ID or item.get("root_request_id") == REQUEST_ID)
                assert dispatch.get("entered_at_ns") and execution.get("execution_id"), before
                assert before.get("is_processing") is True and execution.get("state") == "running", before
                lease = retain_worker(deployment)
                identities.append((deployment.worker.pid, before["worker_instance"], execution["execution_id"]))
                result["deployments"][deployment.marker] = {"data_dir": str(deployment.data), "home": str(deployment.home),
                    "worker_pid": deployment.worker.pid, "worker_started_at": deployment.worker_created,
                    "worker_instance": before["worker_instance"], "execution_id": execution["execution_id"],
                    "active_runtime": before, "lease_before": lease, "lease_path": str(deployment.lease_path)}
            assert all(len({identity[index] for identity in identities}) == 2 for index in range(3)), identities
            result["both_workers_alive_simultaneously"] = all(deployment.worker.is_running() for deployment in deployments)
            assert result["both_workers_alive_simultaneously"]
            for deployment in deployments:
                deployment.fixture.release.set()
            completions = await asyncio.gather(*(finish(deployment) for deployment in deployments), return_exceptions=True)
            errors = [value for value in completions if isinstance(value, BaseException)]
            if errors:
                raise errors[0]
            for deployment in deployments:
                target = result["deployments"][deployment.marker]
                text, records = await durable_history(deployment)
                assert deployment.fixture.main_calls == 2, "original input unexpectedly executed again"
                assert not deployment.fixture.violations, deployment.fixture.violations
                assert deployment.fixture.counter.read_text().splitlines() == [deployment.marker], "tool side effect missing or repeated"
                assert text.startswith(deployment.seed), "old conversation was lost"
                assert len([row for row in records if row.get("role") == "user" and row.get("request_id") == REQUEST_ID]) == 1
                assert any(row.get("event_type") == "chat.tool_result" and row.get("success") is True for row in records)
                assert any(row.get("event_type") == "chat.final" and deployment.marker + "-reply" in row.get("content", "") for row in records)
                assert deployment.fixture.other_marker not in text
                assert deployment.fixture.other_marker not in json.dumps(deployment.frames)
                metadata = json.loads((deployment.session / "metadata.json").read_text(encoding="utf-8"))
                assert metadata["session_id"] == SESSION_ID and metadata["user_id"] == USER_ID and metadata["title"] == deployment.marker
                target.update(history_records=records, history_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    metadata=metadata, wire_frames=deployment.frames, model_calls=deployment.fixture.calls,
                    main_model_calls=deployment.fixture.main_calls, restored_seed=True,
                    tool_side_effects=deployment.fixture.counter.read_text().splitlines())
            left, right = deployments
            result["deployments"][left.marker]["shutdown"] = await close_owned(left, code)
            assert right.worker.is_running() and right.worker.create_time() == right.worker_created
            after = (await rpc(right, "same-after-query", "session.get_metadata"))["runtime"]
            assert after["worker_instance"] == identities[1][1]
            result["second_worker_survived_first_final_shutdown"] = True
            result["deployments"][right.marker]["runtime_after_other_shutdown"] = after
            result["deployments"][right.marker]["shutdown"] = await close_owned(right, code)
            result["passed"] = True
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            for deployment in deployments:
                deployment.fixture.release.set()
                target = result["deployments"].setdefault(deployment.marker, {})
                target["fixture_before_cleanup"] = fixture_observation(deployment)
                target["wire_frames"] = list(deployment.frames)
                try:
                    if not deployment.closed:
                        await close_owned(deployment, code, timeout=5)
                except (Exception, asyncio.CancelledError) as exc:
                    result["cleanup_errors"].append(f"{deployment.marker}: {type(exc).__name__}: {exc}")
                for pid, created in reversed(deployment.owned_workers):
                    if deployment.closed:
                        continue
                    try:
                        worker = psutil.Process(pid)
                        if worker.create_time() == created:
                            for child in reversed(worker.children(recursive=True)):
                                child.kill()
                            worker.kill()
                            worker.wait(timeout=5)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired) as exc:
                        if not isinstance(exc, psutil.NoSuchProcess):
                            result["cleanup_errors"].append(f"{deployment.marker}: fallback cleanup {pid}: {exc}")
                if deployment.process_handle is not None:
                    deployment.process_handle.close()
                deployment.log.close()
                deployment.model.shutdown()
                deployment.model.server_close()
                target["fixture_observation"] = fixture_observation(deployment)
                history_path = deployment.session / "history.jsonl"
                if history_path.exists():
                    target["final_history"] = history_path.read_text(encoding="utf-8")
                for path in deployment.data.rglob("*.log"):
                    target = output.parent / (output.stem + "-logs") / deployment.marker / path.relative_to(deployment.data)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.with_suffix(target.suffix + ".gz").write_bytes(gzip.compress(path.read_bytes(), mtime=0))
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ["passed", "both_workers_alive_simultaneously",
        "second_worker_survived_first_final_shutdown"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--front-executable", help="Optional actual frozen Front/Worker executable")
    asyncio.run(run(parser.parse_args()))
