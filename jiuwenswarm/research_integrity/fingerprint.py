# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Environment fingerprinting for reproducible experiment provenance.

Captures the execution environment (python/platform/git/deps/config/datasets/
code) into a stable, hash-addressed record so any experiment run can later be
answered with "under exactly which environment did this number happen?".

Secret safety: only environment variables explicitly listed in the whitelist
are recorded; no secrets are ever captured.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Directories never included when hashing project source code.
_CODE_HASH_IGNORED_DIRS = frozenset(
    {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache",
     ".jiuwen", ".mypy_cache", ".ruff_cache", "htmlcov", ".idea", ".vscode"}
)


class EnvironmentFingerprint(BaseModel):
    """Hash-addressed snapshot of the experiment execution environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint_id: str = Field(min_length=64, max_length=64)
    python_version: str = ""
    platform: str = ""
    git_commit: str | None = None
    git_dirty: bool | None = None
    dependency_hash: str | None = None
    config_hash: str | None = None
    dataset_hashes: dict[str, str] = Field(default_factory=dict)
    code_hash: str | None = None
    environment_variables_whitelist: dict[str, str] = Field(default_factory=dict)


def _sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """Return the hex sha256 digest of a file's content (streamed)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command in *cwd*; return stdout or ``None`` on any failure."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _capture_git_state(project_root: Path) -> tuple[str | None, bool | None]:
    """Return ``(commit, dirty)`` for *project_root* (``None`` when not a repo)."""
    commit = _run_git(["rev-parse", "HEAD"], project_root)
    if commit is None:
        return None, None
    status = _run_git(["status", "--porcelain"], project_root)
    dirty = bool(status) if status is not None else None
    return commit, dirty


def _capture_dependency_hash() -> str | None:
    """Hash the installed distribution set (name, version) pairs).

    Uses ``importlib.metadata`` (no network, no pip subprocess). Returns
    ``None`` when metadata enumeration fails.
    """
    try:
        from importlib import metadata as _md
    except ImportError:  # pragma: no cover - py>=3.8 always has it
        return None
    try:
        pairs = sorted(
            (dist.metadata["Name"] or "", dist.version)
            for dist in _md.distributions()
        )
    except Exception:  # noqa: BLE001 - any metadata failure is non-fatal
        return None
    payload = json.dumps(pairs, sort_keys=True).encode("utf-8")
    return _sha256_bytes(payload)


def _capture_code_hash(project_root: Path) -> str | None:
    """Hash the project's Python source tree (deterministic order, streamed)."""
    if not project_root.is_dir():
        return None
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _CODE_HASH_IGNORED_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                files.append(Path(dirpath) / filename)
    if not files:
        return None
    digest = hashlib.sha256()
    for path in sorted(files):
        try:
            digest.update(str(path.relative_to(project_root)).replace("\\", "/").encode("utf-8"))
            digest.update(b"\0")
            digest.update(_sha256_file(path).encode("ascii"))
            digest.update(b"\0")
        except OSError:  # noqa: PERF203 - unreadable file must not abort hash
            continue
    return digest.hexdigest()


def _capture_environment_variables(whitelist: list[str] | None) -> dict[str, str]:
    """Record only whitelisted environment variables (secret-safe)."""
    recorded: dict[str, str] = {}
    for name in whitelist or []:
        value = os.environ.get(name)
        if value is not None:
            recorded[name] = value
    return recorded


def capture_environment_fingerprint(
    project_root: str | Path,
    *,
    config_path: str | Path | None = None,
    dataset_paths: list[str | Path] | None = None,
    capture_git: bool = True,
    env_whitelist: list[str] | None = None,
) -> EnvironmentFingerprint:
    """Capture a hash-addressed snapshot of the current execution environment.

    Args:
        project_root: Project directory used for git state / code hashing.
        config_path: Optional experiment config file to hash.
        dataset_paths: Optional dataset files to hash individually.
        capture_git: When false, skip git commit/dirty capture (offline safe).
        env_whitelist: Environment variable names allowed to be recorded.

    Returns:
        An :class:`EnvironmentFingerprint` whose ``fingerprint_id`` is the
        sha256 over the canonical JSON of all captured fields.
    """
    root = Path(project_root)

    git_commit: str | None = None
    git_dirty: bool | None = None
    if capture_git:
        git_commit, git_dirty = _capture_git_state(root)

    config_hash: str | None = None
    if config_path is not None:
        cfg = Path(config_path)
        if cfg.is_file():
            config_hash = _sha256_file(cfg)

    dataset_hashes: dict[str, str] = {}
    for item in dataset_paths or []:
        data_path = Path(item)
        if data_path.is_file():
            dataset_hashes[str(data_path)] = _sha256_file(data_path)

    fields: dict[str, Any] = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "dependency_hash": _capture_dependency_hash(),
        "config_hash": config_hash,
        "dataset_hashes": dataset_hashes,
        "code_hash": _capture_code_hash(root),
        "environment_variables_whitelist": _capture_environment_variables(
            env_whitelist
        ),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    fingerprint_id = _sha256_bytes(canonical.encode("utf-8"))

    return EnvironmentFingerprint(fingerprint_id=fingerprint_id, **fields)


__all__ = [
    "EnvironmentFingerprint",
    "capture_environment_fingerprint",
]
