"""``PersistentPorts.workspace_access`` / ``workspace_write_access``: a colleague's office directory (PA-14, PA-16).

Each colleague owns one directory under the JiuwenMate data root, apart from the
database, Templates and windows. The read scope is that directory, and the write
scope is the same directory, each only for a Request whose startup and source
scopes both include the capability; every other Request is refused.

``sessions/`` holds one private draft directory per Session. A Session reads and
writes its own ``sessions/<session_id>/`` and cannot reach another Session's.
core asks again before every file access, so the port never caches a decision,
and it never creates, moves or reads files.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.sqlite_store import private_dir
from openjiuwen.harness.persistent_agent import ExecutionContext, WorkspaceReadScope, WorkspaceWriteScope

WORKSPACE_READ = "workspace_read"
WORKSPACE_WRITE = "workspace_write"
WORKSPACE_KNOWLEDGE_READ = "workspace_knowledge_read"
SESSIONS_DIR = "sessions"

_DIR_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _require_dir_name(name: str, value: str) -> None:
    if not isinstance(value, str) or not _DIR_NAME.fullmatch(value):
        raise PersistentAgentError(f"invalid {name} for an office directory")


class WorkspaceStore:
    """Office directories, one per colleague, laid out as ``<root>/<namespace_id>/<agent_id>``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        private_dir(self._root)

    def path(self, namespace_id: str, agent_id: str) -> Path:
        _require_dir_name("namespace_id", namespace_id)
        _require_dir_name("agent_id", agent_id)
        return self._root / namespace_id / agent_id

    @staticmethod
    def _is_redirected(metadata: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        )

    def _assert_safe_directory(self, path: Path, metadata: os.stat_result) -> None:
        if self._is_redirected(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise PersistentAgentError("workspace directory must be a real directory")
        try:
            path.resolve(strict=True).relative_to(self._root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PersistentAgentError(
                "workspace directory must stay within its root"
            ) from exc

    def _check_directory(self, path: Path, *, create: bool) -> Path:
        """Create or validate a directory without accepting links below the workspace root."""
        try:
            relative = path.relative_to(self._root)
            root_metadata = self._root.lstat()
        except (OSError, ValueError) as exc:
            raise PersistentAgentError("workspace directory is not available") from exc
        self._assert_safe_directory(self._root, root_metadata)

        current = self._root
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if not create or current == self._root:
                    return path
                try:
                    current.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise PersistentAgentError(
                        "workspace directory could not be prepared"
                    ) from exc
                try:
                    metadata = current.lstat()
                except OSError as exc:
                    raise PersistentAgentError(
                        "workspace directory is not available"
                    ) from exc
            except OSError as exc:
                raise PersistentAgentError("workspace directory is not available") from exc

            self._assert_safe_directory(current, metadata)

            if create:
                try:
                    os.chmod(current, 0o700)
                    metadata = current.lstat()
                except OSError as exc:
                    raise PersistentAgentError(
                        "workspace directory could not be secured"
                    ) from exc
                self._assert_safe_directory(current, metadata)
        return path

    def validate_session_paths(
        self, namespace_id: str, agent_id: str, session_id: str
    ) -> tuple[Path, Path]:
        """Validate existing Session ancestors before exposing them as an authorization scope."""
        _require_dir_name("session_id", session_id)
        root = self.path(namespace_id, agent_id)
        sessions = root / SESSIONS_DIR
        current = sessions / session_id
        self._check_directory(current, create=False)
        return sessions, current

    def ensure(self, namespace_id: str, agent_id: str) -> Path:
        path = self.path(namespace_id, agent_id)
        self._check_directory(path.parent, create=True)
        self._check_directory(path, create=True)
        self._check_directory(path / "output", create=True)
        return path

    def ensure_session(self, namespace_id: str, agent_id: str, session_id: str) -> Path:
        """Ensure the Agent's shared output and one private execution-session directory."""
        _require_dir_name("session_id", session_id)
        root = self.ensure(namespace_id, agent_id)
        session_root = root / SESSIONS_DIR / session_id
        self._check_directory(session_root, create=True)
        return session_root

    def resolve_shared_output(self, namespace_id: str, agent_id: str, file_path: str) -> Path | None:
        """Resolve a regular file directly under this Agent's shared ``output/`` directory."""
        parts = file_path.split("/") if isinstance(file_path, str) else []
        if (
            len(parts) != 2
            or parts[0] != "output"
            or parts[1] in ("", ".", "..")
            or any(char in parts[1] for char in ("\\", ":", "\x00"))
        ):
            return None

        try:
            workspace = self.path(namespace_id, agent_id)
            output_root = workspace / "output"
            self._check_directory(workspace, create=False)
            self._check_directory(output_root, create=False)
            output_metadata = output_root.lstat()
            if self._is_redirected(output_metadata) or not stat.S_ISDIR(output_metadata.st_mode):
                return None

            candidate = output_root / parts[1]
            metadata = candidate.lstat()
            if self._is_redirected(metadata) or not stat.S_ISREG(metadata.st_mode):
                return None
            candidate.resolve(strict=True).relative_to(output_root.resolve(strict=True))
            return candidate
        except (OSError, RuntimeError, ValueError, PersistentAgentError):
            return None

    def remove(self, namespace_id: str, agent_id: str) -> None:
        shutil.rmtree(self.path(namespace_id, agent_id), ignore_errors=True)


class HostWorkspaceAccess:
    """The read and write scopes of one Execution's colleague; core checks every path against them."""

    def __init__(
        self,
        store: WorkspaceStore,
        namespace_id: str,
        agent_id: str,
        *,
        knowledge_root: str | None = None,
        permission_profile: str = "default",
    ) -> None:
        self._root = store.path(namespace_id, agent_id)
        self._store = store
        self._namespace_id = namespace_id
        self._agent_id = agent_id
        self._knowledge_root = self._validated_knowledge_root(knowledge_root)
        if permission_profile not in {"default", "automatic", "full_access"}:
            raise PersistentAgentError("invalid permission_profile")
        self._permission_profile = permission_profile

    @property
    def scope(self) -> tuple[str, str]:
        return self._namespace_id, self._agent_id

    @property
    def permission_profile(self) -> str:
        return self._permission_profile

    def _session_roots(self, execution: ExecutionContext) -> tuple[Path, Path]:
        if execution.namespace_id != self._namespace_id or execution.agent_id != self._agent_id:
            raise PersistentAgentError("Execution belongs to another colleague")
        return self._store.validate_session_paths(
            execution.namespace_id, execution.agent_id, execution.session_id
        )

    @staticmethod
    def _granted(execution: ExecutionContext, feature: str) -> bool:
        return feature in execution.startup_scope and feature in execution.source_scope

    def _source_ref(self) -> str:
        return f"workspace://{self._namespace_id}/{self._agent_id}"

    def _validated_knowledge_root(self, relative: str | None) -> Path | None:
        if relative is None:
            return None
        parts = relative.split("/") if isinstance(relative, str) else ()
        if (
            len(parts) != 3
            or parts[:2] != ["resources", "knowledge"]
            or re.fullmatch(r"[a-f0-9]{16}", parts[2]) is None
        ):
            raise PersistentAgentError("invalid published knowledge scope")
        return self._root.joinpath(*parts)

    def _existing_knowledge_root(self) -> Path | None:
        candidate = self._knowledge_root
        if candidate is None:
            return None
        try:
            workspace = self._root.resolve()
            for parent in (candidate, candidate.parent, candidate.parent.parent):
                if parent.is_symlink():
                    return None
            candidate.resolve().relative_to(workspace)
        except (OSError, ValueError):
            return None
        return candidate if candidate.is_dir() else None

    async def resolve_read_scope(self, execution: ExecutionContext) -> WorkspaceReadScope:
        return self.read_scope(execution)

    def read_scope(self, execution: ExecutionContext) -> WorkspaceReadScope:
        """The read scope of ``resolve_read_scope``, for host code that runs outside the event loop."""
        sessions, current = self._session_roots(execution)
        workspace_granted = self._granted(execution, WORKSPACE_READ)
        knowledge_root = self._existing_knowledge_root()
        knowledge_granted = (
            not workspace_granted
            and self._granted(execution, WORKSPACE_KNOWLEDGE_READ)
            and knowledge_root is not None
        )
        host_wide = workspace_granted and self._permission_profile == "full_access"
        allowed_roots = (Path(self._root.anchor),) if host_wide else (self._root,) if workspace_granted else ()
        if not host_wide and knowledge_granted and knowledge_root is not None:
            allowed_roots = (knowledge_root,)
        return WorkspaceReadScope(
            status="allowed" if workspace_granted or knowledge_granted else "forbidden",
            agent_id=execution.agent_id,
            session_id=execution.session_id,
            workspace_root=self._root,
            allowed_roots=allowed_roots,
            private_sessions_root=sessions,
            current_session_root=current if workspace_granted else None,
            source_ref=self._source_ref(),
            policy_revision=execution.policy_revision,
            principal_ref=execution.principal_ref,
            host_wide=host_wide,
        )

    async def resolve_write_scope(self, execution: ExecutionContext) -> WorkspaceWriteScope:
        sessions, current = self._session_roots(execution)
        granted = self._granted(execution, WORKSPACE_WRITE)
        host_wide = granted and self._permission_profile == "full_access"
        return WorkspaceWriteScope(
            status="allowed" if granted else "forbidden",
            agent_id=execution.agent_id,
            session_id=execution.session_id,
            workspace_root=self._root,
            allowed_write_roots=(Path(self._root.anchor),) if host_wide else (self._root,) if granted else (),
            private_sessions_root=sessions,
            current_session_root=current if granted else None,
            source_ref=self._source_ref(),
            policy_revision=execution.policy_revision,
            principal_ref=execution.principal_ref,
            host_wide=host_wide,
        )
