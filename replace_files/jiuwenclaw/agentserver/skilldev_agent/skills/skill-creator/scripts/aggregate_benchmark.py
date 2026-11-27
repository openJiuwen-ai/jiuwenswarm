#!/usr/bin/env python3
"""
Aggregate individual run results into benchmark summary statistics.

Reads grading.json files from run directories and produces:
- run_summary with mean, stddev, min, max for each metric
- delta between with_skill and the baseline configuration

Usage:
    python aggregate_benchmark.py <iteration_dir>

Example:
    python aggregate_benchmark.py evals/iteration-1/

The script supports two directory layouts:

    Workspace layout (from skill-creator iterations):
    <benchmark_dir>/
    └── eval-N/
        ├── eval_metadata.json
        ├── with_skill/
        │   ├── transcript.md
        │   ├── grading.json
        │   └── outputs/
        │       └── metrics.json
        └── without_skill/ or old_skill/
            ├── transcript.md
            ├── grading.json
            └── outputs/
                └── metrics.json

    Legacy layout (with runs/ subdirectory):
    <benchmark_dir>/
    └── runs/
        └── eval-N/
            ├── eval_metadata.json
            ├── with_skill/
            │   ├── transcript.md
            │   ├── grading.json
            │   └── outputs/
            │       └── metrics.json
            └── without_skill/ or old_skill/
                ├── transcript.md
                ├── grading.json
                └── outputs/
                    └── metrics.json
"""

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


logger = logging.getLogger(__name__)
CONFIG_NAMES = ("with_skill", "without_skill", "old_skill")


class BenchmarkLayoutError(RuntimeError):
    """Raised when an eval iteration does not match the required artifact layout."""


def calculate_stats(values: list[float]) -> dict:
    """Calculate mean, stddev, min, max for a list of values."""
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}

    n = len(values)
    mean = sum(values) / n

    if n > 1:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        stddev = math.sqrt(variance)
    else:
        stddev = 0.0

    return {
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4)
    }


def load_run_results(benchmark_dir: Path) -> dict:
    """
    Load all run results from a benchmark directory.

    Returns dict keyed by config name (e.g. "with_skill"/"without_skill",
    or "with_skill"/"old_skill"), each containing a list of run results.
    """
    # Support both layouts: eval dirs directly under benchmark_dir, or under runs/
    runs_dir = benchmark_dir / "runs"
    if runs_dir.exists():
        search_dir = runs_dir
    elif list(benchmark_dir.glob("eval-*")):
        search_dir = benchmark_dir
    else:
        raise BenchmarkLayoutError(
            f"No eval directories found in {benchmark_dir} or {benchmark_dir / 'runs'}"
        )

    results: dict[str, list] = {}
    eval_dirs = sorted(search_dir.glob("eval-*"))
    if not eval_dirs:
        raise BenchmarkLayoutError(f"No eval-* directories found under {search_dir}")
    expected_baseline = None

    for eval_idx, eval_dir in enumerate(eval_dirs):
        metadata_path = eval_dir / "eval_metadata.json"
        if not metadata_path.exists():
            raise BenchmarkLayoutError(f"Missing eval metadata: {metadata_path}")
        try:
            with open(metadata_path, encoding="utf-8") as mf:
                metadata = json.load(mf)
        except (json.JSONDecodeError, OSError) as exc:
            raise BenchmarkLayoutError(f"Invalid eval metadata {metadata_path}: {exc}") from exc

        eval_id = metadata.get("eval_id", eval_idx)
        eval_name = metadata.get("eval_name") or metadata.get("name") or eval_dir.name

        configs_present = [name for name in CONFIG_NAMES if (eval_dir / name).is_dir()]
        if "with_skill" not in configs_present:
            raise BenchmarkLayoutError(f"Missing required config directory: {eval_dir / 'with_skill'}")

        baselines = [name for name in ("without_skill", "old_skill") if name in configs_present]
        if len(baselines) != 1:
            raise BenchmarkLayoutError(
                f"{eval_dir} must contain exactly one baseline config: without_skill or old_skill"
            )
        baseline = baselines[0]
        if expected_baseline is None:
            expected_baseline = baseline
        elif baseline != expected_baseline:
            raise BenchmarkLayoutError(
                f"{eval_dir} uses baseline {baseline}, but earlier evals use {expected_baseline}"
            )

        for config in ("with_skill", baseline):
            config_dir = eval_dir / config
            required_paths = [
                config_dir / "transcript.md",
                config_dir / "outputs",
                config_dir / "outputs" / "metrics.json",
                config_dir / "grading.json",
            ]
            for required_path in required_paths:
                if not required_path.exists():
                    raise BenchmarkLayoutError(f"Missing required artifact: {required_path}")

            if config not in results:
                results[config] = []

            run_number = 1
            grading_file = config_dir / "grading.json"

            try:
                with open(grading_file, encoding="utf-8") as f:
                    grading = json.load(f)
            except json.JSONDecodeError as e:
                raise BenchmarkLayoutError(f"Invalid JSON in {grading_file}: {e}") from e

            metrics_file = config_dir / "outputs" / "metrics.json"
            try:
                with open(metrics_file, encoding="utf-8") as mf:
                    output_metrics = json.load(mf)
            except json.JSONDecodeError as e:
                raise BenchmarkLayoutError(f"Invalid JSON in {metrics_file}: {e}") from e

            # Extract metrics. Grader metrics can override executor metrics, but keep
            # executor values when the grader omits a field.
            execution_metrics = {**output_metrics, **grading.get("execution_metrics", {})}
            result = {
                "eval_id": eval_id,
                "eval_name": eval_name,
                "run_number": run_number,
                "pass_rate": grading.get("summary", {}).get("pass_rate", 0.0),
                "passed": grading.get("summary", {}).get("passed", 0),
                "failed": grading.get("summary", {}).get("failed", 0),
                "total": grading.get("summary", {}).get("total", 0),
                "tool_calls": execution_metrics.get("total_tool_calls", 0),
                "errors": execution_metrics.get("errors_encountered", 0),
                "output_chars": execution_metrics.get("output_chars", 0),
            }

            # Extract expectations — viewer requires fields: text, passed, evidence
            raw_expectations = grading.get("expectations", [])
            for exp in raw_expectations:
                if "text" not in exp or "passed" not in exp:
                    logger.warning(
                        "Expectation in %s missing required fields (text, passed, evidence): %s",
                        grading_file,
                        exp,
                    )
            result["expectations"] = raw_expectations

            # Extract notes from user_notes_summary
            notes_summary = grading.get("user_notes_summary", {})
            notes = []
            notes.extend(notes_summary.get("uncertainties", []))
            notes.extend(notes_summary.get("needs_review", []))
            notes.extend(notes_summary.get("workarounds", []))
            result["notes"] = notes

            results[config].append(result)

    return results


