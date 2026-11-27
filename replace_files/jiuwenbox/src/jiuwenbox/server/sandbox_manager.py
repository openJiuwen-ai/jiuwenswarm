"""Sandbox lifecycle manager.

Coordinates runtime adapters, policy engine, and audit logger to manage
the full lifecycle of sandboxes: create -> start -> stop -> delete.
Persists sandbox state to disk for crash recovery.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import textwrap
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jiuwenbox.models.common import AuditEventType
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import (
    ExecResult,
    PolicyMode,
    SandboxPhase,
    SandboxRef,
    SandboxSpec,
)
from jiuwenbox.server.audit_logger import AuditLogger
from jiuwenbox.server.policy_engine import PolicyEngine
from jiuwenbox.server.runtime.base import RuntimeAdapter, RuntimeExecRequest
from jiuwenbox.server.runtime.process import ProcessRuntime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_POLICY_PATH_ENV = "JIUWENBOX_DEFAULT_POLICY_PATH"


@dataclass(frozen=True)
class SandboxExecRequest:
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin_data: bytes | None = None
    timeout: float | None = None


@dataclass(frozen=True)
class SandboxListRequest:
    sandbox_path: str
    recursive: bool = False
    max_depth: int | None = None
    include_files: bool = True
    include_dirs: bool = True


class SandboxNotFoundError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.error("%s: %s", self.__class__.__name__, str(self))


class SandboxStateError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.error("%s: %s", self.__class__.__name__, str(self))


class SandboxManager:
    """Manages sandbox lifecycle and state."""

    def __init__(
        self,
        runtime: RuntimeAdapter | None = None,
        policy_engine: PolicyEngine | None = None,
        audit_logger: AuditLogger | None = None,
        state_dir: Path | None = None,
        default_policy_path: Path | None = None,
    ) -> None:
        self.runtime = runtime or ProcessRuntime()
        self.policy_engine = policy_engine or PolicyEngine()
        self.audit = audit_logger or AuditLogger()
        self.state_dir = state_dir or Path.home() / ".jiuwenbox" / "sandboxes"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.default_policy_path = (
            Path(default_policy_path)
            if default_policy_path is not None
            else self._resolve_default_policy_path()
        )
        self.default_policy = self._load_default_policy()

        self._lock = asyncio.Lock()
        self._sandboxes: dict[str, SandboxRef] = {}
        self._policies: dict[str, SecurityPolicy] = {}
        self._load_state()

    @staticmethod
    def _resolve_default_policy_path() -> Path:
        env_path = os.environ.get(DEFAULT_POLICY_PATH_ENV)
        if env_path:
            return Path(env_path).expanduser()
        return Path(__file__).resolve().parents[3] / "configs" / "default-policy.yaml"

    def _load_default_policy(self) -> SecurityPolicy:
        if self.default_policy_path.exists():
            return self.policy_engine.load_policy_from_file(self.default_policy_path)

        logger.warning(
            "Default policy file not found at %s, falling back to SecurityPolicy defaults",
            self.default_policy_path,
        )
        return SecurityPolicy()

    def _resolve_effective_policy(
        self,
        policy_data: SecurityPolicy | Mapping[str, object] | None,
        policy_mode: PolicyMode,
    ) -> SecurityPolicy:
        base_policy = self.default_policy.model_copy(deep=True)
        if policy_data is None:
            return base_policy

        if isinstance(policy_data, SecurityPolicy):
            policy_payload: SecurityPolicy | Mapping[str, object] = policy_data
        else:
            policy_payload = dict(policy_data)

        if policy_mode == PolicyMode.APPEND:
            return self.policy_engine.merge_policy(base_policy, policy_payload)

        if isinstance(policy_payload, SecurityPolicy):
            return policy_payload.model_copy(deep=True)

        policy_payload.setdefault("sandbox_workspace", base_policy.sandbox_workspace)
        return SecurityPolicy.model_validate(policy_payload)

    def _load_state(self) -> None:
        """Load persisted sandbox state on startup."""
        for state_file in self.state_dir.glob("*.json"):
            try:
                data = json.loads(state_file.read_text())
                ref = SandboxRef.model_validate(data)
                self._sandboxes[ref.id] = ref
                logger.info("Loaded sandbox state: %s (%s)", ref.id, ref.phase.value)
            except Exception:
                logger.warning("Failed to load state from %s", state_file, exc_info=True)

    def _save_state(self, sandbox: SandboxRef) -> None:
        """Persist a single sandbox's state to disk."""
        path = self.state_dir / f"{sandbox.id}.json"
        path.write_text(sandbox.model_dump_json(indent=2))

    def _delete_state(self, sandbox_id: str) -> None:
        path = self.state_dir / f"{sandbox_id}.json"
        path.unlink(missing_ok=True)

    def _get_sandbox(self, sandbox_id: str) -> SandboxRef:
        ref = self._sandboxes.get(sandbox_id)
        if ref is None:
            raise SandboxNotFoundError(f"Sandbox '{sandbox_id}' not found")
        return ref

    async def create_sandbox(
        self,
        spec: SandboxSpec,
        command: list[str] | None = None,
        policy_data: SecurityPolicy | Mapping[str, object] | None = None,
        policy_mode: PolicyMode = PolicyMode.OVERRIDE,
    ) -> SandboxRef:
        """Create a new sandbox."""
        async with self._lock:
            sandbox_id = str(uuid.uuid4())[:12]
            effective_command = list(command or [])
            policy = self._resolve_effective_policy(policy_data, policy_mode)
            self.policy_engine.validate_policy(policy)
            # Create sandbox ref
            ref = SandboxRef(
                id=sandbox_id,
                phase=SandboxPhase.PROVISIONING,
                command=effective_command,
                workdir=spec.workdir,
                env=dict(spec.env),
            )
            self._sandboxes[sandbox_id] = ref
            self._policies[sandbox_id] = policy
            self._save_state(ref)

            self.audit.log(AuditEventType.SANDBOX_CREATED, sandbox_id)

            # Write resolved policy
            policy_path = self.policy_engine.write_sandbox_policy(sandbox_id, policy)
            self.audit.log(AuditEventType.POLICY_APPLIED, sandbox_id, policy_name=policy.name)

            # Start via runtime
            try:
                pid = await self.runtime.create(
                    sandbox_id=sandbox_id,
                    policy_path=policy_path,
                    command=ref.command,
                    workdir=ref.workdir,
                    env=ref.env,
                )
                ref.phase = SandboxPhase.READY
                ref.pid = pid
                ref.started_at = datetime.now(timezone.utc)
            except Exception as e:
                ref.phase = SandboxPhase.ERROR
                ref.error_message = str(e)
                logger.error("Failed to create sandbox %s: %s", sandbox_id, e)

            self._save_state(ref)
            return ref

    async def get_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            # Refresh running status
            if ref.phase == SandboxPhase.READY:
                if not await self.runtime.is_running(sandbox_id):
                    ref.phase = SandboxPhase.STOPPED
                    self._save_state(ref)
            return ref

    async def list_sandboxes(self) -> list[SandboxRef]:
        async with self._lock:
            return list(self._sandboxes.values())

    async def start_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            return await self._start_sandbox_unlocked(sandbox_id)

    async def _start_sandbox_unlocked(self, sandbox_id: str) -> SandboxRef:
        ref = self._get_sandbox(sandbox_id)
        if ref.phase == SandboxPhase.READY:
            if await self.runtime.is_running(sandbox_id):
                return ref

        policy = self._policies.get(sandbox_id)
        if policy is None:
            policy_path = self.policy_engine.get_sandbox_policy_path(sandbox_id)
            if policy_path:
                policy = self.policy_engine.load_policy_from_file(policy_path)
            else:
                raise SandboxStateError(f"No policy found for sandbox {sandbox_id}")

        policy_path = self.policy_engine.get_sandbox_policy_path(sandbox_id)
        if policy_path is None:
            policy_path = self.policy_engine.write_sandbox_policy(sandbox_id, policy)

        try:
            pid = await self.runtime.create(
                sandbox_id=sandbox_id,
                policy_path=policy_path,
                command=ref.command,
                workdir=ref.workdir,
                env=ref.env,
            )
            ref.phase = SandboxPhase.READY
            ref.pid = pid
            ref.started_at = datetime.now(timezone.utc)
            ref.error_message = None
        except Exception as e:
            logger.error("Failed to start sandbox %s: %s", sandbox_id, e, exc_info=True)
            ref.phase = SandboxPhase.ERROR
            ref.error_message = str(e)

        self._save_state(ref)
        self.audit.log(AuditEventType.SANDBOX_STARTED, sandbox_id)
        return ref

    async def stop_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            return await self._stop_sandbox_unlocked(sandbox_id)

    async def _stop_sandbox_unlocked(self, sandbox_id: str) -> SandboxRef:
        ref = self._get_sandbox(sandbox_id)
        await self.runtime.stop(sandbox_id)
        ref.phase = SandboxPhase.STOPPED
        ref.pid = None
        self._save_state(ref)
        self.audit.log(AuditEventType.SANDBOX_STOPPED, sandbox_id)
        return ref

    async def restart_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            await self._stop_sandbox_unlocked(sandbox_id)
            return await self._start_sandbox_unlocked(sandbox_id)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            ref.phase = SandboxPhase.DELETING
            self._save_state(ref)

            await self.runtime.cleanup(sandbox_id)
            self.policy_engine.delete_sandbox_policy(sandbox_id)
            self.audit.log(AuditEventType.SANDBOX_DELETED, sandbox_id)

            self._sandboxes.pop(sandbox_id, None)
            self._policies.pop(sandbox_id, None)
            self._delete_state(sandbox_id)

    async def exec_in_sandbox(
        self,
        sandbox_id: str,
        request: SandboxExecRequest,
    ) -> ExecResult:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            if ref.phase != SandboxPhase.READY:
                raise SandboxStateError(
                    f"Cannot exec in sandbox '{sandbox_id}': state is {ref.phase.value}"
                )

            self.audit.log(
                AuditEventType.EXEC_COMMAND, sandbox_id,
                command=request.command, workdir=request.workdir,
            )

        return await self.runtime.exec(
            sandbox_id,
            RuntimeExecRequest(
                command=request.command,
                workdir=request.workdir,
                env=request.env,
                stdin_data=request.stdin_data,
                timeout=request.timeout,
            ),
        )

    async def upload_file_to_sandbox(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
    ) -> None:
        self.audit.log(
            AuditEventType.FILE_TRANSFER,
            sandbox_id,
            direction="upload",
            sandbox_path=sandbox_path,
        )

        encoded_content = base64.b64encode(content)
        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                command=[
                    "/usr/bin/bash",
                    "-c",
                    (
                        "set -euo pipefail; "
                        'target="$1"; '
                        'parent=$(/usr/bin/dirname -- "$target"); '
                        '/usr/bin/mkdir -p -- "$parent"; '
                        '/usr/bin/base64 -d > "$target"'
                    ),
                    "jiuwenbox-upload",
                    sandbox_path,
                ],
                stdin_data=encoded_content,
            ),
        )
        if result.exit_code != 0:
            raise SandboxStateError(
                f"Failed to upload file to '{sandbox_path}': {result.stderr or result.stdout}"
            )

    async def download_file_from_sandbox(
        self,
        sandbox_id: str,
        sandbox_path: str,
    ) -> bytes:
        self.audit.log(
            AuditEventType.FILE_TRANSFER,
            sandbox_id,
            direction="download",
            sandbox_path=sandbox_path,
        )

        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                command=[
                    "/usr/bin/bash",
                    "-c",
                    (
                        "set -euo pipefail; "
                        'target="$1"; '
                        'if [ ! -e "$target" ]; then exit 44; fi; '
                        'if [ -d "$target" ]; then exit 45; fi; '
                        '/usr/bin/base64 -w 0 -- "$target"'
                    ),
                    "jiuwenbox-download",
                    sandbox_path,
                ],
            ),
        )
        if result.exit_code == 44:
            raise FileNotFoundError(sandbox_path)
        if result.exit_code == 45:
            raise SandboxStateError(f"Sandbox path '{sandbox_path}' is a directory")
        if result.exit_code != 0:
            raise SandboxStateError(
                f"Failed to download file from '{sandbox_path}': {result.stderr or result.stdout}"
            )

        try:
            return base64.b64decode(result.stdout.encode(), validate=True)
        except binascii.Error as exc:
            raise SandboxStateError(
                f"Failed to decode downloaded file from '{sandbox_path}'"
            ) from exc

    async def list_files_in_sandbox(
        self,
        sandbox_id: str,
        request: SandboxListRequest,
    ) -> list[dict[str, object]]:
        script = textwrap.dedent(
            """
            import datetime
            import json
            import os
            from pathlib import Path
            import sys

            root = Path(sys.argv[1])
            recursive = sys.argv[2] == "1"
            max_depth = None if sys.argv[3] == "" else int(sys.argv[3])
            include_files = sys.argv[4] == "1"
            include_dirs = sys.argv[5] == "1"

            if not root.exists():
                sys.exit(44)
            if not root.is_dir():
                sys.exit(45)

            if recursive:
                entries = root.rglob("*")
            else:
                entries = root.iterdir()

            items = []
            for entry in entries:
                try:
                    stat = entry.stat()
                except OSError:
                    continue

                rel_parts = entry.relative_to(root).parts
                if max_depth is not None and len(rel_parts) > max_depth:
                    continue

                is_dir = entry.is_dir()
                if is_dir and not include_dirs:
                    continue
                if not is_dir and not include_files:
                    continue

                items.append({
                    "name": entry.name,
                    "path": str(entry),
                    "size": 0 if is_dir else stat.st_size,
                    "is_directory": is_dir,
                    "modified_time": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "type": None if is_dir else os.path.splitext(entry.name)[1] or None,
                })

            items.sort(key=lambda item: item["path"])
            print(json.dumps(items, ensure_ascii=False))
            """
        ).strip()

        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                command=[
                    "/usr/bin/python3",
                    "-c",
                    script,
                    request.sandbox_path,
                    "1" if request.recursive else "0",
                    "" if request.max_depth is None else str(request.max_depth),
                    "1" if request.include_files else "0",
                    "1" if request.include_dirs else "0",
                ],
            ),
        )
        if result.exit_code == 44:
            raise FileNotFoundError(request.sandbox_path)
        if result.exit_code == 45:
            raise SandboxStateError(f"Sandbox path '{request.sandbox_path}' is not a directory")
        if result.exit_code != 0:
            raise SandboxStateError(
                f"Failed to list files in '{request.sandbox_path}': {result.stderr or result.stdout}"
            )
        return json.loads(result.stdout or "[]")

    async def search_files_in_sandbox(
        self,
        sandbox_id: str,
        sandbox_path: str,
        pattern: str,
        exclude_patterns: list[str] | None = None,
    ) -> list[dict[str, object]]:
        script = textwrap.dedent(
            """
            import datetime
            import fnmatch
            import json
            import os
            from pathlib import Path
            import sys

            root = Path(sys.argv[1])
            pattern = sys.argv[2]
            exclude_patterns = json.loads(sys.argv[3])

            if not root.exists():
                sys.exit(44)
            if not root.is_dir():
                sys.exit(45)

            items = []
            for entry in root.rglob("*"):
                if not entry.is_file():
                    continue
                rel = str(entry.relative_to(root))
                if not (fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(rel, pattern)):
                    continue
                if any(fnmatch.fnmatch(entry.name, item) or fnmatch.fnmatch(rel, item) for item in exclude_patterns):
                    continue

                try:
                    stat = entry.stat()
                except OSError:
                    continue

                items.append({
                    "name": entry.name,
                    "path": str(entry),
                    "size": stat.st_size,
                    "is_directory": False,
                    "modified_time": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "type": os.path.splitext(entry.name)[1] or None,
                })

            items.sort(key=lambda item: item["path"])
            print(json.dumps(items, ensure_ascii=False))
            """
        ).strip()

        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                command=[
                    "/usr/bin/python3",
                    "-c",
                    script,
                    sandbox_path,
                    pattern,
                    json.dumps(exclude_patterns or []),
                ],
            ),
        )
        if result.exit_code == 44:
            raise FileNotFoundError(sandbox_path)
        if result.exit_code == 45:
            raise SandboxStateError(f"Sandbox path '{sandbox_path}' is not a directory")
        if result.exit_code != 0:
            raise SandboxStateError(
                f"Failed to search files in '{sandbox_path}': {result.stderr or result.stdout}"
            )
        return json.loads(result.stdout or "[]")

    async def get_logs(self, sandbox_id: str) -> str:
        async with self._lock:
            self._get_sandbox(sandbox_id)
            return self.audit.read_logs_raw(sandbox_id)

    async def get_policy(self, sandbox_id: str) -> SecurityPolicy | None:
        async with self._lock:
            policy = self._policies.get(sandbox_id)
            if policy is not None:
                return policy

            if self._sandboxes.get(sandbox_id) is None:
                return None

            policy_path = self.policy_engine.get_sandbox_policy_path(sandbox_id)
            if policy_path is None:
                return None

            policy = self.policy_engine.load_policy_from_file(policy_path)
            self._policies[sandbox_id] = policy
            return policy
