"""Process-based runtime adapter (bare-metal mode).

Spawns box-supervisor as a subprocess for each sandbox.
"""

from __future__ import annotations

import asyncio
import base64
import grp
import json
import logging
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from jiuwenbox.models.policy import NetworkMode, SecurityPolicy
from jiuwenbox.models.sandbox import ExecResult
from jiuwenbox.server.runtime.base import RuntimeAdapter, RuntimeExecRequest
from jiuwenbox.supervisor import network as network_module

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ProcessRuntime(RuntimeAdapter):
    """Runtime that spawns supervisor as a local process."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._policy_paths: dict[str, Path] = {}
        self._network_modes: dict[str, NetworkMode] = {}
        self._netns_names: dict[str, str] = {}
        self._directory_roots: dict[str, Path] = {}
        self._log_dir = Path.home() / ".jiuwenbox" / "sandbox_logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_policy(policy_path: Path) -> SecurityPolicy:
        with open(policy_path) as f:
            data = yaml.safe_load(f)
        return SecurityPolicy.model_validate(data)

    @staticmethod
    def _build_supervisor_command(policy_path: Path, command: list[str]) -> list[str]:
        return [
            sys.executable, "-m", "jiuwenbox.supervisor.main",
            str(policy_path),
            *command,
        ]

    def _get_netns_name(self, sandbox_id: str) -> str:
        return self._netns_names.setdefault(
            sandbox_id,
            network_module.netns_name_for_sandbox(sandbox_id),
        )

    def _ensure_named_netns(self, sandbox_id: str, policy: SecurityPolicy) -> str | None:
        if policy.network.mode != NetworkMode.ISOLATED:
            return None

        namespace = self._get_netns_name(sandbox_id)
        if network_module.namespace_exists(namespace):
            return namespace

        network_module.create_named_namespace(namespace)
        try:
            network_module.setup_network_isolation(policy.network, namespace=namespace)
        except Exception:
            try:
                network_module.delete_named_namespace(namespace)
            except Exception:
                logger.warning(
                    "Failed to rollback network namespace %s after setup error",
                    namespace,
                    exc_info=True,
                )
            raise

        return namespace

    @staticmethod
    def _directory_spec(directory: object) -> tuple[str, str | None]:
        if isinstance(directory, str):
            return directory, None
        return getattr(directory, "path"), getattr(directory, "permissions", None)

    @staticmethod
    def _host_directory_name(sandbox_path: str) -> str:
        encoded = base64.urlsafe_b64encode(sandbox_path.encode()).decode()
        return encoded.rstrip("=")

    @staticmethod
    def _sandbox_root_for_policy(policy: SecurityPolicy) -> Path:
        return Path(policy.sandbox_workspace).expanduser()

    @staticmethod
    def _resolve_backing_identity(policy: SecurityPolicy) -> tuple[int, int]:
        if policy.namespace.user:
            # With bwrap user namespaces, the server process uid is mapped to
            # the sandbox run uid. Keep lifecycle backing dirs owned by that uid.
            return os.getuid(), os.getgid()

        try:
            uid = pwd.getpwnam(policy.process.run_as_user).pw_uid
        except KeyError:
            uid = 65534
        try:
            gid = grp.getgrnam(policy.process.run_as_group).gr_gid
        except KeyError:
            gid = 65534
        return uid, gid

    @staticmethod
    def _apply_directory_ownership(path: Path, uid: int, gid: int) -> bool:
        try:
            os.chown(path, uid, gid)
            return True
        except PermissionError:
            logger.warning(
                "Failed to chown policy directory %s to %d:%d; keeping current owner",
                path,
                uid,
                gid,
            )
            return False

    @staticmethod
    def _apply_directory_permissions(path: Path, permissions: str | None) -> None:
        if permissions is None:
            return
        os.chmod(path, int(permissions, 8))

    @staticmethod
    def _ensure_writable_when_chown_unavailable(path: Path, owner_applied: bool) -> None:
        if owner_applied:
            return

        if path.stat().st_uid == os.getuid():
            return

        mode = path.stat().st_mode & 0o777
        if mode & 0o005 and not mode & 0o002:
            fallback_mode = mode | 0o002
            logger.warning(
                "Relaxing policy directory %s permissions from %s to %s because chown failed",
                path,
                oct(mode),
                oct(fallback_mode),
            )
            os.chmod(path, fallback_mode)

    def _ensure_policy_directories(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
    ) -> list[dict[str, str]]:
        directories = policy.filesystem_policy.directories
        if not directories:
            return []

        sandbox_root = self._sandbox_root_for_policy(policy)
        sandbox_root.mkdir(parents=True, exist_ok=True)
        directory_root = self._directory_roots.get(sandbox_id)
        if directory_root is None:
            directory_root = Path(tempfile.mkdtemp(
                prefix=f"{sandbox_id}-dirs-",
                dir=sandbox_root,
            ))
            self._directory_roots[sandbox_id] = directory_root
        else:
            directory_root.mkdir(parents=True, exist_ok=True)

        uid, gid = self._resolve_backing_identity(policy)
        binds: list[dict[str, str]] = []
        for directory in directories:
            sandbox_path, permissions = self._directory_spec(directory)
            host_path = directory_root / self._host_directory_name(sandbox_path)
            host_path.mkdir(parents=True, exist_ok=True)
            owner_applied = self._apply_directory_ownership(host_path, uid, gid)
            self._apply_directory_permissions(host_path, permissions)
            self._ensure_writable_when_chown_unavailable(host_path, owner_applied)
            binds.append({
                "host_path": str(host_path),
                "sandbox_path": sandbox_path,
            })
        return binds

    @staticmethod
    def _wrap_command_in_namespace(command: list[str], namespace: str | None) -> list[str]:
        if not namespace:
            return command
        return [network_module.IP_BINARY, "netns", "exec", namespace, *command]

    @staticmethod
    def _apply_runtime_env(
        process_env: dict[str, str],
        *,
        netns_name: str | None,
        directory_binds: list[dict[str, str]],
    ) -> None:
        if netns_name:
            process_env["JIUWENBOX_NETNS_READY"] = "1"
        if directory_binds:
            process_env["JIUWENBOX_DIRECTORY_BINDS"] = json.dumps(
                directory_binds,
                separators=(",", ":"),
            )

    def _network_mode_for_cleanup(self, sandbox_id: str) -> NetworkMode | None:
        mode = self._network_modes.get(sandbox_id)
        if mode is not None:
            return mode

        policy_path = self._policy_paths.get(sandbox_id)
        if policy_path is None or not policy_path.exists():
            return None

        try:
            mode = self._load_policy(policy_path).network.mode
        except Exception:
            logger.warning("Failed to reload policy for sandbox %s during cleanup", sandbox_id, exc_info=True)
            return None

        self._network_modes[sandbox_id] = mode
        return mode

    async def create(
        self,
        sandbox_id: str,
        policy_path: Path,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> int:
        existing = self._processes.get(sandbox_id)
        if existing is not None:
            if existing.poll() is None:
                raise RuntimeError(f"Sandbox {sandbox_id} already has a running process")
            self._processes.pop(sandbox_id, None)

        policy = self._load_policy(policy_path)
        self._network_modes[sandbox_id] = policy.network.mode
        netns_name = self._ensure_named_netns(sandbox_id, policy)
        directory_binds = self._ensure_policy_directories(sandbox_id, policy)

        # Build supervisor command
        supervisor_cmd = self._wrap_command_in_namespace(
            self._build_supervisor_command(policy_path, command),
            netns_name,
        )

        process_env = {**os.environ, **(env or {})}
        self._apply_runtime_env(
            process_env,
            netns_name=netns_name,
            directory_binds=directory_binds,
        )

        log_file = self._log_dir / f"{sandbox_id}.log"

        logger.info("Spawning supervisor for %s: %s", sandbox_id, supervisor_cmd)

        try:
            with open(log_file, "w", encoding="utf-8") as log_fd:
                proc = subprocess.Popen(
                    supervisor_cmd,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    env=process_env,
                    cwd=workdir,
                    start_new_session=True,
                )
        except Exception:
            directory_root = self._directory_roots.pop(sandbox_id, None)
            if directory_root is not None:
                shutil.rmtree(directory_root, ignore_errors=True)
            if netns_name and network_module.namespace_exists(netns_name):
                network_module.delete_named_namespace(netns_name)
            self._network_modes.pop(sandbox_id, None)
            raise

        self._processes[sandbox_id] = proc
        self._policy_paths[sandbox_id] = Path(policy_path)
        logger.info("Supervisor started for %s (pid=%d)", sandbox_id, proc.pid)
        return proc.pid

    async def stop(self, sandbox_id: str, timeout: float = 10.0) -> None:
        proc = self._processes.get(sandbox_id)
        if proc is None:
            return
        if proc.poll() is not None:
            self._processes.pop(sandbox_id, None)
            return

        logger.info("Stopping sandbox %s (pid=%d)", sandbox_id, proc.pid)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            self._processes.pop(sandbox_id, None)
            return

        try:
            await asyncio.get_running_loop().run_in_executor(
                None, proc.wait, timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("SIGTERM timeout for %s, killing", sandbox_id)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                self._processes.pop(sandbox_id, None)
                return
            await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5.0)

        self._processes.pop(sandbox_id, None)

    async def is_running(self, sandbox_id: str) -> bool:
        proc = self._processes.get(sandbox_id)
        if proc is None:
            return False
        return proc.poll() is None

    async def exec(
        self,
        sandbox_id: str,
        request: RuntimeExecRequest,
    ) -> ExecResult:
        """Execute a command by spawning a new supervisor inside the sandbox netns."""
        policy_path = self._policy_paths.get(sandbox_id)
        if policy_path is None:
            return ExecResult(exit_code=1, stderr="No policy found for sandbox")

        netns_name = None
        if self._network_mode_for_cleanup(sandbox_id) == NetworkMode.ISOLATED:
            policy = self._load_policy(policy_path)
            netns_name = self._ensure_named_netns(sandbox_id, policy)
        else:
            policy = self._load_policy(policy_path)
        directory_binds = self._ensure_policy_directories(sandbox_id, policy)

        supervisor_cmd = self._wrap_command_in_namespace(
            self._build_supervisor_command(policy_path, list(request.command)),
            netns_name,
        )

        process_env = {**os.environ, **(request.env or {})}
        self._apply_runtime_env(
            process_env,
            netns_name=netns_name,
            directory_binds=directory_binds,
        )

        try:
            logger.info("Executing command in sandbox %s: %s", sandbox_id, supervisor_cmd)
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    supervisor_cmd,
                    input=request.stdin_data,
                    capture_output=True,
                    timeout=request.timeout,
                    env=process_env,
                    cwd=request.workdir,
                ),
            )
            return ExecResult(
                exit_code=result.returncode,
                stdout=result.stdout.decode(errors="replace"),
                stderr=result.stderr.decode(errors="replace"),
            )
        except subprocess.TimeoutExpired:
            return ExecResult(exit_code=124, stderr="Command timed out")

    async def cleanup(self, sandbox_id: str) -> None:
        await self.stop(sandbox_id)
        self._processes.pop(sandbox_id, None)
        policy_path = self._policy_paths.pop(sandbox_id, None)
        network_mode = self._network_modes.pop(sandbox_id, None)
        if network_mode is None and policy_path is not None and policy_path.exists():
            try:
                network_mode = self._load_policy(policy_path).network.mode
            except Exception:
                logger.warning(
                    "Failed to reload policy for sandbox %s during namespace cleanup",
                    sandbox_id,
                    exc_info=True,
                )

        if network_mode == NetworkMode.ISOLATED:
            namespace = self._netns_names.pop(
                sandbox_id,
                network_module.netns_name_for_sandbox(sandbox_id),
            )
            if network_module.namespace_exists(namespace):
                network_module.delete_named_namespace(namespace)
        else:
            self._netns_names.pop(sandbox_id, None)

        directory_root = self._directory_roots.pop(sandbox_id, None)
        if directory_root is not None and directory_root.exists():
            shutil.rmtree(directory_root, ignore_errors=True)

        log_file = self._log_dir / f"{sandbox_id}.log"
        log_file.unlink(missing_ok=True)
