"""Content-addressed implementation approval helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from io_utils import safe_join
from runtime_models import ImplementationManifest


_APPROVAL_FIELDS = {
    "execution_approved",
    "approval_digest",
    "approved_by",
    "approved_at_utc",
    "approval_type",
    "reviewer_model",
    "review_digest",
    "reviewed_code_digest",
    "reviewed_smoke_digest",
}
_SMOKE_RUNTIME_FILES = {"smoke-config.json", "smoke-metrics.json"}


def implementation_approval_digest(
    manifest: ImplementationManifest,
    run_dir: Path,
) -> str:
    """Hash the scientific manifest plus every reachable in-run code file."""

    payload = _without_approval_metadata(manifest.model_dump(mode="json"))
    code_files = _implementation_files(manifest, run_dir)
    evidence = {
        "manifest": payload,
        "files": [
            {
                "path": _approval_path_id(path, run_dir),
                "sha256": _digest_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in code_files
        ],
    }
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def implementation_code_digest(
    manifest: ImplementationManifest,
    run_dir: Path,
) -> str:
    """Hash code, commands and dependencies while excluding test outcomes.

    The first review happens before a fresh smoke test.  Therefore readiness
    flags and smoke output files cannot be part of this digest; commands,
    dependency pins and smoke commands remain covered.
    """

    payload = _without_approval_metadata(manifest.model_dump(mode="json"))
    for definition in payload["implementations"].values():
        definition["ready"] = False
        definition["smoke_test_passed"] = False
        # The bounded smoke command is the reviewed formal argv with a
        # different config/result path.  It is recorded after the first
        # review, so the formal command remains the stable reviewed input.
        definition["smoke_test_command"] = []
    files = [
        path
        for path in _implementation_files(manifest, run_dir)
        if path.name not in _SMOKE_RUNTIME_FILES
    ]
    evidence = {
        "manifest": payload,
        "files": implementation_file_evidence(files, run_dir),
    }
    encoded = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def implementation_file_evidence(
    paths: list[Path],
    run_dir: Path,
) -> list[dict[str, Any]]:
    return [
        {
            "path": _approval_path_id(path, run_dir),
            "sha256": _digest_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in paths
    ]


def implementation_source_files(
    manifest: ImplementationManifest,
    run_dir: Path,
) -> list[Path]:
    return [
        path
        for path in _implementation_files(manifest, run_dir)
        if path.name not in _SMOKE_RUNTIME_FILES
    ]


def _without_approval_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    copied = dict(payload)
    copied["execution_approved"] = False
    for field in _APPROVAL_FIELDS - {"execution_approved"}:
        copied[field] = None
    return copied


def _implementation_files(
    manifest: ImplementationManifest,
    run_dir: Path,
) -> list[Path]:
    root = run_dir.resolve()
    found: set[Path] = set()
    lexical_implementations = root / "implementations"
    if lexical_implementations.exists() and lexical_implementations.is_symlink():
        raise ValueError("implementation approval rejects symbolic-link directories")
    implementations = safe_join(root, "implementations")
    if implementations.is_dir():
        for path in implementations.rglob("*"):
            if path.is_symlink():
                raise ValueError(
                    f"implementation approval rejects symbolic links: {path.name}"
                )
            if path.is_file():
                found.add(path.resolve())
    for definition in manifest.implementations.values():
        for argument in [
            *definition.command[1:],
            *definition.smoke_test_command[1:],
        ]:
            if "{" in argument or "}" in argument:
                continue
            candidate = Path(argument)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.exists() and candidate.is_symlink():
                raise ValueError(
                    "implementation approval rejects symbolic-link code files"
                )
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_file():
                found.add(resolved)
    return sorted(found, key=lambda path: _approval_path_id(path, run_dir))


def _approval_path_id(path: Path, run_dir: Path) -> str:
    root = run_dir.resolve()
    resolved = path.resolve()
    if resolved == root or root in resolved.parents:
        return resolved.relative_to(root).as_posix()
    # Do not persist a teammate's absolute path; bind to a one-way path ID and
    # the file content hash instead.
    path_digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return f"external-path-sha256:{path_digest}"


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
