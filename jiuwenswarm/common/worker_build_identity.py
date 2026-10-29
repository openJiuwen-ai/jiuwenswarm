# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Artifact identity contract shared by release tooling and Runtime.

This module uses only the standard library and can be loaded by a build
backend without importing the application or knowing where release scripts live.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

IDENTITY_FILENAME = "_worker_build_identity.json"


def write_build_metadata(path: Path, version: str) -> None:
    """Stamp release metadata without depending on a release-script checkout."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    built_at = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch else datetime.now(timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version,
        "build_date": built_at.astimezone(timezone(timedelta(hours=8))).date().isoformat()}) + "\n", encoding="utf-8")


def valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def read_build_identity(path: Path) -> dict:
    """Reject corrupt metadata rather than silently treating it as old code."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or type(value.get("schema")) is not int:
            raise ValueError("identity must be an object")
        if value.get("schema") == 1 and valid_digest(value.get("fingerprint")):
            return value
        keys = ("backend_build_id", "source_digest", "dependency_digest", "packaging_digest")
        if value.get("schema") == 2 and all(valid_digest(value.get(key)) for key in keys):
            return value
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Invalid Worker build identity: {path}") from exc
    raise RuntimeError(f"Invalid Worker build identity: {path}")


def _digest_files(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_digest(root: Path) -> str:
    """Describe backend inputs without embedding checkout-specific paths."""
    paths = []
    excluded = {"node_modules", "__pycache__", ".venv", "dist", "build", ".git"}
    for name in ("jiuwenswarm", "gateway", "jiuwenbox/src"):
        directory = root / name
        for current, children, files in os.walk(directory):
            children[:] = [child for child in children if child not in excluded]
            for filename in files:
                if filename == IDENTITY_FILENAME:
                    continue
                if Path(filename).suffix in {".py", ".yaml", ".yml", ".json", ".toml"}:
                    paths.append(Path(current) / filename)
    return _digest_files(root, paths)


def dependency_digest(root: Path) -> str:
    return _digest_files(root, [root / name for name in ("pyproject.toml", "uv.lock") if (root / name).is_file()])


def packaging_digest(root: Path) -> str:
    paths = []
    suffixes = {".py", ".spec", ".ps1", ".psm1", ".sh", ".iss", ".bat"}
    for path in (root / "scripts").rglob("*"):
        if path.is_file() and path.suffix in suffixes:
            paths.append(path)
    return _digest_files(root, paths)


def _revision(root: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        return subprocess.check_output(
            [str(Path(git).resolve()), "-C", str(root), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL,
            text=True, timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def validate_build_inputs(value: dict, source_root: Path, packaging_root: Path | None = None) -> None:
    if (value.get("schema") != 2 or value.get("source_digest") != source_digest(source_root)
            or value.get("dependency_digest") != dependency_digest(source_root)):
        raise RuntimeError("Worker artifact does not match the selected source and dependency inputs")
    if packaging_root is not None and value.get("packaging_digest") != packaging_digest(packaging_root):
        raise RuntimeError("Worker artifact does not match the selected packaging inputs")


def write_build_identity(destination: Path, source_root: Path, *,
                         packaging_root: Path | None = None, identity_input: Path | None = None) -> dict:
    """Allocate a fresh artifact ID; supplied identities describe provenance only."""
    if not (source_root / "jiuwenswarm").is_dir():
        raise RuntimeError("Selected source root does not contain the jiuwenswarm package")
    if identity_input is not None:
        supplied = read_build_identity(identity_input)
        validate_build_inputs(supplied, source_root, packaging_root)
    value = {
        "schema": 2, "backend_build_id": secrets.token_hex(32),
        "source_commit": _revision(source_root),
        "packaging_commit": _revision(packaging_root or source_root),
        "source_digest": source_digest(source_root),
        "dependency_digest": dependency_digest(source_root),
        "packaging_digest": packaging_digest(packaging_root or source_root),
    }
    if identity_input is not None:
        value["input_build_id"] = supplied["backend_build_id"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    read_build_identity(destination)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--packaging-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--identity-input", type=Path, help="Validated provenance input; a rebuild still gets a new ID")
    parser.add_argument("--verify", type=Path, help="Validate a reused backend's identity without changing it")
    args = parser.parse_args()
    if args.verify:
        validate_build_inputs(read_build_identity(args.verify), args.source_root.resolve(), args.packaging_root)
    elif args.output:
        write_build_identity(args.output, args.source_root.resolve(), packaging_root=args.packaging_root,
                             identity_input=args.identity_input)
    else:
        parser.error("--output or --verify is required")


if __name__ == "__main__":
    main()
