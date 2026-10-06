"""Run real three-agent A2A comparisons directly in an existing Linux host.

Office is an explicitly adapted offline task. Development uses the frozen
SWE-bench Flask issue, base source and original test patch, without Docker.
Agent tools get task directories and a minimal environment, not model secrets.
This is directory separation, not a security sandbox.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from openjiuwen.core.runner import Runner

from jiuwenswarm.benchmarks.duplex_metrics import Events, summarize_events
from jiuwenswarm.benchmarks.duplex_runtime import load_models
from jiuwenswarm.benchmarks.duplex_workload_team import WorkloadTeam


PRICES = """item,unit_price
laptop,950
monitor,240
dock,110
headset,65
"""
REQUESTS = """department,item,quantity,revision,status
Marketing,laptop,9,1,active
Marketing,monitor,6,1,active
Marketing,headset,12,1,active
Product,laptop,7,1,active
Product,monitor,8,1,active
Product,dock,8,1,active
HR,laptop,5,1,active
HR,monitor,5,1,active
HR,dock,5,1,active
HR,headset,20,1,active
Marketing,laptop,8,2,active
Product,monitor,10,2,active
HR,headset,10,2,active
HR,dock,5,2,cancelled
"""
OFFICE_TASK = """Prepare equipment budget replies for Jessica Lee (Marketing),
Huang Jie (Product), and Chen Xinyi (HR). Each department has a $10,000 budget.
Read the supplied prices.csv, requests.csv and purchasing_policy.md.
For each department use the latest revision of EACH item's request; cancelled
items cost zero. Add the shipping charge from the policy once per department.
Do not silently reduce quantities to fit the budget.
Create final/budget.json with this exact structure:
{"departments": [{"department": "Marketing", "manager": "Jessica Lee",
"equipment_cost": NUMBER, "shipping": NUMBER, "total_cost": NUMBER,
"budget": NUMBER, "remaining": NUMBER, "sufficient": BOOLEAN,
"items": [{"item": STRING, "quantity": NUMBER}]}]}.
Include all three departments; omit cancelled items from items.
Also create final/replies.md, containing one concise draft reply per manager
with the total cost, remaining budget (or shortfall), and explicit sufficient
or insufficient conclusion. Drafts only; there is no external messaging site.
Coordinator owns the final outputs. Researcher audits request revisions and
prices; reviewer independently checks calculations and the final documents.
Share actionable findings through SendMessage while others work. Do not send
acknowledgement-only messages or ask for repeated final confirmation.
"""


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def source_hash(tree):
    digest = hashlib.sha256()
    for path in sorted((tree / "src").rglob("*.py")):
        digest.update(str(path.relative_to(tree)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def copy_tree(source, destination):
    shutil.copytree(source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.egg-info", ".git"))


class DirectEnvironment:
    def __init__(self, suite, root, assets):
        self.suite, self.root, self.assets = suite, root, assets
        self.lock = asyncio.Lock()
        self.work = root / "work"
        self.work.mkdir(parents=True)
        if suite == "office":
            self.roles = {
                "coordinator": "Coordinate calculations and write final/budget.json and final/replies.md in the shared work directory.",
                "researcher": "Audit the source materials and request revisions. Send concrete findings promptly to coordinator. Use researcher/ for scratch files.",
                "reviewer": "Independently compute totals, check constraints and review final outputs. Send mistakes promptly to coordinator. Use reviewer/ for scratch files.",
            }
            self.directories = {name: self.work for name in self.roles}
            (self.work / "prices.csv").write_text(PRICES)
            (self.work / "requests.csv").write_text(REQUESTS)
            (self.work / "purchasing_policy.md").write_text(
                "# Purchasing policy\nAll prices include tax. Shipping is $180 once per department.\n"
                "Budget sufficiency means total including shipping <= $10,000.\n"
                "Latest revision per department/item supersedes earlier revisions.\n"
                "A cancelled latest revision removes that item entirely.\n")
            self.python = Path(sys.executable)
        else:
            self.roles = {
                "implementer": "Implement the issue fix in your current worktree. Only your source tree is submitted. Request review and incorporate failures.",
                "diagnoser": "Locate the cause and reproduce the issue in your own current worktree. Send evidence and proposed fixes promptly to implementer.",
                "reviewer": "Derive tests independently, then use ReviewSnapshot to copy the current implementation into your own worktree. Run tests and send results with the returned SHA256 to implementer.",
            }
            ready = json.loads((assets / "ready.json").read_text())
            self.baseline, self.python = Path(ready["source"]), Path(ready["python"])
            self.directories = {name: self.work / name for name in self.roles}
            for directory in self.directories.values():
                copy_tree(self.baseline, directory)
        self.instructions = (
            "\nWork directly in the supplied current working directory. Bash starts there on every call. "
            "Use relative paths and `python` or `python -m pytest`. Do not access files outside your task "
            "directory, model configs, grading files, other trials, benchmark answers, or the network. "
            "Keep messages concise and substantive. When all issues are resolved, finish without "
            "acknowledgement-only messages. Your final response should be plain text, not a JSON task plan.\n"
        )

    async def execute(self, member, command, timeout_seconds):
        async def run():
            directory = self.directories[member]
            env = {"PATH": str(self.python.parent) + ":/usr/local/bin:/usr/bin:/bin",
                   "LANG": "C.UTF-8", "PYTHONPATH": str(directory / "src"),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                   "PYTEST_ADDOPTS": "-W ignore::DeprecationWarning"}
            proc = await asyncio.create_subprocess_exec("bash", "-c", command,
                cwd=directory, env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), timeout_seconds)
            except (TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.communicate()
                raise
            return f"exit_code={proc.returncode}\n" + output.decode(errors="replace")
        if member == "implementer":
            async with self.lock:
                return await run()
        return await run()

    async def review_snapshot(self):
        async with self.lock:
            source = self.directories["implementer"]
            destination = self.directories["reviewer"]
            # Only source is submitted; retain reviewer's own tests and reproductions.
            if (destination / "src").exists():
                shutil.rmtree(destination / "src")
            copy_tree(source / "src", destination / "src")
            return {"patch_sha256": source_hash(source), "directory": str(destination)}


def office_grade(env):
    prices = {row["item"]: int(row["unit_price"]) for row in csv.DictReader(io.StringIO(PRICES))}
    latest = {}
    for row in csv.DictReader(io.StringIO(REQUESTS)):
        key = row["department"], row["item"]
        if key not in latest or int(row["revision"]) > int(latest[key]["revision"]):
            latest[key] = row
    checks = []
    try:
        answer = json.loads((env.work / "final/budget.json").read_text())
        entries = answer["departments"]
        rows = {row["department"]: row for row in entries}
    except (OSError, ValueError, KeyError, TypeError):
        entries, rows = [], {}
    checks.append({"name": "exactly_three_departments", "passed": len(entries) == 3 and set(rows) == {"Marketing", "Product", "HR"}})
    for department, manager in (("Marketing", "Jessica Lee"), ("Product", "Huang Jie"), ("HR", "Chen Xinyi")):
        items = {item: int(row["quantity"]) for (dept, item), row in latest.items()
                 if dept == department and row["status"] != "cancelled"}
        cost = sum(prices[item] * quantity for item, quantity in items.items())
        expected = {"manager": manager, "equipment_cost": cost, "shipping": 180,
                    "total_cost": cost + 180, "budget": 10000, "remaining": 10000 - cost - 180,
                    "sufficient": cost + 180 <= 10000}
        row = rows.get(department, {})
        for key, value in expected.items():
            checks.append({"name": f"{department}.{key}", "passed": row.get(key) == value})
        try:
            actual_items = {item["item"]: item["quantity"] for item in row.get("items", [])}
            valid_items = actual_items == items and len(row.get("items", [])) == len(items)
        except (TypeError, KeyError):
            valid_items = False
        checks.append({"name": f"{department}.items", "passed": valid_items})
    reply_path = env.work / "final/replies.md"
    replies = reply_path.read_text() if reply_path.exists() else ""
    for name in ("Jessica Lee", "Huang Jie", "Chen Xinyi"):
        checks.append({"name": "reply_present." + name, "passed": name in replies})
    passed = sum(item["passed"] for item in checks)
    return {"source": "TheAgentCompany task adapted to offline synthetic materials; not official score",
            "score": passed / len(checks), "passed": passed, "total": len(checks), "checks": checks,
            "reply_content_requires_manual_review": True}


def development_grade(env):
    submitted = env.directories["implementer"]
    patch_lines = []
    paths = {p.relative_to(env.baseline) for p in (env.baseline / "src").rglob("*.py")}
    paths.update(p.relative_to(submitted) for p in (submitted / "src").rglob("*.py"))
    for path in sorted(paths):
        original, changed = env.baseline / path, submitted / path
        old = original.read_text().splitlines(keepends=True) if original.exists() else []
        new = changed.read_text().splitlines(keepends=True) if changed.exists() else []
        patch_lines.extend(difflib.unified_diff(old, new, fromfile="a/" + str(path), tofile="b/" + str(path)))
    (env.root / "submission.patch").write_text("".join(patch_lines))
    grading = env.root / "grading"
    copy_tree(env.baseline, grading)
    shutil.rmtree(grading / "src")
    copy_tree(env.directories["implementer"] / "src", grading / "src")
    private = json.loads((env.assets / "flask-grader.json").read_text())
    patch = subprocess.run(["git", "apply", "-"], input=private["test_patch"], text=True,
                           cwd=grading, capture_output=True)
    if patch.returncode:
        return {"score": None, "error": "Original test patch could not apply", "details": patch.stderr}
    junit = env.root / "grading.xml"
    run_env = {**os.environ, "PYTHONPATH": str(grading / "src"), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
               "PYTEST_ADDOPTS": "-W ignore::DeprecationWarning"}
    started = time.monotonic()
    result = subprocess.run([str(env.python), "-m", "pytest", "-q", "tests",
                             "--junitxml=" + str(junit)], cwd=grading, env=run_env,
                            text=True, capture_output=True, timeout=180)
    (env.root / "grading.log").write_text(result.stdout + result.stderr)
    tree = ET.parse(junit)
    cases = tree.findall(".//testcase")
    failures = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
    skipped = sum(case.find("skipped") is not None for case in cases)
    outcomes = {}
    for case in cases:
        classname = case.attrib.get("classname", "")
        # Original test IDs are module.py::test_name, possibly parametrized.
        node = classname.replace(".", "/") + ".py::" + case.attrib["name"]
        outcomes[node] = case.find("failure") is None and case.find("error") is None and case.find("skipped") is None
    f2p = {key: outcomes.get(key, False) for key in private["FAIL_TO_PASS"]}
    p2p = {key: outcomes.get(key, False) for key in private["PASS_TO_PASS"]}
    resolved = bool(f2p) and all(f2p.values()) and all(p2p.values())
    return {"source": "SWE-bench Verified pallets__flask-5014 original test patch, native Python environment",
            "score": int(resolved), "resolved": resolved, "fail_to_pass": f2p, "pass_to_pass": p2p,
            "passed": len(cases) - failures - skipped, "failed": failures, "skipped": skipped,
            "total": len(cases), "pytest_exit_code": result.returncode,
            "grading_seconds": time.monotonic() - started,
            "patch_sha256": source_hash(env.directories["implementer"])}


async def trial(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=False)
    setup = time.monotonic()
    environment = DirectEnvironment(args.suite, root, Path(args.assets))
    if args.suite == "office":
        task, task_id = OFFICE_TASK, "admin-check-employees-budget-and-reply-offline-v1"
    else:
        data = [json.loads(line) for line in Path(args.inputs).read_text().splitlines()]
        row = next(row for row in data if row["instance_id"] == "pallets__flask-5014")
        task_id = row["instance_id"]
        task = row["problem_statement"] + (
            "\nFix the issue in the current repository. Reproduce it, add regression tests, and run relevant tests. "
            "Implementer edits the submitted source; diagnoser investigates independently; reviewer tests a "
            "ReviewSnapshot and reports its SHA256. Keep useful messages timely. When resolved, finish; "
            "do not exchange acknowledgements or repeated final confirmations.")
    models = load_models(Path(args.models))
    metadata = {"suite": args.suite, "policy": args.policy, "repeat": args.repeat,
                "task_id": task_id, "task": task, "roles": environment.roles,
                "instructions": environment.instructions, "timeout": args.timeout,
                "max_model_calls_per_agent": 30, "max_bash_calls": 90,
                "slow_model": models["slow"].model_request_config.model_name,
                "fast_model": models["fast"].model_request_config.model_name,
                "setup_seconds": time.monotonic() - setup}
    write_json(root / "metadata.json", metadata)
    events = Events(root / "events.jsonl")
    team = WorkloadTeam(suite=args.suite, task=task, models=models, policy=args.policy,
                        environment=environment, output=root, events=events,
                        timeout=args.timeout, max_model_calls=30, max_tool_calls=90)
    await Runner.start()
    try:
        execution = await team.run()
    finally:
        await Runner.stop()
    write_json(root / "execution.json", execution)
    try:
        quality = office_grade(environment) if args.suite == "office" else development_grade(environment)
    except Exception as error:
        quality = {"score": None, "error": repr(error)}
    effective = [row for row in events.records if row["event"] == "delivery_effective"]
    result = {**metadata, **execution, "quality": quality,
              "metrics": summarize_events([events.path]),
              "actual_interrupts": sum(row["action"] == "INTERRUPT" for row in effective),
              "messages": sum(row["event"] == "message_sent" for row in events.records)}
    write_json(root / "result.json", result)
    print(json.dumps({key: result[key] for key in ("suite", "policy", "status", "agent_seconds", "actual_interrupts", "messages", "quality")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["office", "development"], required=True)
    parser.add_argument("--policy", choices=["steer", "model"], required=True)
    parser.add_argument("--repeat", type=int, default=0)
    parser.add_argument("--output", required=True)
    parser.add_argument("--assets", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    asyncio.run(trial(parser.parse_args()))


if __name__ == "__main__":
    main()
