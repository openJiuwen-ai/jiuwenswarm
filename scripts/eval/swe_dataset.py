# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Load SWE-bench rows for the shared coding runner.

The agent only sees ``problem_statement``. Official submissions do not use
``hints_text`` (issue comments). Pass ``include_hints=True`` only for an
explicit ablation. Gold ``patch`` / ``test_patch`` / ``FAIL_TO_PASS`` stay
off the prompt.
"""

from __future__ import annotations

from typing import Any, Iterable

BENCHMARK_CONTEXTBENCH = "contextbench"
BENCHMARK_SWE = "swe"

SWE_SPLIT_ALIASES = {
    "verified": "SWE-bench/SWE-bench_Verified",
    "lite": "SWE-bench/SWE-bench_Lite",
    "test": "SWE-bench/SWE-bench",
    "full": "SWE-bench/SWE-bench",
}

# Official leaderboard denominators. ``test`` is the full SWE-bench set,
# not the Verified HF ``split=test`` slice (that slice is ``verified``).
OFFICIAL_SPLIT_N = {
    "verified": 500,
    "lite": 300,
    "test": 2294,
    "full": 2294,
}

# Official scoring fields. Never copy these into the agent query.
_LEAK_KEYS = ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS")


def resolve_benchmark(raw: str | None) -> str:
    text = (raw or BENCHMARK_CONTEXTBENCH).strip().lower()
    if text in {BENCHMARK_CONTEXTBENCH, BENCHMARK_SWE}:
        return text
    raise ValueError(f"unknown benchmark {raw!r}; expected contextbench or swe")


def resolve_swe_dataset(name: str | None, *, split: str = "verified") -> str:
    text = (name or split or "verified").strip()
    return SWE_SPLIT_ALIASES.get(text.lower(), text)


def official_split_n(split: str | None) -> int | None:
    """Leaderboard denominator, or None when the alias is unknown."""
    return OFFICIAL_SPLIT_N.get((split or "").strip().lower())


def swe_split_warning(split: str | None) -> str | None:
    """Human warning when ``test`` is mistaken for Verified's HF test slice."""
    key = (split or "").strip().lower()
    if key in {"test", "full"}:
        return (
            "--swe-split test|full is SWE-bench (2294), not Verified. "
            "Verified 500 is --swe-split verified (HF dataset split=test)."
        )
    return None


def resolve_include_hints(*, hints: bool = False, no_hints: bool = False) -> bool:
    """Official default is no hints. ``--hints`` opts in; ``--no-hints`` is a no-op."""
    if hints and no_hints:
        raise ValueError("pass only one of --hints / --no-hints")
    return bool(hints)


def swe_issue_text(row: dict[str, Any], *, include_hints: bool = False) -> str:
    """Issue the agent is allowed to see. Official default omits ``hints_text``."""
    for key in ("problem_statement", "issue", "query"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    else:
        text = ""
    if include_hints:
        hints = str(row.get("hints_text") or "").strip()
        if hints:
            text = f"{text}\n\n=== HINTS ===\n{hints}" if text else hints
    return text


def assert_no_gold_leak(query: str, row: dict[str, Any]) -> None:
    """Guardrail: gold patch / F2P lists must not appear in the prompt."""
    blob = query or ""
    gold = str(row.get("patch") or "").strip()
    if gold and len(gold) > 40 and gold in blob:
        raise RuntimeError("gold patch leaked into SWE prompt")
    tests = str(row.get("FAIL_TO_PASS") or "").strip()
    if tests and len(tests) > 20 and tests in blob:
        raise RuntimeError("FAIL_TO_PASS leaked into SWE prompt")


def filter_swe_rows(
    rows: list[dict[str, Any]],
    *,
    wanted: Iterable[str] | None = None,
    limit: int = 0,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ids = {item.strip() for item in (wanted or []) if str(item).strip()}
    if ids:
        rows = [
            row
            for row in rows
            if str(row.get("instance_id") or "").strip() in ids
            or str(row.get("original_inst_id") or "").strip() in ids
        ]
    skip = max(0, int(offset or 0))
    if skip:
        rows = rows[skip:]
    if limit > 0:
        rows = rows[:limit]
    return rows


def load_swe_rows(
    *,
    split: str = "verified",
    dataset: str | None = None,
    limit: int = 0,
    offset: int = 0,
    instance_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Load the HF (or local) SWE split. Requires the ``datasets`` package."""
    name = resolve_swe_dataset(dataset, split=split)
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "Install datasets to load SWE-bench: uv run --with datasets"
        ) from exc
    try:
        table = load_dataset(name, split="test")
    except Exception as exc:
        raise SystemExit(f"failed to load SWE dataset {name!r}: {exc}") from exc
    rows = [dict(item) for item in table]
    return filter_swe_rows(rows, wanted=instance_ids, limit=limit, offset=offset)
