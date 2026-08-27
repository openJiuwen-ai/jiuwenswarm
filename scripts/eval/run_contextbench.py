#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run the coding agent on ContextBench and write official trajectories.

This is a retrieval exam: locate + declare context. It is not SWE resolved.
Score with ``scripts/eval/run_evaluate.py`` → official ``contextbench.evaluate``.

Point the runner at a ContextBench checkout (``CONTEXTBENCH_ROOT`` or
``--contextbench-root``). A sibling ``../ContextBench`` also works.

    UV_NO_SYNC=1 uv run --with pyarrow python scripts/eval/run_contextbench.py \
        --limit 5 --profile graph --graph-agent root

    UV_NO_SYNC=1 uv run --with pyarrow python scripts/eval/run_contextbench.py \
        --instance pallets__flask-5014 \
        --profile graph --graph-agent root \
        --max-iterations 10 \
        --output ./tmp/cb-manual/cfg_b__graph
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
JIUWEN_ROOT = SCRIPT_DIR.parents[1]

for path in (SCRIPT_DIR, JIUWEN_ROOT):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from jiuwenswarm.server.runtime.agent_adapter.code_graph_flags import (  # noqa: E402
    PROFILE_GRAPH,
    PROFILE_OFF,
    resolve_profile,
)
from eval_paths import (  # noqa: E402
    DEFAULT_OUTPUT,
    prepend_contextbench,
    resolve_contextbench_parquet,
    resolve_contextbench_root,
)
from local_openjiuwen import (  # noqa: E402
    assert_engine_matches_branch,
    describe_eval_pair,
    describe_openjiuwen,
    load_eval_dotenv,
    prepend_local_agent_core,
)
from trajectory import contextbench_record  # noqa: E402

prepend_local_agent_core()

from coding_agent import (  # noqa: E402
    CONTEXTBENCH_CODE_HIDDEN_TOOLS,
    CONTEXTBENCH_FIND_HIDDEN_TOOLS,
    CONTEXTBENCH_ROOT_HIDDEN_TOOLS,
    cfg_paths,
    config_dir_name,
    isolate_eval_logs,
    model_env_snapshot,
    write_run_config,
)

FIND_CONTRACT_TOOLS = ("resolve_symbol", "read_symbol", "submit_code_context")


def resolve_graph_agent(profile: str, explicit: str | None) -> str:
    """Who owns retrieval. Off defaults to root so ``--profile off`` stays run09."""
    text = (explicit or "").strip().lower()
    if text in {"root", "code_agent"}:
        return text
    if text:
        raise ValueError(f"unknown graph agent {text!r}; expected root or code_agent")
    if resolve_profile(profile) == PROFILE_OFF:
        return "root"
    return "code_agent"


def describe_protocol(profile: str, graph_agent: str) -> str:
    if resolve_profile(profile) == PROFILE_OFF:
        if graph_agent == "code_agent":
            return "product-baseline-delegates-code-agent"
        return "find-product-baseline"
    if graph_agent == "code_agent":
        return "find-root-delegates-code-agent"
    return "find-on-root"


ROOT_DELEGATE_PROMPT = """You are the root coding agent for a locate exam.
You have no repository search tools (no grep, glob, bash, or Code Graph).
Do not implement a patch. Do not type <PATCH_CONTEXT> yourself.

Call task_tool with subagent_type=code_agent and pass the full issue below.
code_agent owns Code Graph locate tools and will submit_code_context.
After it returns, stop. Do not search or edit files yourself.

=== ISSUE ===
{issue}
"""

ROOT_DELEGATE_BASELINE_PROMPT = """You are the root coding agent for a locate exam.
You have no repository search tools (no grep, glob, bash, or Code Graph).
Do not implement a patch.

Call task_tool with subagent_type=code_agent and pass the full issue below.
code_agent has grep / read_file / bash and will emit the scored PATCH_CONTEXT.
After it returns, stop. Do not search or edit files yourself.

=== ISSUE ===
{issue}
"""

