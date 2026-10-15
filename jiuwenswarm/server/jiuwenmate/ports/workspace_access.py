"""``PersistentPorts.workspace_access``: the office directory a colleague may read (PA-14).

Each colleague owns one directory under the JiuwenMate data root, apart from the
database, Templates and windows. The read scope is that directory alone, and only
for a Request whose startup and source scopes both include ``workspace_read``;
every other Request is refused. core asks again before every file read, so the
port never caches a decision, and it never creates, moves or reads files.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.sqlite_store import private_dir
from openjiuwen.harness.persistent_agent import ExecutionContext, WorkspaceReadScope

WORKSPACE_READ = "workspace_read"

_DIR_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class WorkspaceStore:
    """Office directories, one per colleague, laid out as ``<root>/<namespace_id>/<agent_id>``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()
        private_dir(self._root)

    def path(self, namespace_id: str, agent_id: str) -> Path:
        for name, value in (("namespace_id", namespace_id), ("agent_id", agent_id)):
            if not isinstance(value, str) or not _DIR_NAME.fullmatch(value):
                raise PersistentAgentError(f"invalid {name} for an office directory")
        return self._root / namespace_id / agent_id

    def ensure(self, namespace_id: str, agent_id: str) -> Path:
        path = self.path(namespace_id, agent_id)
        private_dir(path.parent)
        private_dir(path)
        return path

    def remove(self, namespace_id: str, agent_id: str) -> None:
        shutil.rmtree(self.path(namespace_id, agent_id), ignore_errors=True)


class HostWorkspaceAccess:
    """The read scope of one Execution's colleague; core checks every path against it."""

    def __init__(self, store: WorkspaceStore, namespace_id: str, agent_id: str) -> None:
        self._root = store.path(namespace_id, agent_id)
        self._namespace_id = namespace_id
        self._agent_id = agent_id

    @property
    def scope(self) -> tuple[str, str]:
        return self._namespace_id, self._agent_id

    async def resolve_read_scope(self, execution: ExecutionContext) -> WorkspaceReadScope:
        if execution.namespace_id != self._namespace_id or execution.agent_id != self._agent_id:
            raise PersistentAgentError("Execution belongs to another colleague")
        granted = WORKSPACE_READ in execution.startup_scope and WORKSPACE_READ in execution.source_scope
        return WorkspaceReadScope(
            status="allowed" if granted else "forbidden",
            agent_id=execution.agent_id,
            session_id=execution.session_id,
            workspace_root=self._root,
            allowed_roots=(self._root,) if granted else (),
            private_sessions_root=None,
            current_session_root=None,
            source_ref=f"workspace://{self._namespace_id}/{self._agent_id}",
            policy_revision=execution.policy_revision,
            principal_ref=execution.principal_ref,
        )