def aggregate_results(results: dict) -> dict:
    """
    Aggregate run results into summary statistics.

    Returns run_summary with stats for each configuration and delta.
    """
    run_summary = {}
    configs = list(results.keys())

    for config in configs:
        runs = results.get(config, [])

        if not runs:
            run_summary[config] = {
                "pass_rate": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "tool_calls": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                "errors": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
            }
            continue

        pass_rates = [r["pass_rate"] for r in runs]
        tool_calls = [r.get("tool_calls", 0) for r in runs]
        errors = [r.get("errors", 0) for r in runs]

        run_summary[config] = {
            "pass_rate": calculate_stats(pass_rates),
            "tool_calls": calculate_stats(tool_calls),
            "errors": calculate_stats(errors)
        }

    # Calculate delta between the first two configs (if two exist)
    if len(configs) >= 2:
        primary = run_summary.get(configs[0], {})
        baseline = run_summary.get(configs[1], {})
    else:
        primary = run_summary.get(configs[0], {}) if configs else {}
        baseline = {}

    delta_pass_rate = primary.get("pass_rate", {}).get("mean", 0) - baseline.get("pass_rate", {}).get("mean", 0)
    delta_tool_calls = primary.get("tool_calls", {}).get("mean", 0) - baseline.get("tool_calls", {}).get("mean", 0)
    delta_errors = primary.get("errors", {}).get("mean", 0) - baseline.get("errors", {}).get("mean", 0)

    run_summary["delta"] = {
        "pass_rate": f"{delta_pass_rate:+.2f}",
        "tool_calls": f"{delta_tool_calls:+.1f}",
        "errors": f"{delta_errors:+.1f}"
    }

    return run_summary


def generate_benchmark(benchmark_dir: Path, skill_name: str = "", skill_path: str = "") -> dict:
    """
    Generate complete benchmark.json from run results.
    """
    results = load_run_results(benchmark_dir)
    run_summary = aggregate_results(results)

    # Build runs array for benchmark.json
    runs = []
    for config in results:
        for result in results[config]:
            runs.append({
                "eval_id": result["eval_id"],
                "eval_name": result["eval_name"],
                "configuration": config,
                "run_number": result["run_number"],
                "result": {
                    "pass_rate": result["pass_rate"],
                    "passed": result["passed"],
                    "failed": result["failed"],
                    "total": result["total"],
                    "tool_calls": result.get("tool_calls", 0),
                    "errors": result.get("errors", 0),
                    "output_chars": result.get("output_chars", 0)
                },
                "expectations": result["expectations"],
                "notes": result["notes"]
            })

    # Determine eval IDs from results
    eval_ids = sorted(set(
        r["eval_id"]
        for config in results.values()
        for r in config
    ))

    benchmark = {
        "metadata": {
            "skill_name": skill_name or "<skill-name>",
            "skill_path": skill_path or "<path/to/skill>",
            "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "evals_run": eval_ids,
            "runs_per_configuration": max(
                (len(runs) for runs in results.values()), default=0
            )
        },
        "runs": runs,
        "run_summary": run_summary,
        "notes": []  # To be filled by analyzer
    }

    return benchmark


