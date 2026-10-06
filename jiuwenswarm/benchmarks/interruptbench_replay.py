"""Real-model replay of original InterruptBench updates, without website scores.

The original initial request and every update are unchanged. Injection happens
on the first real streamed text of a request that has seen all earlier updates.
This is a concurrent user-input experiment, not the official action boundary.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import copy
import hashlib
import json
import statistics
import time
import types
from pathlib import Path

from openjiuwen.core.runner import Runner

from jiuwenswarm.benchmarks.duplex_runtime import Events, UserInputPeer, load_models


SELECTION = [("1retraction", 0), ("1update", 1), ("2modification", 1),
             ("2retraction", 1), ("2update", 0), ("3mixed", 0)]
SYSTEM_PROMPT = """Translate the user's current request into a concrete task specification and work plan.
Follow subsequent user corrections, preserving unaffected requirements and removing withdrawn ones.
There is no browser or external action tool in this run. Do not invent website results or claim execution.
Respond with one JSON object, no markdown, with keys "task_spec" and "work_plan".
task_spec must contain these keys (use null when a field does not apply or was not specified):
domain ("products" or "airports"), top_n (integer), month (integer 1-12), year (integer),
product_category (string), minimum_price (number), minimum_reviews (integer),
airport_type ("international", "domestic", or "all"), origin (string),
max_distance_km (number), distance_type ("driving" or "straight_line"),
required_flight_destination (string), operating_hours (string),
exclude_private (boolean or null), full_address (boolean or null).
work_plan is an array of four concrete steps that would fulfill the current request.
Put the specification first. Do not use a JSON task-plan control protocol or ask for confirmation.
Return the final revised specification and plan directly.
"""


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_cases(repo):
    cases = []
    for suite, task_id in SELECTION:
        path = repo / "Eval/interrupt_config/raw" / (suite + ".json")
        rows = json.loads(path.read_text(encoding="utf-8"))
        row = next(item for item in rows if item["task_id"] == task_id)
        cases.append({"case_id": f"{suite}-{task_id}", "suite": suite, "task_id": task_id,
                      "initial": row["transformed_initial_intent"], "updates": row["updates"],
                      "target_intent_grading_only": row["intent"],
                      "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return cases


def grade(task_id, answer):
    text = answer.get("output", "") if isinstance(answer, dict) else ""
    try:
        start = text.index("{")
        document, _ = json.JSONDecoder().raw_decode(text[start:])
        spec = document["task_spec"]
        if not isinstance(spec, dict):
            raise ValueError("task_spec must be an object")
    except (ValueError, KeyError, TypeError):
        document, spec = {}, {}
    expected = ({"domain": "products", "top_n": 3, "month": 1, "year": 2023,
                 "product_category": None, "minimum_price": None, "minimum_reviews": None}
                if task_id == 0 else
                {"domain": "airports", "airport_type": "international", "origin": "Carnegie Mellon University",
                 "max_distance_km": 50, "distance_type": "driving", "required_flight_destination": None,
                 "operating_hours": None, "exclude_private": None, "full_address": True})
    checks = {key: key in spec and spec[key] == value for key, value in expected.items()}
    if task_id == 1 and isinstance(spec.get("origin"), str):
        checks["origin"] = spec["origin"].strip().lower() in ("carnegie mellon university", "cmu")
    checks["work_plan_present"] = isinstance(document.get("work_plan"), list) and len(document["work_plan"]) == 4 and all(isinstance(x, str) and x.strip() for x in document["work_plan"])
    return {"checks": checks, "passed": sum(checks.values()), "total": len(checks),
            "score": sum(checks.values()) / len(checks), "all_constraints_pass": all(checks.values()),
            "task_spec": spec, "scope": "custom final-intent constraint checks, NOT official task success"}


class RecordedUserPeer(UserInputPeer):
    async def deliver_input(self, content, *, use_steer=True):
        active = self.harness.active_round
        phase = active.iter_phase.value if active is not None else self.harness.state.value
        result = await super().deliver_input(content, use_steer=use_steer)
        self.events.add("delivery_effective", member=self.name,
                        message_id=getattr(getattr(content, "message", None), "message_id", None),
                        phase=phase, action="INTERRUPT" if result == "INTERRUPT" else
                        "IDLE_START" if phase == "idle" else "APPEND")
        return result


async def one(models, case, policy, repeat, output):
    label = f"{case['case_id']}-r{repeat}-{policy}"
    directory = output / label
    directory.mkdir()
    events = Events(directory / "events.jsonl")
    accepted_updates = []
    peer = RecordedUserPeer(name=label, models=copy.deepcopy(models), policy=policy,
        system_prompt=SYSTEM_PROMPT, tools=[], events=events, max_iterations=12, max_model_calls=12,
        goal=lambda: case["initial"] + "".join("\nAccepted user update: " + x for x in accepted_updates))
    updates = case["updates"]
    gates = [asyncio.Event() for _ in updates]
    requests, arrivals = [], []
    live = set()
    original_stream = peer.model.stream
    started = time.monotonic()

    async def watched(self, *args, **kwargs):
        messages = kwargs.get("messages", args[0] if args else [])
        prompt = "\n".join(str(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")) for m in messages)
        index = len(requests)
        row = {"index": index, "started_seconds": time.monotonic() - started,
               "updates_in_prompt": [update in prompt for update in updates], "first_text_seconds": None,
               "output_text": "", "status": "complete", "finish_reason": None}
        requests.append(row)
        live.add(index)
        try:
            async for chunk in original_stream(*args, **kwargs):
                content = getattr(chunk, "content", None)
                if isinstance(content, str) and content:
                    row["output_text"] += content
                    if row["first_text_seconds"] is None:
                        row["first_text_seconds"] = time.monotonic() - started
                        for i, gate in enumerate(gates):
                            if all(row["updates_in_prompt"][:i]) and not row["updates_in_prompt"][i]:
                                gate.set()
                reason = getattr(chunk, "finish_reason", None)
                if reason:
                    row["finish_reason"] = reason
                yield chunk
        except asyncio.CancelledError:
            row["status"] = "cancelled"
            raise
        except Exception:
            row["status"] = "error"
            raise
        finally:
            row["finished_seconds"] = time.monotonic() - started
            live.discard(index)

    peer.model.stream = types.MethodType(watched, peer.model)
    answer, status, error = None, "complete", None
    try:
        await peer.start()
        started = time.monotonic()
        events.add("run_start", policy=policy, case_id=case["case_id"])
        async with asyncio.timeout(240):
            await peer.send(case["initial"])
            for i, update in enumerate(updates):
                gate = asyncio.create_task(gates[i].wait())
                finished = asyncio.create_task(peer.wait(timeout=100))
                try:
                    done, pending = await asyncio.wait({gate, finished}, timeout=100,
                                                        return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        raise TimeoutError("No streamed text or completion")
                    for task in done:
                        task.result()
                    trigger = "first_real_stream_text" if gate in done else "idle_completion"
                finally:
                    for task in (gate, finished):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(gate, finished, return_exceptions=True)
                active = peer.harness.active_round
                arrival = {"index": i, "seconds": time.monotonic() - started, "trigger": trigger,
                           "in_flight": bool(live), "phase": active.iter_phase.value if active else "idle",
                           "message_id": f"{label}-u{i}", "content": update}
                arrivals.append(arrival)
                events.add("injection", **arrival)
                await peer.receive_user(update, message_id=arrival["message_id"])
                accepted_updates.append(update)
                arrival["delivery_returned_seconds"] = time.monotonic() - started
            answer = await peer.wait(timeout=140)
    except Exception as exc:
        status, error = "error", type(exc).__name__
        answer = peer._last_result
    ended = time.monotonic()
    events.add("run_end", status=status, error_type=error)
    try:
        await asyncio.wait_for(peer.close(), timeout=20)
    except Exception as exc:
        events.add("close_error", error_type=type(exc).__name__)
    save(directory / "answer.json", answer)
    save(directory / "requests.json", requests)
    effective = [row for row in events.records if row["event"] == "delivery_effective"]
    model_ends = [row for row in events.records if row["event"] == "model_end"]
    routes = [row for row in events.records if row["event"] == "route_decision"]
    for arrival in arrivals:
        candidates = [row for row in requests if row["started_seconds"] >= arrival["seconds"] and row["updates_in_prompt"][arrival["index"]]]
        arrival["adoption_seconds"] = min((row["started_seconds"] - arrival["seconds"] for row in candidates), default=None)
    result = {"case_id": case["case_id"], "suite": case["suite"], "task_id": case["task_id"],
              "policy": policy, "repeat": repeat, "status": status, "error_type": error,
              "elapsed_seconds": ended - started,
              "first_update_to_finish_seconds": ended - started - arrivals[0]["seconds"] if arrivals else None,
              "last_update_to_finish_seconds": ended - started - arrivals[-1]["seconds"] if arrivals else None,
              "updates_expected": len(updates), "updates_sent": len(arrivals), "arrivals": arrivals,
              "actual_interrupts": sum(row["action"] == "INTERRUPT" for row in effective),
              "idle_deliveries": sum(row["action"] == "IDLE_START" for row in effective),
              "slow_calls": len(model_ends), "cancelled_slow_calls": sum(row["status"] == "cancelled" for row in model_ends),
              "fast_attempts": sum(row["attempts"] for row in routes), "routes": routes,
              "observed_slow_tokens": sum((row.get("usage") or {}).get("total_tokens", 0) or 0 for row in model_ends),
              "slow_calls_without_usage": sum(row.get("usage") is None for row in model_ends),
              "total_billed_tokens": None, "quality": grade(case["task_id"], answer)}
    save(directory / "result.json", result)
    print(json.dumps({key: result[key] for key in ("case_id", "policy", "repeat", "status", "elapsed_seconds", "actual_interrupts", "quality")}), flush=True)
    return result


def report(output, rows):
    groups = {}
    for policy in ("steer", "model"):
        group = [row for row in rows if row["policy"] == policy]
        if not group:
            continue
        completed = [row for row in group if row["status"] == "complete"]
        groups[policy] = {"runs": len(group), "completed": len(completed),
            "all_constraints_pass": sum(row["quality"]["all_constraints_pass"] for row in group),
            "constraint_checks_passed": sum(row["quality"]["passed"] for row in group),
            "constraint_checks_total": sum(row["quality"]["total"] for row in group),
            "mean_elapsed_seconds": statistics.mean(row["elapsed_seconds"] for row in completed) if completed else None,
            "mean_first_update_to_finish_seconds": statistics.mean(row["first_update_to_finish_seconds"] for row in completed) if completed else None,
            "mean_last_update_to_finish_seconds": statistics.mean(row["last_update_to_finish_seconds"] for row in completed) if completed else None,
            "actual_interrupts": sum(row["actual_interrupts"] for row in group),
            "updates_sent": sum(row["updates_sent"] for row in group),
            "in_flight_updates": sum(arrival["in_flight"] for row in group for arrival in row["arrivals"]),
            "cancelled_slow_calls": sum(row["cancelled_slow_calls"] for row in group),
            "slow_calls": sum(row["slow_calls"] for row in group),
            "fast_attempts": sum(row["fast_attempts"] for row in group),
            "route_actions": dict(collections.Counter(route["action"] for row in group for route in row["routes"])),
            "route_statuses": dict(collections.Counter(route["status"] for row in group for route in row["routes"]))}
    pairs = []
    for baseline in rows:
        if baseline["policy"] != "steer":
            continue
        candidate = next((row for row in rows if row["policy"] == "model" and
                          (row["case_id"], row["repeat"]) == (baseline["case_id"], baseline["repeat"])), None)
        if candidate:
            pairs.append({"case_id": baseline["case_id"], "repeat": baseline["repeat"],
                          "both_completed": baseline["status"] == candidate["status"] == "complete",
                          "seconds_saved": baseline["elapsed_seconds"] - candidate["elapsed_seconds"],
                          "quality_delta": candidate["quality"]["score"] - baseline["quality"]["score"]})
    summary = {"scope": "original request/update replay; custom intent constraints; NOT official website execution or score",
               "groups": groups, "pairs": pairs, "rows": rows}
    save(output / "summary.json", summary)
    return summary


async def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    cases = read_cases(Path(args.repo))
    models = load_models(Path(args.models))
    save(output / "manifest.json", {"cases": cases, "system_prompt": SYSTEM_PROMPT, "repeats": args.repeats,
        "concurrent_pairs": args.concurrency, "injection": "first_real_stream_text_after_previous_update_seen",
        "source_revision": "17da111e4858b93c0cab1d88f85e1735fbd1d423", "scope": "input replay, not official website score",
        "models": {key: value.model_request_config.model_dump() for key, value in models.items()},
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "case_selection": "Six update variants across original task IDs 0 and 1; not six independent tasks."})
    rows = []
    semaphore = asyncio.Semaphore(args.concurrency)
    await Runner.start()
    async def pair(case, repeat, index):
        async with semaphore:
            for policy in (("steer", "model") if (index + repeat) % 2 == 0 else ("model", "steer")):
                row = await one(models, case, policy, repeat, output)
                rows.append(row)
                report(output, rows)
                save(output / "progress.json", {"finished": len(rows), "expected": len(cases) * args.repeats * 2,
                                               "last": {key: row[key] for key in ("case_id", "repeat", "policy", "status")}})
    try:
        await asyncio.gather(*(pair(case, repeat, index) for repeat in range(args.repeats)
                               for index, case in enumerate(cases)))
    finally:
        await Runner.stop()
    save(output / "done.json", {"finished": len(rows), "timestamp": time.time()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=2)
    asyncio.run(run(parser.parse_args()))
