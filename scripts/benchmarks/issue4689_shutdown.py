# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real active-execution final shutdown, distinct from Front detach/reconnect.

Uses the actual SDK and PowerShell tool with a local model protocol fixture.
The second model request stays blocked until the Worker has exited. Import
observers only record calls to the original lifecycle functions.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from http.server import ThreadingHTTPServer
from pathlib import Path

import psutil
import yaml

import issue4689_native as native


OBSERVER = r'''
import functools, importlib.abc, importlib.machinery, json, os, sys, threading, time
lock = threading.Lock()
def record(stage, **values):
    value = dict(stage=stage, pid=os.getpid(), at_ns=time.perf_counter_ns(), **values)
    with lock, open(os.environ['ISSUE4689_SHUTDOWN_STEPS'], 'a', encoding='utf-8') as out:
        out.write(json.dumps(value) + '\n')
class Observer(importlib.abc.MetaPathFinder):
    targets = {
        'jiuwenswarm.server.worker.dispatcher',
        'jiuwenswarm.runtime.session.coordinator',
        'jiuwenswarm.server.runtime.session.session_history',
        'jiuwenswarm.server.runtime.session.session_metadata',
        'jiuwenswarm.runtime.kernel',
        'jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_owner',
        'jiuwenswarm.server.ipc.server',
    }
    def find_spec(self, name, path=None, target=None):
        if name not in self.targets:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return None
        original = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                if name.endswith('dispatcher'):
                    call = module.WorkerDispatcher.drain
                    @functools.wraps(call)
                    async def drain(instance, *args, **kwargs):
                        record('drain.begin', tasks=len(instance._tasks))
                        result = await call(instance, *args, **kwargs)
                        record('drain.done', ok=result, stopped_accepting=instance._draining,
                               queued=len(instance._queued))
                        return result
                    module.WorkerDispatcher.drain = drain
                elif name.endswith('coordinator'):
                    call = module.RuntimeSessionCoordinator.cancel_execution
                    @functools.wraps(call)
                    async def cancel(instance, *args, **kwargs):
                        result = await call(instance, *args, **kwargs)
                        record('cancel.done', matched=result.matched, cancelled=result.cancelled,
                               timed_out=list(result.timed_out))
                        return result
                    module.RuntimeSessionCoordinator.cancel_execution = cancel
                elif name.endswith(('session_history', 'session_metadata')):
                    call = module.flush_pending_writes
                    @functools.wraps(call)
                    def flush(*args, **kwargs):
                        result = call(*args, **kwargs)
                        record(name.rsplit('.', 1)[-1] + '.flush.done', ok=result)
                        return result
                    module.flush_pending_writes = flush
                else:
                    owner = (module.ResourceKernel if name.endswith('kernel') else
                             module.KVCacheApplicationOwner if name.endswith('kv_cache_application_owner')
                             else module.IpcServer)
                    call = owner.close
                    @functools.wraps(call)
                    async def close(instance, *args, **kwargs):
                        result = await call(instance, *args, **kwargs)
                        record(name.rsplit('.', 1)[-1] + '.close.done')
                        return result
                    owner.close = close
        spec.loader = Loader()
        return spec
sys.meta_path.insert(0, Observer())
'''


class ShutdownModelFixture(native.ModelFixture):
    cancelled_connections = 0

    def do_POST(self):
        try:
            super().do_POST()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            if not type(self).release.is_set():
                raise
            # The gate opens only after final exit (or failure cleanup). An
            # already cancelled Worker HTTP request cannot receive that reply.
            type(self).cancelled_connections += 1


