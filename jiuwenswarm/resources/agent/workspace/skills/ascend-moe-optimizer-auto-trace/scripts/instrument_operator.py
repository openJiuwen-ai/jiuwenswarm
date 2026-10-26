#!/usr/bin/env python3
"""
Apply TRACE_POINT instrumentation to operator source files with function-level granularity.
Rules:
- Root label defaults to `processing`
- Max depth defaults to 5
- Idempotent: skip if function already contains MoeTracing/TRACE_POINT
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import re
from dataclasses import dataclass
from typing import List, Tuple

logger = logging.getLogger(__name__)

FUNC_RE = re.compile(
    r"(^\s*(?:template\s*<[^;{>]+>\s*)?(?:[\w:<>~*&\s]+)\s+([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{)",
    re.MULTILINE,
)


@dataclass
class FunctionBlock:
    name: str
    start: int
    open_brace: int
    close_brace: int
    indent: str


def find_functions(text: str) -> List[FunctionBlock]:
    out: List[FunctionBlock] = []
    for m in FUNC_RE.finditer(text):
        sig = m.group(1)
        name = m.group(2)
        open_idx = m.end() - 1
        close_idx = find_match_brace(text, open_idx)
        indent = re.match(r"^\s*", sig).group(0)
        out.append(FunctionBlock(name=name, start=m.start(), open_brace=open_idx, close_brace=close_idx, indent=indent))
    return out


def find_match_brace(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def normalize_label(func_name: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", func_name).lower()
    s = s.replace("_", "-")
    return s


def should_skip(body: str) -> bool:
    return "MoeTracing(" in body or "TRACE_POINT(" in body


def apply_file(path: pathlib.Path, root_label: str, dry_run: bool) -> Tuple[int, str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    funcs = find_functions(text)
    if not funcs:
        return 0, f"skip {path} (no function found)"

    edits = []
    for fn in funcs:
        body = text[fn.open_brace:fn.close_brace + 1]
        if should_skip(body):
            continue
        label = root_label if fn.name.lower() == "process" else normalize_label(fn.name)
        begin = f'\n{fn.indent}    MoeTracing(TRACE_POINT("{label}", "B"));\n'
        end = f'\n{fn.indent}    MoeTracing(TRACE_POINT("{label}", "E"));\n'
        edits.append((fn.open_brace + 1, begin))
        edits.append((fn.close_brace, end))

    if not edits:
        return 0, f"skip {path} (already instrumented)"

    edits.sort(key=lambda x: x[0], reverse=True)
    for pos, content in edits:
        text = text[:pos] + content + text[pos:]

    if not dry_run:
        path.write_text(text, encoding="utf-8")
        return len(edits) // 2, f"write {path}"
    return len(edits) // 2, f"plan  {path}"


def iter_targets(root: pathlib.Path) -> List[pathlib.Path]:
    if root.is_file():
        return [root]
    exts = {".h", ".hpp", ".c", ".cc", ".cpp"}
    return [p for p in root.rglob("*") if p.is_file() and p.suffix in exts]


def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Apply TRACE_POINT instrumentation to operator source files.")
    parser.add_argument("--target", required=True, help="target source file or directory")
    parser.add_argument("--root-label", default="processing", help="root label name")
    parser.add_argument("--dry-run", action="store_true", help="preview only")
    args = parser.parse_args()

    target = pathlib.Path(args.target).resolve()
    files = iter_targets(target)
    total = 0
    for f in files:
        c, msg = apply_file(f, args.root_label, args.dry_run)
        total += c
        logger.info("%s", msg)
    logger.info("instrumented functions: %d", total)


if __name__ == "__main__":
    main()
