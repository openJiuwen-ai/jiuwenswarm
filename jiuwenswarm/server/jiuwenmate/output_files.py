"""Retained copies of the files a colleague hands off (PA-08).

A file candidate names a logical path in the colleague's office directory and the
size and SHA-256 of the bytes core read at the end of the Execution. Saving the
candidate copies that file, as it is then, to ``<root>/<namespace_id>/<sha256>/<file name>``
and checks both values; a file that changed since core read it is refused, and the
candidate is not saved. A copy never changes afterwards: delivery and download read
only the copy, never the office directory. Gateway finds a copy by its key, the
path relative to the root, so no office path leaves the AgentServer.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import HostWorkspaceAccess, WorkspaceStore
from jiuwenswarm.server.jiuwenmate.presentation_package import normalize_presentation_package
from jiuwenswarm.server.jiuwenmate.sqlite_store import private_dir
from openjiuwen.harness.persistent_agent import ExecutionContext, OutputPayload
from openjiuwen.harness.persistent_agent.workspace import resolve_read_path, validate_read_scope

OUTPUTS_DIR = "outputs"
FILE_SHARE_MODES = frozenset({"upload", "attachment"})

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHUNK = 1 << 20


def _file_name(file_path: str) -> str:
    name = PurePosixPath(file_path).name if isinstance(file_path, str) else ""
    if name in ("", ".", "..") or "\\" in name:
        raise PersistentAgentError("invalid shared file path", code="OUTPUT_FILE_INVALID")
    return name


def _digest_hex(file_ref: str) -> str:
    match = _DIGEST.fullmatch(file_ref) if isinstance(file_ref, str) else None
    if match is None:
        raise PersistentAgentError("invalid shared file reference", code="OUTPUT_FILE_INVALID")
    return match.group(1)


def output_file_key(namespace_id: str, file_ref: str, file_path: str) -> str:
    """Key of the retained copy of one file candidate, relative to the outputs root."""
    if not isinstance(namespace_id, str) or not _NAMESPACE.fullmatch(namespace_id):
        raise PersistentAgentError("invalid namespace_id for an output file", code="OUTPUT_FILE_INVALID")
    return f"{namespace_id}/{_digest_hex(file_ref)}/{_file_name(file_path)}"


def resolve_output_file(root: Path, key: str) -> Path | None:
    """The retained copy a key names, or None when the key is malformed or the copy is missing."""
    parts = key.split("/") if isinstance(key, str) else []
    if len(parts) != 3:
        return None
    namespace_id, digest, name = parts
    try:
        if output_file_key(namespace_id, f"sha256:{digest}", name) != key:
            return None
    except PersistentAgentError:
        return None
    path = Path(root) / namespace_id / digest / name
    try:
        return path if stat.S_ISREG(os.lstat(path).st_mode) else None
    except OSError:
        return None


def _open_regular(path: Path) -> int:
    """Open a regular file without following a final symlink, refusing one swapped in after the check."""
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


class OutputFileStore:
    """Immutable copies of handed-off files, laid out as ``<root>/<namespace_id>/<sha256>/<file name>``."""

    def __init__(self, root: Path, workspaces: WorkspaceStore) -> None:
        self._root = Path(root).resolve()
        self._workspaces = workspaces
        private_dir(self._root)

    @property
    def root(self) -> Path:
        return self._root

    def path(self, namespace_id: str, output: OutputPayload) -> Path:
        return self._root / output_file_key(namespace_id, output.file_ref, output.file_path)

    def retain(self, execution: ExecutionContext, output: OutputPayload) -> None:
        """Keep a verified copy of a file candidate; a candidate without a file is left alone.

        Raises ``PersistentAgentError`` when the file can no longer be read under the
        Execution's read scope, or its size or digest differs from the candidate.
        """
        if output.share_mode not in FILE_SHARE_MODES:
            return
        if output.file_ref is None or output.file_path is None or output.file_size is None:
            raise PersistentAgentError("file candidate lacks its file reference", code="OUTPUT_FILE_INVALID")
        target = self.path(execution.namespace_id, output)
        try:
            if target.stat().st_size == output.file_size:
                normalize_presentation_package(target)
                return
        except FileNotFoundError:
            pass
        access = HostWorkspaceAccess(self._workspaces, execution.namespace_id, execution.agent_id)
        try:
            scope = validate_read_scope(access.read_scope(execution), execution)
            source = resolve_read_path(scope, output.file_path).path
        except Exception as exc:
            raise PersistentAgentError(
                "shared file is outside the colleague's read scope", code="OUTPUT_FILE_FORBIDDEN"
            ) from exc
        private_dir(target.parent.parent)
        private_dir(target.parent)
        self._copy(Path(source), target, output.file_ref, output.file_size)
        normalize_presentation_package(target)

    @staticmethod
    def _copy(source: Path, target: Path, file_ref: str, file_size: int) -> None:
        try:
            descriptor = _open_regular(source)
        except FileNotFoundError as exc:
            raise PersistentAgentError("shared file no longer exists", code="OUTPUT_FILE_MISMATCH") from exc
        except OSError as exc:
            raise PersistentAgentError("shared file cannot be read", code="OUTPUT_FILE_UNAVAILABLE") from exc
        temporary = ""
        digest = hashlib.sha256()
        size = 0
        try:
            handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".retain-")
            with os.fdopen(handle, "wb") as sink:
                while chunk := os.read(descriptor, min(_CHUNK, file_size + 1 - size)):
                    size += len(chunk)
                    if size > file_size:
                        break
                    digest.update(chunk)
                    sink.write(chunk)
            if size != file_size or "sha256:" + digest.hexdigest() != file_ref:
                raise PersistentAgentError("shared file changed after it was handed off", code="OUTPUT_FILE_MISMATCH")
            os.replace(temporary, target)
        except OSError as exc:
            raise PersistentAgentError("shared file cannot be retained", code="OUTPUT_FILE_UNAVAILABLE") from exc
        finally:
            os.close(descriptor)
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
