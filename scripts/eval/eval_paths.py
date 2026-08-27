# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Portable ContextBench checkout / gold parquet resolution.

Do not assume a machine-local folder such as ``reconstruct_tmp``. Testers set
``CONTEXTBENCH_ROOT`` (or ``--contextbench-root``), or clone ContextBench as a
sibling of this repository.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent
JIUWEN_ROOT = _EVAL_DIR.parents[1]
DEFAULT_OUTPUT = (
    JIUWEN_ROOT / "docs" / "ai" / "experiments-contextbench" / "runs" / "scratch-contextbench"
)
GOLD_PARQUET_NAME = "contextbench_verified.parquet"


def is_contextbench_checkout(root: Path) -> bool:
    """True when ``root`` can run ``python -m contextbench.evaluate``."""
    return (root / "contextbench" / "evaluate.py").is_file()


def _unique(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    ordered: list[Path] = []
    for raw in paths:
        try:
            key = str(raw.expanduser().resolve())
        except OSError:
            key = str(raw)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(raw)
    return ordered


def contextbench_root_candidates(
    *,
    explicit: Path | str | None = None,
    parquet: Path | str | None = None,
) -> list[Path]:
    """Places to look, first match wins.

    ``reconstruct_tmp/ContextBench`` is a last-resort local layout, not a
    required path for other testers.
    """
    parent = JIUWEN_ROOT.parent
    env = os.environ.get("CONTEXTBENCH_ROOT", "").strip()
    parquet_env = os.environ.get("CONTEXTBENCH_PARQUET", "").strip()
    found: list[Path] = []
    if explicit is not None and str(explicit).strip():
        found.append(Path(str(explicit)).expanduser())
    if env:
        found.append(Path(env).expanduser())
    for raw in (parquet, parquet_env):
        if raw is None or not str(raw).strip():
            continue
        parquet_path = Path(str(raw)).expanduser()
        found.append(parquet_path.parent.parent)
    found.extend(
        [
            parent / "ContextBench",
            JIUWEN_ROOT / "third_party" / "ContextBench",
            parent / "reconstruct_tmp" / "ContextBench",
        ]
    )
    return _unique(found)


def missing_contextbench_message(looked: list[Path]) -> str:
    listed = "\n".join(f"  {path}" for path in looked) or "  (none)"
    return (
        "ContextBench checkout not found. Clone it and pass --contextbench-root "
        "PATH or set CONTEXTBENCH_ROOT. A sibling ../ContextBench also works.\n"
        f"Looked in:\n{listed}"
    )


def resolve_contextbench_root(
    explicit: Path | str | None = None,
    *,
    parquet: Path | str | None = None,
) -> Path:
    """Return the ContextBench source tree, or exit with how to set it."""
    looked = contextbench_root_candidates(explicit=explicit, parquet=parquet)
    for candidate in looked:
        if is_contextbench_checkout(candidate):
            return candidate.expanduser().resolve()
    raise SystemExit(missing_contextbench_message(looked))


def gold_parquet_in(root: Path) -> Path:
    return root / "data" / GOLD_PARQUET_NAME


def resolve_contextbench_parquet(
    explicit: Path | str | None = None,
    *,
    root: Path | None = None,
) -> Path:
    """Gold parquet: ``--parquet``, ``CONTEXTBENCH_PARQUET``, then ``<root>/data/``."""
    env = os.environ.get("CONTEXTBENCH_PARQUET", "").strip()
    candidates: list[Path] = []
    if explicit is not None and str(explicit).strip():
        candidates.append(Path(str(explicit)).expanduser())
    if env:
        candidates.append(Path(env).expanduser())
    if root is not None:
        candidates.append(gold_parquet_in(root))
    for candidate in _unique(candidates):
        if candidate.is_file():
            return candidate.expanduser().resolve()
    hint = ""
    if root is None:
        hint = " Pass --contextbench-root or set CONTEXTBENCH_ROOT as well."
    raise SystemExit(
        f"{GOLD_PARQUET_NAME} not found. Pass --parquet PATH or set "
        f"CONTEXTBENCH_PARQUET.{hint}"
    )


def prepend_contextbench(root: Path) -> Path:
    """Put the checkout on ``sys.path`` so ``contextbench.*`` imports resolve."""
    resolved = root.expanduser().resolve()
    text = str(resolved)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)
    return resolved
