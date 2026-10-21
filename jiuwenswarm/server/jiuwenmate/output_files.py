"""Validate and resolve PA-08 file candidates in a colleague's shared ``output/`` directory."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import HostWorkspaceAccess, WorkspaceStore
from openjiuwen.harness.persistent_agent import ExecutionContext, OutputPayload
from openjiuwen.harness.persistent_agent.workspace import resolve_read_path, validate_read_scope

FILE_SHARE_MODES = frozenset({"upload", "attachment"})

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHUNK = 1 << 20


def shared_output_file_name(file_path: object, file_ref: object, file_size: object) -> str:
    """Validate the JM-09 shared-output contract and return its basename."""
    parts = file_path.split("/") if isinstance(file_path, str) else []
    if (
        len(parts) != 2
        or parts[0] != "output"
        or parts[1] in ("", ".", "..")
        or any(char in parts[1] for char in ("\\", ":", "\x00"))
    ):
        raise PersistentAgentError("shared files must be under output/<file name>", code="OUTPUT_FILE_INVALID")
    match = _DIGEST.fullmatch(file_ref) if isinstance(file_ref, str) else None
    if match is None:
        raise PersistentAgentError("invalid shared file reference", code="OUTPUT_FILE_INVALID")
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
        raise PersistentAgentError("invalid shared file size", code="OUTPUT_FILE_INVALID")
    return parts[1]


def workspace_output_key(
    namespace_id: str, agent_id: str, file_path: object, file_ref: object, file_size: object
) -> str:
    """Build a logical workspace locator; it contains no absolute filesystem path."""
    if not isinstance(namespace_id, str) or not _IDENTIFIER.fullmatch(namespace_id):
        raise PersistentAgentError("invalid namespace_id for an output file", code="OUTPUT_FILE_INVALID")
    if not isinstance(agent_id, str) or not _IDENTIFIER.fullmatch(agent_id):
        raise PersistentAgentError("invalid agent_id for an output file", code="OUTPUT_FILE_INVALID")
    name = shared_output_file_name(file_path, file_ref, file_size)
    return f"{namespace_id}/{agent_id}/output/{name}"


def _open_regular(path: Path) -> int:
    """Open a regular file without following a final symlink or accepting a swapped file."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise PersistentAgentError("shared path is not a regular file", code="OUTPUT_FILE_MISMATCH")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(descriptor)
        raise PersistentAgentError("shared file changed while opening", code="OUTPUT_FILE_MISMATCH")
    return descriptor


def _verify_file(path: Path, file_ref: str, file_size: int) -> None:
    descriptor = _open_regular(path)
    digest = hashlib.sha256()
    size = 0
    try:
        while size <= file_size:
            chunk = os.read(descriptor, min(_CHUNK, file_size + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size <= file_size:
                digest.update(chunk)
        if size != file_size or "sha256:" + digest.hexdigest() != file_ref:
            raise PersistentAgentError("shared file no longer matches its candidate", code="OUTPUT_FILE_MISMATCH")
    finally:
        os.close(descriptor)


class WorkspaceOutputFiles:
    """Verify candidates at handoff and resolve their original workspace files for delivery."""

    def __init__(self, workspaces: WorkspaceStore) -> None:
        self._workspaces = workspaces

    def verify(self, execution: ExecutionContext, output: OutputPayload) -> None:
        """Verify the request-authorized shared output without creating a retained copy."""
        if output.share_mode not in FILE_SHARE_MODES:
            return
        if (
            not isinstance(output.file_path, str)
            or not isinstance(output.file_ref, str)
            or isinstance(output.file_size, bool)
            or not isinstance(output.file_size, int)
        ):
            raise PersistentAgentError("shared file candidate is incomplete", code="OUTPUT_FILE_INVALID")
        shared_output_file_name(output.file_path, output.file_ref, output.file_size)
        access = HostWorkspaceAccess(self._workspaces, execution.namespace_id, execution.agent_id)
        try:
            scope = validate_read_scope(access.read_scope(execution), execution)
            source = resolve_read_path(scope, output.file_path).path
        except Exception as exc:
            raise PersistentAgentError(
                "shared file is outside the colleague's read scope", code="OUTPUT_FILE_FORBIDDEN"
            ) from exc

        workspace_file = self._workspaces.resolve_shared_output(
            execution.namespace_id, execution.agent_id, output.file_path
        )
        if workspace_file is None or source.resolve(strict=True) != workspace_file.resolve(strict=True):
            raise PersistentAgentError("shared file is not available under output/", code="OUTPUT_FILE_FORBIDDEN")
        _verify_file(workspace_file, output.file_ref, output.file_size)

    def resolve(
        self,
        namespace_id: str,
        agent_id: str,
        file_path: object,
        file_ref: object,
        file_size: object,
    ) -> Path | None:
        """Resolve a still-matching Agent output for Web or Gateway; fail closed otherwise."""
        try:
            name = shared_output_file_name(file_path, file_ref, file_size)
            workspace_file = self._workspaces.resolve_shared_output(namespace_id, agent_id, f"output/{name}")
            if workspace_file is None:
                return None
            _verify_file(workspace_file, file_ref, file_size)
            return workspace_file
        except (OSError, PersistentAgentError, TypeError, ValueError):
            return None

    def resolve_key(self, key: object, file_ref: object, file_size: object) -> Path | None:
        """Resolve a logical ``namespace/agent/output/name`` key for Gateway delivery."""
        parts = key.split("/") if isinstance(key, str) else []
        if len(parts) != 4 or parts[2] != "output":
            return None
        try:
            expected = workspace_output_key(
                parts[0], parts[1], f"{parts[2]}/{parts[3]}", file_ref, file_size
            )
        except PersistentAgentError:
            return None
        if expected != key:
            return None
        return self.resolve(parts[0], parts[1], f"{parts[2]}/{parts[3]}", file_ref, file_size)

    def path(self, namespace_id: str, agent_id: str, output: OutputPayload) -> Path | None:
        """Resolve a candidate's original file after checking its recorded size and digest."""
        return self.resolve(
            namespace_id,
            agent_id,
            output.file_path,
            output.file_ref,
            output.file_size,
        )
