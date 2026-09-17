#!/usr/bin/env python3
"""
Deploy the trace toolchain Python scripts into a target build directory (flat layout).

Typical uses:
  - **Other repos** (no committed copies): copy *from* this skill directory *into* e.g. ``build/cam/comm_operator``.
  - **UMDK**: keep ``umdk/build/cam/comm_operator/`` in sync with this skill's
    ``scripts/`` directory (same folder as ``SKILL.md``) — from repo root, e.g.::

      python3 \\
        jiuwenswarm/resources/agent/workspace/skills/ \\
        ascend-moe-optimizer-auto-trace/scripts/bootstrap_trace_toolchain.py \\
        --build-dir umdk/build/cam/comm_operator

  Or, after this file is committed under ``umdk/build/cam/comm_operator/``::

      python3 umdk/build/cam/comm_operator/bootstrap_trace_toolchain.py \\
        --build-dir umdk/build/cam/comm_operator

Idempotent: existing files are left unchanged unless ``--force``. Use ``--dry-run`` to preview.

``TOOLCHAIN_FILES`` must stay aligned with ``SKILL.md`` and
``patch_build_pipeline.py`` / ``apply_trace_scaffold.sh`` expectations.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path


logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent

TOOLCHAIN_FILES = [
    "trace_preprocessor.py",
    "trace_utils.py",
    "trace_save.py",
    "trace_collector.py",
    "validate_trace_points.py",
    "check_compile_safety.py",
    "inspect_rank_pt.py",
]


def deploy_file(src: Path, dst: Path, force: bool, dry_run: bool) -> tuple[str, bool]:
    """
    Returns (message, wrote) where wrote is True if a copy would be / was performed.
    """
    if not src.is_file():
        return (f"MISS {src}", False)
    if src.resolve() == dst.resolve():
        return (f"same {dst}", False)
    if dst.exists() and not force:
        return (f"skip {dst}", False)
    if dry_run:
        return (f"would write {dst}", True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return (f"write {dst}", True)


def main() -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Copy trace toolchain .py files into --build-dir (same filenames).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--build-dir",
        required=True,
        type=Path,
        help="Target directory (e.g. umdk/build/cam/comm_operator)",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing files in build-dir")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without copying")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print TOOLCHAIN_FILES and exit",
    )
    args = parser.parse_args()

    if args.list:
        for f in TOOLCHAIN_FILES:
            logger.info("%s", f)
        return 0

    base = args.build_dir.resolve()
    missing_src = 0
    wrote = 0
    same = 0
    for fname in TOOLCHAIN_FILES:
        src = SCRIPT_DIR / fname
        dst = base / fname
        msg, did = deploy_file(src, dst, args.force, args.dry_run)
        logger.info("%s", msg)
        if msg.startswith("MISS"):
            missing_src += 1
        if msg.startswith("same"):
            same += 1
        if did:
            wrote += 1

    if missing_src:
        logger.error(
            "ERROR: %s source file(s) missing under %s",
            missing_src,
            SCRIPT_DIR,
        )
        return 1
    if args.dry_run and wrote == 0 and same < len(TOOLCHAIN_FILES):
        logger.info("(dry-run: nothing to do — targets already match or skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
