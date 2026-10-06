# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read official benchmark inputs without copying datasets or inventing labels.

This prepares reproducible inputs, not benchmark scores. Website/container
execution and the official evaluators are still required for task results.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REVISIONS = {
    "agentradio": "5e4e137991ce5662d95cf890e534b5b223d24d6a",
    "interruptbench": "17da111e4858b93c0cab1d88f85e1735fbd1d423",
}
INTERRUPT_SUITES = ("1update", "2update", "2modification", "1retraction", "2retraction", "3mixed")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_checkout(root: Path, benchmark: str) -> str:
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISIONS[benchmark]:
        raise ValueError(f"{benchmark}: expected {REVISIONS[benchmark]}, got {revision}")
    # Modified tracked files invalidate the claimed source version. Untracked
    # credentials and run outputs are allowed, and are never read here.
    changed = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], text=True)
    if changed.strip():
        raise ValueError(f"{benchmark}: tracked source files have local changes")
    return revision


def agentradio_manifest(root: Path) -> dict[str, Any]:
    revision = verify_checkout(root, "agentradio")
    tasks = []
    for folder in sorted((root / "data/qa").glob("task-*")):
        config = tomllib.loads((folder / "task.toml").read_text(encoding="utf-8"))
        files = [folder / "instruction.md", folder / "task.toml", *sorted((folder / "tests").glob("*"))]
        rubrics = read_json(folder / "tests/rubrics.json")
        tasks.append({"task_id": folder.name, "metadata": config["metadata"],
                      "environment": config["environment"], "rubric_count": len(rubrics),
                      "files": {str(p.relative_to(root)).replace("\\", "/"): fingerprint(p)
                                for p in files if p.is_file()}})
    if not tasks:
        raise ValueError("AgentRadio checkout has no tasks")
    return {"benchmark": "agentradio", "revision": revision, "tasks": tasks,
            "protocol_files": {str(p.relative_to(root)).replace("\\", "/"): fingerprint(p)
                               for p in (root / "multi_agent/startup.sh",
                                         root / "multi_agent/startup_passive.sh")},
            "verifier": {"path": "verify_local.py", "sha256": fingerprint(root / "verify_local.py")}}


def interruptbench_manifest(root: Path) -> dict[str, Any]:
    revision = verify_checkout(root, "interruptbench")
    suites = {}
    for suite in INTERRUPT_SUITES:
        source = root / f"Eval/interrupt_config/raw/{suite}.json"
        records = read_json(source)
        ids = [str(item["task_id"]) for item in records]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate task id in {suite}")
        suites[suite] = {"path": str(source.relative_to(root)).replace("\\", "/"),
                         "sha256": fingerprint(source), "task_ids": ids,
                         "update_count": sum(len(item["updates"]) for item in records)}
    return {"benchmark": "interruptbench", "revision": revision, "suites": suites,
            "runner": {"path": "Eval/run.py", "sha256": fingerprint(root / "Eval/run.py")},
            "verifier": "Eval/evaluation_harness/evaluators.py:evaluator_router",
            "trigger": "official spec + saved baseline trajectory; no replacement injection point"}


def resolve_official_trigger(root: Path, spec: dict, action_count: int) -> int:
    """Run the pinned upstream resolver itself, without importing WebArena.

    run.py imports live browser/model clients at module scope. Extracting this
    pure function preserves upstream percentage/clamping semantics while keeping
    input preparation independent of website credentials and browser packages.
    """
    tree = ast.parse((root / "Eval/run.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_resolve_interrupt_at_action")
    namespace = {"math": math}
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(root / "Eval/run.py"), "exec"), namespace)
    point, _ = namespace[function.name](spec, num_replay_actions=action_count)
    return point


@dataclass(frozen=True)
class OfficialInterrupt:
    task_id: str
    initial_intent: str
    update: str
    update_mode: str
    action_boundary: int
    provenance: dict[str, str]

    async def deliver(self, lifecycle_handler, *, completed_actions: int, run_id: str,
                      episode_ended: bool = False):
        """Deliver the official update through the production U2A entry.

        The environment runner must call this at its original replay boundary;
        it still owns resets, actions, trajectory recording and final scoring.
        The caller supplies a unique run/stage id and reuses it on retries.
        """
        if completed_actions != self.action_boundary or episode_ended:
            raise ValueError("official interruption boundary was not reached exactly")
        if not run_id:
            raise ValueError("run_id is required")
        from openjiuwen.agent_teams.agent.coordination.event_bus import InnerEventMessage, InnerEventType

        identity = json.dumps([run_id, self.task_id, self.provenance], sort_keys=True)
        message_id = "interruptbench-" + hashlib.sha256(identity.encode()).hexdigest()
        # Never include true_intent, expected answers or the evaluator in the
        # model input. update_mode is metadata, not a fast-model action label.
        return await lifecycle_handler.on_user_input(InnerEventMessage(
            event_type=InnerEventType.USER_INPUT,
            payload={"content": self.update, "message_id": message_id}))


def load_official_interrupt(root: Path, *, suite: str, task_id: str,
                            config_file: Path, spec_file: Path, trajectory_file: Path) -> OfficialInterrupt:
    verify_checkout(root, "interruptbench")
    if suite not in INTERRUPT_SUITES:
        raise ValueError("unknown InterruptBench suite")
    source = root / f"Eval/interrupt_config/raw/{suite}.json"
    records = [item for item in read_json(source) if str(item["task_id"]) == task_id]
    if len(records) != 1:
        raise ValueError("task_id must identify exactly one official task")
    record = records[0]
    config = read_json(config_file)
    if str(config.get("task_id")) != task_id:
        raise ValueError("config task_id mismatch")
    spec = read_json(spec_file)["tasks"][task_id]
    update = spec.get("update_intent")
    if update not in record["updates"]:
        raise ValueError("spec update is not an unchanged official update")
    mode = spec.get("update_mode", "append")
    if mode not in ("append", "replace"):
        raise ValueError("unsupported update mode")
    initial = config["intent"]
    if initial != record["transformed_initial_intent"]:
        # Chained rounds need their upstream-generated stage config and complete
        # history checked as well. Do not silently substitute the base intent.
        raise ValueError("only initial-stage configs are supported by this bridge")
    trajectory = read_json(trajectory_file)
    if str(trajectory.get("task_id")) != task_id or not isinstance(trajectory.get("actions"), list):
        raise ValueError("baseline trajectory task_id/actions mismatch")
    point = resolve_official_trigger(root, spec, len(trajectory["actions"]))
    return OfficialInterrupt(task_id, initial, update, mode, point, {
        "revision": REVISIONS["interruptbench"], "suite": suite,
        "source_sha256": fingerprint(source), "config_sha256": fingerprint(config_file),
        "spec_sha256": fingerprint(spec_file), "trajectory_sha256": fingerprint(trajectory_file),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentradio", type=Path, required=True)
    parser.add_argument("--interruptbench", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"status": "inputs_prepared_not_evaluated", "benchmarks": [
        agentradio_manifest(args.agentradio), interruptbench_manifest(args.interruptbench)]}
    # Refuse to overwrite an earlier experiment manifest.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": report["status"], "manifest": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
