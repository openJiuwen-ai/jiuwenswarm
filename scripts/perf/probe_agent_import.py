"""Measure cold Agent adapter imports in fresh, isolated Python processes.

Preload app_agentserver/agent_ws_server, then time the real threaded import path
and the selected adapter constructor. Use --adapter to compare Flash and Deep.
No listener, session/agent prewarm or model call is started. This measures module
loading and construction, NOT submission-to-model latency or OS-cold disk access. The empty test
configuration also excludes user plugins. Run A/B sequentially with one Python
environment; --trace-imports is diagnostic and excluded from A/B distributions.
"""

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


TARGET = "jiuwenswarm.server.runtime.agent_adapter.interface_deep"
HEAVY_MODULES = ("fastmcp", "google.genai")


def measure(checkout, output, trace_imports, adapter_name):
    output.mkdir(parents=True, exist_ok=False)
    config = output / "runtime" / "config"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(
        "startup:\n  warmup:\n    enabled: false\nflash:\n  enabled: "
        + ("true" if adapter_name == "flash" else "false") + "\n",
        encoding="utf-8",
    )
    (config / ".env").write_text("", encoding="utf-8")
    os.environ.update(
        JIUWENSWARM_DATA_DIR=str(output / "runtime"),
        JIUWENCLAW_DATA_DIR=str(output / "runtime"),
        JIUWENSWARM_CONFIG_DIR=str(config),
        JIUWENSWARM_HOME=str(output / "runtime" / "home"),
        JIUWENSWARM_LOG_CONSOLE_ENABLED="false",
        JIUWENSWARM_LOG_FILE_ENABLED="false",
        JIUWENSWARM_SESSION_CREATE_PREWARM="0",
        JIUWENSWARM_AGENT_PREWARM="0",
        JIUWENSWARM_EDITION="personal",
        OTEL_SDK_DISABLED="true",
    )
    sys.path.insert(0, str(checkout))
    os.chdir(output)
    sys.argv = [__file__]

    start = time.perf_counter()
    importlib.import_module("jiuwenswarm.server.app_agentserver")
    server = importlib.import_module("jiuwenswarm.server.agent_ws_server")
    preload_ms = (time.perf_counter() - start) * 1000
    assert TARGET not in sys.modules, "Target was preloaded; measurement invalid"
    assert not any(name in sys.modules for name in HEAVY_MODULES), "Image SDKs preloaded"
    before = set(sys.modules)
    result = {
        "checkout": str(checkout), "python": sys.version,
        "pid": os.getpid(), "preload_ms": preload_ms,
        "trace_imports": trace_imports, "adapter": adapter_name,
    }

    records, stack = [], []
    bootstrap = importlib.import_module("importlib._bootstrap")
    original_load = bootstrap._find_and_load_unlocked

    def traced(name, import_):
        frame = {"name": name, "parent": stack[-1]["name"] if stack else None,
                 "children_ms": 0.0}
        stack.append(frame)
        started = time.perf_counter()
        try:
            return original_load(name, import_)
        finally:
            frame["total_ms"] = (time.perf_counter() - started) * 1000
            frame["self_ms"] = frame["total_ms"] - frame.pop("children_ms")
            stack.pop()
            if stack:
                stack[-1]["children_ms"] += frame["total_ms"]
            records.append(frame)

    original_import = server._import_interface_deep_blocking
    submitted = 0.0

    def measured_import():
        started = time.perf_counter()
        result["dispatch_ms"] = (started - submitted) * 1000
        cpu_start = time.thread_time()
        try:
            return original_import()
        finally:
            result["import_ms"] = (time.perf_counter() - started) * 1000
            result["import_cpu_ms"] = (time.thread_time() - cpu_start) * 1000

    async def cold_import():
        nonlocal submitted
        submitted = time.perf_counter()
        await server._warm_interface_deep_module()
        result["import_wall_ms"] = (time.perf_counter() - submitted) * 1000

    server._import_interface_deep_blocking = measured_import
    if trace_imports:
        bootstrap._find_and_load_unlocked = traced
    try:
        asyncio.run(cold_import())
    finally:
        server._import_interface_deep_blocking = original_import
        bootstrap._find_and_load_unlocked = original_load

    result["new_module_count"] = len(set(sys.modules) - before)
    source = Path(sys.modules[TARGET].__file__).resolve()
    assert source.is_relative_to(checkout), f"Wrong checkout imported: {source}"
    started = time.perf_counter()
    if adapter_name == "flash":
        module = importlib.import_module("jiuwenswarm.server.runtime.agent_adapter.interface_flash")
        adapter_class = module.JiuwenSwarmFlashAdapter
    else:
        adapter_class = sys.modules[TARGET].JiuWenSwarmDeepAdapter
    result["adapter_module_ms"] = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    adapter = adapter_class()
    result["constructor_ms"] = (time.perf_counter() - started) * 1000
    assert isinstance(adapter, adapter_class)
    result["adapter_ready_ms"] = sum(result[key] for key in (
        "import_wall_ms", "adapter_module_ms", "constructor_ms"
    ))
    # Do not import the eager implementation merely to inspect metadata.
    source_paths = ["jiuwenswarm/agents/harness/common/tools/image_tools.py",
                    "jiuwenswarm/agents/harness/common/tools/image_generation_tool.py",
                    "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py",
                    "jiuwenswarm/server/runtime/agent_adapter/interface_flash.py"]
    result["source_sha256"] = {
        path: hashlib.sha256((checkout / path).read_bytes()).hexdigest()
        for path in source_paths if (checkout / path).exists()
    }
    # BaseCard.id is a fresh UUID per interpreter, not part of the tool interface.
    face = sys.modules[TARGET].generate_image.card.model_dump(mode="json", exclude={"id"})
    result["image_card_sha256"] = hashlib.sha256(
        json.dumps(face, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    result["heavy_modules_after_constructor"] = [name for name in HEAVY_MODULES if name in sys.modules]
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if trace_imports:
        (output / "import_tree.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


def distribution(values):
    values = sorted(values)
    return {"n": len(values), "p50": statistics.median(values),
            "p95": values[math.ceil(len(values) * .95) - 1],
            "min": values[0], "max": values[-1]}


def compare(baseline, checkout, output, samples, adapter_name):
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    for pair in range(1, samples + 1):
        order = [("baseline", baseline), ("optimized", checkout)]
        if pair % 2 == 0:
            order.reverse()
        for label, root in order:
            run_dir = output / f"{label}-{pair:02d}"
            process = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--checkout", str(root),
                 "--output", str(run_dir), "--adapter", adapter_name],
                env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            (output / f"{label}-{pair:02d}.log").write_text(
                process.stdout + process.stderr, encoding="utf-8"
            )
            if process.returncode:
                raise RuntimeError(f"{label}-{pair:02d} failed; inspect its log")
            row = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            row.update(label=label, pair=pair)
            rows.append(row)
            with (output / "samples.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
        pair_rows = {row["label"]: row for row in rows[-2:]}
        print(f"pair {pair}/{samples}: " + ", ".join(
            f"{label}={pair_rows[label]['adapter_ready_ms']:.1f} ms"
            for label in ("baseline", "optimized")), flush=True)
    assert len({row["image_card_sha256"] for row in rows}) == 1, "Tool card changed"
    expected = [] if adapter_name == "flash" else list(HEAVY_MODULES)
    assert all(row["heavy_modules_after_constructor"] == expected
               for row in rows if row["label"] == "optimized")
    summary = {
        label: {key: distribution([row[key] for row in rows if row["label"] == label])
                for key in ("import_wall_ms", "import_ms", "preload_ms", "adapter_module_ms",
                            "constructor_ms", "adapter_ready_ms", "new_module_count")}
        for label in ("baseline", "optimized")
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    parser.add_argument("--trace-imports", action="store_true")
    parser.add_argument("--baseline", type=Path, help="Compare this checkout against --checkout")
    parser.add_argument("--samples", type=int, default=30, help="Fresh processes per checkout")
    parser.add_argument("--adapter", choices=("flash", "deep"), default="flash")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    if args.baseline and args.trace_imports:
        parser.error("Trace samples must be measured separately from the comparison")
    if args.baseline:
        compare(args.baseline.resolve(), args.checkout.resolve(), args.output.resolve(), args.samples, args.adapter)
    else:
        measure(args.checkout.resolve(), args.output.resolve(), args.trace_imports, args.adapter)


if __name__ == "__main__":
    main()
