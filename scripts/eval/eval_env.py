# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Eval support: ContextBench paths, project ``.env``, and the pinned engine.

Path lookup: ``CONTEXTBENCH_ROOT`` / ``--contextbench-root`` / sibling
``../ContextBench``. Do not assume ``reconstruct_tmp``.

``.env``: eval is a plain ``python`` process. Keys already exported in the
shell (often from ``~/.zshrc``) sit in ``os.environ`` before this file runs.
``python-dotenv`` defaults to *not* overwriting those, so a stale shell
``API_KEY`` would beat ``jiuwenswarm/resources/.env``. ``load_eval_dotenv``
uses ``override=True`` so the project file wins. Product ``jiuwenswarm-start``
loads its own instance ``.env`` separately; this module is eval-only.

Engine: ``uv sync --extra code-graph`` installs
``openJiuwen/agent-core`` ``agent_os_code_search``. Eval does not prepend a
sibling checkout.
"""

from __future__ import annotations

import os
import subprocess
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


def describe_openjiuwen() -> str:
    try:
        import openjiuwen
    except ImportError as exc:
        return f"openjiuwen not importable: {exc}"
    location = getattr(openjiuwen, "__file__", "?")
    has_graph = True
    try:
        import openjiuwen.core.retrieval.code_graph  # noqa: F401
    except ImportError:
        has_graph = False
    return f"openjiuwen={location} code_graph={'yes' if has_graph else 'NO'}"


def git_identity(root: Path | None) -> str:
    """Return ``branch@sha`` for a checkout, or ``?`` if it is not a git repo."""
    if root is None or not Path(root).is_dir():
        return "?"
    try:
        branch = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        sha = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "?"
    return f"{branch}@{sha}" if branch and sha else "?"


def has_code_graph() -> bool:
    try:
        import openjiuwen.core.retrieval.code_graph  # noqa: F401
    except ImportError:
        return False
    return True


def describe_eval_pair() -> str:
    jiuwen = git_identity(JIUWEN_ROOT)
    graph = "yes" if has_code_graph() else "NO"
    return f"jiuwenswarm={jiuwen} {describe_openjiuwen()} code_graph={graph}"


def assert_engine_matches_branch() -> None:
    """Refuse a baseline jiuwenswarm run that still loads a Code Graph engine."""
    if os.environ.get("EVAL_ALLOW_ENGINE_MISMATCH", "").strip() in {"1", "true", "yes"}:
        return
    branch = git_identity(JIUWEN_ROOT).split("@", 1)[0]
    if "baseline" not in branch:
        return
    if not has_code_graph():
        return
    raise SystemExit(
        "eval pair mismatch: this jiuwenswarm checkout looks like the original "
        f"baseline ({git_identity(JIUWEN_ROOT)}) but openjiuwen still has Code Graph "
        f"({describe_openjiuwen()}).\n"
        "Override with EVAL_ALLOW_ENGINE_MISMATCH=1 only if you intend this."
    )


def require_code_graph() -> None:
    """Fail if the pinned openjiuwen does not expose Code Graph."""
    try:
        import openjiuwen.core.retrieval.code_graph  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "openjiuwen.core.retrieval.code_graph is missing.\n"
            f"  {describe_openjiuwen()}\n"
            f"  import error: {exc}\n"
            "Install the pinned engine:\n"
            "  uv sync --extra code-graph"
        ) from exc


_LOADED_FROM: Path | None = None


def resolve_dotenv_path() -> Path | None:
    for index, arg in enumerate(sys.argv):
        if arg == "--dotenv" and index + 1 < len(sys.argv):
            path = Path(sys.argv[index + 1]).expanduser().resolve()
            return path if path.is_file() else None
    env = os.environ.get("EVAL_DOTENV", "").strip()
    if env:
        path = Path(env).expanduser().resolve()
        return path if path.is_file() else None
    for candidate in (
        JIUWEN_ROOT / "jiuwenswarm" / "resources" / ".env",
        JIUWEN_ROOT / ".env",
    ):
        if candidate.is_file():
            return candidate.resolve()
    return None


def mask_secret(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= 8:
        return f"(set, len={len(text)})"
    return f"{text[:6]}...{text[-4:]} (len={len(text)})"


def describe_model_env() -> str:
    return (
        f"MODEL_PROVIDER={os.getenv('MODEL_PROVIDER', '')!s} "
        f"MODEL_NAME={os.getenv('MODEL_NAME', '')!s} "
        f"MODEL_MAX_TOKENS={os.getenv('MODEL_MAX_TOKENS', '')!s} "
        f"API_BASE={os.getenv('API_BASE', '')!s} "
        f"API_KEY={mask_secret(os.getenv('API_KEY', ''))}"
    )


def load_eval_dotenv(path: str | Path | None = None, *, verbose: bool = True) -> Path | None:
    """Load project .env with override=True so it beats already-exported shell keys."""
    global _LOADED_FROM
    if path is not None:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise SystemExit(f"dotenv file not found: {target}")
    else:
        target = resolve_dotenv_path()
    if target is None:
        if verbose and _LOADED_FROM is None:
            print("dotenv: (none) using process env", file=sys.stderr, flush=True)
        return None
    if _LOADED_FROM == target:
        return target
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=target, override=True)
    _LOADED_FROM = target
    if verbose:
        print(f"dotenv: {target} (overrides shell)", file=sys.stderr, flush=True)
        print(describe_model_env(), file=sys.stderr, flush=True)
    return target