class NativeProcessHandle:
    """Hold the Windows process object before shutdown, avoiding PID reuse."""

    def __init__(self, pid):
        if sys.platform != "win32":
            raise RuntimeError("This formal shutdown acceptance requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout):
        result = self.kernel.WaitForSingleObject(self.handle, max(1, int(timeout * 1000)))
        if result != 0:
            raise TimeoutError(f"Original Worker process handle did not exit: {result}")
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


async def run(args):
    root = Path(args.code).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    native.GATEWAY = False
    fixture = ShutdownModelFixture
    fixture.calls = fixture.main_calls = 0
    fixture.entered.clear()
    fixture.release.clear()
    fixture.scenario = "tool"
    model = ThreadingHTTPServer(("127.0.0.1", 0), fixture)
    model.daemon_threads = False
    threading.Thread(target=model.serve_forever, daemon=True).start()
    result = {"surface": "Windows/native-Front/Worker/SDK/PowerShell",
              "fixture": "local-openai-protocol", "scenario": "active-final-shutdown",
              "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()}
    worker = process_handle = front = ws = None
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-active-shutdown-", ignore_cleanup_errors=True) as directory:
        data = Path(directory) / "data"
        cfg = data / "config"
        cfg.mkdir(parents=True)
        config = yaml.safe_load((root / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8"))
        config["models"]["defaults"] = [{"model_id": "mdl_fixture", "is_default": True,
            "model_client_config": {"client_provider": "OpenAI", "api_base": f"http://127.0.0.1:{model.server_port}/v1",
                                    "api_key": "local-fixture", "model_name": "fixture-model"},
            "model_config_obj": {"temperature": 0, "max_tokens": 128}}]
        config["progressive_tool_enabled"] = False
        config["permissions"]["enabled"] = False
        config.setdefault("react", {}).update(enable_skill_evolution=False, enable_read_image_multimodal=False)
        (cfg / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        (cfg / ".env").write_text("# isolated active shutdown fixture\n", encoding="utf-8")
        session = data / "agent/sessions/shutdown-session"
        session.mkdir(parents=True)
        (session / "metadata.json").write_text(json.dumps({"session_id": "shutdown-session", "mode": "agent",
            "channel_id": "web", "user_id": "acceptance-owner", "title": "Active shutdown acceptance",
            "work_mode": "work", "project_dir": str(data / "agent/workspace"), "created_at": time.time()}), encoding="utf-8")
        (session / "history.jsonl").write_text(json.dumps({"role": "user", "content": "Earlier persisted input"}) + "\n"
            + json.dumps({"role": "assistant", "content": "Earlier persisted output"}) + "\n", encoding="utf-8")
        fixture.counter = data / "side-effects.txt"
        steps = Path(directory) / "shutdown-steps.jsonl"
        observer = Path(directory) / "observer"
        observer.mkdir()
        (observer / "sitecustomize.py").write_text(OBSERVER, encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(observer), str(root))),
            "JIUWENSWARM_DATA_DIR": str(data), "JIUWENSWARM_HOME": str(Path(directory) / "home"),
            "USERPROFILE": str(Path(directory) / "home"), "HOME": str(Path(directory) / "home"),
            "JIUWENSWARM_RUNTIME_PROFILE": "desktop", "NO_PROXY": "127.0.0.1,localhost",
            "ISSUE4689_SHUTDOWN_STEPS": str(steps)}
        env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
        log = output.with_suffix(".log").open("wb")
        try:
            port = native.port()
            front = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(port)],
                                     cwd=root, env=env, stdout=log, stderr=log)
            ws = await native.open_front(port)
            request_id = "active-shutdown-chat"
            await native.send(ws, request_id, "chat.send", "shutdown-session",
                              {"query": "[4689-original] Execute the tool once, then continue waiting", "mode": "agent"}, stream=True)
            result["original_chat_submissions"] = 1
            deadline = time.monotonic() + 90
            while fixture.main_calls < 2:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Original SDK execution did not complete the tool and enter the next model call")
                await asyncio.sleep(.05)
            assert fixture.counter.read_text().splitlines() == ["once"]
            snapshot = await native.rpc(ws, "active-state", "session.get_metadata", "shutdown-session")
            runtime = snapshot.get("runtime", {})
            executions = [e for e in runtime.get("executions", []) if e.get("request_id") == request_id]
            assert runtime.get("is_processing") is True and len(executions) == 1, runtime
            assert executions[0]["state"] not in {"cancelled", "succeeded", "failed"}, executions
            worker = native.module_processes(front, "jiuwenswarm.server.worker.main")[-1]
            created = worker.create_time()
            process_handle = NativeProcessHandle(worker.pid)
            result.update(worker_pid=worker.pid, worker_created=created, before=runtime,
                          model_calls_before_shutdown=fixture.main_calls,
                          history_before=(session / "history.jsonl").read_text(encoding="utf-8"))
            await ws.close()
            ws = None
            native.stop_front(front)
            assert worker.is_running() and worker.create_time() == created
            assert not fixture.release.is_set(), "model must remain blocked until final exit"
            started = time.perf_counter_ns()
            close_deadline = time.monotonic() + 30
            helper = await asyncio.create_subprocess_exec(sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown",
                "--shutdown-worker", "--timeout", "29", cwd=root, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(helper.communicate(), max(.001, close_deadline - time.monotonic()))
            assert helper.returncode == 0, stderr.decode(errors="replace")
            result["shutdown"] = json.loads(stdout.splitlines()[-1])
            assert result["shutdown"].get("kind") == "shutdown.done" and result["shutdown"].get("ok") is True
            result["native_process_exit_code"] = await asyncio.to_thread(process_handle.wait, max(.001, close_deadline - time.monotonic()))
            result["blocked_model_released_after_exit"] = not fixture.release.is_set()
            result["shutdown_ms"] = (time.perf_counter_ns() - started) / 1e6
            assert result["shutdown_ms"] <= 30000
            observed = [json.loads(line) for line in steps.read_text(encoding="utf-8").splitlines()]
            observed = [step for step in observed if step["pid"] == worker.pid and step["at_ns"] >= started]
            result["shutdown_steps"] = observed
            drain = next(step for step in observed if step["stage"] == "drain.done")
            assert drain["ok"] is True and drain["stopped_accepting"] is True and drain["queued"] == 0
            assert any(step["stage"] == "cancel.done" and step["cancelled"] >= 1 and not step["timed_out"] for step in observed)
            for stage in ["session_history.flush.done", "session_metadata.flush.done"]:
                assert any(step["stage"] == stage and step["ok"] is True for step in observed), stage
            for stage in ["kernel.close.done", "kv_cache_application_owner.close.done", "server.close.done"]:
                assert any(step["stage"] == stage for step in observed), stage
            history = (session / "history.jsonl").read_text(encoding="utf-8")
            records = [json.loads(line) for line in history.splitlines()]
            metadata = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
            assert history.startswith(result["history_before"]), "final flush must preserve previous history"
            assert len([row for row in records if row.get("role") == "user" and row.get("request_id") == request_id]) == 1
            assert any(row.get("event_type") == "chat.tool_result" and row.get("success") is True for row in records)
            assert metadata.get("session_id") == "shutdown-session" and metadata.get("title") == "Active shutdown acceptance"
            result.update(history_after=history, history_sha256=hashlib.sha256(history.encode()).hexdigest(),
                          metadata_after=metadata, side_effects=fixture.counter.read_text().splitlines(),
                          model_calls_after_shutdown=fixture.main_calls, passed=True)
            assert result["side_effects"] == ["once"] and fixture.main_calls == 2
        except Exception as exc:
            result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            fixture.release.set()
            if ws is not None:
                await ws.close()
            if worker is None and front is not None and front.poll() is None:
                children = native.module_processes(front, "jiuwenswarm.server.worker.main")
                if children:
                    worker = children[-1]
                    created = worker.create_time()
            if front is not None and front.poll() is None:
                native.stop_front(front)
            if worker is not None:
                try:
                    if worker.is_running() and worker.create_time() == created:
                        subprocess.run([sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker", "--timeout", "5"],
                                       cwd=root, env=env, capture_output=True, timeout=8)
                except psutil.NoSuchProcess:
                    pass
            if process_handle is not None:
                process_handle.close()
            log.close()
            model.shutdown()
            model.server_close()
            result["fixture_cancelled_connections"] = fixture.cancelled_connections
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result.get(key) for key in ["passed", "worker_pid", "shutdown_ms", "native_process_exit_code", "side_effects"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default=".")
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))
