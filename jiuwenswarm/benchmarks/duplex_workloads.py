"""Paired office/code pilot: python -m jiuwenswarm.benchmarks.duplex_workloads --help."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from jiuwenswarm.benchmarks.duplex_workload_environments import (
    command, DevelopmentEnvironment, OfficeEnvironment, SWE_REVISION,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_inputs(index_path, suites):
    index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    selection = json.loads(Path(index["manifest"]).read_text(encoding="utf-8"))
    tasks = {}
    for suite in suites:
        file = index["agent_inputs"][suite + ".jsonl"]
        raw = Path(file["path"]).read_bytes()
        if digest(raw) != file["sha256"]:
            raise ValueError(f"Frozen {suite} input hash mismatch")
        rows = [json.loads(line) for line in raw.decode().splitlines() if line]
        key = "task_id" if suite == "office" else "instance_id"
        allowed = {"task_id", "instruction"} if suite == "office" else set(selection[suite]["agent_input_fields"])
        if any(set(row) - allowed for row in rows):
            raise ValueError(f"{suite} agent inputs include non-public fields")
        by_id = {row[key]: row for row in rows}
        if len(by_id) != len(rows):
            raise ValueError("Duplicate task IDs")
        ids = selection[suite]["pilot_task_ids"] if suite == "office" else [r["instance_id"] for r in selection[suite]["pilot_tasks"]]
        tasks[suite] = [by_id[identity] for identity in ids]
        if suite == "development":
            if digest(Path(index["swe_source"]).read_bytes()) != selection[suite]["source_sha256"]:
                raise ValueError("Frozen SWE source hash mismatch")
            for selected in selection[suite]["pilot_tasks"]:
                if by_id[selected["instance_id"]]["base_commit"] != selected["base_commit"]:
                    raise ValueError("SWE base commit mismatch")
    return index, selection, tasks


def schedule(tasks, repeats):
    """Alternate which policy goes first to reduce fixed ordering bias."""
    result = []
    for repeat in range(repeats):
        for suite, rows in tasks.items():
            for i, row in enumerate(rows):
                policies = ("steer", "model") if (i + repeat) % 2 == 0 else ("model", "steer")
                for policy in policies:
                    result.append({"suite": suite, "task_id": row.get("task_id", row.get("instance_id")),
                                   "repeat": repeat, "policy": policy, "task": row})
    return result


def preflight(args, suites):
    checks = []
    def check(name, fn):
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": detail})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})
    check("docker", lambda: command(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20).stdout.decode().strip())
    if "office" in suites:
        def office():
            if not args.office_host:
                raise ValueError("Set --office-host to a dedicated TheAgentCompany service host (init resets its data)")
            import urllib.request
            for service in ("rocketchat", "owncloud", "gitlab", "plane"):
                url = f"http://{args.office_host}:2999/api/healthcheck/{service}"
                with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=10) as response:
                    if response.status != 200:
                        raise ValueError(f"{service}: HTTP {response.status}")
            return "All four original services are healthy"
        check("office_services", office)
        check("office_workstation", lambda: command(["docker", "image", "inspect", args.workstation_image,
            "--format", "{{.Id}}"], timeout=20).stdout.decode().strip())
    if "development" in suites:
        def swe():
            if not args.swe_root or not args.swe_python:
                raise ValueError("Set --swe-root and --swe-python for the pinned official grader")
            rev = command(["git", "-C", args.swe_root, "rev-parse", "HEAD"], timeout=10).stdout.decode().strip()
            if rev != SWE_REVISION:
                raise ValueError(f"Expected SWE harness revision {SWE_REVISION}; got {rev}")
            command([args.swe_python, "-m", "swebench.harness.run_evaluation", "--help"], timeout=45,
                    cwd=args.swe_root)
            return rev
        check("swe_grader", swe)
    return {"ready": all(c["ok"] for c in checks), "checks": checks}


def summarize_trials(trials):
    """Missing graders are missing observations, never silently counted as zero."""
    result = {}
    for suite in sorted({r["suite"] for r in trials}):
        rows = [r for r in trials if r["suite"] == suite]
        groups = {}
        for policy in ("steer", "model"):
            group = [r for r in rows if r["policy"] == policy]
            scored = [r for r in group if r.get("quality", {}).get("score") is not None]
            times = [r["execution"]["agent_seconds"] for r in scored if r.get("execution")]
            groups[policy] = {"attempted": len(group), "scored": len(scored), "unscored": len(group) - len(scored),
                "mean_score": statistics.mean(r["quality"]["score"] for r in scored) if scored else None,
                "success_rate": statistics.mean(int(r["quality"]["success"]) for r in scored) if scored else None,
                "mean_agent_seconds_scored": statistics.mean(times) if times else None,
                "effective_interrupts": sum(r.get("metrics", {}).get("effective_interrupts", 0) for r in group)}
        paired = {}
        for row in rows:
            key = row["task_id"], row["repeat"]
            if row["policy"] in paired.setdefault(key, {}):
                raise ValueError("Duplicate trial identity; do not silently overwrite repeats")
            paired[key][row["policy"]] = row
        deltas, ratios, both_success, missing, mismatched = [], [], [], 0, 0
        for pair in paired.values():
            if set(pair) != {"steer", "model"} or any(p.get("quality", {}).get("score") is None for p in pair.values()):
                missing += 1
                continue
            a, b = pair["steer"], pair["model"]
            if a.get("comparison_key") != b.get("comparison_key") or a.get("images") != b.get("images"):
                mismatched += 1
                continue
            deltas.append(b["quality"]["score"] - a["quality"]["score"])
            ratio = b["execution"]["agent_seconds"] / a["execution"]["agent_seconds"]
            ratios.append(ratio)
            if a["quality"]["success"] and b["quality"]["success"]:
                both_success.append(ratio)
        result[suite] = {"groups": groups, "paired_scored": len(deltas), "missing_pairs": missing,
            "incomparable_pairs": mismatched,
            "mean_paired_score_delta": statistics.mean(deltas) if deltas else None,
            "median_time_ratio_model_over_steer": statistics.median(ratios) if ratios else None,
            "both_success_pairs": len(both_success),
            "median_time_ratio_both_success": statistics.median(both_success) if both_success else None}
    return result


def report(output):
    trials = [json.loads(p.read_text(encoding="utf-8")) for p in output.glob("trials/*/trial.json")]
    summary = summarize_trials(trials)
    write_json(output / "summary.json", summary)
    planned = len(json.loads((output / "plan.json").read_text())) if (output / "plan.json").exists() else 0
    scored = sum(r.get("quality", {}).get("score") is not None for r in trials)
    lines = ["# 办公 / 开发 A2A 对照", "", "普通组 = 原 SDK steer；全双工组 = 快模型路由 + 安全暂停后重新规划。",
             "", f"计划 {planned} 次；已完成正式评分 {scored} 次。",
             "", "耗时包含三名 Agent 执行和收尾，环境初始化及评分耗时另存。缺失评分不记作零分。", "",
             "| 数据集 | 组 | 已评分 / 已尝试 | 平均质量 | 平均耗时（秒） | 实际打断 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for suite, value in summary.items():
        for policy, group in value["groups"].items():
            fmt = lambda v: "未测得" if v is None else f"{v:.3f}"
            lines.append(f"| {suite} | {policy} | {group['scored']} / {group['attempted']} | "
                         f"{fmt(group['mean_score'])} | {fmt(group['mean_agent_seconds_scored'])} | {group['effective_interrupts']} |")
    if not trials:
        lines += ["", "尚无正式任务成绩。查看 preflight.json 中的环境检查结果。"]
    lines += ["", "这是预先选定的试跑题目；单次结果不足以证明稳定收益。总 token 在路由器/取消请求用量缺失时保留为空。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


async def run(args, index, selection, plan):
    from filelock import FileLock
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.benchmarks.duplex_runtime import load_models
    from jiuwenswarm.benchmarks.duplex_metrics import Events, summarize_events
    from jiuwenswarm.benchmarks.duplex_workload_team import WorkloadTeam, COMMON, ROLES
    models = load_models(Path(args.models))
    official = {}
    if any(r["suite"] == "development" for r in plan):
        if args.data_python_path:
            sys.path.insert(0, args.data_python_path)
        import pyarrow.parquet as pq
        official = {r["instance_id"]: r for r in pq.read_table(index["swe_source"]).to_pylist()}
    source_root = Path(__file__).resolve().parents[1]
    sources = [*Path(__file__).parent.glob("duplex_*.py"),
               source_root / "agents/harness/team/duplex_native.py",
               source_root / "agents/harness/team/duplex_shadow.py", source_root / "common/duplex_router.py"]
    base_settings = {"models": {name: value.model_request_config.model_dump() for name, value in models.items()},
        "model_endpoints": {name: value.model_client_config.api_base for name, value in models.items()},
        "timeout": args.timeout, "max_model_calls": args.max_model_calls, "max_tool_calls": args.max_tool_calls,
        "prompts_sha256": digest((COMMON + json.dumps(ROLES, sort_keys=True)).encode()), "swe_harness": SWE_REVISION,
        "sources_sha256": {str(p.relative_to(source_root)): digest(p.read_bytes()) for p in sorted(sources)}}
    previous = args.output / "settings.json"
    if previous.exists() and json.loads(previous.read_text(encoding="utf-8")) != base_settings:
        raise ValueError("Run settings or source changed; use a new output directory")
    write_json(args.output / "settings.json", base_settings)
    for item in plan:
        suite, identity, policy = item["suite"], item["task_id"], item["policy"]
        folder = args.output / "trials" / f"{suite}-{identity}-{item['repeat']}-{policy}"
        if (folder / "trial.json").exists():
            continue
        if folder.exists():
            raise RuntimeError(f"Incomplete trial directory exists: {folder}; use a new output directory")
        folder.mkdir(parents=True)
        record = {k: v for k, v in item.items() if k != "task"}
        record["comparison_key"] = digest(json.dumps({**base_settings, "task": item["task"]}, sort_keys=True).encode())
        env = (DevelopmentEnvironment(item["task"], folder, swe_python=args.swe_python, swe_root=args.swe_root)
            if suite == "development" else OfficeEnvironment(identity, folder, server_host=args.office_host,
                env_model=models["slow"], workstation_image=args.workstation_image, instruction=item["task"]["instruction"]))
        events = Events(folder / "events.jsonl")
        begin = time.monotonic()
        lock = FileLock(str(Path(tempfile.gettempdir()) / ("jiuwen-office-" +
                        digest((args.office_host or "").encode())[:20] + ".lock")), thread_local=False)
        # A single office service deployment must never be reset by two trials.
        if suite == "office":
            await asyncio.to_thread(lock.acquire, timeout=1)
        try:
            await asyncio.to_thread(env.prepare)
            record["setup_seconds"] = time.monotonic() - begin
            record["images"] = env.images
            text = item["task"].get("instruction", item["task"].get("problem_statement"))
            if suite == "development":
                text = f"Repository: {item['task']['repo']}\nIssue:\n{text}"
            team = WorkloadTeam(suite=suite, task=text, models=models, policy=policy, environment=env,
                output=folder, events=events, timeout=args.timeout, max_model_calls=args.max_model_calls,
                max_tool_calls=args.max_tool_calls)
            await Runner.start()
            try:
                record["execution"] = await team.run()
            finally:
                await Runner.stop()
            began_grade = time.monotonic()
            record["quality"] = await asyncio.to_thread(env.grade, official.get(identity), events.path)
            record["grading_seconds"] = time.monotonic() - began_grade
            metrics = summarize_events([events.path])
            metrics["arrivals_by_phase"] = dict(Counter(r.get("phase", "unknown") for r in events.records if r["event"] == "message_arrived"))
            metrics["effective_interrupts"] = sum(r["event"] == "delivery_effective" and r["action"] == "INTERRUPT" for r in events.records)
            record["metrics"] = metrics
        except Exception as exc:
            record["quality"] = {"score": None, "success": None, "status": "infrastructure_error", "error": str(exc)}
        finally:
            await asyncio.to_thread(env.close)
            if suite == "office":
                lock.release()
        record["total_seconds"] = time.monotonic() - begin
        write_json(folder / "trial.json", record)
        report(args.output)
        print(json.dumps({"task": identity, "policy": policy, "quality": record["quality"].get("score"),
                          "status": record["quality"]["status"]}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "check", "run", "report"])
    parser.add_argument("--inputs", help="Frozen local-inputs.json from dataset preparation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models")
    parser.add_argument("--suite", choices=["both", "office", "development"], default="both")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, help="First N frozen pilot tasks per suite, for environment smoke runs")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-model-calls", type=int, default=40, help="Per agent, counts cancelled stream calls; never reset by interruption")
    parser.add_argument("--max-tool-calls", type=int, default=160, help="Team Bash call budget")
    parser.add_argument("--office-host", help="Dedicated benchmark service host; upstream init resets all service data")
    parser.add_argument("--workstation-image", default="jiuwen-office-workstation:20260915")
    parser.add_argument("--swe-root", type=Path)
    parser.add_argument("--swe-python", type=Path)
    parser.add_argument("--data-python-path", help="Optional isolated pyarrow installation directory")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.action == "report":
        print(json.dumps(report(args.output), ensure_ascii=False))
        return
    if not args.inputs:
        parser.error("--inputs is required")
    if min(args.repeats, args.timeout, args.max_model_calls, args.max_tool_calls,
           args.limit if args.limit is not None else 1) <= 0:
        parser.error("Budgets, repeats and limit must be positive")
    suites = ["office", "development"] if args.suite == "both" else [args.suite]
    index, selection, tasks = load_inputs(args.inputs, suites)
    if args.limit:
        tasks = {k: v[:args.limit] for k, v in tasks.items()}
    plan = schedule(tasks, args.repeats)
    public_plan = [{k: v for k, v in r.items() if k != "task"} for r in plan]
    previous = args.output / "plan.json"
    if previous.exists() and json.loads(previous.read_text(encoding="utf-8")) != public_plan:
        raise ValueError("Trial selection changed; use a new output directory")
    write_json(previous, public_plan)
    if args.action == "plan":
        print(json.dumps({"trials": len(plan), "tasks": {s: len(rows) for s, rows in tasks.items()}}))
        return
    checks = preflight(args, suites)
    write_json(args.output / "preflight.json", checks)
    if args.action == "check" or not checks["ready"]:
        print(json.dumps(checks, ensure_ascii=False))
        report(args.output)
        if not checks["ready"]:
            raise SystemExit(2)
        return
    if not args.models:
        parser.error("--models is required for run")
    asyncio.run(run(args, index, selection, plan))


if __name__ == "__main__":
    main()
