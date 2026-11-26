# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded, read-only Git identity queries for a host-authorized workspace."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time

from jiuwenswarm.common.git_safe_directory import is_dubious_ownership_error


_QUERY_TIMEOUT_SECONDS = 5.0


class SessionRepoError(RuntimeError):
    """A workspace query failed, with a stable code and no raw Git output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"session.repo query failed: {code}")


def read_session_repo(cwd: str) -> dict[str, object] | None:
    """Read the nearest Git working tree without refreshing or modifying it.

    Args:
        cwd: Absolute execution directory authorized by the session host.

    Returns:
        A new dictionary containing repo_root, branch, head and detached, or
        None for a non-Git directory or a bare repository.

    Raises:
        SessionRepoError: The workspace or Git is unavailable, a query times
            out, ownership is rejected, or Git reports an unexpected failure.
    """
    try:
        directory = Path(cwd).resolve(strict=True)
        if not directory.is_dir():
            raise OSError("not a directory")
    except (OSError, RuntimeError):
        raise SessionRepoError("WORKSPACE_UNAVAILABLE") from None

    executable = shutil.which("git")
    if executable is None:
        raise SessionRepoError("GIT_NOT_FOUND")
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "LC_ALL": "C",
        "LANG": "C",
        "LANGUAGE": "C",
    })
    deadline = time.monotonic() + _QUERY_TIMEOUT_SECONDS

    def query(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SessionRepoError("GIT_QUERY_TIMEOUT")
        try:
            result = subprocess.run(
                [executable, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", *arguments],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=remaining,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise SessionRepoError("GIT_QUERY_TIMEOUT") from None
        except FileNotFoundError:
            code = "GIT_NOT_FOUND" if directory.is_dir() else "WORKSPACE_UNAVAILABLE"
            raise SessionRepoError(code) from None
        except OSError:
            raise SessionRepoError("GIT_QUERY_FAILED") from None
        if is_dubious_ownership_error(result):
            raise SessionRepoError("GIT_DUBIOUS_OWNERSHIP")
        return result

    inside = query(["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0:
        if "not a git repository" in inside.stderr.lower():
            return None
        raise SessionRepoError("GIT_QUERY_FAILED")
    if inside.stdout.strip() == "false":
        return None
    if inside.stdout.strip() != "true":
        raise SessionRepoError("GIT_QUERY_FAILED")

    root = query(["rev-parse", "--show-toplevel"])
    if root.returncode != 0 or not root.stdout.strip():
        raise SessionRepoError("GIT_QUERY_FAILED")
    try:
        repo_root = Path(root.stdout.rstrip("\r\n")).resolve(strict=True)
        if not repo_root.is_dir() or not directory.is_relative_to(repo_root):
            raise ValueError("unexpected repository root")
    except (OSError, RuntimeError, ValueError):
        raise SessionRepoError("GIT_QUERY_FAILED") from None

    symbolic = query(["symbolic-ref", "--quiet", "HEAD"])
    if symbolic.returncode not in (0, 1):
        raise SessionRepoError("GIT_QUERY_FAILED")
    reference = symbolic.stdout.strip() if symbolic.returncode == 0 else None
    if reference is not None and not reference.startswith("refs/heads/"):
        raise SessionRepoError("GIT_QUERY_FAILED")
    branch = reference.removeprefix("refs/heads/") if reference else None
    if symbolic.returncode == 0 and not branch:
        raise SessionRepoError("GIT_QUERY_FAILED")
    head = query(["rev-parse", "--verify", "--quiet", "HEAD"])
    if head.returncode != 0 and not (head.returncode == 1 and branch is not None):
        raise SessionRepoError("GIT_QUERY_FAILED")
    commit = head.stdout.strip() if head.returncode == 0 else None
    if head.returncode == 0 and not commit:
        raise SessionRepoError("GIT_QUERY_FAILED")
    return {"repo_root": str(repo_root), "branch": branch, "head": commit, "detached": branch is None}
