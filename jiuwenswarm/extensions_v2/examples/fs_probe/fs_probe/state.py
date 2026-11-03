# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session snapshot kept by the FS Probe example."""

from __future__ import annotations

from dataclasses import dataclass, field

from openjiuwen.core.events.ops_impl.fs.models import (
    FsAncestorsOutput,
    FsEntry,
    FsListOutput,
    FsReadOutput,
    FsStatOutput,
    FsStatus,
    FsWriteOutput,
)


@dataclass(frozen=True, slots=True)
class FileCheck:
    """One instruction or manifest file checked below the session cwd."""

    name: str
    path: str
    exists: bool


@dataclass(frozen=True, slots=True)
class ProbeSnapshot:
    """One completed scan, kept for refresh recovery."""

    cwd: str
    refreshed_at: str
    listed: FsListOutput
    exists_status: FsStatus
    checks: tuple[FileCheck, ...]
    preview: FsReadOutput | None
    preview_stat: FsStatOutput | None
    ancestors: FsAncestorsOutput
    ancestor_stop: str | None = None
    write: FsWriteOutput | None = None


@dataclass(slots=True)
class SessionProbe:
    """Latest scan, expanded directories and round identity for one session."""

    snapshot: ProbeSnapshot | None = None
    request_id: str = ""
    expanded: set[str] = field(default_factory=set)
    children: dict[str, tuple[FsEntry, ...]] = field(default_factory=dict)