def generate_markdown(benchmark: dict) -> str:
    """Generate human-readable benchmark.md from benchmark data."""
    metadata = benchmark["metadata"]
    run_summary = benchmark["run_summary"]

    # Determine config names (excluding "delta")
    configs = [k for k in run_summary if k != "delta"]
    config_a = configs[0] if len(configs) >= 1 else "config_a"
    config_b = configs[1] if len(configs) >= 2 else "config_b"
    label_a = config_a.replace("_", " ").title()
    label_b = config_b.replace("_", " ").title()

    lines = [
        f"# Skill Benchmark: {metadata['skill_name']}",
        "",
        f"**Date**: {metadata['timestamp']}",
        (
            f"**Evals**: {', '.join(map(str, metadata['evals_run']))} "
            f"({metadata['runs_per_configuration']} runs each per configuration)"
        ),
        "",
        "## Summary",
        "",
        f"| Metric | {label_a} | {label_b} | Delta |",
        "|--------|------------|---------------|-------|",
    ]

    a_summary = run_summary.get(config_a, {})
    b_summary = run_summary.get(config_b, {})
    delta = run_summary.get("delta", {})

    # Format pass rate
    a_pr = a_summary.get("pass_rate", {})
    b_pr = b_summary.get("pass_rate", {})
    lines.append(
        f"| Pass Rate | {a_pr.get('mean', 0) * 100:.0f}% ± "
        f"{a_pr.get('stddev', 0) * 100:.0f}% | "
        f"{b_pr.get('mean', 0) * 100:.0f}% ± "
        f"{b_pr.get('stddev', 0) * 100:.0f}% | "
        f"{delta.get('pass_rate', '—')} |"
    )

    # Format tool calls
    a_tool_calls = a_summary.get("tool_calls", {})
    b_tool_calls = b_summary.get("tool_calls", {})
    lines.append(
        f"| Tool Calls | {a_tool_calls.get('mean', 0):.1f} ± "
        f"{a_tool_calls.get('stddev', 0):.1f} | "
        f"{b_tool_calls.get('mean', 0):.1f} ± "
        f"{b_tool_calls.get('stddev', 0):.1f} | "
        f"{delta.get('tool_calls', '—')} |"
    )

    # Format errors
    a_errors = a_summary.get("errors", {})
    b_errors = b_summary.get("errors", {})
    lines.append(
        f"| Errors | {a_errors.get('mean', 0):.1f} ± "
        f"{a_errors.get('stddev', 0):.1f} | "
        f"{b_errors.get('mean', 0):.1f} ± "
        f"{b_errors.get('stddev', 0):.1f} | "
        f"{delta.get('errors', '—')} |"
    )

    # Notes section
    if benchmark.get("notes"):
        lines.extend([
            "",
            "## Notes",
            ""
        ])
        for note in benchmark["notes"]:
            lines.append(f"- {note}")

    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Aggregate benchmark run results into summary statistics"
    )
    parser.add_argument(
        "benchmark_dir",
        type=Path,
        help="Path to the benchmark directory"
    )
    parser.add_argument(
        "--skill-name",
        default="",
        help="Name of the skill being benchmarked"
    )
    parser.add_argument(
        "--skill-path",
        default="",
        help="Path to the skill being benchmarked"
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        help="Output path for benchmark.json (default: <benchmark_dir>/benchmark.json)"
    )

    args = parser.parse_args()

    if not args.benchmark_dir.exists():
        logger.error("Directory not found: %s", args.benchmark_dir)
        sys.exit(1)

    # Generate benchmark
    try:
        benchmark = generate_benchmark(args.benchmark_dir, args.skill_name, args.skill_path)
    except BenchmarkLayoutError as exc:
        logger.error("Invalid benchmark layout: %s", exc)
        sys.exit(1)

    # Determine output paths
    output_json = args.output or (args.benchmark_dir / "benchmark.json")
    output_md = output_json.with_suffix(".md")

    # Write benchmark.json
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(benchmark, f, indent=2, ensure_ascii=False)
    logger.info("Generated: %s", output_json)

    # Write benchmark.md
    markdown = generate_markdown(benchmark)
    with open(output_md, "w", encoding="utf-8") as f:
        f.write(markdown)
    logger.info("Generated: %s", output_md)

    # Print summary
    run_summary = benchmark["run_summary"]
    configs = [k for k in run_summary if k != "delta"]
    delta = run_summary.get("delta", {})

    logger.info("Summary:")
    for config in configs:
        pr = run_summary[config]["pass_rate"]["mean"]
        label = config.replace("_", " ").title()
        logger.info("%s: %.1f%% pass rate", label, pr * 100)
    logger.info("Delta: %s", delta.get("pass_rate", "-"))


if __name__ == "__main__":
    main()