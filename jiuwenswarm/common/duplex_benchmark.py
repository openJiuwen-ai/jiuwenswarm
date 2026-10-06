# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline labeled routing replay, with failures included in the denominator.

This measures route classification only, not full-task quality or speedups.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from dataclasses import asdict
from pathlib import Path

from jiuwenswarm.common.duplex_router import (
    ControlSnapshot, InboundMessage, SYSTEM_PROMPT, decision_schema, observe, prompt_for,
)


def load_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    ids = set()
    for case in cases:
        if case["case_id"] in ids:
            raise ValueError("duplicate case_id")
        ids.add(case["case_id"])
        if case["expected_action"] not in ("APPEND", "INTERRUPT"):
            raise ValueError("invalid expected_action")
        ControlSnapshot(**case["snapshot"])
        messages = [InboundMessage(**m) for m in case["messages"]]
        if not messages or len({m.message_id for m in messages}) != len(messages):
            raise ValueError("messages must be nonempty and have unique IDs")
    if not cases:
        raise ValueError("empty benchmark")
    return cases


async def replay(cases, classify, *, repeats=1, timeout_seconds=2.0):
    if repeats < 1:
        raise ValueError("repeats must be positive")
    rows = []
    for repeat in range(repeats):
        for case in cases:
            snapshot = ControlSnapshot(**case["snapshot"])
            messages = tuple(InboundMessage(**m) for m in case["messages"])
            result = await observe(snapshot, messages, classify=classify,
                                   current_snapshot=lambda: snapshot,
                                   timeout_seconds=timeout_seconds)
            rows.append({"case_id": case["case_id"], "repeat": repeat,
                         "expected_action": case["expected_action"], **asdict(result),
                         "correct": result.status == "ok" and
                         result.proposed_action == case["expected_action"]})
    latencies = sorted(r["latency_ms"] for r in rows)
    return {
        "scope": "routing_replay_only",
        "runs": len(rows), "correct": sum(r["correct"] for r in rows),
        "accuracy_including_failures": sum(r["correct"] for r in rows) / len(rows),
        "failures": sum(r["status"] != "ok" for r in rows),
        "false_interrupts": sum(r["status"] == "ok" and r["expected_action"] == "APPEND"
                                and r["proposed_action"] == "INTERRUPT" for r in rows),
        "latency_p50_ms": statistics.median(latencies),
        "latency_p95_ms": latencies[max(0, (95 * len(rows) + 99) // 100 - 1)],
        "observations": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--model-config", type=Path, help="JSON TeamModelConfig; keep credentials out of Git")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=2.0)
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.validate_only:
        print(json.dumps({"validated_cases": len(cases), "model_calls": 0}))
        return
    if args.model_config is None or args.output is None:
        parser.error("--model-config and --output are required for model replay")

    from openjiuwen.agent_teams import create_tiny_agent
    from openjiuwen.agent_teams.schema.deep_agent_spec import TeamModelConfig

    config = TeamModelConfig.model_validate(json.loads(args.model_config.read_text(encoding="utf-8")))
    model_name = config.model_request_config.model_name

    async def classify(snapshot, messages):
        async with create_tiny_agent(
            system_prompt=SYSTEM_PROMPT, model_name=model_name,
            model_resolver=lambda name: config if name == model_name else None,
            default_schema=decision_schema(), name="duplex-replay",
            language="en", max_iterations=1,
        ) as agent:
            return await agent.run(prompt_for(snapshot, messages))

    report = asyncio.run(replay(cases, classify, repeats=args.repeats,
                               timeout_seconds=args.timeout_seconds))
    report["model_name"] = model_name
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "observations"}))


if __name__ == "__main__":
    main()
