"""Native Worker upgrade from old protocol/code; reject reuse, close, then resume."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import psutil
import yaml

import issue4689_native as native


async def run(args):
    native.GATEWAY = False
    root = Path(args.code).resolve()
    old = Path(args.old_code).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fixture = native.ModelFixture
    fixture.calls = fixture.main_calls = 0
    fixture.scenario = "tool"
    fixture.release.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result = {"old_code": str(old), "new_code": str(root), "scenario": args.scenario}
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-upgrade-", ignore_cleanup_errors=True) as temporary:
        data = Path(temporary) / "data"
        home = Path(temporary) / "home"
        home.mkdir()
        cfg = data / "config"
        cfg.mkdir(parents=True)
        config = yaml.safe_load((root / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8"))
        config["models"]["defaults"] = [{"model_id": "mdl_fixture", "is_default": True,
            "model_client_config": {"client_provider": "OpenAI", "api_base": f"http://127.0.0.1:{server.server_port}/v1",
                                    "api_key": "local-fixture", "model_name": "fixture-model"},
            "model_config_obj": {"temperature": 0, "max_tokens": 128}}]
        config["progressive_tool_enabled"] = False
        config["permissions"]["enabled"] = False
        config.setdefault("react", {}).update(enable_skill_evolution=False, enable_read_image_multimodal=False)
        (cfg / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        (cfg / ".env").write_text("# isolated upgrade fixture\n", encoding="utf-8")
        fixture.counter = data / "side-effects.txt"
        sid = data / "agent/sessions/upgrade-session"
        sid.mkdir(parents=True)
        (sid / "metadata.json").write_text(json.dumps({"session_id": "upgrade-session", "mode": "agent",
            "channel_id": "web", "user_id": "acceptance-owner", "title": "Upgrade acceptance", "work_mode": "work",
            "project_dir": str(data / "agent/workspace"), "created_at": time.time()}), encoding="utf-8")
        endpoint = str(data / "run/upgrade.pipe")
        env = {**os.environ, "JIUWENSWARM_DATA_DIR": str(data), "JIUWENSWARM_HOME": str(home),
               "USERPROFILE": str(home), "HOME": str(home), "JIUWENSWARM_RUNTIME_PROFILE": "desktop",
               "JIUWENSWARM_RUNTIME_SOCKET": endpoint, "NO_PROXY": "127.0.0.1,localhost"}
        log = output.with_suffix(".log").open("wb")
        prepare = subprocess.run([sys.executable, "-c",
            "from jiuwenswarm.common.utils import prepare_runtime_workspace; prepare_runtime_workspace(); "
            "from jiuwenswarm.common.config import migrate_model_business_ids; migrate_model_business_ids()"],
            cwd=root, env={**env, "PYTHONPATH": str(root)}, stdout=log, stderr=log, timeout=90)
        assert prepare.returncode == 0
        env["JIUWENSWARM_RUNTIME_WORKSPACE_READY"] = "1"
        old_env = {**env, "PYTHONPATH": str(old), "JIUWENSWARM_BUILD_ID": "upgrade-before"}
        new_env = {**env, "PYTHONPATH": str(root), "JIUWENSWARM_BUILD_ID": "upgrade-after"}
        front_port = native.port()
        def launch(path, environment):
            return subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(front_port)],
                                    cwd=path, env=environment, stdout=log, stderr=log)
        front = launch(old, old_env)
        old_worker = None
        ws = None
        try:
            ws = await native.open_front(front_port)
            await native.send(ws, "original-upgrade", "chat.send", "upgrade-session",
                              {"query": "[4689-original] Execute the original tool once", "mode": "agent"}, stream=True)
            while True:
                wire = json.loads(await asyncio.wait_for(ws.recv(), 120))
                if wire.get("request_id") == "original-upgrade" and wire.get("is_final"):
                    break
            assert fixture.counter.read_text().splitlines() == ["once"]
            old_worker = native.module_processes(front, "jiuwenswarm.server.worker.main")[-1]
            created = old_worker.create_time()
            result["old_worker_pid"] = old_worker.pid
            history = sid / "history.jsonl"
            result["history_before"] = history.read_text(encoding="utf-8")
            calls_before = fixture.main_calls
            await ws.close()
            ws = None
            native.stop_front(front)
            front = launch(root, new_env)
            ws = await native.open_front(front_port)
            await asyncio.sleep(3)
            health = await native.rpc(ws, "incompatible-health", "health_check.get_conf", "upgrade-session")
            result["incompatible_readiness"] = health
            result["no_automatic_replay"] = fixture.main_calls == calls_before
            assert old_worker.is_running() and old_worker.create_time() == created
            assert result["no_automatic_replay"]
            await ws.close()
            ws = None
            native.stop_front(front)
            # Original launch ownership proves this exact deployment/PID. The
            # old protocol has no data_scope, so a new generic owner must not
            # pretend it validated the deployment from that hello alone.
            from jiuwenswarm.server.ipc.client import connect_with_retry
            conn = await connect_with_retry(endpoint, timeout=5)
            hello = await asyncio.wait_for(conn.recv(), 5)
            assert hello["pid"] == old_worker.pid and old_worker.create_time() == created
            result["old_hello"] = hello
            started = time.monotonic()
            await conn.send({"kind": "shutdown"})
            while True:
                try:
                    message = await asyncio.wait_for(conn.recv(), 29)
                    if message.get("kind") == "shutdown.done":
                        result["old_shutdown"] = message
                        break
                except (OSError, EOFError, asyncio.IncompleteReadError):
                    break
            await conn.close()
            await asyncio.to_thread(old_worker.wait, max(.01, 30 - (time.monotonic() - started)))
            result["old_shutdown_ms"] = (time.monotonic() - started) * 1000
            front = launch(root, new_env)
            ws = await native.open_front(front_port)
            await native.send(ws, "new-continuation", "chat.send", "upgrade-session",
                              {"query": "[4689-original] Continue after the explicit upgrade", "mode": "agent"}, stream=True)
            while True:
                wire = json.loads(await asyncio.wait_for(ws.recv(), 120))
                if wire.get("request_id") == "new-continuation" and wire.get("is_final"):
                    break
            result["new_worker_pid"] = native.module_processes(front, "jiuwenswarm.server.worker.main")[-1].pid
            result["history_after"] = history.read_text(encoding="utf-8")
            result["side_effects"] = fixture.counter.read_text().splitlines()
            assert result["history_after"].startswith(result["history_before"])
            assert result["side_effects"] == ["once"]
            assert result["new_worker_pid"] != result["old_worker_pid"]
            assert health.get("state") == "FAILED", "incompatible Worker must report FAILED rather than stay warming"
            result["passed"] = True
        except Exception as exc:
            result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if ws is not None:
                await ws.close()
            workers = native.module_processes(front, "jiuwenswarm.server.worker.main")
            if front.poll() is None:
                native.stop_front(front)
            helper = subprocess.run([sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker"],
                                    cwd=root, env=new_env, capture_output=True, timeout=35)
            result["final_shutdown_exit"] = helper.returncode
            for worker in workers:
                try:
                    await asyncio.to_thread(worker.wait, 5)
                except psutil.NoSuchProcess:
                    pass
            log.close()
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    server.shutdown()
    server.server_close()
    print(json.dumps({k: result[k] for k in ["passed", "old_worker_pid", "new_worker_pid", "side_effects"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default=".")
    parser.add_argument("--old-code", required=True)
    parser.add_argument("--scenario", default="protocol-upgrade")
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))
