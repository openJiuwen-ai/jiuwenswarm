"""Dependency-free event recording and official-score summaries."""
import json
import math
import random
import time
from pathlib import Path


class Events:
    def __init__(self, path: Path):
        self.path = path
        self.started = time.monotonic()
        self.records = []

    def add(self, event, **fields):
        record = {"event": event, "timestamp": time.time(),
                  "elapsed_seconds": time.monotonic() - self.started, **fields}
        self.records.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def summarize_events(paths):
    records = [json.loads(line) for path in paths for line in path.read_text(encoding="utf-8").splitlines() if line]
    slow = [item for item in records if item["event"] == "model_end"]
    fast = [item for item in records if item["event"] == "route_decision"]
    known_tokens = sum((item.get("usage") or {}).get("total_tokens", 0) or 0 for item in slow)
    arrivals = {item["message_id"]: item["timestamp"] for item in records if item["event"] == "message_arrived"}
    accepted = [item["timestamp"] - arrivals[item["message_id"]] for item in records
                if item["event"] == "message_accepted" and item["message_id"] in arrivals]
    starts = [item["timestamp"] for item in records if item["event"] == "run_start"]
    ends = [item["timestamp"] for item in records if item["event"] == "run_end"]
    return {"slow_calls": len(slow), "fast_attempts": sum(item["attempts"] for item in fast),
            "cancelled_slow_calls": sum(item["status"] == "cancelled" for item in slow),
            "observed_slow_tokens": known_tokens,
            "slow_calls_without_usage": sum(item.get("usage") is None for item in slow),
            "total_tokens": None, "gpu_seconds": None, "duplicate_side_effects": None,
            "message_acceptance_seconds": accepted,
            "makespan_seconds": max(ends) - min(starts) if starts and ends else None}


def official_scores(result_dir: Path, task_ids, *, benchmark):
    rows = []
    for task_id in task_ids:
        if benchmark == "interruptbench":
            path = result_dir / "actions" / f"{task_id}.json"
            score = json.loads(path.read_text()).get("score") if path.is_file() else None
        else:
            paths = list(result_dir.glob(f"{task_id}__*/verifier/reward.txt"))
            if len(paths) > 1:
                raise ValueError("multiple trials for one task: report repeats separately")
            score = float(paths[0].read_text().strip()) if paths else None
        if score is not None and (not isinstance(score, (int, float)) or not math.isfinite(score)):
            raise ValueError("invalid official score")
        rows.append({"task_id": str(task_id), "score": score, "success": score is not None and score >= 1,
                     "status": "scored" if score is not None else "missing_official_score"})
    return {"tasks": rows, "expected": len(rows), "scored": sum(row["score"] is not None for row in rows),
            "success_rate": sum(row["success"] for row in rows) / len(rows) if rows else None}


def paired_success(reports, baseline):
    """Pair final-stage outcomes; bootstrap whole tasks across repeated runs."""
    final = {}
    for report in reports:
        key = report["policy"], report["repeat"]
        if key not in final or report.get("stage", 0) > final[key].get("stage", 0):
            final[key] = report
    comparisons = {}
    for policy in sorted({key[0] for key in final} - {baseline}):
        by_task = {}
        missing = 0
        for (candidate, repeat), report in final.items():
            reference = final.get((baseline, repeat))
            if candidate != policy or reference is None or report.get("stage") != reference.get("stage"):
                continue
            scores = {row["task_id"]: row for row in reference["official"]["tasks"]}
            for row in report["official"]["tasks"]:
                other = scores.get(row["task_id"])
                if other is None:
                    continue
                missing += row["score"] is None or other["score"] is None
                by_task.setdefault(row["task_id"], []).append(int(row["success"]) - int(other["success"]))
        if not by_task:
            comparisons[policy] = {"paired_tasks": 0, "success_delta": None}
            continue
        deltas = [sum(values) / len(values) for values in by_task.values()]
        rng = random.Random(42)
        boot = sorted(sum(rng.choices(deltas, k=len(deltas))) / len(deltas) for _ in range(2000))
        comparisons[policy] = {"paired_tasks": len(deltas), "success_delta": sum(deltas) / len(deltas),
                               "task_bootstrap_ci95": [boot[49], boot[1949]],
                               "pairs_with_missing_scores": missing}
    return {"baseline": baseline, "comparisons": comparisons}
