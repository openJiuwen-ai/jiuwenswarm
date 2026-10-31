# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read Claude-shaped repository identity without changing a working copy."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit

from jiuwenswarm.common.git_safe_directory import is_dubious_ownership_error
from jiuwenswarm.server.runtime.session.session_repo import SessionRepoError, read_session_repo


def _public_remote(value: str) -> str | None:
    """Remove URL credentials and query/fragment tokens from public metadata."""
    if not value or any(character in value for character in ("\n", "\r", "\0")):
        return None
    if "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if hostname is None:
            return value if parsed.scheme == "file" else None
        if ":" in hostname:
            hostname = f"[{hostname}]"
        authority = hostname + (f":{parsed.port}" if parsed.port is not None else "")
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except ValueError:
        return None


def read_session_repo_view(cwd: str) -> dict[str, object] | None:
    """Answer root/remote/internal/name; a linked worktree uses its main root.

    This build declares no internal repository allowlist, so internal=False and
    name=None are deliberate. Do not infer internal ownership from a host name.
    """
    deadline = time.monotonic() + 5.0
    original = read_session_repo(cwd)
    if original is None:
        return None
    executable = shutil.which("git")
    if executable is None:
        raise SessionRepoError("GIT_NOT_FOUND")
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                        "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})

    def query(arguments: list[str], missing: bool = False) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SessionRepoError("GIT_QUERY_TIMEOUT")
        try:
            result = subprocess.run(
                [executable, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", *arguments],
                cwd=cwd, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=remaining, shell=False, check=False,
            )
        except subprocess.TimeoutExpired:
            raise SessionRepoError("GIT_QUERY_TIMEOUT") from None
        except OSError:
            raise SessionRepoError("GIT_QUERY_FAILED") from None
        if is_dubious_ownership_error(result):
            raise SessionRepoError("GIT_DUBIOUS_OWNERSHIP")
        if missing and result.returncode == 1:
            return None
        if result.returncode:
            raise SessionRepoError("GIT_QUERY_FAILED")
        return result.stdout.rstrip("\r\n")

    worktrees = query(["worktree", "list", "--porcelain", "-z"])
    first = worktrees.split("\0", 1)[0]
    if not first.startswith("worktree "):
        raise SessionRepoError("GIT_QUERY_FAILED")
    try:
        root = Path(first[len("worktree "):]).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("unavailable main root")
    except (OSError, RuntimeError, ValueError):
        raise SessionRepoError("WORKSPACE_UNAVAILABLE") from None
    remote = query(["config", "--get", "remote.origin.pushurl"], missing=True)
    if remote is None:
        remote = query(["config", "--get", "remote.origin.url"], missing=True)
    return {"root": str(root), "remote": _public_remote(remote) if remote else None,
            "internal": False, "name": None}
