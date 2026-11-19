#!/usr/bin/env python3
"""
Generate a function-level instrumentation plan (max depth=5) for Ascend operator code.
This script does not modify source files.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

FUNC_DEF_RE = re.compile(
    r"^\s*(?:template\s*<[^;{>]+>\s*)?(?:[\w:<>~*&\s]+)\s+([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{",
    re.MULTILINE,
)
CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "static_cast", "reinterpret_cast"}
HOT_WORDS = ("wait", "sync", "send", "recv", "copy", "quant", "dequant")
MAX_DEPTH = 5


@dataclass
class FuncInfo:
    name: str
    file: str
    start_line: int
    body: str
    calls: Set[str] = field(default_factory=set)


def iter_code_files(root: pathlib.Path) -> List[pathlib.Path]:
    exts = {".h", ".hpp", ".c", ".cc", ".cpp"}
    return [p for p in root.rglob("*") if p.suffix in exts and p.is_file()]


def extract_functions(path: pathlib.Path) -> List[FuncInfo]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    matches = list(FUNC_DEF_RE.finditer(text))
    funcs: List[FuncInfo] = []

    for m in matches:
        name = m.group(1)
        start = m.start()
        line = text.count("\n", 0, start) + 1
        body, _ = extract_block(text, m.end() - 1)
        calls = set(c for c in CALL_RE.findall(body) if c not in KEYWORDS)
        funcs.append(FuncInfo(name=name, file=str(path), start_line=line, body=body, calls=calls))
    return funcs


def extract_block(text: str, open_brace_idx: int) -> Tuple[str, int]:
    depth = 0
    i = open_brace_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_idx:i + 1], i
        i += 1
    return text[open_brace_idx:], n - 1


def build_call_graph(funcs: List[FuncInfo]) -> Dict[str, Set[str]]:
    known = {f.name for f in funcs}
    graph: Dict[str, Set[str]] = defaultdict(set)
    for f in funcs:
        for c in f.calls:
            if c in known and c != f.name:
                graph[f.name].add(c)
    return graph


def normalize_label(func_name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", func_name).lower()
    s = s.replace("_", "-")
    return s


def build_plan(entry: str, graph: Dict[str, Set[str]]) -> Dict:
    root = {"label": "processing", "function": entry, "depth": 1, "children": [], "merged": False}
    q = deque([(root, entry, 1)])
    seen_edges = set()

    while q:
        node, fn, depth = q.popleft()
        children = sorted(graph.get(fn, []))
        for c in children:
            edge = (fn, c, depth)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)

            label = normalize_label(c)
            child = {"label": label, "function": c, "depth": min(depth + 1, MAX_DEPTH), "children": [], "merged": False}

            if depth + 1 > MAX_DEPTH:
                child["merged"] = True
                child["merge_reason"] = "depth_limit"
                node["children"].append(child)
                continue

            if is_low_value_helper(c):
                child["merged"] = True
                child["merge_reason"] = "helper_merge"
                node["children"].append(child)
                continue

            node["children"].append(child)
            q.append((child, c, depth + 1))

    return root


def is_low_value_helper(name: str) -> bool:
    n = name.lower()
    if any(w in n for w in HOT_WORDS):
        return False
    return n.startswith(("get", "set", "check", "calc", "init")) and len(n) <= 18


def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Generate instrumentation plan for operator code.")
    parser.add_argument("--root", required=True, help="Operator source root directory")
    parser.add_argument("--entry", required=True, help="Entry function name to build plan from")
    parser.add_argument("-o", "--output", default="instrumentation_plan.json", help="Output JSON file")
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    funcs: List[FuncInfo] = []
    for p in iter_code_files(root):
        funcs.extend(extract_functions(p))

    graph = build_call_graph(funcs)
    if args.entry not in {f.name for f in funcs}:
        raise SystemExit(f"entry function not found: {args.entry}")

    plan = build_plan(args.entry, graph)
    output = {
        "root": str(root),
        "entry": args.entry,
        "max_depth": MAX_DEPTH,
        "plan": plan,
    }

    pathlib.Path(args.output).write_text(json.dumps(output, indent=2), encoding="utf-8")
    logger.info("plan saved: %s", args.output)


if __name__ == "__main__":
    main()
