"""Real-process optional capability enablement and controlled failure acceptance."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import psutil
import yaml

import issue4689_native as native

FAULT_OBSERVER = '''
import importlib.abc, importlib.machinery, sys
class Faults(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name not in ('jiuwenswarm.server.personal_context.host_api', 'jiuwenswarm.server.runtime.proactive_adapter', 'jiuwenswarm.server.runtime.image_modality_warmup'):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        original = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec): return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                async def failed(*args, **kwargs):
                    raise RuntimeError('optional capability acceptance fault: ' + name)
                if name.endswith('host_api'): module.PersonalContextHostAPI.start = failed
                elif name.endswith('proactive_adapter'): module.init_proactive_engine = failed
                else: module.probe_image_support = failed
        spec.loader = Loader()
        return spec
sys.meta_path.insert(0, Faults())
'''


async def rpc(ws, request_id, method, params=None):
    await native.send(ws, request_id, method, "optional-session", params)
    while True:
        wire = json.loads(await asyncio.wait_for(ws.recv(), 90))
        if wire.get("request_id") != request_id:
            continue
        body = native.payload(wire)
        if isinstance(body, dict) and body.get("event_type") in {"runtime.accepted", "runtime.warming"}:
            continue
        return {"wire": wire, "payload": body}


async def run(args):
    root = Path(args.code).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    native.GATEWAY = False
    fixture = native.ModelFixture
    fixture.calls = fixture.main_calls = 0
    fixture.scenario = "restart"
    fixture.release.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result = {"scenario": args.scenario, "transport": "real-Front/Worker/SDK; local model protocol fixture"}
    with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-optional-", ignore_cleanup_errors=True) as temporary:
        data = Path(temporary) / "data"
        config_dir = data / "config"
        config_dir.mkdir(parents=True)
        config = yaml.safe_load((root / "jiuwenswarm/resources/config.yaml").read_text(encoding="utf-8"))
        config["models"]["defaults"] = [{"model_id": "mdl_fixture", "is_default": True,
            "model_client_config": {"client_provider": "OpenAI", "api_base": f"http://127.0.0.1:{server.server_port}/v1",
                                    "api_key": "local-fixture", "model_name": "fixture-model"},
            "model_config_obj": {"temperature": 0, "max_tokens": 128}}]
        config["progressive_tool_enabled"] = False
        config["permissions"]["enabled"] = False
        config.setdefault("react", {}).update(enable_skill_evolution=False)
        config["react"]["enable_read_image_multimodal"] = False if args.scenario == "default" else None
        config["models"]["enable_free_models"] = args.scenario != "default"
        config.setdefault("proactive_recommendation", {})["enabled"] = args.scenario != "default"
        if args.scenario == "mcp-failure":
            marker = data / "failed-mcp-started.txt"
            config["mcp"] = {"servers": [{"name": "failed-optional-mcp", "enabled": True, "transport": "stdio",
                "command": sys.executable, "args": ["-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('started'); raise SystemExit(17)"]}]}
        (config_dir / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        (config_dir / ".env").write_text("# isolated optional fixture\n", encoding="utf-8")
        session = data / "agent/sessions/optional-session"
        session.mkdir(parents=True)
        (session / "metadata.json").write_text(json.dumps({"session_id": "optional-session", "mode": "agent",
            "channel_id": "web", "user_id": "acceptance-owner", "title": "Optional acceptance", "work_mode": "work",
            "project_dir": str(data / "agent/workspace")}), encoding="utf-8")
        (session / "history.jsonl").write_text(json.dumps({"role": "user", "content": "Existing isolated history"}) + "\n"
            + json.dumps({"role": "assistant", "content": "Existing isolated response"}) + "\n", encoding="utf-8")
        observer = Path(temporary) / "observer"
        observer.mkdir()
        if args.scenario == "faults":
            (observer / "sitecustomize.py").write_text(FAULT_OBSERVER, encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(observer), str(root))),
            "JIUWENSWARM_DATA_DIR": str(data), "JIUWENSWARM_HOME": str(Path(temporary) / "home"),
            "USERPROFILE": str(Path(temporary) / "home"), "HOME": str(Path(temporary) / "home"),
            "JIUWENSWARM_RUNTIME_PROFILE": "desktop", "NO_PROXY": "127.0.0.1,localhost"}
        env.pop("JIUWENSWARM_RUNTIME_SOCKET", None)
        port = native.port()
        log = output.with_suffix(".log").open("wb")
        front = subprocess.Popen([sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(port)],
                                 cwd=root, env=env, stdout=log, stderr=log)
        ws = None
        try:
            ws = await native.open_front(port)
            result["warming_controls"] = {}
            for index, method in enumerate(["session.list", "project.list", "config.get", "history.get"]):
                response = await rpc(ws, f"control-{index}", method, {"page_idx": 1})
                assert response["wire"].get("status") != "failed", response
                result["warming_controls"][method] = response["wire"].get("status")
            result["capabilities"] = {}
            queries = [
                ("skills", "skills.list", {}), ("MCP", "mcp.list", {}),
                ("PersonalContext", "personal_context.runtime.status", {}),
                ("Sandbox-Windows", "command.sandbox", {"sub": "enable"}),
                ("Zen-policy", "models.zen_entries", {}),
            ]
            if args.scenario == "enabled":
                queries += [("PersonalContext-master", "personal_context.runtime.set_master_enabled", {"enabled": True}),
                            ("PersonalContext-enable", "personal_context.runtime.start_agent_use", {}),
                            ("Proactive", "proactive.tick", {})]
            for index, (name, method, params) in enumerate(queries):
                result["capabilities"][name] = await rpc(ws, f"optional-{index}", method, params)
            if args.scenario == "enabled":
                assert result["capabilities"]["PersonalContext-enable"]["wire"].get("status") == "succeeded"
            if args.scenario == "mcp-failure":
                await native.send(ws, "failed-mcp-use", "chat.send", "optional-session",
                    {"query": "[4689-original] Request the unavailable MCP capability", "mode": "agent", "mcp": ["failed-optional-mcp"]}, stream=True)
                failed_mcp_events = []
                while True:
                    wire = json.loads(await asyncio.wait_for(ws.recv(), 120))
                    if wire.get("request_id") == "failed-mcp-use":
                        failed_mcp_events.append(wire)
                        if wire.get("is_final"):
                            break
                result["mcp_child_started"] = marker.exists()
                result["mcp_first_use_events"] = failed_mcp_events
                assert result["mcp_child_started"], "the actual failing MCP subprocess must be exercised"
            await native.send(ws, "base-chat", "chat.send", "optional-session",
                              {"query": "[4689-original] Base chat must remain available", "mode": "agent", "mcp": []}, stream=True)
            chat = []
            while True:
                wire = json.loads(await asyncio.wait_for(ws.recv(), 120))
                if wire.get("request_id") == "base-chat":
                    chat.append(wire)
                    if wire.get("is_final"):
                        break
            assert fixture.main_calls > 0 and "resident-worker-ok" in json.dumps(chat)
            result["chat_completed"] = True
            result["health"] = (await rpc(ws, "health", "health_check.get_conf"))["payload"]
            assert result["health"].get("state") != "FAILED", result["health"]
            result["passed"] = True
        except Exception as exc:
            result.update(passed=False, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if ws is not None:
                await ws.close()
            workers = native.module_processes(front, "jiuwenswarm.server.worker.main")
            native.stop_front(front)
            helper = subprocess.run([sys.executable, "-m", "jiuwenswarm.server.supervisor.shutdown", "--shutdown-worker"],
                                    cwd=root, env=env, capture_output=True, timeout=35)
            result["shutdown_exit_code"] = helper.returncode
            for worker in workers:
                try:
                    await asyncio.to_thread(worker.wait, 5)
                except psutil.NoSuchProcess:
                    pass
            log.close()
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    server.shutdown()
    server.server_close()
    print(json.dumps({"scenario": args.scenario, "passed": result["passed"], "health": result["health"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default=".")
    parser.add_argument("--scenario", choices=("default", "enabled", "faults", "mcp-failure"), default="default")
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))
