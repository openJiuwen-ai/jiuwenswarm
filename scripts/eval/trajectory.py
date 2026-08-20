# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Convert coding-agent tool calls into a ContextBench trajectory.

The recorder is a shared list. Each agent (root / explore / plan / code) gets
its own rail instance so ``init`` can bind to that agent, but they all append to
the same recorder. Code Graph tools now live on ``code_agent``, so its steps are
part of the same trajectory as its edits.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

_GREP_LINE_RE = re.compile(r"^(.*?):(\d+)[:\-]")
_GRAPH_TOOLS = frozenset(
    {
        "resolve_symbol",
        "find_code_symbols",
        "search_source_text",
        "inspect_code_structure",
        "read_symbol",
        "read_code",
        "find_callers",
        "find_callees",
        "find_importers",
        "find_base_classes",
        "find_subclasses",
        "trace_call_paths",
        "submit_code_context",
    }
)
_READ_TOOLS = frozenset({"read_file"})
_EDIT_TOOLS = frozenset({"edit_file", "write_file"})
EXPLORED_TOOLS = frozenset({"read_file", "read_code", "read_symbol", "grep", "bash"})
HIT_TOOLS = frozenset(
    {
        "resolve_symbol",
        "find_code_symbols",
        "search_source_text",
        "inspect_code_structure",
        "find_callers",
        "find_callees",
        "find_importers",
        "find_base_classes",
        "find_subclasses",
        "trace_call_paths",
    }
)
UTILIZED_TOOLS = frozenset(
    {
        "select_code_context",
        "submit_code_context",
    }
)
MODE_CONTEXTBENCH = "contextbench"

