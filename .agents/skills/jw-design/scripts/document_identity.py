# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve document authors from repository-effective Git configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class GitIdentity:
    """Identify the account writing a document."""

    name: str
    email: str


def git_identity(repo: Path) -> GitIdentity:
    """Read repository-effective Git identity without changing configuration."""
    values = []
    for key in ("user.name", "user.email"):
        result = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            raise ValueError(f"Git {key} is missing; configure an actual identity before writing documents")
        values.append(result.stdout.strip())
    return GitIdentity(values[0], values[1])


