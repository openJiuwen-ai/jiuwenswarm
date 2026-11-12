#!/usr/bin/env python3
"""
Patch compile/build script to inject trace preprocessor command.
Idempotent by marker comments.

Anchor strategy: tries multiple patterns to find where sources have been
copied into the build directory but before the actual compilation starts.
Falls back to inserting right before the first './build.sh' invocation.
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

START_MARK = "# TRACE_PREPROCESSOR_HOOK_START"
END_MARK = "# TRACE_PREPROCESSOR_HOOK_END"

ANCHOR_PATTERNS = [
    re.compile(r'[Cc]opy_?[Oo]ps\s+.*ascend_kernels.*'),
    re.compile(r'cp\s+-rf\s+.*ascend_kernels/pregen\b.*'),
    re.compile(r'modify_func_cmake'),
]

FALLBACK_PATTERN = re.compile(r'^\s*\./build\.sh', re.MULTILINE)


def inject_hook(script_text: str, cmd: str) -> str:
    if START_MARK in script_text and END_MARK in script_text:
        return script_text

    insert_at = -1
    for pattern in ANCHOR_PATTERNS:
        m = pattern.search(script_text)
        if m:
            nl = script_text.find("\n", m.end())
            if nl >= 0:
                insert_at = nl
    if insert_at < 0:
        m = FALLBACK_PATTERN.search(script_text)
        if m:
            insert_at = m.start() - 1
    if insert_at < 0:
        raise RuntimeError(
            "Cannot find anchor in compile script. "
            "Looked for copy_ops/CopyOps, modify_func_cmake, or ./build.sh. "
            "Please insert the hook manually before the build step."
        )

    hook = (
        f"\n    {START_MARK}\n"
        f"    {cmd}\n"
        f"    {END_MARK}\n"
    )
    return script_text[:insert_at + 1] + hook + script_text[insert_at + 1:]


def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Patch compile script with trace preprocessor hook.")
    parser.add_argument(
        "--compile-script",
        required=True,
        help="path to compile script, e.g. build/.../compile_ascend_proj.sh",
    )
    parser.add_argument("--preprocessor-cmd", required=True, help="command line to run preprocessor")
    args = parser.parse_args()

    path = Path(args.compile_script)
    text = path.read_text(encoding="utf-8")
    patched = inject_hook(text, args.preprocessor_cmd)
    if patched == text:
        logger.info("no change (hook already exists)")
        return
    path.write_text(patched, encoding="utf-8")
    logger.info("patched: %s", path)


if __name__ == "__main__":
    main()
