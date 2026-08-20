# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prefer a local agent-core checkout over the published openjiuwen wheel.

JiuwenSwarm's pyproject pins:

    openjiuwen @ git+https://gitcode.com/openJiuwen/agent-core.git@develop

Local Code Graph work lives in a sibling checkout and is invisible until that
path is on ``sys.path`` (eval scripts) or installed editable (product / uv run).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent
_JIUWEN_ROOT = _EVAL_DIR.parents[1]
_DEFAULT_AGENT_CORE = _JIUWEN_ROOT.parent / "agent-core"


def resolve_agent_core_root() -> Path | None:
    """Return the local agent-core root, or None to keep the published package."""
    if os.environ.get("USE_PUBLISHED_OPENJIUWEN", "").strip() in {"1", "true", "yes"}:
        return None
    env = os.environ.get("AGENT_CORE_ROOT") or os.environ.get("OPENJIUWEN_SRC")
    if env:
        path = Path(env).expanduser().resolve()
        return path if (path / "openjiuwen").is_dir() else None
    if (_DEFAULT_AGENT_CORE / "openjiuwen").is_dir():
        return _DEFAULT_AGENT_CORE.resolve()
    return None


def _evict_openjiuwen_not_from(root: Path) -> None:
    """Drop a published openjiuwen already cached in sys.modules."""
    loaded = sys.modules.get("openjiuwen")
    location = getattr(loaded, "__file__", None) or ""
    if not location:
        return
    try:
        from_local = Path(location).resolve().is_relative_to(root)
    except (OSError, ValueError):
        from_local = False
    if from_local:
        return
    for name in list(sys.modules):
        if name == "openjiuwen" or name.startswith("openjiuwen."):
            del sys.modules[name]


def prepend_local_agent_core(*, verbose: bool = True) -> Path | None:
    """Insert local agent-core ahead of site-packages. Call before importing openjiuwen."""
    root = resolve_agent_core_root()
    if root is None:
        return None
    text = str(root)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)
    _evict_openjiuwen_not_from(root)
    if verbose:
        print(f"using local agent-core: {root}", file=sys.stderr, flush=True)
    return root


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
    jiuwen = git_identity(_JIUWEN_ROOT)
    core = resolve_agent_core_root()
    engine = git_identity(core) if core is not None else "published-wheel"
    graph = "yes" if has_code_graph() else "NO"
    return f"jiuwenswarm={jiuwen} agent-core={engine} code_graph={graph}"


def assert_engine_matches_branch() -> None:
    """Refuse a baseline jiuwenswarm run that still loads a Code Graph engine."""
    if os.environ.get("EVAL_ALLOW_ENGINE_MISMATCH", "").strip() in {"1", "true", "yes"}:
        return
    branch = git_identity(_JIUWEN_ROOT).split("@", 1)[0]
    if "baseline" not in branch:
        return
    if not has_code_graph():
        return
    raise SystemExit(
        "eval pair mismatch: this jiuwenswarm checkout looks like the original "
        f"baseline ({git_identity(_JIUWEN_ROOT)}) but openjiuwen still has Code Graph "
        f"({describe_openjiuwen()}).\n"
        "Checkout agent-core ``eval/contextbench-baseline`` (or origin/develop), "
        "or set USE_PUBLISHED_OPENJIUWEN=1 so the lockfile wheel is used.\n"
        "Override with EVAL_ALLOW_ENGINE_MISMATCH=1 only if you intend this."
    )


def require_code_graph() -> None:
    """Fail with an actionable message if this env still has the published package."""
    try:
        import openjiuwen.core.retrieval.code_graph  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "openjiuwen.core.retrieval.code_graph is missing.\n"
            f"  {describe_openjiuwen()}\n"
            f"  import error: {exc}\n"
            "JiuwenSwarm pins the published agent-core; local edits are invisible until you:\n"
            "  1. bash scripts/eval/install_local_agent_core.sh   (product / uv run)\n"
            "  2. keep a sibling ../agent-core checkout           (eval scripts prepend it)\n"
            "  3. export AGENT_CORE_ROOT=/path/to/agent-core\n"
            "After `uv sync`, re-run step 1. USE_PUBLISHED_OPENJIUWEN=1 skips the local checkout."
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
        _JIUWEN_ROOT / "jiuwenswarm" / "resources" / ".env",
        _JIUWEN_ROOT / ".env",
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
    """Load project .env with override=True so it beats ~/.zshrc exports."""
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
