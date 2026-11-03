# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Run trigger evaluation for a skill description.

Tests whether a skill's description causes Claude to trigger (read the skill)
for a set of queries. Outputs results as JSON.
"""

import argparse
import json
import logging
import os
import select
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.utils import parse_skill_md


@dataclass
class SkillInfo:
    """Information about the skill being tested."""

    name: str
    description: str


@dataclass
class EvalContext:
    """Context for evaluation execution."""

    timeout: int
    project_root: str
    model: str | None = None


@dataclass
class EvalConfig:
    """Configuration for evaluation."""

    num_workers: int
    runs_per_query: int = 1
    trigger_threshold: float = 0.5


# Configure logging
logger = logging.getLogger(__name__)


def find_project_root() -> Path:
    """Find the project root by walking up from cwd looking for .claude/.

    Mimics how Claude Code discovers its project root, so the command file
    we create ends up where claude -p will look for it.

    Returns:
        Path to the project root directory.
    """
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".claude").is_dir():
            return parent
    return current


def run_single_query(
    query: str,
    skill_info: SkillInfo,
    context: EvalContext,
) -> bool:
    """Run a single query and return whether the skill was triggered.

    Creates a command file in .claude/commands/ so it appears in Claude's
    available_skills list, then runs `claude -p` with the raw query.
    Uses --include-partial-messages to detect triggering early from
    stream events (content_block_start) rather than waiting for the
    full assistant message, which only arrives after tool execution.

    Args:
        query: The query to test.
        skill_info: Information about the skill being tested.
        context: Context for evaluation execution.

    Returns:
        True if the skill was triggered, False otherwise.
    """
    unique_id = uuid.uuid4().hex[:8]
    clean_name = f"{skill_info.name}-skill-{unique_id}"
    project_commands_dir = Path(context.project_root) / ".claude" / "commands"
    command_file = project_commands_dir / f"{clean_name}.md"

    try:
        project_commands_dir.mkdir(parents=True, exist_ok=True)
        # Use YAML block scalar to avoid breaking on quotes in description
        indented_desc = "\n  ".join(skill_info.description.split("\n"))
        command_content = (
            f"---\n"
            f"description: |\n"
            f"  {indented_desc}\n"
            f"---\n\n"
            f"# {skill_info.name}\n\n"
            f"This skill handles: {skill_info.description}\n"
        )
        command_file.write_text(command_content)

        cmd = [
            "claude",
            "-p", query,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if context.model:
            cmd.extend(["--model", context.model])

        # Remove CLAUDECODE env var to allow nesting claude -p inside a
        # Claude Code session. The guard is for interactive terminal
        # conflicts; programmatic subprocess usage is safe.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=context.project_root,
            env=env,
        )

        triggered = False
        start_time = time.time()
        buffer = ""
        # Track state for stream event detection
        pending_tool_name = None
        accumulated_json = ""

        try:
            while time.time() - start_time < context.timeout:
                if process.poll() is not None:
                    remaining = process.stdout.read()
                    if remaining:
                        buffer += remaining.decode("utf-8", errors="replace")
                    break

                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue

                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Early detection via stream events
                    if event.get("type") == "stream_event":
                        se = event.get("event", {})
                        se_type = se.get("type", "")

                        if se_type == "content_block_start":
                            cb = se.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                tool_name = cb.get("name", "")
                                if tool_name in ("Skill", "Read"):
                                    pending_tool_name = tool_name
                                    accumulated_json = ""
                                else:
                                    return False

                        elif (
                            se_type == "content_block_delta"
                            and pending_tool_name
                        ):
                            delta = se.get("delta", {})
                            if delta.get("type") == "input_json_delta":
                                accumulated_json += delta.get(
                                    "partial_json", ""
                                )
                                if clean_name in accumulated_json:
                                    return True

                        elif se_type in ("content_block_stop", "message_stop"):
                            if pending_tool_name:
                                return clean_name in accumulated_json
                            if se_type == "message_stop":
                                return False

                    # Fallback: full assistant message
                    elif event.get("type") == "assistant":
                        message = event.get("message", {})
                        for content_item in message.get("content", []):
                            if content_item.get("type") != "tool_use":
                                continue
                            tool_name = content_item.get("name", "")
                            tool_input = content_item.get("input", {})
                            if (
                                tool_name == "Skill"
                                and clean_name in tool_input.get("skill", "")
                            ):
                                triggered = True
                            elif (
                                tool_name == "Read"
                                and clean_name in tool_input.get(
                                    "file_path", ""
                                )
                            ):
                                triggered = True
                            return triggered

                    elif event.get("type") == "result":
                        return triggered
        finally:
            # Clean up process on any exit path
            if process.poll() is None:
                process.kill()
                process.wait()

        return triggered
    finally:
        if command_file.exists():
            command_file.unlink()


def run_eval(
    eval_set: list[dict],
    skill_info: SkillInfo,
    context: EvalContext,
    config: EvalConfig,
) -> dict[str, Any]:
    """Run the full eval set and return results.

    Args:
        eval_set: List of eval items with 'query' and 'should_trigger' keys.
        skill_info: Information about the skill being tested.
        context: Context for evaluation execution.
        config: Configuration for evaluation.

    Returns:
        Dictionary with results and summary.
    """
    results = []

    with ProcessPoolExecutor(max_workers=config.num_workers) as executor:
        future_to_info: dict[Any, tuple[dict, int]] = {}
        for item in eval_set:
            for run_idx in range(config.runs_per_query):
                future = executor.submit(
                    run_single_query,
                    item["query"],
                    skill_info,
                    context,
                )
                future_to_info[future] = (item, run_idx)

        query_triggers: dict[str, list[bool]] = {}
        query_items: dict[str, dict] = {}
        for future in as_completed(future_to_info):
            item, _ = future_to_info[future]
            query = item["query"]
            query_items[query] = item
            if query not in query_triggers:
                query_triggers[query] = []
            try:
                query_triggers[query].append(future.result())
            except Exception as e:
                logger.warning("Query failed: %s", e)
                query_triggers[query].append(False)

    for query, triggers in query_triggers.items():
        item = query_items[query]
        trigger_rate = sum(triggers) / len(triggers)
        should_trigger = item["should_trigger"]
        if should_trigger:
            did_pass = trigger_rate >= config.trigger_threshold
        else:
            did_pass = trigger_rate < config.trigger_threshold
        results.append({
            "query": query,
            "should_trigger": should_trigger,
            "trigger_rate": trigger_rate,
            "triggers": sum(triggers),
            "runs": len(triggers),
            "pass": did_pass,
        })

    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    return {
        "skill_name": skill_info.name,
        "description": skill_info.description,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
    }


def main() -> None:
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(
        description="Run trigger evaluation for a skill description"
    )
    parser.add_argument(
        "--eval-set", required=True, help="Path to eval set JSON file"
    )
    parser.add_argument(
        "--skill-path", required=True, help="Path to skill directory"
    )
    parser.add_argument(
        "--description", default=None,
        help="Override description to test"
    )
    parser.add_argument(
        "--num-workers", type=int, default=10,
        help="Number of parallel workers"
    )
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="Timeout per query in seconds"
    )
    parser.add_argument(
        "--runs-per-query", type=int, default=3,
        help="Number of runs per query"
    )
    parser.add_argument(
        "--trigger-threshold", type=float, default=0.5,
        help="Trigger rate threshold"
    )
    parser.add_argument(
        "--model", default=None,
        help="Model to use for claude -p (default: user's configured model)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print progress to stderr"
    )
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        logger.error("No SKILL.md found at %s", skill_path)
        sys.exit(1)

    name, original_description, content = parse_skill_md(skill_path)
    description = args.description or original_description
    project_root = find_project_root()

    if args.verbose:
        logger.info("Evaluating: %s", description)

    skill_info = SkillInfo(name=name, description=description)
    context = EvalContext(
        timeout=args.timeout,
        project_root=str(project_root),
        model=args.model,
    )
    config = EvalConfig(
        num_workers=args.num_workers,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
    )
    output = run_eval(
        eval_set=eval_set,
        skill_info=skill_info,
        context=context,
        config=config,
    )

    if args.verbose:
        summary = output["summary"]
        logger.info(
            "Results: %s/%s passed",
            summary["passed"],
            summary["total"]
        )
        for r in output["results"]:
            status = "PASS" if r["pass"] else "FAIL"
            rate_str = f"{r['triggers']}/{r['runs']}"
            logger.info(
                "  [%s] rate=%s expected=%s: %s",
                status,
                rate_str,
                r["should_trigger"],
                r["query"][:70]
            )

    logger.info("%s", json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