# Lists of file/line-bearing dicts a graph payload can carry. ``paths`` nests its
# symbols one level down, so it is walked separately.
_MATCH_LIST_KEYS = (
    "matches",
    "symbols",
    "related",
    "locations",
    "selected",
    "definitions",
    "direct_callers",
    "transitive_callers",
    "subclasses",
    "implementations",
    "imports",
    "tests",
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return data
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        inner = dumped.get("data")
        return inner if isinstance(inner, dict) else dumped
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


_SKIP_DIR_NAMES = frozenset(
    {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".tox"}
)


@lru_cache(maxsize=16)
def _repo_file_index(repo_root: str) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    """Map repo-relative files and basename → unique-or-ambiguous candidates."""
    root = os.path.realpath(repo_root)
    files: list[str] = []
    if not root or not os.path.isdir(root):
        return frozenset(), {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIR_NAMES]
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            try:
                rel = os.path.relpath(abs_path, root).replace("\\", "/")
            except ValueError:
                continue
            if rel.startswith(".."):
                continue
            files.append(rel)
    by_name: dict[str, list[str]] = {}
    for rel in files:
        by_name.setdefault(os.path.basename(rel), []).append(rel)
    return frozenset(files), {key: tuple(sorted(value)) for key, value in by_name.items()}


def _strip_rel(path: str) -> str:
    raw = str(path or "").strip().strip("'\"").replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _search_root_rel(search_root: str, repo_root: str, existing: frozenset[str]) -> str:
    search = _strip_rel(search_root).rstrip("/")
    if not search:
        return ""
    if repo_root and os.path.isabs(search):
        try:
            rel = os.path.relpath(os.path.realpath(search), os.path.realpath(repo_root)).replace("\\", "/")
        except ValueError:
            return ""
        if rel.startswith(".."):
            return ""
        search = "" if rel == "." else rel
    if search in existing:
        search = os.path.dirname(search).replace("\\", "/")
    return search


def resolve_repo_path(path: str, repo_root: str, *, search_root: str = "") -> str:
    """Map a tool path to a repo-relative file that actually exists.

    ContextBench evaluate drops paths that are not files under the checkout.
    Grep often prints ``altaz.py`` because it was run inside a subdirectory.
    When *repo_root* is missing or empty, keep the stripped relative path so
    unit tests without a real checkout still work.
    """
    raw = _strip_rel(path).lstrip("/")
    if not raw:
        return ""

    search = _search_root_rel(search_root, repo_root, frozenset())
    candidates = [raw]
    if search and not os.path.isabs(search) and not raw.startswith(search + "/") and raw != search:
        candidates.append(f"{search}/{raw}")
    fallback = candidates[-1] if len(candidates) > 1 else raw

    if not repo_root:
        return fallback
    repo = os.path.realpath(repo_root)
    if not os.path.isdir(repo):
        return fallback

    existing, by_name = _repo_file_index(repo)
    if not existing:
        return fallback

    search = _search_root_rel(search_root, repo, existing)
    candidates = [raw]
    if search and not raw.startswith(search + "/") and raw != search:
        candidates.insert(0, f"{search}/{raw}")

    for cand in candidates:
        abs_cand = cand if os.path.isabs(cand) else os.path.join(repo, cand)
        try:
            rel = os.path.relpath(os.path.realpath(abs_cand), repo).replace("\\", "/")
        except ValueError:
            rel = cand.lstrip("/")
        if rel.startswith(".."):
            continue
        if rel in existing:
            return rel
    for cand in candidates:
        suffix = cand.lstrip("/")
        hits = [rel for rel in existing if rel == suffix or rel.endswith("/" + suffix)]
        if len(hits) == 1:
            return hits[0]
    named = by_name.get(os.path.basename(raw)) or ()
    if len(named) == 1:
        return named[0]
    return ""


def _repo_relpath(path: str, repo_root: str, *, search_root: str = "") -> str:
    return resolve_repo_path(path, repo_root, search_root=search_root)


def _add_span(step: dict[str, Any], file_path: str, start: int, end: int) -> None:
    if not file_path:
        return
    step["files"].append(file_path)
    step["spans"].setdefault(file_path, []).append({"start": max(1, int(start)), "end": max(1, int(end))})


def _iter_match_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[Any] = []
    for key in _MATCH_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(value)
    # trace_call_chain / analyze_impact return call paths, each one a list of nodes.
    paths = payload.get("paths")
    if isinstance(paths, list):
        for path in paths:
            nodes = path.get("nodes") if isinstance(path, dict) else path
            if isinstance(nodes, list):
                items.extend(nodes)
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            out.append(item)
    return out


def extract_step(
    tool_name: str,
    tool_args: Any,
    tool_result: Any,
    *,
    repo_root: str = "",
) -> dict[str, Any] | None:
    """Turn one tool call into a ContextBench step, or None if it has no files."""
    name = (tool_name or "").strip()
    args = _as_args(tool_args)
    payload = _as_dict(tool_result)
    step: dict[str, Any] = {"files": [], "spans": {}, "symbols": {}, "tool": name}

    if name in _READ_TOOLS:
        rel = _repo_relpath(str(args.get("file_path") or ""), repo_root)
        offset = int(args.get("offset") or 0)
        raw_limit = args.get("limit")
        # Official MiniSWE / SWE-agent: no explicit range → file only, no span.
        # Inventing a 1999-line window inflates span Coverage and kills Precision.
        if raw_limit is None:
            if rel:
                step["files"].append(rel)
        else:
            start = offset + 1
            end = start + int(raw_limit) - 1
            _add_span(step, rel, start, end)
    elif name in {"read_code", "read_symbol"}:
        rel = _repo_relpath(
            str(args.get("path") or args.get("file") or payload.get("file") or ""),
            repo_root,
        )
        start = int(args.get("start_line") or payload.get("start_line") or 1)
        raw_end = args.get("end_line") or payload.get("end_line")
        end = int(raw_end) if raw_end is not None else start
        _add_span(step, rel, start, end)
        symbol = str(payload.get("name") or payload.get("symbol_id") or "")
        if rel and symbol:
            step["symbols"].setdefault(rel, []).append(symbol)
    elif name == "grep":
        search_root = str(args.get("path") or args.get("file_path") or "")
        content = str(payload.get("content") or payload.get("stdout") or "")
        filenames = payload.get("filenames") or []
        if isinstance(filenames, list):
            for item in filenames:
                rel = _repo_relpath(str(item), repo_root, search_root=search_root)
                if rel:
                    step["files"].append(rel)
        for line in content.splitlines():
            match = _GREP_LINE_RE.match(line)
            if not match:
                continue
            rel = _repo_relpath(match.group(1), repo_root, search_root=search_root)
            line_no = int(match.group(2))
            _add_span(step, rel, line_no, line_no)
    elif name == "select_code_context":
        status = str(payload.get("status") or "COMPLETE").upper()
        if status != "COMPLETE":
            return None
        rel = _repo_relpath(str(args.get("file") or payload.get("file") or ""), repo_root)
        symbol_id = str(args.get("symbol_id") or payload.get("symbol_id") or payload.get("name") or "")
        start = int(payload.get("start_line") or payload.get("start") or 1)
        end = int(payload.get("end_line") or payload.get("end") or start)
        _add_span(step, rel, start, end)
        if rel and symbol_id:
            step["symbols"].setdefault(rel, []).append(symbol_id)
    elif name in _GRAPH_TOOLS:
        for item in _iter_match_dicts(payload):
            rel = _repo_relpath(str(item.get("file") or ""), repo_root)
            start = int(item.get("start_line") or item.get("start") or 1)
            end = int(item.get("end_line") or item.get("end") or start)
            _add_span(step, rel, start, end)
            symbol = str(item.get("name") or item.get("symbol_id") or "")
            if rel and symbol:
                step["symbols"].setdefault(rel, []).append(symbol)
    elif name in _EDIT_TOOLS:
        rel = _repo_relpath(str(args.get("file_path") or ""), repo_root)
        if rel:
            step["files"].append(rel)
    elif name == "bash":
        command = str(args.get("command") or payload.get("command") or "")
        for view in _extract_views_from_bash(command):
            rel = _repo_relpath(str(view.get("file") or ""), repo_root)
            if not rel:
                continue
            start = view.get("start_line")
            end = view.get("end_line")
            if start is not None and end is not None:
                _add_span(step, rel, int(start), int(end))
            else:
                step["files"].append(rel)
    else:
        return None

    step["files"] = sorted(set(step["files"]))
    if not step["files"] and not step["spans"]:
        return None
    return step


def to_traj_data(
    steps: list[dict[str, Any]],
    *,
    scored_tools: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Aggregate recorded steps into ContextBench ``traj_data``.

    When *scored_tools* is set, only those tools enter pred_files / pred_spans.
    """
    pred_files: list[str] = []
    pred_spans: dict[str, list[dict[str, int]]] = {}
    pred_symbols: dict[str, list[str]] = {}
    pred_steps: list[dict[str, Any]] = []
    seen_span: set[tuple[str, int, int]] = set()
    if scored_tools is not None:
        steps = [raw for raw in steps if raw.get("tool") in scored_tools]

    for raw in steps:
        files = list(raw.get("files") or [])
        spans = dict(raw.get("spans") or {})
        symbols = dict(raw.get("symbols") or {})
        pred_steps.append({"files": files, "spans": spans, "symbols": symbols})
        for path in files:
            if path not in pred_files:
                pred_files.append(path)
        for path, items in spans.items():
            bucket = pred_spans.setdefault(path, [])
            for span in items:
                start = int(span.get("start") or 1)
                end = int(span.get("end") or start)
                key = (path, start, end)
                if key in seen_span:
                    continue
                seen_span.add(key)
                bucket.append({"start": start, "end": end})
        for path, names in symbols.items():
            pred_symbols.setdefault(path, [])
            for name in names:
                if name not in pred_symbols[path]:
                    pred_symbols[path].append(name)

    return {
        "pred_steps": pred_steps,
        "pred_files": pred_files,
        "pred_spans": pred_spans,
        "pred_symbols": pred_symbols,
    }


def _extract_tag_blocks(text: str, tag: str) -> list[str]:
    if not text:
        return []
    pattern = rf"<{re.escape(tag)}>\s*([\s\S]*?)\s*</{re.escape(tag)}>"
    return [match.group(1) for match in re.finditer(pattern, text, flags=re.IGNORECASE)]


def _parse_file_lines_pairs(block: str) -> dict[str, list[dict[str, int]]]:
    spans: dict[str, list[dict[str, int]]] = {}
    current_file = ""
    for raw in (block or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("file:"):
            current_file = _strip_rel(line.split(":", 1)[1])
            continue
        if line.lower().startswith("lines:") and current_file:
            match = re.match(r"(\d+)\s*-\s*(\d+)", line.split(":", 1)[1].strip())
            if not match:
                continue
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                start, end = end, start
            spans.setdefault(current_file, []).append({"start": start, "end": end})
    return spans


def parse_patch_context(text: str) -> dict[str, list[dict[str, int]]]:
    """Official MiniSWE final: last ``<PATCH_CONTEXT>`` block only.

    Multiple blocks in one string are not merged. Empty → no utilized context.
    """
    blocks = _extract_tag_blocks(text, "PATCH_CONTEXT")
    if not blocks:
        return {}
    return _parse_file_lines_pairs(blocks[-1])


def last_patch_context_from_texts(texts: list[str]) -> dict[str, list[dict[str, int]]]:
    """Scan every message and keep the last ``<PATCH_CONTEXT>`` block."""
    last = ""
    for text in texts:
        blocks = _extract_tag_blocks(str(text or ""), "PATCH_CONTEXT")
        if blocks:
            last = blocks[-1]
    return _parse_file_lines_pairs(last) if last else {}


def _extract_views_from_bash(command: str) -> list[dict[str, Any]]:
    """Official MiniSWE bash fallback: file only unless a line range is explicit."""
    cmd = (command or "").strip()
    if not cmd or any(token in cmd for token in ("sed -i", "echo ", "mkdir", "rm ", "git add", "git commit")):
        return []
    views: list[dict[str, Any]] = []
    for chunk in re.split(r"\s*(?:&&|\|\||;)\s*", cmd):
        piece = chunk.strip()
        if not piece:
            continue
        match = re.search(r"nl\s+[^|]+\s+([^\s|]+)\s*\|\s*sed\s+-n\s+['\"]?(\d+),(\d+)p", piece)
        if match:
            views.append({"file": match.group(1).strip("'\""), "start_line": int(match.group(2)), "end_line": int(match.group(3))})
            continue
        match = re.search(r"sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s+([^\s&|>;<]+)", piece)
        if match:
            views.append({"file": match.group(3).strip("'\""), "start_line": int(match.group(1)), "end_line": int(match.group(2))})
            continue
        match = re.search(r"\bhead\s+-n\s+(\d+)\s+([^\s&|>]+)", piece)
        if match:
            views.append({"file": match.group(2).strip("'\""), "start_line": 1, "end_line": int(match.group(1))})
            continue
        match = re.search(r"\b(?:cat|less|more)\s+([^\s&|>]+)", piece)
        if match:
            views.append({"file": match.group(1).strip("'\"")})
            continue
        match = re.search(
            r"\bgrep\s+.*?\s+([^\s&|>]+\.(?:py|js|java|go|rs|c|cpp|h|hpp|ts|tsx|jsx|rb|php|cs))\b",
            piece,
        )
        if match:
            views.append({"file": match.group(1).strip("'\"")})
    return views


def _spans_to_steps(
    spans: dict[str, list[dict[str, int]]], *, tool: str
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for path, items in spans.items():
        if not path or not items:
            continue
        steps.append(
            {
                "files": [path],
                "spans": {path: list(items)},
                "symbols": {},
                "tool": tool,
            }
        )
    return steps


def to_contextbench_traj_data(
    steps: list[dict[str, Any]],
    *,
    utilized_spans: dict[str, list[dict[str, int]]] | None = None,
    fallback_explored: bool = False,
) -> dict[str, Any]:
    """Official ContextBench grain: explored reads vs declared utilized context.

    Search hit lists stay in ``retrieved_hits`` and do not enter ``pred_files``.
    Official MiniSWE never backfills final from explored: no ``<PATCH_CONTEXT>``
    and no ``submit_code_context`` → empty ``pred_files`` (``no_context_extracted``).
    """
    explored = [raw for raw in steps if raw.get("tool") in EXPLORED_TOOLS]
    utilized = [raw for raw in steps if raw.get("tool") in UTILIZED_TOOLS]
    hits = [raw for raw in steps if raw.get("tool") in HIT_TOOLS]
    if utilized_spans:
        utilized = _spans_to_steps(utilized_spans, tool="patch_context")
    utilized_source = "declared" if utilized else "empty"
    if not utilized and fallback_explored:
        utilized = list(explored)
        utilized_source = "explored_fallback" if explored else "empty"
    official = to_traj_data(explored)
    final = to_traj_data(utilized)
    official["pred_files"] = final["pred_files"]
    official["pred_spans"] = final["pred_spans"]
    official["pred_symbols"] = final["pred_symbols"]
    official["utilized_source"] = utilized_source
    official["retrieved_hits"] = to_traj_data(hits)["pred_steps"]
    return official


def _hint_unresolved(path: str, resolved_dirs: set[str], existing: frozenset[str]) -> str:
    """If siblings in this step share a directory that contains *path*, use it."""
    raw = _strip_rel(path).lstrip("/")
    if not raw or not resolved_dirs:
        return ""
    base = os.path.basename(raw)
    hits = []
    for directory in resolved_dirs:
        cand = f"{directory}/{base}" if directory else base
        if cand in existing and cand not in hits:
            hits.append(cand)
        if raw != base:
            cand = f"{directory}/{raw}" if directory else raw
            if cand in existing and cand not in hits:
                hits.append(cand)
    return hits[0] if len(hits) == 1 else ""


def normalize_traj_data(traj_data: dict[str, Any], repo_root: str) -> dict[str, Any]:
    """Rewrite already-recorded traj_data so every path exists under repo_root."""
    existing, _by_name = _repo_file_index(os.path.realpath(repo_root)) if repo_root else (frozenset(), {})
    steps = traj_data.get("pred_steps") or []
    rewritten: list[dict[str, Any]] = []
    for raw in steps:
        files: list[str] = []
        spans: dict[str, list[dict[str, int]]] = {}
        symbols: dict[str, list[str]] = {}
        unresolved_files: list[str] = []
        unresolved_spans: list[tuple[str, list]] = []
        unresolved_symbols: list[tuple[str, list]] = []
        for path in raw.get("files") or []:
            rel = resolve_repo_path(str(path), repo_root)
            if rel:
                if rel not in files:
                    files.append(rel)
            else:
                unresolved_files.append(str(path))
        for path, items in (raw.get("spans") or {}).items():
            rel = resolve_repo_path(str(path), repo_root)
            if rel:
                bucket = spans.setdefault(rel, [])
                for span in items or []:
                    start = int((span or {}).get("start") or 1)
                    end = int((span or {}).get("end") or start)
                    bucket.append({"start": start, "end": end})
            else:
                unresolved_spans.append((str(path), items or []))
        for path, names in (raw.get("symbols") or {}).items():
            rel = resolve_repo_path(str(path), repo_root)
            if rel:
                symbols.setdefault(rel, [])
                for name in names or []:
                    if name not in symbols[rel]:
                        symbols[rel].append(name)
            else:
                unresolved_symbols.append((str(path), names or []))
        resolved_dirs = {os.path.dirname(rel).replace("\\", "/") for rel in files}
        for path in unresolved_files:
            rel = _hint_unresolved(path, resolved_dirs, existing)
            if rel and rel not in files:
                files.append(rel)
        for path, items in unresolved_spans:
            rel = _hint_unresolved(path, resolved_dirs, existing)
            if not rel:
                continue
            bucket = spans.setdefault(rel, [])
            for span in items:
                start = int((span or {}).get("start") or 1)
                end = int((span or {}).get("end") or start)
                bucket.append({"start": start, "end": end})
        for path, names in unresolved_symbols:
            rel = _hint_unresolved(path, resolved_dirs, existing)
            if not rel:
                continue
            symbols.setdefault(rel, [])
            for name in names:
                if name not in symbols[rel]:
                    symbols[rel].append(name)
        rewritten.append({"files": sorted(files), "spans": spans, "symbols": symbols})
    normalized_steps = to_traj_data(rewritten)
    declared_final = "pred_files" in traj_data or "pred_spans" in traj_data
    final_files = []
    final_spans: dict[str, list[dict[str, int]]] = {}
    for path in traj_data.get("pred_files") or []:
        rel = resolve_repo_path(str(path), repo_root) or _hint_unresolved(
            str(path),
            {os.path.dirname(item).replace("\\", "/") for item in normalized_steps["pred_files"]},
            existing,
        )
        if rel and rel not in final_files:
            final_files.append(rel)
    for path, items in (traj_data.get("pred_spans") or {}).items():
        rel = resolve_repo_path(str(path), repo_root) or _hint_unresolved(
            str(path),
            {os.path.dirname(item).replace("\\", "/") for item in (final_files or normalized_steps["pred_files"])},
            existing,
        )
        if not rel:
            continue
        bucket = final_spans.setdefault(rel, [])
        for span in items or []:
            start = int((span or {}).get("start") or 1)
            end = int((span or {}).get("end") or start)
            bucket.append({"start": start, "end": end})
    # Official final is the declared set only. Do not silently refill from
    # explored steps when the caller already set pred_files / pred_spans
    # (including empty). Legacy records without those keys still use steps.
    if declared_final:
        normalized_steps["pred_files"] = final_files
        normalized_steps["pred_spans"] = final_spans
    elif final_files or final_spans:
        normalized_steps["pred_files"] = final_files or list(final_spans)
        normalized_steps["pred_spans"] = final_spans
    for key in ("utilized_source", "retrieved_hits", "pred_symbols"):
        if key in traj_data and key not in normalized_steps:
            normalized_steps[key] = traj_data[key]
    if "pred_symbols" in traj_data:
        symbols: dict[str, list[str]] = {}
        for path, names in (traj_data.get("pred_symbols") or {}).items():
            rel = resolve_repo_path(str(path), repo_root)
            if not rel:
                continue
            symbols.setdefault(rel, [])
            for name in names or []:
                if name not in symbols[rel]:
                    symbols[rel].append(name)
        normalized_steps["pred_symbols"] = symbols
    return normalized_steps


def contextbench_record(raw: dict[str, Any], *, repo_root: str = "") -> dict[str, Any]:
    """Keep only the fields ``contextbench.evaluate`` reads."""
    traj = raw.get("traj_data") if isinstance(raw.get("traj_data"), dict) else {}
    if repo_root:
        traj = normalize_traj_data(traj, repo_root)
    else:
        traj = {
            "pred_steps": list(traj.get("pred_steps") or []),
            "pred_files": list(traj.get("pred_files") or []),
            "pred_spans": dict(traj.get("pred_spans") or {}),
            "pred_symbols": dict(traj.get("pred_symbols") or {}),
        }
    out = {
        "instance_id": str(raw.get("instance_id") or raw.get("original_inst_id") or "").strip(),
        "traj_data": {
            "pred_steps": list(traj.get("pred_steps") or []),
            "pred_files": list(traj.get("pred_files") or []),
            "pred_spans": dict(traj.get("pred_spans") or {}),
            "pred_symbols": dict(traj.get("pred_symbols") or {}),
        },
        # Empty patch is required by the official schema. evaluate.py will
        # then fall back to gold ``patch`` for EditLoc — run_evaluate.py
        # drops that metric so we never report gold-vs-gold.
        "model_patch": raw.get("model_patch") or "",
    }
    if isinstance(raw.get("usage"), dict) and raw["usage"]:
        out["usage"] = raw["usage"]
    return out


@dataclass
class TrajectoryRecorder:
    repo_root: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    mode: str = MODE_CONTEXTBENCH
    utilized_from_text: dict[str, list[dict[str, int]]] | None = None
    assistant_texts: list[str] = field(default_factory=list)

    def record(self, tool_name: str, tool_args: Any, tool_result: Any) -> None:
        step = extract_step(tool_name, tool_args, tool_result, repo_root=self.repo_root)
        if step is not None:
            self.steps.append(step)

    def apply_output_text(self, text: str) -> None:
        self.apply_texts([text])

    def apply_texts(self, texts: list[str]) -> None:
        """Scan every message; official final is the last PATCH_CONTEXT only."""
        for text in texts:
            if text:
                self.assistant_texts.append(str(text))
        spans = last_patch_context_from_texts(self.assistant_texts)
        self.utilized_from_text = spans or None

    def traj_data(self) -> dict[str, Any]:
        return to_contextbench_traj_data(
            self.steps,
            utilized_spans=self.utilized_from_text,
            fallback_explored=False,
        )

    def make_rail(self) -> "TrajectoryCaptureRail":
        return TrajectoryCaptureRail(self)


class TrajectoryCaptureRail(DeepAgentRail):
    """Append ContextBench steps after each tool call."""

    priority = 10

    def __init__(self, recorder: TrajectoryRecorder) -> None:
        super().__init__()
        self.recorder = recorder

    async def after_tool_call(self, ctx: Any) -> None:
        inputs = getattr(ctx, "inputs", None)
        tool_name = getattr(inputs, "tool_name", "") or ""
        self.recorder.record(
            tool_name,
            getattr(inputs, "tool_args", None),
            getattr(inputs, "tool_result", None),
        )