CODE_AGENT_BASELINE_PROMPT = """You are the code agent for a locate exam: find the
code that must be read or changed to resolve the issue. Do not implement a patch.

You have grep / read_file / bash. There are no Code Graph tools.

Finish with exactly one MiniSWE block. Official scoring uses only this last block:

<PATCH_CONTEXT>
File: path/relative.py
Lines: 12-40
</PATCH_CONTEXT>

Rules:
- Use tools instead of guessing file contents.
- Prefer the smallest enclosing function or method.
- Do not include tests unless the issue is about tests.
- Do not type File/Lines outside the tag.
"""

CODE_AGENT_FIND_PROMPT = """You are the code agent for a locate exam: find the
code that must be read or changed to resolve the issue. Do not implement a patch.
Do not type <PATCH_CONTEXT> yourself.

Rules:
- If the issue names a class, function, or method, call resolve_symbol first.
- Never submit a large class. If read_symbol returns large_class, call
  inspect_code_structure and read_symbol on the methods that change.
- For frames / transforms / registration across modules: after resolve, call
  find_importers, then search_source_text for register/decorator, then read.
- Use find_callers / find_callees / find_importers / find_base_classes /
  find_subclasses for structure. Do not approximate those with text search.
- search_source_text is only for exact literals, error messages, config keys,
  or decorators the graph does not store.
- find_code_symbols is candidate generation (default 5), not the answer.
- Prefer read_symbol over reading a whole file. context_before/after max is 5.
- Do not include tests unless the issue is about tests.
- As soon as the primary location is read, call submit_code_context with those
  symbol_ids. The system will emit the scored PATCH_CONTEXT block.
"""

BASELINE_PROMPT = """You are working in a checked-out repository at its base commit.
This is a locate exam: find the code that must be read or changed to resolve
the issue. Do not implement a patch.

You are the original product coding agent: grep / read_file / bash are available.
There is no code_agent subagent and no Code Graph tools. Do not delegate.

Finish with exactly one MiniSWE block. Official scoring uses only this last block:

<PATCH_CONTEXT>
File: path/relative.py
Lines: 12-40
</PATCH_CONTEXT>

Rules:
- Use tools instead of guessing file contents.
- Prefer the smallest enclosing function or method.
- Do not include tests unless the issue is about tests.
- Do not type File/Lines outside the tag.

=== ISSUE ===
{issue}
"""

FIND_PROMPT = """You are working in a checked-out repository at its base commit.
This is a locate exam: find the code that must be read or changed to resolve
the issue. Do not implement a patch. Do not type <PATCH_CONTEXT> yourself.

Rules:
- You are the persistent code agent for this task. Do not delegate. There is no
  code_agent subagent.
- If the issue names a class, function, or method, call resolve_symbol first.
- Never submit a large class. If read_symbol returns large_class, call
  inspect_code_structure and read_symbol on the methods that change.
- For frames / transforms / registration across modules: after resolve, call
  find_importers, then search_source_text for register/decorator, then read.
- Use find_callers / find_callees / find_importers / find_base_classes /
  find_subclasses for structure. Do not approximate those with text search.
- search_source_text is only for exact literals, error messages, config keys,
  or decorators the graph does not store.
- find_code_symbols is candidate generation (default 5), not the answer.
- Prefer read_symbol over reading a whole file. context_before/after max is 5.
- Do not include tests unless the issue is about tests.
- As soon as the primary location is read, call submit_code_context with those
  symbol_ids. The system will emit the scored PATCH_CONTEXT block.

=== ISSUE ===
{issue}
"""


def resolve_parquet(explicit: Path | None, *, root: Path) -> Path:
    return resolve_contextbench_parquet(explicit, root=root)


