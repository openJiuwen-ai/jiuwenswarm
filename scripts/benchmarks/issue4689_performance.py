# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Paired Windows source acceptance against an immutable Issue 4689 baseline.

Run output belongs outside both checkouts. Prepared-workspace and first-install
cold requests are separate populations. Model transport is a local fixture;
real model/tool and lifecycle correctness acceptance must be reported separately.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml
import psutil
from websockets.legacy.client import connect

from issue4689_chat import OBSERVER as SDK_OBSERVER
from issue4689_front import receive, seed, terminate_owned
from issue4689_native import ModelFixture, open_front, payload, port, send


SDK_OBSERVER = SDK_OBSERVER.replace(
    '"pid": os.getpid()',
    '"pid": os.getpid(), "sdk_module_file": module.__file__, '
    '"jiuwenswarm_module_file": getattr(sys.modules.get("jiuwenswarm"), "__file__", None), '
    '"python_executable": sys.executable',
)


STAGE_OBSERVER = r'''
import functools, inspect, threading
_trace_lock = threading.Lock()
_targets = {
 "jiuwenswarm.server.supervisor.worker_process": ["WorkerProcess.spawn"],
 "jiuwenswarm.server.supervisor.runtime_supervisor": ["RuntimeSupervisor._bring_up"],
 "jiuwenswarm.server.ipc.server": ["IpcServer.start"],
 "jiuwenswarm.extensions.manager": ["ExtensionManager.load_all_extensions"],
 "jiuwenswarm.runtime.kernel": ["ResourceKernel.acquire", "ResourceKernel.acquire_async"],
 "jiuwenswarm.server.worker.dispatcher": ["WorkerDispatcher._run_request"],
 "jiuwenswarm.common.utils": ["prepare_runtime_workspace", "prepare_workspace",
   "ensure_default_builtin_skills", "ensure_config_migrated_from_template",
   "cleanup_stale_openjiuwen_descs", "setup_logger"],
 "jiuwenswarm.common.config": ["migrate_model_business_ids"],
 "jiuwenswarm.common.model_migration": ["migrate_legacy_model_selections"],
 "jiuwenswarm.server.worker.lifecycle": ["_preload_runtime_backend",
   "_configure_openjiuwen_logging", "_apply_runtime_entry_patches", "start_worker_runtime"],
 "jiuwenswarm.server.worker.service": ["_send_hello"],
 "jiuwenswarm.server.agent_ws_server": ["AgentWebSocketServer.__init__",
   "AgentWebSocketServer.start", "AgentWebSocketServer._start_asset_services",
   "AgentWebSocketServer._start_personal_context_best_effort",
   "AgentWebSocketServer._bootstrap_internal_jiuwenbox"],
 "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_owner": [
   "KVCacheApplicationOwner.__init__", "KVCacheApplicationOwner.activate_from_config"],
 "jiuwenswarm.runtime.service": ["AgentRuntime.start"],
 "jiuwenswarm.agents.harness.code.rails.heartbeat.runtime": ["HeartbeatRailRuntime.start"],
 "jiuwenswarm.server.runtime.agent_manager": ["AgentManager.initialize",
   "AgentManager._create_agent", "AgentManager.get_agent_for_request"],
 "jiuwenswarm.server.runtime.agent_adapter.interface_deep": [
   "JiuWenSwarmDeepAdapter.__init__", "JiuWenSwarmDeepAdapter._create_instance",
   "JiuWenSwarmDeepAdapter._create_model", "JiuWenSwarmDeepAdapter._build_agent_rails",
   "JiuWenSwarmDeepAdapter._resolve_session_git_snapshot",
   "JiuWenSwarmDeepAdapter._update_runtime_config"],
 "jiuwenswarm.agents.harness.common.session_ops_service": ["warmup_session_context"],
 "jiuwenswarm.server.runtime.session.session_history": ["read_session_history_records"],
 "jiuwenswarm.server.runtime.proactive_adapter": ["init_proactive_engine", "build_proactive_agent"],
}
_import_targets = set(_targets) | {"openjiuwen.harness.tools", "openjiuwen.harness.deep_agent",
 "openjiuwen.core.session", "openjiuwen.core.single_agent", "openjiuwen.harness.factory",
 "jiuwenswarm.server.agent_ws_server", "jiuwenswarm.common.schema.agent"}
def _record(stage, begin, error=None):
    row={"stage":stage,"begin_ns":begin,"end_ns":time.perf_counter_ns(),
         "pid":os.getpid(),"thread":threading.get_ident(),
         "requested_mode":os.environ.get("ISSUE4689_REQUESTED_MODE"),
         "profile":os.environ.get("JIUWENSWARM_RUNTIME_PROFILE", "desktop"),
         "frozen":bool(getattr(sys,"frozen",False))}
    started=os.environ.get("ISSUE4689_TRIAL_START_NS")
    if started: row["from_front_spawn_ms"]=(row["end_ns"]-int(started))/1e6
    if error is not None: row["error_type"]=type(error).__name__
    with _trace_lock, open(os.environ["ISSUE4689_STAGE_LOG"],"a",encoding="utf-8") as f:
        f.write(json.dumps(row)+"\n")
def _load_sdk(original,module):
    begin=time.perf_counter_ns(); error=None
    try: original.exec_module(module)
    except BaseException as exc: error=exc; raise
    finally: _record("import:openjiuwen.harness.deep_agent",begin,error)
def _wrap(original, name):
    if inspect.isasyncgenfunction(original): return original
    if inspect.iscoroutinefunction(original):
        @functools.wraps(original)
        async def async_wrapped(*args, **kwargs):
            begin=time.perf_counter_ns(); error=None
            try: return await original(*args, **kwargs)
            except BaseException as exc: error=exc; raise
            finally: _record(name,begin,error)
        return async_wrapped
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        begin=time.perf_counter_ns(); error=None
        try: return original(*args, **kwargs)
        except BaseException as exc: error=exc; raise
        finally: _record(name,begin,error)
    return wrapped
class StageObserver(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _import_targets: return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec is None or spec.loader is None: return None
        original=spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self,spec): return original.create_module(spec)
            def exec_module(self,module):
                begin=time.perf_counter_ns(); error=None
                try: original.exec_module(module)
                except BaseException as exc: error=exc; raise
                finally: _record("import:"+fullname,begin,error)
                for attr in _targets.get(fullname,[]):
                    owner=module
                    parts=attr.split(".")
                    for part in parts[:-1]: owner=getattr(owner,part,None)
                    if owner is None: continue
                    descriptor=inspect.getattr_static(owner,parts[-1],None)
                    if isinstance(descriptor,(staticmethod,classmethod)):
                        function=_wrap(descriptor.__func__,fullname+"."+attr)
                        setattr(owner,parts[-1],type(descriptor)(function))
                    elif inspect.isfunction(descriptor):
                        setattr(owner,parts[-1],_wrap(descriptor,fullname+"."+attr))
        spec.loader=Loader()
        return spec
# SDK observer stays first so the real DeepAgent entry remains identical in
# both versions; stage observation must precede the standard PathFinder.
sys.meta_path.insert(1, StageObserver())
'''

