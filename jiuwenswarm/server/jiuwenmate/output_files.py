"""Validate and resolve PA-08 file candidates in their originating Session output directory."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.ports.workspace_access import HostWorkspaceAccess, WorkspaceStore
from openjiuwen.harness.persistent_agent import ExecutionContext, OutputPayload
from openjiuwen.harness.persistent_agent.workspace import resolve_read_path, validate_read_scope

FILE_SHARE_MODES = frozenset({"upload", "attachment"})

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHUNK = 1 << 20
_TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".toml",
    ".ini", ".cfg", ".sql", ".sh", ".ps1", ".rst", ".tex",
})
_TEXT_SEARCH_MAX_BYTES = 2 << 20


def shared_output_file_name(file_path: object, file_ref: object, file_size: object) -> str:
    """Validate the Session-scoped output contract and return its basename."""
    parts = file_path.split("/") if isinstance(file_path, str) else []
    if (
        len(parts) != 4
        or parts[0] != "sessions"
        or not _IDENTIFIER.fullmatch(parts[1])
        or parts[2] != "output"
        or parts[3] in ("", ".", "..")
        or any(char in parts[3] for char in ("\\", ":", "\x00"))
    ):
        raise PersistentAgentError(
            "shared files must be under sessions/<session_id>/output/<file name>", code="OUTPUT_FILE_INVALID"
        )
    match = _DIGEST.fullmatch(file_ref) if isinstance(file_ref, str) else None
    if match is None:
        raise PersistentAgentError("invalid shared file reference", code="OUTPUT_FILE_INVALID")
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
        raise PersistentAgentError("invalid shared file size", code="OUTPUT_FILE_INVALID")
    return parts[3]


def _output_session_id(file_path: object) -> str | None:
    parts = file_path.split("/") if isinstance(file_path, str) else []
    if len(parts) != 4 or parts[0] != "sessions" or parts[2] != "output":
        return None
    return parts[1] if _IDENTIFIER.fullmatch(parts[1]) else None


def workspace_output_key(
    namespace_id: str, agent_id: str, file_path: object, file_ref: object, file_size: object
) -> str:
    """Build a logical Session-scoped workspace locator, without an absolute filesystem path."""
    if not isinstance(namespace_id, str) or not _IDENTIFIER.fullmatch(namespace_id):
        raise PersistentAgentError("invalid namespace_id for an output file", code="OUTPUT_FILE_INVALID")
    if not isinstance(agent_id, str) or not _IDENTIFIER.fullmatch(agent_id):
        raise PersistentAgentError("invalid agent_id for an output file", code="OUTPUT_FILE_INVALID")
    shared_output_file_name(file_path, file_ref, file_size)
    return f"{namespace_id}/{agent_id}/{file_path}"


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
        if _output_session_id(output.file_path) != execution.session_id:
            raise PersistentAgentError(
                "shared file must belong to the current Session", code="OUTPUT_FILE_FORBIDDEN"
            )
        shared_output_file_name(output.file_path, output.file_ref, output.file_size)
        access = HostWorkspaceAccess(self._workspaces, execution.namespace_id, execution.agent_id)
        try:
            scope = validate_read_scope(access.read_scope(execution), execution)
            source = resolve_read_path(scope, output.file_path).path
        except Exception as exc:
            raise PersistentAgentError(
                "shared file is outside the colleague's read scope", code="OUTPUT_FILE_FORBIDDEN"
            ) from exc

        workspace_file = self._workspaces.resolve_shared_output(execution.namespace_id, execution.agent_id,
                                                                output.file_path)
        if workspace_file is None or source.resolve(strict=True) != workspace_file.resolve(strict=True):
            raise PersistentAgentError(
                "shared file is not available under this Session's output/", code="OUTPUT_FILE_FORBIDDEN"
            )
        _verify_file(workspace_file, output.file_ref, output.file_size)

    def resolve(
        self,
        namespace_id: str,
        agent_id: str,
        file_path: object,
        file_ref: object,
        file_size: object,
        *,
        expected_session_id: str | None = None,
    ) -> Path | None:
        """Resolve a still-matching Session output for Web or Gateway; fail closed otherwise."""
        try:
            session_id = _output_session_id(file_path)
            if session_id is None or (expected_session_id is not None and session_id != expected_session_id):
                return None
            shared_output_file_name(file_path, file_ref, file_size)
            workspace_file = self._workspaces.resolve_shared_output(namespace_id, agent_id, file_path)
            if workspace_file is None:
                return None
            _verify_file(workspace_file, file_ref, file_size)
            return workspace_file
        except (OSError, PersistentAgentError, TypeError, ValueError):
            return None

    def resolve_key(self, key: object, file_ref: object, file_size: object) -> Path | None:
        """Resolve a logical ``namespace/agent/sessions/id/output/name`` key for Gateway delivery."""
        parts = key.split("/") if isinstance(key, str) else []
        if len(parts) != 6 or parts[2] != "sessions" or parts[4] != "output":
            return None
        try:
            expected = workspace_output_key(
                parts[0], parts[1], f"{parts[2]}/{parts[3]}/{parts[4]}/{parts[5]}", file_ref, file_size
            )
        except PersistentAgentError:
            return None
        if expected != key:
            return None
        return self.resolve(
            parts[0], parts[1], f"{parts[2]}/{parts[3]}/{parts[4]}/{parts[5]}", file_ref, file_size
        )

    def path(
        self, namespace_id: str, agent_id: str, output: OutputPayload, *, expected_session_id: str | None = None
    ) -> Path | None:
        """Resolve a candidate's original file after checking its recorded size and digest."""
        return self.resolve(
            namespace_id,
            agent_id,
            output.file_path,
            output.file_ref,
            output.file_size,
            expected_session_id=expected_session_id,
        )

    def search_text(self, path: Path, file_name: str) -> str:
        """Read a bounded text preview from a verified output file for literal search."""
        suffix = Path(file_name).suffix.lower()
        try:
            if suffix == ".docx":
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo("word/document.xml")
                    if info.file_size > _TEXT_SEARCH_MAX_BYTES:
                        return ""
                    root = ET.fromstring(archive.read(info))
                text = " ".join(item.text or "" for item in root.iter()
                                if item.tag.rsplit("}", 1)[-1] == "t")
                return text[:_TEXT_SEARCH_MAX_BYTES]
            if suffix not in _TEXT_SUFFIXES or path.stat().st_size > _TEXT_SEARCH_MAX_BYTES:
                return ""
            raw = path.read_bytes()
            try:
                return raw.decode("utf-8-sig")[:_TEXT_SEARCH_MAX_BYTES]
            except UnicodeDecodeError:
                return raw.decode("utf-16", errors="replace")[:_TEXT_SEARCH_MAX_BYTES]
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
            return ""

    def import_approved(
        self,
        namespace_id: str,
        agent_id: str,
        target_session_id: str,
        artifact_id: str,
        source_file_path: str,
        file_ref: str,
        file_size: int,
    ) -> str:
        """Copy an owner-approved source file into the target Session's private imports/."""
        if not _IDENTIFIER.fullmatch(artifact_id):
            raise PersistentAgentError("invalid approved artifact ID", code="OUTPUT_FILE_INVALID")
        source_session_id = _output_session_id(source_file_path)
        if source_session_id is None:
            raise PersistentAgentError("approved artifact is outside Session output", code="OUTPUT_FILE_FORBIDDEN")
        source = self.resolve(namespace_id, agent_id, source_file_path, file_ref, file_size,
                              expected_session_id=source_session_id)
        if source is None:
            raise PersistentAgentError("approved artifact no longer matches its record", code="OUTPUT_FILE_MISMATCH")
        original_name = shared_output_file_name(source_file_path, file_ref, file_size)
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", original_name).strip(" .")[:120] or "artifact"
        destination_name = f"{artifact_id}_{safe_name}"
        imports = self._workspaces.ensure_session_imports(namespace_id, agent_id, target_session_id)
        destination = imports / destination_name
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if self._workspaces._is_redirected(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise PersistentAgentError("approved import path is not a regular file", code="OUTPUT_FILE_MISMATCH")
            _verify_file(destination, file_ref, file_size)
            return f"sessions/{target_session_id}/imports/{destination_name}"

        source_fd = _open_regular(source)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            target_fd = os.open(destination, flags, 0o600)
        except FileExistsError:
            os.close(source_fd)
            return self.import_approved(
                namespace_id, agent_id, target_session_id, artifact_id, source_file_path, file_ref, file_size
            )
        except BaseException:
            os.close(source_fd)
            raise
        digest = hashlib.sha256()
        size = 0
        try:
            while size <= file_size:
                chunk = os.read(source_fd, min(_CHUNK, file_size + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size <= file_size:
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(target_fd, view):]
            os.fsync(target_fd)
            if size != file_size or "sha256:" + digest.hexdigest() != file_ref:
                raise PersistentAgentError("approved artifact changed during import", code="OUTPUT_FILE_MISMATCH")
        except BaseException:
            os.close(source_fd)
            os.close(target_fd)
            destination.unlink(missing_ok=True)
            raise
        else:
            os.close(source_fd)
            os.close(target_fd)
        return f"sessions/{target_session_id}/imports/{destination_name}"
