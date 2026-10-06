"""Summarize observed direct trials, keeping timeouts separate from completion."""
import argparse
import collections
import json
from pathlib import Path


def report(root):
    trials = []
    for path in sorted(root.glob("*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (path.parent / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        result["arrival_phases"] = dict(collections.Counter(row.get("phase") for row in events if row["event"] == "message_arrived"))
        decisions = [row for row in events if row["event"] == "route_decision"]
        result["router_statuses"] = dict(collections.Counter(row["status"] for row in decisions))
        result["router_proposals"] = dict(collections.Counter(row["action"] for row in decisions))
        result["router_latency_ms"] = [row["latency_ms"] for row in decisions]
        result["output_directory"] = path.parent.name
        trials.append(result)
    pairs = []
    for suite in ("office", "development"):
        for repeat in sorted({row["repeat"] for row in trials if row["suite"] == suite}):
            by_policy = {row["policy"]: row for row in trials if row["suite"] == suite and row["repeat"] == repeat}
            if set(by_policy) != {"model", "steer"}:
                continue
            baseline, duplex = by_policy["steer"], by_policy["model"]
            if baseline["task_id"] != duplex["task_id"]:
                raise ValueError("Cannot pair different tasks")
            a, b = baseline["metrics"]["makespan_seconds"], duplex["metrics"]["makespan_seconds"]
            completed = baseline["status"] == duplex["status"] == "completed"
            scores = [row["quality"].get("score") for row in (baseline, duplex)]
            pairs.append({"suite": suite, "repeat": repeat, "both_completed": completed,
                          "observed_elapsed_delta_seconds": b - a,
                          "completion_time_change_percent": (b / a - 1) * 100 if completed and a else None,
                          "score_delta": scores[1] - scores[0] if None not in scores else None})
    summary = {"trial_count": len(trials), "paired_comparisons": pairs, "trials": trials,
               "limits": ["One task per suite and one run per policy; no statistical conclusion.",
                          "Office uses offline synthetic materials adapted from TheAgentCompany, not its official score.",
                          "SWE-bench source and test patch are original; native environment differs from official container.",
                          "Timeouts are capped observations, not successful completion times.",
                          "Full fast-model and cancelled-call token usage is unavailable."]}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 直接测试题：普通与全双工 A2A", "", "三名 Agent：DeepSeek-V3.2；全双工路由：Qwen3.5-9B。执行于 wzr@47.79.124.13:31442。", "",
             "耗时从三名 Agent 注册完成、任务发出前开始计，到团队停止结束；不含模型库导入、环境准备和评分。", "",
             "| 题目 | 策略 | 状态 | 耗时（秒） | 质量 | 实际打断 | 消息 | 慢 / 快请求 |",
             "| --- | --- | --- | ---: | --- | ---: | ---: | ---: |"]
    for row in sorted(trials, key=lambda r: (r["suite"] != "office", r["policy"] != "steer", r["repeat"])):
        q, m = row["quality"], row["metrics"]
        if row["suite"] == "office":
            quality = f"{q.get('passed', '?')}/{q.get('total', '?')} 检查"
        else:
            f2p, p2p = q.get("fail_to_pass", {}), q.get("pass_to_pass", {})
            quality = f"修复 {sum(f2p.values())}/{len(f2p)}，回归 {sum(p2p.values())}/{len(p2p)}"
        label = "办公预算" if row["suite"] == "office" else "Flask 修 Bug"
        policy = "普通" if row["policy"] == "steer" else "全双工"
        status = {"completed": "完成", "timeout": "超时", "agent_error": "Agent 错误"}.get(row["status"], row["status"])
        if row["status"] == "agent_error" and "budget exhausted" in str(row.get("error")):
            status = "调用预算耗尽"
        lines.append(f"| {label} | {policy} | {status} | {m['makespan_seconds']:.1f} | {quality} | {row['actual_interrupts']} | {row['messages']} | {m['slow_calls']} / {m['fast_attempts']} |")
    lines.extend(["", "每个场景仅一题、每策略一次。办公为离线改编题，开发为原题的原生环境试跑。超时不等于任务已完成，不能据此计算完成速度提升。", "",
                  "开发质量使用原题 1 个修复测试及 59 个回归测试。额外完整 pytest 中，Python 3.12 的警告会让 test_max_cookie_size 在未修改源码上也失败；原始 59 个回归测试全部能通过。", "",
                  "每次试验的 metadata.json / events.jsonl / result.json 保留原题、角色、日志与评分；开发另有 submission.patch、grading.log、grading.xml，办公输出位于 work/final。"])
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = report(args.directory)
    print(json.dumps({"trial_count": result["trial_count"], "pairs": result["paired_comparisons"]}))