PREPARE = '''
import importlib.metadata as metadata, importlib.util, json, os, sys
from jiuwenswarm.common.utils import prepare_runtime_workspace
prepare_runtime_workspace(cleanup_stale_descs=True, migrate_config=True)
from jiuwenswarm.common.config import migrate_model_business_ids
from jiuwenswarm.common.model_migration import migrate_legacy_model_selections
migrate_model_business_ids()
migrate_legacy_model_selections()
import jiuwenswarm
provenance = {"python": sys.executable, "base_prefix": sys.base_prefix,
 "jiuwenswarm": jiuwenswarm.__file__, "openjiuwen": importlib.util.find_spec("openjiuwen").origin,
 "data_directory": os.environ["JIUWENSWARM_DATA_DIR"]}
with open(os.environ["ISSUE4689_PREP_PROVENANCE"], "w", encoding="utf-8") as f:
    json.dump(provenance, f, indent=2)
'''


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def append_json(path: Path, value) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(value, ensure_ascii=False) + "\n")


def discover_node_binary(explicit: str | None = None) -> Path | None:
    """Find optional Web tooling from an explicit setting or PATH, without drive assumptions."""
    chosen = explicit or os.environ.get("JIUWENSWARM_NODE_EXECUTABLE")
    found = shutil.which(chosen or "node")
    if found is None and chosen and Path(chosen).is_file():
        found = chosen
    if chosen and found is None:
        raise FileNotFoundError(f"configured Node executable does not exist: {chosen}")
    if not found:
        return None
    executable = Path(found).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise PermissionError(f"Node executable is not a runnable file: {executable}")
    return executable


