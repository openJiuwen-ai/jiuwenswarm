"""Build inputs for the standalone toolkit (stdlib only, shared with setup.py)."""

from __future__ import annotations

import hashlib
from pathlib import Path


def runtime_files(root: Path) -> list[Path]:
    directories = (
        "src",
        "backends/macos/Sources",
        "backends/windows",
        "skills/computer-use-macos",
        "skills/computer-use-windows",
        "assets/agent-pointer",
    )
    excluded = {
        "Tests",
        "tests",
        "bin",
        "obj",
        "node_modules",
        "__pycache__",
        "_resources",
    }
    files = []
    for directory in directories:
        for path in (root / directory).rglob("*"):
            parts = path.relative_to(root).parts
            if path.is_file() and not any(
                part.startswith(".") or part.endswith(".egg-info") or part in excluded
                for part in parts
            ):
                files.append(path)
    files.extend(
        root / name
        for name in (
            "pyproject.toml",
            "uv.lock",
            "setup.py",
            "MANIFEST.in",
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "SOURCE.json",
            "backends/macos/Package.swift",
            "skills/computer-use/base.md",
        )
    )
    return sorted(set(file for file in files if file.is_file()))


def source_digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()[:24]
