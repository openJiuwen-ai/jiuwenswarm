"""Bubblewrap (bwrap) sandbox wrapper.

Translates a SecurityPolicy into bwrap command-line arguments and manages
the lifecycle of the sandboxed process.
"""

from __future__ import annotations

import logging
import posixpath
import signal
import subprocess
from dataclasses import dataclass, field
from typing import IO

from jiuwenbox.models.policy import (
    CapabilityPolicy,
    FilesystemPolicy,
    NetworkPolicy,
    NetworkMode,
    NamespacePolicy,
    ProcessPolicy,
    SecurityPolicy,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BWRAP_BINARY = "/usr/bin/bwrap"


def _normalize_capability(capability: str) -> str:
    capability = capability.strip().upper()
    if capability == "ALL" or capability.startswith("CAP_"):
        return capability
    return f"CAP_{capability}"


def _same_path_bind_is_covered_by_root(src: str, dst: str) -> bool:
    return src == dst and dst != "/" and dst.startswith("/")


def _path_depth(path: str) -> int:
    return len([part for part in path.split("/") if part])


def _bind_parent_dirs(binds: list[tuple[str, str]], existing_dirs: set[str]) -> list[str]:
    """Return parent directories bwrap must create before nested bind targets."""
    result: list[str] = []
    seen = {"/", *existing_dirs}

    for _, target in binds:
        parent = posixpath.dirname(target.rstrip("/") or "/")
        parents: list[str] = []
        while parent and parent != "/" and parent not in seen:
            parents.append(parent)
            parent = posixpath.dirname(parent)

        for path in reversed(parents):
            if path not in seen:
                seen.add(path)
                result.append(path)

    return result


@dataclass
class BwrapConfig:
    """Configuration that maps to bwrap CLI arguments."""

    command: list[str] = field(default_factory=list)
    uid: int | None = None
    gid: int | None = None

    # namespace isolation flags
    unshare_net: bool = True
    unshare_ipc: bool = True
    unshare_cgroup: bool = True
    unshare_pid: bool = True
    unshare_uts: bool = True
    unshare_user: bool = True
    cap_add: list[str] = field(default_factory=list)
    cap_drop: list[str] = field(default_factory=list)

    # filesystem
    rootfs: str | None = None
    ro_binds: list[tuple[str, str]] = field(default_factory=list)
    rw_binds: list[tuple[str, str]] = field(default_factory=list)
    dir_mounts: list[tuple[str, str | None]] = field(default_factory=list)
    tmpfs_mounts: list[str] = field(default_factory=list)
    dev_path: str = "/dev"
    proc_path: str = "/proc"

    # seccomp
    seccomp_fd: int | None = None

    # environment
    env: dict[str, str] = field(default_factory=dict)
    workdir: str | None = None

    # extra raw arguments
    extra_args: list[str] = field(default_factory=list)

    @classmethod
    def from_policy(cls, policy: SecurityPolicy, command: list[str]) -> BwrapConfig:
        """Build a BwrapConfig from a SecurityPolicy."""
        cfg = cls(command=command)
        cls._apply_filesystem(cfg, policy.filesystem_policy)
        cls._apply_process(cfg, policy.process)
        cls._apply_namespace(cfg, policy.namespace)
        cls._apply_capabilities(cfg, policy.capabilities)
        cls._apply_network(cfg, policy.network)
        return cfg

    def add_dir_mount(self, path: str, permissions: str | None = None) -> None:
        for index, (existing_path, existing_permissions) in enumerate(self.dir_mounts):
            if existing_path == path:
                if existing_permissions is None and permissions is not None:
                    self.dir_mounts[index] = (path, permissions)
                return
        self.dir_mounts.append((path, permissions))

    @staticmethod
    def _apply_filesystem(cfg: BwrapConfig, fs: FilesystemPolicy) -> None:
        # read_only/read_write are in-sandbox access rules enforced by
        # Landlock. Only bind_mounts expose host paths in the sandbox.
        for mount in fs.bind_mounts:
            if mount.mode == "ro":
                cfg.ro_binds.append((mount.host_path, mount.sandbox_path))
            else:
                cfg.rw_binds.append((mount.host_path, mount.sandbox_path))

    @staticmethod
    def _apply_process(cfg: BwrapConfig, proc: ProcessPolicy) -> None:
        # Resolve user/group names to numeric IDs; fall back to 65534 (nobody)
        import pwd
        import grp

        try:
            cfg.uid = pwd.getpwnam(proc.run_as_user).pw_uid
        except KeyError:
            cfg.uid = 65534
        try:
            cfg.gid = grp.getgrnam(proc.run_as_group).gr_gid
        except KeyError:
            cfg.gid = 65534

    @staticmethod
    def _apply_namespace(cfg: BwrapConfig, namespace: NamespacePolicy) -> None:
        cfg.unshare_user = namespace.user
        cfg.unshare_pid = namespace.pid
        cfg.unshare_ipc = namespace.ipc
        cfg.unshare_cgroup = namespace.cgroup
        cfg.unshare_uts = namespace.uts

    @staticmethod
    def _apply_capabilities(cfg: BwrapConfig, capabilities: CapabilityPolicy) -> None:
        cfg.cap_add = [_normalize_capability(cap) for cap in capabilities.add]
        cfg.cap_drop = [_normalize_capability(cap) for cap in capabilities.drop]

    @staticmethod
    def _apply_network(cfg: BwrapConfig, net: NetworkPolicy) -> None:
        cfg.unshare_net = net.mode == NetworkMode.ISOLATED

    def to_args(self) -> list[str]:
        """Convert this config into a bwrap argument list."""
        args: list[str] = [BWRAP_BINARY]

        if self.unshare_user:
            args.append("--unshare-user")
        if self.unshare_pid:
            args.append("--unshare-pid")
        if self.unshare_net:
            args.append("--unshare-net")
        if self.unshare_ipc:
            args.append("--unshare-ipc")
        if self.unshare_cgroup:
            args.append("--unshare-cgroup")
        if self.unshare_uts:
            args.append("--unshare-uts")

        if self.unshare_user and self.uid is not None:
            args.extend(["--uid", str(self.uid)])
        if self.unshare_user and self.gid is not None:
            args.extend(["--gid", str(self.gid)])

        for cap in self.cap_add:
            args.extend(["--cap-add", cap])
        for cap in self.cap_drop:
            args.extend(["--cap-drop", cap])

        root_ro_binds = [(src, dst) for src, dst in self.ro_binds if dst == "/"]
        root_rw_binds = [(src, dst) for src, dst in self.rw_binds if dst == "/"]
        ro_binds = [(src, dst) for src, dst in self.ro_binds if dst != "/"]
        rw_binds = [(src, dst) for src, dst in self.rw_binds if dst != "/"]
        if root_rw_binds:
            # A read-write root bind already exposes every absolute child path.
            # Re-binding private children such as /home/<user>/.jiuwenclaw can
            # fail because bwrap may open sources after entering userns.
            # Keep synthetic binds whose source differs from the sandbox path,
            # such as the Landlock launcher mounted into /run.
            ro_binds = [
                (src, dst)
                for src, dst in ro_binds
                if not _same_path_bind_is_covered_by_root(src, dst)
            ]
            rw_binds = [
                (src, dst)
                for src, dst in rw_binds
                if not _same_path_bind_is_covered_by_root(src, dst)
            ]
        elif root_ro_binds:
            # Read-only child binds are redundant under a read-only root; keep
            # read-write children because they intentionally override root RO.
            ro_binds = [
                (src, dst)
                for src, dst in ro_binds
                if not _same_path_bind_is_covered_by_root(src, dst)
            ]

        # A root bind must be applied before more specific mounts. Otherwise a
        # policy entry for "/" hides bwrap-created /proc, /dev, /tmp, /run, etc.
        for src, dst in root_ro_binds:
            args.extend(["--ro-bind", src, dst])
        for src, dst in root_rw_binds:
            args.extend(["--bind", src, dst])

        # mount /proc and /dev
        args.extend(["--proc", self.proc_path])
        args.extend(["--dev", self.dev_path])

        explicit_dir_paths = {path for path, _ in self.dir_mounts}
        auto_dir_mounts = [
            (path, None)
            for path in _bind_parent_dirs([*ro_binds, *rw_binds], explicit_dir_paths)
        ]
        dir_mounts = [*self.dir_mounts, *auto_dir_mounts]
        dir_mounts.sort(key=lambda item: _path_depth(item[0]))

        # create directories before binding over them
        for path, permissions in dir_mounts:
            if permissions:
                args.extend(["--perms", permissions])
            args.extend(["--dir", path])

        # read-only binds
        for src, dst in ro_binds:
            args.extend(["--ro-bind", src, dst])

        # read-write binds
        for src, dst in rw_binds:
            args.extend(["--bind", src, dst])

        # tmpfs mounts
        for path in self.tmpfs_mounts:
            args.extend(["--tmpfs", path])

        if self.seccomp_fd is not None:
            args.extend(["--seccomp", str(self.seccomp_fd)])

        if self.workdir:
            args.extend(["--chdir", self.workdir])

        args.extend(self.extra_args)

        # environment variables
        for key, value in self.env.items():
            args.extend(["--setenv", key, value])

        # the command to run inside the sandbox
        args.extend(self.command)

        return args


class BwrapProcess:
    """Manages a bubblewrap sandboxed process."""

    def __init__(self, config: BwrapConfig) -> None:
        self._config = config
        self._process: subprocess.Popen | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def returncode(self) -> int | None:
        if self._process is None:
            return None
        return self._process.returncode

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None

    def start(
        self,
        stdin: int | IO | None = None,
        stdout: int | IO | None = None,
        stderr: int | IO | None = None,
    ) -> None:
        """Spawn the bwrap process."""
        if self._process is not None and self.is_running:
            raise RuntimeError("Process already running")

        args = self._config.to_args()
        logger.debug("Starting bwrap with args: %s", args)
        pass_fds = ()
        if self._config.seccomp_fd is not None:
            pass_fds = (self._config.seccomp_fd,)

        self._process = subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pass_fds=pass_fds,
        )

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the process to exit and return the exit code."""
        if self._process is None:
            raise RuntimeError("Process not started")
        self._process.wait(timeout=timeout)
        return self._process.returncode

    def stop(self, timeout: float = 10.0) -> int | None:
        """Gracefully stop: SIGTERM then SIGKILL after timeout."""
        if self._process is None or not self.is_running:
            return self.returncode

        logger.info("Sending SIGTERM to bwrap pid %d", self._process.pid)
        self._process.send_signal(signal.SIGTERM)

        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("SIGTERM timeout, sending SIGKILL to pid %d", self._process.pid)
            self._process.kill()
            self._process.wait(timeout=5.0)

        return self._process.returncode
