# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Artifact tracking: hashing, discovery, and tamper verification.

Every file an experiment claims as a result is hashed (sha256) and recorded.
Verification recomputes the hash so a post-hoc edit of an artifact
invalidates the metrics extracted from it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from jiuwenswarm.research_integrity.fingerprint import _sha256_file
from jiuwenswarm.research_integrity.schemas import ArtifactRecord

_SUFFIX_KINDS = {
    ".json": "json",
    ".csv": "csv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".txt": "txt",
    ".log": "txt",
}

_ARTIFACT_ID_PREFIX = "art_"


def sha256_file(path: str | Path) -> str:
    """Return the hex sha256 digest of a file (public convenience wrapper)."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"{file_path}: not a file")
    return _sha256_file(file_path)


def _kind_for(path: Path) -> str:
    """Map a file suffix to an artifact kind label."""
    return _SUFFIX_KINDS.get(path.suffix.lower(), "other")


def _artifact_id(run_id: str, path: Path) -> str:
    """Stable artifact id: run + digest of the normalized path."""
    path_token = hashlib.sha256(
        str(path).replace("\\", "/").encode("utf-8")
    ).hexdigest()[:12]
    return f"{_ARTIFACT_ID_PREFIX}{run_id}_{path_token}"


def record_artifact(
    path: str | Path,
    *,
    run_id: str,
) -> ArtifactRecord:
    """Hash one existing artifact file and wrap it in an ArtifactRecord.

    Args:
        path: The artifact file produced by the run.
        run_id: The owning experiment run id.

    Returns:
        A hashed :class:`ArtifactRecord` (the file must already exist).

    Raises:
        FileNotFoundError: The artifact file does not exist — expected
            artifacts missing after a run are an integrity failure and must
            surface as such, not as an empty record.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"{file_path}: expected artifact missing")
    return ArtifactRecord(
        artifact_id=_artifact_id(run_id, file_path),
        run_id=run_id,
        path=str(file_path),
        sha256=_sha256_file(file_path),
        size_bytes=file_path.stat().st_size,
        kind=_kind_for(file_path),
    )


def discover_artifacts(
    paths: list[str | Path],
    *,
    run_id: str,
    base_dir: str | Path | None = None,
) -> list[ArtifactRecord]:
    """Record every existing artifact path; missing ones raise immediately.

    Args:
        paths: Artifact paths (absolute, or relative to *base_dir*).
        run_id: The owning experiment run id.
        base_dir: Directory used to resolve relative artifact paths.

    Returns:
        The :class:`ArtifactRecord` list in input order.

    Raises:
        FileNotFoundError: Naming the first missing artifact, so callers can
            turn it into a MISSING_ARTIFACT integrity issue.
    """
    base = Path(base_dir) if base_dir is not None else None
    records: list[ArtifactRecord] = []
    for item in paths:
        artifact_path = Path(item)
        if base is not None and not artifact_path.is_absolute():
            artifact_path = base / artifact_path
        records.append(record_artifact(artifact_path, run_id=run_id))
    return records


def verify_artifact(record: ArtifactRecord) -> bool:
    """Recompute the hash of a recorded artifact and compare with the record.

    Returns:
        True when the file still exists with the recorded size and sha256;
        False on any mismatch (tampered, truncated, replaced, or deleted).
    """
    path = Path(record.path)
    try:
        if path.stat().st_size != record.size_bytes:
            return False
        return _sha256_file(path) == record.sha256
    except OSError:
        return False


__all__ = [
    "sha256_file",
    "record_artifact",
    "discover_artifacts",
    "verify_artifact",
]