def load_verified_rows(parquet_path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("Install pyarrow to read parquet: uv run --with pyarrow") from exc
    table = pq.read_table(parquet_path)
    if limit > 0:
        table = table.slice(0, limit)
    return table.to_pylist()


def record_id(row: dict[str, Any]) -> str:
    return str(row.get("original_inst_id") or row.get("instance_id") or "").strip()


def problem_statement(row: dict[str, Any]) -> str:
    for key in ("problem_statement", "issue", "query"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    title = str(row.get("title") or "").strip()
    body = str(row.get("body") or row.get("issue_body") or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def repo_url(row: dict[str, Any]) -> str:
    url = str(row.get("repo_url") or "").strip()
    if url:
        return url
    repo = str(row.get("repo") or "").strip()
    if "/" in repo and not repo.startswith("http"):
        return f"https://github.com/{repo}"
    return repo


def base_commit(row: dict[str, Any]) -> str:
    return str(row.get("base_commit") or row.get("commit") or "").strip()


def _worktree_dir_for(url: str, commit: str) -> Path:
    from contextbench.core.repo import _normalize_url

    tmp_root = os.environ.get("CONTEXTBENCH_TMP_ROOT") or tempfile.gettempdir()
    return Path(tmp_root) / "contextbench_worktrees" / _normalize_url(url) / commit


def _repair_stale_worktree(url: str, commit: str, cache_dir: Path) -> None:
    """Reset or drop a leftover worktree whose HEAD no longer matches ``commit``.

    Official ``checkout()`` reuses the path only when HEAD matches. A dirty
    leftover (for example after bash ``git checkout`` in a prior instance)
    makes ``git worktree add`` fail with "already exists" and abort the run.
    """
    worktree = _worktree_dir_for(url, commit)
    if not worktree.is_dir():
        return
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode == 0 and head.stdout.strip() == commit:
        return
    reset = subprocess.run(
        ["git", "-C", str(worktree), "checkout", "--detach", "--force", commit],
        capture_output=True,
        text=True,
        check=False,
    )
    if reset.returncode == 0:
        print(f"repaired stale worktree {worktree} -> {commit}", flush=True)
        return
    from contextbench.core.repo import _normalize_url

    base = cache_dir / _normalize_url(url)
    subprocess.run(
        ["git", "-C", str(base), "worktree", "remove", "--force", str(worktree)],
        capture_output=True,
        text=True,
        check=False,
    )
    if worktree.is_dir():
        shutil.rmtree(worktree, ignore_errors=True)
    if base.is_dir():
        subprocess.run(
            ["git", "-C", str(base), "worktree", "prune"],
            capture_output=True,
            text=True,
            check=False,
        )
    print(f"removed stale worktree {worktree}", flush=True)


def checkout_repo(row: dict[str, Any], cache_dir: Path) -> str:
    from contextbench.core.repo import checkout

    url = repo_url(row)
    commit = base_commit(row)
    if not url or not commit:
        raise RuntimeError(f"missing repo/commit for {record_id(row)}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    _repair_stale_worktree(url, commit, cache_dir)
    repo_dir = checkout(url, commit, str(cache_dir), verbose=True)
    if not repo_dir:
        raise RuntimeError(f"checkout failed for {url}@{commit}")
    return repo_dir


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _aggregate_pred(output_dir: Path) -> Path:
    pred_path = output_dir / "pred.jsonl"
    records: list[dict[str, Any]] = []
    for traj in sorted(output_dir.glob("*.traj.json")):
        try:
            raw = json.loads(traj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skip {traj}: {exc}", file=sys.stderr)
            continue
        instance_id = str(raw.get("instance_id") or "").strip()
        repo_dir = ""
        meta_path = output_dir / f"{instance_id}.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                saved = str((meta or {}).get("repo_dir") or "").strip()
                if saved and os.path.isdir(saved):
                    repo_dir = saved
            except (OSError, json.JSONDecodeError):
                repo_dir = ""
        records.append(contextbench_record(raw, repo_root=repo_dir))
    with pred_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"aggregated {len(records)} trajectories -> {pred_path}", flush=True)
    return pred_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ContextBench locate runner")
    parser.add_argument("--parquet", type=Path, default=None)
    parser.add_argument(
        "--contextbench-root",
        type=Path,
        default=None,
        help="ContextBench checkout (or set CONTEXTBENCH_ROOT). "
        "Sibling ../ContextBench also works.",
    )
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--instance", default="", help="Comma-separated instance ids")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument(
        "--profile",
        default=PROFILE_GRAPH,
        choices=(PROFILE_OFF, PROFILE_GRAPH),
        help="off=original coding tools, no graph; graph=find_* tools. "
        "default no code_agent for off (pass --graph-agent code_agent to hang it)",
    )
    parser.add_argument("--max-iterations", type=int, default=40)
    parser.add_argument(
        "--graph-agent",
        choices=("root", "code_agent"),
        default=None,
        help="who owns retrieval: root or code_agent. default: root when "
        "--profile off, code_agent otherwise",
    )
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dotenv", type=Path, default=None)
    return parser.parse_args()


async def run_one(
    row: dict[str, Any],
    *,
    output_dir: Path,
    cache_dir: Path,
    max_iterations: int,
    profile: str,
    force_rerun: bool,
    graph_agent: str,
) -> str:
    instance_id = record_id(row)
    dest_traj = output_dir / f"{instance_id}.traj.json"
    dest_meta = output_dir / f"{instance_id}.json"
    dest_trace = output_dir / f"{instance_id}.trace.json"
    if dest_traj.is_file() and dest_trace.is_file() and not force_rerun:
        print(f"SKIP {instance_id}", flush=True)
        return "skipped"

    issue = problem_statement(row)
    if not issue:
        raise RuntimeError(f"empty problem_statement for {instance_id}")
    repo_dir = checkout_repo(row, cache_dir)

    from coding_agent import (
        CONTEXTBENCH_CODE_HIDDEN_TOOLS,
        CONTEXTBENCH_FIND_HIDDEN_TOOLS,
        CONTEXTBENCH_ROOT_HIDDEN_TOOLS,
        HIDDEN_SEARCH_TOOLS,
        create_coding_agent,
        hide_agent_tools,
        invoke_coding_agent,
        list_agent_tools,
        list_subagent_names,
        subagent_graph_profiles,
    )

    baseline = profile == PROFILE_OFF
    delegate = graph_agent == "code_agent"
    handle = create_coding_agent(
        repo_dir,
        workspace=output_dir.parent / "workspaces" / instance_id,
        max_iterations=max_iterations,
        enable_code_subagent=delegate,
        enable_explore=False,
        enable_plan=False,
        profile=profile,
        hide_grep=not baseline,
        hide_bash=not baseline,
        hide_edit=True,
        cache_dir=output_dir.parent / "code_graph_cache" / instance_id,
        code_agent_system_prompt=(
            None
            if not delegate
            else (CODE_AGENT_BASELINE_PROMPT if baseline else CODE_AGENT_FIND_PROMPT)
        ),
    )
    handle.trace.recorder.repo_root = str(repo_dir)
    await handle.agent.ensure_initialized()
    if delegate:
        root_hidden = CONTEXTBENCH_ROOT_HIDDEN_TOOLS
    elif baseline:
        root_hidden = ("edit_file", "write_file", "task_tool")
    else:
        root_hidden = CONTEXTBENCH_FIND_HIDDEN_TOOLS
    hide_agent_tools(handle.agent, root_hidden)
    tools = list_agent_tools(handle.agent)
    leftover_hidden = [name for name in root_hidden if name in tools]
    if leftover_hidden:
        raise RuntimeError(f"Root still has hidden tools: {leftover_hidden}")
    if not baseline or delegate:
        leftover_search = [name for name in HIDDEN_SEARCH_TOOLS if name in tools]
        if leftover_search:
            raise RuntimeError(f"Root still has hidden tools: {leftover_search}")
    find_on_root = [name for name in FIND_CONTRACT_TOOLS if name in tools]
    if baseline and not delegate:
        if find_on_root:
            raise RuntimeError(f"baseline must not have find graph tools: {find_on_root}")
        if "grep" not in tools:
            raise RuntimeError("baseline Root needs grep")
        if "task_tool" in tools:
            raise RuntimeError("baseline must not have task_tool")
        sub_names = list_subagent_names(handle.agent)
        if "code_agent" in sub_names:
            raise RuntimeError(f"baseline must not hang code_agent: {sub_names}")
        sub_graph = subagent_graph_profiles(handle.agent)
        leaked = [name for name, value in sub_graph.items() if value != "off"]
        if leaked:
            raise RuntimeError(f"baseline graph profile leaked: {leaked}")
    elif delegate:
        if find_on_root:
            raise RuntimeError(f"Root must not have find graph tools: {find_on_root}")
        if "task_tool" not in tools:
            raise RuntimeError("Root needs task_tool to delegate to code_agent")
        sub_names = list_subagent_names(handle.agent)
        if "code_agent" not in sub_names:
            raise RuntimeError(f"code_agent was not hung: {sub_names}")
        sub_graph = subagent_graph_profiles(handle.agent)
        if sub_graph.get("code_agent") != profile:
            raise RuntimeError(f"code_agent graph profile mismatch: {sub_graph}")
        leaked = [name for name, value in sub_graph.items() if name != "code_agent" and value != "off"]
        if leaked:
            raise RuntimeError(f"graph profile leaked onto subagents: {leaked}")
        if baseline and "grep" in tools:
            raise RuntimeError("baseline delegate Root must not keep grep")
    else:
        missing_graph = [name for name in FIND_CONTRACT_TOOLS if name not in tools]
        if missing_graph:
            raise RuntimeError(f"find graph tools missing on root: {missing_graph}")
        forbidden_graph = [
            name
            for name in ("analyze_impact", "analyze_patch_impact", "expand_related", "task_tool")
            if name in tools
        ]
        if forbidden_graph:
            raise RuntimeError(f"graph agent still has forbidden tools: {forbidden_graph}")
    print(
        f"START {instance_id} profile={profile} graph_agent="
        f"{graph_agent if (delegate or not baseline) else 'product'} "
        f"tools={tools} "
        f"subagents={list_subagent_names(handle.agent)} "
        f"subagent_graph={subagent_graph_profiles(handle.agent)}",
        flush=True,
    )
    started = time.perf_counter()
    if baseline and not delegate:
        query = BASELINE_PROMPT.format(issue=issue)
    elif delegate:
        query = (
            ROOT_DELEGATE_BASELINE_PROMPT.format(issue=issue)
            if baseline
            else ROOT_DELEGATE_PROMPT.format(issue=issue)
        )
    else:
        query = FIND_PROMPT.format(issue=issue)
    result = await invoke_coding_agent(
        handle,
        query,
        hide_tools_named=root_hidden,
    )
    output_text = result.get("output") if isinstance(result, dict) else str(result)
    texts = list(result.get("message_texts") or [])
    if output_text:
        texts.append(str(output_text))
    handle.trace.recorder.apply_texts(texts)
    runtime = time.perf_counter() - started
    traj_data = handle.recorder.traj_data()
    _write_json(
        dest_traj,
        {
            "instance_id": instance_id,
            "traj_data": traj_data,
            "model_patch": "",
            "output": output_text,
        },
    )
    trace_payload = handle.trace.finish(output=output_text)
    trace_payload["instance_id"] = instance_id
    trace_payload["runtime_seconds"] = runtime
    _write_json(dest_trace, trace_payload)
    totals = trace_payload.get("totals") or {}
    _write_json(
        dest_meta,
        {
            "instance_id": instance_id,
            "flags": handle.trace.flags,
            "code_graph_profile": profile,
            "repo": row.get("repo"),
            "repo_url": repo_url(row),
            "repo_dir": repo_dir,
            "base_commit": base_commit(row),
            "task_mode": "locate",
            "benchmark": "contextbench",
            "leaderboard_eligible": False,
            "protocol": describe_protocol(profile, graph_agent),
            "graph_agent": graph_agent if (delegate or not baseline) else "product",
            "report_editloc": False,
            "utilized_source": traj_data.get("utilized_source"),
            "runtime_seconds": runtime,
            "tools": list_agent_tools(handle.agent),
            "subagents": list_subagent_names(handle.agent),
            "find_code_symbols_calls": totals.get("find_code_symbols_calls"),
            "resolve_symbol_calls": totals.get("resolve_symbol_calls"),
            "read_symbol_calls": totals.get("read_symbol_calls"),
            "submit_code_context_calls": totals.get("submit_code_context_calls"),
            "select_code_context_calls": totals.get("select_code_context_calls"),
            "task_tool_calls": totals.get("task_tool_calls"),
            "graph_tool_calls": totals.get("graph_tool_calls"),
            "read_file_calls": totals.get("read_file_calls"),
        },
    )
    print(
        f"OK {instance_id} utilized={traj_data.get('utilized_source')} "
        f"files={len(traj_data.get('pred_files') or [])} "
        f"task_tool={totals.get('task_tool_calls')} "
        f"find={totals.get('find_code_symbols_calls')} "
        f"graph={totals.get('graph_tool_calls')} "
        f"runtime={runtime:.1f}s",
        flush=True,
    )
    return "ok"


async def async_main() -> None:
    args = parse_args()
    load_eval_dotenv(args.dotenv)
    assert_engine_matches_branch()
    contextbench_root = resolve_contextbench_root(
        args.contextbench_root, parquet=args.parquet
    )
    prepend_contextbench(contextbench_root)
    parquet = resolve_parquet(args.parquet, root=contextbench_root)
    profile = resolve_profile(args.profile)
    graph_agent = resolve_graph_agent(profile, args.graph_agent)
    rows = load_verified_rows(parquet, 0)
    wanted = {item.strip() for item in args.instance.split(",") if item.strip()}
    if wanted:
        rows = [row for row in rows if record_id(row) in wanted or str(row.get("instance_id") or "") in wanted]
    if args.limit > 0:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("no ContextBench rows matched")

    run_root = args.output.expanduser().resolve()
    name = config_dir_name(profile=profile)
    paths = cfg_paths(run_root, name)
    paths["raw"].mkdir(parents=True, exist_ok=True)
    isolate_eval_logs(paths["logs"])
    cache_dir = (args.cache or (run_root / "repos")).expanduser().resolve()
    write_run_config(
        paths["config_json"],
        {
            "benchmark": "contextbench",
            "parquet": str(parquet),
            "contextbench_root": str(contextbench_root),
            "limit": args.limit,
            "instances": [record_id(row) for row in rows],
            "profile": profile,
            "pair": describe_eval_pair(),
            "openjiuwen": describe_openjiuwen(),
            "model": model_env_snapshot(),
            "task_mode": "locate",
            "leaderboard_eligible": False,
            "report_editloc": False,
            "protocol": describe_protocol(profile, graph_agent),
            "graph_agent": (
                graph_agent
                if graph_agent == "code_agent" or profile != PROFILE_OFF
                else "product"
            ),
            "root_hidden_tools": (
                list(CONTEXTBENCH_ROOT_HIDDEN_TOOLS)
                if graph_agent == "code_agent"
                else (
                    ["edit_file", "write_file", "task_tool"]
                    if profile == PROFILE_OFF
                    else list(CONTEXTBENCH_FIND_HIDDEN_TOOLS)
                )
            ),
            "code_agent_hidden_tools": (
                list(CONTEXTBENCH_CODE_HIDDEN_TOOLS)
                if graph_agent == "code_agent" and profile != PROFILE_OFF
                else []
            ),
        },
    )
    print(f"pair {describe_eval_pair()}", flush=True)
    print(f"parquet {parquet} n={len(rows)} -> {paths['raw']}", flush=True)
    if args.dry_run:
        for row in rows:
            print(f"DRY {record_id(row)} {row.get('repo')}@{base_commit(row)[:12]}")
        return

    for row in rows:
        await run_one(
            row,
            output_dir=paths["raw"],
            cache_dir=cache_dir,
            max_iterations=args.max_iterations,
            profile=profile,
            force_rerun=args.force_rerun,
            graph_agent=graph_agent,
        )
    _aggregate_pred(paths["raw"])


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