def isolated_environment(code: Path, root: Path, *, observer: Path | None = None,
                         importtime: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("JIUWENSWARM_BUILD_ID", "JIUWENSWARM_RUNTIME_SOCKET",
                "JIUWENSWARM_RUNTIME_WORKSPACE_READY", "PYTHONPROFILEIMPORTTIME",
                "JIUWENBOX_LISTEN", "PYTHONHOME", "ISSUE4689_ENTRY_LOG",
                "ISSUE4689_STAGE_LOG", "ISSUE4689_PREP_PROVENANCE",
                "JIUWENSWARM_SDK_ENTRY_LOG"):
        env.pop(key, None)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update(PYTHONPATH=os.pathsep.join(str(p) for p in (observer, code) if p),
               JIUWENSWARM_DATA_DIR=str(root / "data"), JIUWENSWARM_HOME=str(home),
               USERPROFILE=str(home), HOME=str(home), AGENT_SERVER_HOST="127.0.0.1",
               JIUWENSWARM_RUNTIME_PROFILE="desktop", NO_PROXY="127.0.0.1,localhost",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    if importtime:
        env["PYTHONPROFILEIMPORTTIME"] = "1"
    return env


def configure_fixture(data: Path, model_port: int) -> None:
    path = data / "config" / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["models"]["defaults"] = [{
        "model_id": "mdl_fixture", "is_default": True,
        "model_client_config": {"client_provider": "OpenAI",
                                "api_base": f"http://127.0.0.1:{model_port}/v1",
                                "api_key": "fixture", "model_name": "fixture-model"},
        "model_config_obj": {"temperature": 0, "max_tokens": 128},
    }]
    # Capability switches retain the shipped values; speed must not depend on
    # turning off permissions, progressive tools, or default optional features.
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")


def code_manifest(code: Path) -> dict:
    digest = hashlib.sha256()
    for source in sorted((code / "jiuwenswarm").rglob("*.py")):
        digest.update(source.relative_to(code).as_posix().encode())
        digest.update(source.read_bytes())
    files = {name: hashlib.sha256((code / name).read_bytes()).hexdigest()
             for name in ("pyproject.toml", "uv.lock") if (code / name).exists()}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=code, check=True,
                          capture_output=True, text=True).stdout.strip()
    return {"path": str(code), "head": head, "source_sha256": digest.hexdigest(),
            "dependency_files": files}


def population(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    ordered = sorted(values)
    return {"n": len(values), "mean_ms": statistics.fmean(values),
            **{f"p{p}_ms": ordered[math.ceil(len(ordered) * p / 100) - 1]
               for p in (50, 95, 99)}}


def summarize(rows: list[dict], metadata: dict) -> dict:
    metrics = {}
    for label in metadata["labels"]:
        selected = [row for row in rows if row["label"] == label]
        groups = {}
        for case in metadata["cases"]:
            if case == "front":
                complete = [r for r in selected if r["case"] == case and r["ok"]]
                groups["front.listen"] = population([r["listen_ms"] for r in complete])
                for method in ("session.list", "project.list", "config.get", "history.get"):
                    groups[method] = population([r["controls"][method]["available_from_spawn_ms"]
                                                 for r in complete])
            else:
                for condition in ("cold", "warm_same", "warm_new") if case == "chat" else ("cold",):
                    if condition.startswith("warm") and metadata.get(condition + "_samples") == 0:
                        continue
                    complete = [r for r in selected if r["case"] == case and r["ok"]
                                and r["condition"] == condition]
                    groups[f"{case}.{condition}"] = population([r["entry_ms"] for r in complete])
        metrics[label] = groups
    limits = {"front.listen": 300, "session.list": 500, "project.list": 500,
              "config.get": 500, "history.get": 500, "chat.cold": 2500,
              "chat.warm_same": 500, "chat.warm_new": 500, "fresh.cold": 2500}
    target = "candidate" if "candidate" in metrics else "baseline"
    failures = [row for row in rows if not row["ok"]]
    incomplete = []
    for label, groups in metrics.items():
        for name, values in groups.items():
            count_key = ("front_samples" if name == "front.listen" or "." not in name
                         or name in {"session.list", "project.list", "config.get", "history.get"}
                         else "fresh_install_samples" if name.startswith("fresh.")
                         else "cold_samples" if name == "chat.cold"
                         else name.split(".", 1)[1] + "_samples")
            expected = metadata.get(count_key)
            if expected is not None and values["n"] != expected:
                incomplete.append({"label": label, "population": name,
                                   "expected": expected, "observed": values["n"]})
    decisions = {name: {"limit_ms": limits[name], "p95_ms": values["p95_ms"],
                         "required": name != "fresh.cold",
                         "passed": values["p95_ms"] is not None and values["p95_ms"] <= limits[name]}
                 for name, values in metrics[target].items()}
    return {"metadata": metadata, "metrics": metrics, "acceptance": decisions,
            "failed_attempts": failures, "incomplete_populations": incomplete,
            "all_passed": not failures and not incomplete
                          and all(item["passed"] for item in decisions.values() if item["required"])}


def new_session(data: Path, session_id: str, mode: str) -> None:
    directory = data / "agent" / "sessions" / session_id
    directory.mkdir(parents=True)
    write_json(directory / "metadata.json", {"session_id": session_id, "mode": mode,
        "title": session_id, "work_mode": "code" if mode.startswith("code") else "work",
        "created_at": 1770000000, "round_id": 0})


def prepare_snapshot(args, root: Path, model_port: int, output: Path) -> Path:
    snapshot = root / "snapshot"
    data = snapshot / "data"
    seed(data, args.baseline, args.mode)
    configure_fixture(data, model_port)
    env = isolated_environment(args.baseline, snapshot)
    env["ISSUE4689_PREP_PROVENANCE"] = str(output / "preparation-provenance.json")
    with (output / "preparation.log").open("wb") as log:
        child = subprocess.run([args.python, "-c", PREPARE], cwd=args.baseline, env=env,
                               stdout=log, stderr=log, timeout=180)
    if child.returncode:
        raise RuntimeError(f"baseline workspace preparation exited {child.returncode}; see preparation.log")
    write_json(output / "snapshot-manifest.json", {"files": sum(p.is_file() for p in data.rglob("*")),
        "bytes": sum(p.stat().st_size for p in data.rglob("*") if p.is_file()),
        "actual_preparation_child_exited": True, "runtime_workspace_ready_marker": False,
        "capability_switches": "shipped defaults retained; model transport replaced only"})
    return data


async def request_entry(ws, *, request_id: str, session_id: str, mode: str,
                        entry_log: Path, endpoint: str, code: Path,
                        executable: Path | None = None) -> dict:
    sent = time.perf_counter_ns()
    await send(ws, request_id, "chat.send", session_id,
               {"query": "[4689-original] Reply resident-worker-ok", "mode": mode}, stream=True)
    while True:
        wire = json.loads(await asyncio.wait_for(ws.recv(), 90))
        if wire.get("request_id") != request_id:
            continue
        event = payload(wire)
        if wire.get("status") == "failed" or (isinstance(event, dict)
                                               and event.get("event_type") == "chat.error"):
            raise RuntimeError(f"request {request_id} failed: {wire.get('response_kind')}")
        if wire.get("is_final"):
            completed = time.perf_counter_ns()
            break
    snapshot_id = f"snapshot-{request_id}"
    await send(ws, snapshot_id, "session.get_metadata", session_id)
    response = await receive(ws, snapshot_id)
    runtime = payload(response).get("runtime", {})
    dispatches = [item for item in runtime.get("dispatches", []) if item["request_id"] == request_id]
    if len(dispatches) != 1 or not dispatches[0].get("entered_at_ns"):
        raise AssertionError(f"expected one prepared execution for {request_id}")
    prepared = dispatches[0]["entered_at_ns"]
    entry = prepared
    sdk_pid = None
    provenance = {}
    if endpoint in {"sdk", "native-sdk"}:
        events = [json.loads(line) for line in entry_log.read_text(encoding="utf-8").splitlines()]
        matches = [item for item in events if item["request_id"] == request_id]
        if len(matches) != 1:
            raise AssertionError(f"expected one SDK send_input entry for {request_id}, got {len(matches)}")
        entry, sdk_pid = matches[0]["entered_at_ns"], matches[0]["pid"]
        provenance = {key: matches[0][key] for key in (
            "sdk_module_file", "jiuwenswarm_module_file", "python_executable",
            "sender_module", "sender_owner") if key in matches[0]}
        if (endpoint == "sdk" and not Path(provenance["jiuwenswarm_module_file"])
                .resolve().is_relative_to(code)):
            raise AssertionError("request used JiuwenSwarm from a different checkout")
        if endpoint == "native-sdk":
            if (provenance.get("sender_owner") != "DeepAgent"
                    or provenance.get("sender_module") != "openjiuwen.harness.deep_agent"):
                raise AssertionError("native entry is not the actual DeepAgent SDK sender")
            process = psutil.Process(sdk_pid)
            argv = process.cmdline()
            if (not executable or os.path.normcase(process.exe()) != os.path.normcase(str(executable))
                    or "-m" not in argv or argv[argv.index("-m") + 1] != "jiuwenswarm.server.worker.main"):
                raise AssertionError("native SDK entry did not come from the expected frozen Worker executable")
            provenance["actual_worker_executable"] = process.exe()
    if not sent <= prepared <= entry <= completed:
        raise AssertionError("execution timestamps are not ordered")
    return {"request_id": request_id, "session_id": session_id, "sent_at_ns": sent,
            "entered_at_ns": entry, "completed_at_ns": completed, "prepared_at_ns": prepared,
            "entry_ms": (entry - sent) / 1e6, "prepared_to_sdk_ms": (entry - prepared) / 1e6,
            "worker_instance": runtime.get("worker_instance"), "worker_pid": sdk_pid,
            "execution_id": dispatches[0].get("execution_id"), "entry_endpoint": endpoint,
            "process_provenance": provenance}


async def front_measure(proc, front_port: int, started: int) -> dict:
    while True:
        try:
            with socket.create_connection(("127.0.0.1", front_port), timeout=.02):
                listening = time.perf_counter_ns()
            break
        except OSError:
            if proc.poll() is not None or time.perf_counter_ns() - started > 30_000_000_000:
                raise RuntimeError("Front did not listen")
            await asyncio.sleep(.001)
    controls = {}
    async with connect(f"ws://127.0.0.1:{front_port}", max_size=32 * 1024 * 1024) as ws:
        await ws.recv()
        for method, params in (("session.list", {"limit": 20}), ("project.list", {}),
                               ("config.get", {}), ("history.get", {"page_idx": 1})):
            sent = time.perf_counter_ns()
            await send(ws, method, method, "acceptance-0", params)
            await receive(ws, method)
            received = time.perf_counter_ns()
            controls[method] = {"sent_at_ns": sent, "received_at_ns": received,
                "response_ms": (received - sent) / 1e6,
                "available_from_spawn_ms": (received - started) / 1e6}
    return {"listening_at_ns": listening, "listen_ms": (listening - started) / 1e6,
            "controls": controls}


async def trial(args, label: str, case: str, iteration: int, root: Path,
                snapshot: Path, model_port: int, output: Path, rows: list[dict]) -> None:
    code = getattr(args, label)
    name = f"{case}-{label}-{iteration:03d}"
    sample_root = root / name
    data = sample_root / "data"
    if case == "fresh":
        seed(data, args.baseline, args.mode)
        configure_fixture(data, model_port)
    else:
        shutil.copytree(snapshot, data)
    if case == "chat" and iteration == 0:
        for sample in range(args.warm_new):
            new_session(data, f"warm-new-{sample:03d}", args.mode)
    observer = sample_root / "observer"
    observer.mkdir(parents=True)
    executable = getattr(args, label + "_executable")
    endpoint = "native-sdk" if executable else "sdk"
    if case != "front" or args.trace:
        observed_sdk = (SDK_OBSERVER.replace("original.exec_module(module)", "_load_sdk(original,module)")
                        if args.trace else SDK_OBSERVER)
        (observer / "sitecustomize.py").write_text(
            observed_sdk + (STAGE_OBSERVER if args.trace else ""), encoding="utf-8")
        env = isolated_environment(code, sample_root, observer=observer, importtime=args.importtime)
    else:
        env = isolated_environment(code, sample_root, importtime=args.importtime)
    entries = output / f"{name}.entries.jsonl"
    stages = output / f"{name}.stages.jsonl"
    env["ISSUE4689_ENTRY_LOG"] = str(entries)
    env["ISSUE4689_STAGE_LOG"] = str(stages)
    if args.trace:
        env["ISSUE4689_REQUESTED_MODE"] = args.mode
    if executable:
        # Both official EXEs must contain the identical observation-only hook at
        # the local adapter's actual SDK sender boundary. No snapshot fallback.
        env["JIUWENSWARM_SDK_ENTRY_LOG"] = str(entries)
    elif args.native_sdk_calibration:
        env["JIUWENSWARM_SDK_ENTRY_LOG"] = str(output / f"{name}.native-entries.jsonl")
    front_port = port()
    command = [str(executable), "-m"] if executable else [args.python, "-m"]
    command += ["jiuwenswarm.server.app_agentserver", "--port", str(front_port)]
    identity = {"label": label, "case": case, "iteration": iteration, "mode": args.mode,
                "diagnostic": bool(args.trace or args.importtime or args.native_sdk_calibration),
                "entry_endpoint": endpoint}
    ws = None
    proc = None
    failed = False
    try:
        with (output / f"{name}.log").open("wb") as log:
            started = time.perf_counter_ns()
            if args.trace:
                env["ISSUE4689_TRIAL_START_NS"] = str(started)
            proc = subprocess.Popen(command, cwd=code, env=env, stdout=log, stderr=log)
            identity.update(started_at_ns=started, front_launcher_pid=proc.pid)
            if case == "front":
                row = {**identity, "ok": True, **await front_measure(proc, front_port, started)}
                rows.append(row)
                append_json(args.output, row)
            else:
                ws = await open_front(front_port)
                requests = [("cold", 0, "acceptance-0")]
                if case == "chat" and iteration == 0:
                    requests += [("warm_same", n, "acceptance-0") for n in range(args.warm_same)]
                    requests += [("warm_new", n, f"warm-new-{n:03d}") for n in range(args.warm_new)]
                worker_identity = None
                for condition, sample, session_id in requests:
                    identity.update(condition=condition, sample=sample)
                    measured = await request_entry(ws, request_id=f"{name}-{condition}-{sample}",
                        session_id=session_id, mode=args.mode, entry_log=entries, endpoint=endpoint,
                        code=code, executable=executable)
                    current_worker = (measured["worker_pid"], measured["worker_instance"])
                    if worker_identity is not None and current_worker != worker_identity:
                        raise AssertionError("resident Worker identity changed during warm requests")
                    worker_identity = current_worker
                    row = {**identity, "ok": True, **measured}
                    rows.append(row)
                    append_json(args.output, row)
            print(json.dumps({**identity, "ok": True}), flush=True)
    except Exception as exc:  # Preserve every failed attempt rather than improving percentiles by dropping it.
        failed = True
        row = {**identity, "ok": False, "failed_at_ns": time.perf_counter_ns(),
               "error_type": type(exc).__name__, "error": str(exc)}
        rows.append(row)
        append_json(args.output, row)
        print(json.dumps(row), flush=True)
    finally:
        if ws is not None:
            await ws.close()
        if proc is not None:
            try:
                terminate_owned(proc, [])
            except Exception as exc:
                failed = True
                row = {**identity, "ok": False, "failure_phase": "cleanup",
                       "error_type": type(exc).__name__, "error": str(exc)}
                rows.append(row)
                append_json(args.output, row)
    if failed and args.fail_fast:
        raise RuntimeError(f"trial {name} failed; raw evidence retained")


async def run(args) -> int:
    output = args.output.parent
    output.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(f"refusing to append to existing samples: {args.output}")
    for code in (args.baseline, args.candidate):
        if output.is_relative_to(code):
            raise ValueError("acceptance output must be outside both source checkouts")
    labels = ["baseline"] if args.baseline_only else ["candidate"] if args.candidate_only else ["baseline", "candidate"]
    metadata = {"run_id": uuid.uuid4().hex, "mode": args.mode, "platform": platform.platform(),
        "python": args.python, "driver_python": sys.executable, "python_version": sys.version,
        "baseline": code_manifest(args.baseline), "candidate": code_manifest(args.candidate),
        "labels": labels, "cases": args.cases, "cold_samples": args.cold,
        "front_samples": args.front_samples, "warm_same_samples": args.warm_same,
        "warm_new_samples": args.warm_new, "fresh_install_samples": args.fresh_samples,
        "diagnostic": bool(args.trace or args.importtime or args.native_sdk_calibration),
        "fixture": "local-openai-protocol; shipped capability switches retained",
        "percentile": "nearest-rank", "cold": "new Front and Worker process, no SDK preloading",
        "warm": "sequential requests on the same resident Worker; same-session and new-session separate",
        "source_endpoint": "actual DeepAgent.send_input observer",
        "frozen_endpoint": "observation-only native hook immediately before actual SDK sender; no prepared-hook fallback",
        "native_sdk_calibration": args.native_sdk_calibration,
        "form": "Windows-frozen" if args.baseline_executable or args.candidate_executable else "Windows-source",
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}}
    node = discover_node_binary(args.node)
    metadata["optional_node_executable"] = str(node) if node else None
    metadata["executables"] = {
        label: {"path": str(executable), "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "size": executable.stat().st_size}
        for label in labels if (executable := getattr(args, label + "_executable"))}
    if args.baseline_instrumentation_patch:
        metadata["baseline_measurement_overlay"] = {
            "patch_path": str(args.baseline_instrumentation_patch),
            "sha256": hashlib.sha256(args.baseline_instrumentation_patch.read_bytes()).hexdigest(),
            "source_baseline_unmodified": True,
            "purpose": "observation-only SDK sender hook for frozen baseline, no Core changes"}
    write_json(args.output.with_suffix(".environment.json"), metadata)
    ModelFixture.scenario = "restart"
    ModelFixture.release.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rows = []
    try:
        # OS file caches are not cleared. Baseline/candidate alternate order.
        with tempfile.TemporaryDirectory(prefix="jiuwenswarm-4689-paired-") as temporary:
            root = Path(temporary)
            snapshot = prepare_snapshot(args, root, server.server_port, output)
            for case in args.cases:
                count = args.front_samples if case == "front" else args.fresh_samples if case == "fresh" else args.cold
                for iteration in range(count):
                    ordered = labels if iteration % 2 == 0 else list(reversed(labels))
                    for label in ordered:
                        await trial(args, label, case, iteration, root, snapshot,
                                    server.server_port, output, rows)
    finally:
        server.shutdown()
        server.server_close()
        result = summarize(rows, metadata)
        write_json(args.output.with_suffix(".summary.json"), result)
    # Timing failures are a result, not a reason to stop gathering later samples.
    return 0 if (not result["failed_attempts"] and not result["incomplete_populations"]
                 and (result["all_passed"] or args.diagnostic_exit_success)) else 1


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--candidate", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--output", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--node", help="optional Web-tooling executable; otherwise discover Node from PATH")
    parser.add_argument("--mode", choices=("agent", "code.normal"), default="agent")
    parser.add_argument("--cases", type=lambda value: value.split(","), default=["front", "chat", "fresh"])
    parser.add_argument("--front-samples", type=int, default=30)
    parser.add_argument("--cold", type=int, default=30)
    parser.add_argument("--warm-same", type=int, default=100)
    parser.add_argument("--warm-new", type=int, default=100)
    parser.add_argument("--fresh-samples", type=int, default=30)
    exclusive = parser.add_mutually_exclusive_group()
    exclusive.add_argument("--baseline-only", action="store_true")
    exclusive.add_argument("--candidate-only", action="store_true")
    parser.add_argument("--trace", action="store_true", help="diagnostic-only transparent repository stage observers")
    parser.add_argument("--importtime", action="store_true", help="diagnostic only; never use these timings as acceptance")
    parser.add_argument("--diagnostic-exit-success", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--baseline-executable", type=lambda p: Path(p).resolve())
    parser.add_argument("--candidate-executable", type=lambda p: Path(p).resolve())
    parser.add_argument("--baseline-instrumentation-patch", type=lambda p: Path(p).resolve())
    parser.add_argument("--native-sdk-calibration", action="store_true",
                        help="diagnostic only: capture the native hook separately from the source SDK observer")
    args = parser.parse_args()
    if set(args.cases) - {"front", "chat", "fresh"}:
        parser.error("--cases must contain front, chat, and/or fresh")
    if args.front_samples < 1 or args.cold < 1 or args.fresh_samples < 1 or min(args.warm_same, args.warm_new) < 0:
        parser.error("cold/front/fresh counts must be positive; warm counts nonnegative")
    if (not args.baseline_only and not args.candidate_only
            and bool(args.baseline_executable) != bool(args.candidate_executable)):
        parser.error("paired runs require the same source/frozen form in both versions")
    if (not args.candidate_only and args.baseline_executable
            and {"chat", "fresh"} & set(args.cases) and not args.baseline_instrumentation_patch):
        parser.error("frozen baseline chat requires the explicit observation-only instrumentation patch")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(arguments())))
