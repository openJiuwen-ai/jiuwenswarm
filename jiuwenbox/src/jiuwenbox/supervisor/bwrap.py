# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Bubblewrap (bwrap) sandbox wrapper.

Translates a :class:`SecurityPolicy` into ``bwrap`` command-line arguments.
The runtime adapter (``server/runtime/process.py``) spawns ``bwrap`` itself
because it needs ``pass_fds``/``preexec_fn``/``start_new_session`` knobs
that a generic process wrapper cannot express.
"""

from __future__ import annotations

import fnmatch
import logging
import posixpath
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import (
    BindRootEntries,
    CapabilityPolicy,
    FilesystemPolicy,
    NetworkPolicy,
    NetworkMode,
    NamespacePolicy,
    ProcessPolicy,
    SecurityPolicy,
)

configure_logging()
logger = logging.getLogger(__name__)

BWRAP_BINARY = "bwrap"


def _normalize_capability(capability: str) -> str:
    capability = capability.strip().upper()
    if capability == "ALL" or capability.startswith("CAP_"):
        return capability
    return f"CAP_{capability}"


def _same_path_bind_is_covered_by_root(src: str, dst: str) -> bool:
    return src == dst and dst != "/" and dst.startswith("/")


def _path_depth(path: str) -> int:
    return len([part for part in path.split("/") if part])


def _append_unique(paths: list[str], path: str) -> None:
    if path not in paths:
        paths.append(path)


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

    # ``--die-with-parent``: arm ``PR_SET_PDEATHSIG=SIGKILL`` on every bwrap
    # process in the sandbox chain (outer monitor, inner pid-namespace init,
    # and the launched command). When jiuwenbox-server dies for *any* reason
    # -- clean lifespan shutdown, crash, SIGKILL by an operator, parent
    # jiuwenswarm being SIGKILLed before its grace window expired -- the
    # kernel immediately SIGKILLs the bwrap monitor, which in turn cascades
    # the kill into the pid namespace via bwrap's own ``--die-with-parent``
    # propagation. Without this flag the sandbox-daemon.py + bwrap monitor
    # stay alive after box-server vanishes (they were spawned with
    # ``start_new_session=True`` so they own their own pgrp/session and
    # nothing else kills them), showing up as long-lived ``bwrap ...
    # --daemon /jiuwenbox/sandbox-daemon.py`` processes on the host that
    # outlive every jiuwenbox lifecycle. The defensive layer is intentional:
    # the normal teardown path (``runtime.stop`` -> SIGTERM/SIGKILL the
    # daemon pgrp from lifespan) handles the cooperative case, and this
    # flag covers every uncooperative one.
    die_with_parent: bool = True

    # filesystem
    ro_binds: list[tuple[str, str]] = field(default_factory=list)
    rw_binds: list[tuple[str, str]] = field(default_factory=list)
    device_binds: list[tuple[str, str]] = field(default_factory=list)
    dir_mounts: list[tuple[str, str | None]] = field(default_factory=list)
    tmpfs_mounts: list[str] = field(default_factory=list)
    remount_ro: list[str] = field(default_factory=list)
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
        cls._apply_environment(cfg, policy.environment)
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
        read_write_paths = set(fs.read_write)
        for path in fs.read_only:
            if path not in read_write_paths and path not in cfg.remount_ro:
                cfg.remount_ro.append(path)

        # read_only/read_write are also enforced by Landlock when available.
        # Only bind_mounts expose host paths in the sandbox.
        #
        # ``bind_root_entries`` is processed BEFORE ``bind_mounts`` on purpose:
        # bind_root_entries is the "wildcard / generic" parent mount (e.g.
        # ``host_root="/"`` expanded into every immediate child of the host
        # rootfs), while ``bind_mounts`` are user-explicit per-path overrides
        # (``/sandbox files allow|deny <path>`` lowered by jiuwenswarm's
        # ``sysop_builder``). bwrap's later-overrides-earlier mount semantics
        # means whichever entry we ``append`` last wins on conflicting paths;
        # putting bind_root_entries first guarantees the explicit
        # bind_mounts can selectively flip a child to rw (allow) or stack a
        # rw + remount-ro pair on top (deny) without being clobbered by the
        # wildcard parent re-binding the path back to ro afterwards.
        for spec in fs.bind_root_entries:
            BwrapConfig._apply_bind_root_entries(cfg, spec)
        for mount in fs.bind_mounts:
            if mount.mode == "ro":
                cfg.ro_binds.append((mount.host_path, mount.sandbox_path))
            else:
                cfg.rw_binds.append((mount.host_path, mount.sandbox_path))
        for device in fs.device:
            cfg.device_binds.append((device.host_path, device.sandbox_path))

    @staticmethod
    def _apply_bind_root_entries(cfg: BwrapConfig, spec: BindRootEntries) -> None:
        """Expand one ``BindRootEntries`` spec into per-child bwrap binds.

        Each immediate child of ``spec.host_root`` becomes its own
        ``--ro-bind``/``--bind`` so the sandbox sees the same names at
        ``spec.sandbox_path/<child>``. Hidden entries and excluded names are
        filtered out; non-regular non-directory entries (sockets, fifos,
        block/char devices, broken symlinks) are skipped to avoid bwrap
        failures.
        """
        host_root_str = spec.host_root
        host_root = Path(host_root_str)

        if not host_root.exists():
            logger.warning(
                "bind_root_entries: host_root %r does not exist; skipping",
                host_root_str,
            )
            return
        if not host_root.is_dir():
            logger.warning(
                "bind_root_entries: host_root %r is not a directory; skipping",
                host_root_str,
            )
            return

        sandbox_root = spec.sandbox_path.rstrip("/") or "/"

        try:
            children = sorted(host_root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.warning(
                "bind_root_entries: cannot list %r: %s; skipping",
                host_root_str,
                exc,
            )
            return

        for child in children:
            name = child.name
            if not spec.include_hidden and name.startswith("."):
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in spec.exclude):
                continue

            try:
                stat_result = child.stat()
            except OSError as exc:
                logger.warning(
                    "bind_root_entries: skipping %r: %s",
                    str(child),
                    exc,
                )
                continue

            mode_bits = stat_result.st_mode
            if not (stat_module.S_ISDIR(mode_bits) or stat_module.S_ISREG(mode_bits)):
                logger.debug(
                    "bind_root_entries: skipping non-regular entry %r",
                    str(child),
                )
                continue

            target = f"{sandbox_root}/{name}" if sandbox_root != "/" else f"/{name}"
            source = str(child)
            if spec.mode == "ro":
                cfg.ro_binds.append((source, target))
            else:
                cfg.rw_binds.append((source, target))

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

    @staticmethod
    def _apply_environment(cfg: BwrapConfig, environment: dict[str, str]) -> None:
        cfg.env.update(environment)

    @staticmethod
    def _drop_bwrap_managed_binds(
        binds: list[tuple[str, str]],
        bwrap_managed: set[str],
        kind: str,
    ) -> list[tuple[str, str]]:
        """Drop bind entries whose target shadows a bwrap-managed mount.

        bwrap always emits a fresh tmpfs at ``--proc`` and ``--dev`` targets
        and then needs to be able to ``open(..., O_CREAT)`` the per-device
        bind targets (e.g. ``/dev/null``) inside that tmpfs. If a policy
        later puts a read-only bind on top of ``/proc`` or ``/dev`` (the
        common case is ``bind_root_entries: { host_root: "/" }`` which
        expands into a ``--ro-bind /dev /dev`` child), bwrap fails with
        ``Can't create file at /dev/null: Permission denied`` because the
        ``open`` call lands on a read-only mount.

        Filtering here keeps the failure contained to a single ignored
        bind: the bwrap-owned tmpfs survives intact, ``--dev-bind`` for
        per-device entries works as designed, and operators get a debug
        log line that explains the conflict.
        """
        kept: list[tuple[str, str]] = []
        for src, dst in binds:
            normalized = dst.rstrip("/") or "/"
            if normalized in bwrap_managed:
                logger.debug(
                    "Skipping %s bind %s -> %s; the target shadows the "
                    "bwrap-managed tmpfs and would make --dev-bind fail "
                    "with 'Can't create file at <path>: Permission denied'",
                    kind,
                    src,
                    dst,
                )
                continue
            kept.append((src, dst))
        return kept

    def to_args(self) -> list[str]:
        """Convert this config into a bwrap argument list.

        ``to_args`` is side-effect free: ``self.ro_binds`` / ``self.rw_binds``
        are read-only inside this method so the same ``BwrapConfig`` instance
        can be serialised multiple times without losing or duplicating
        entries.
        """
        args: list[str] = [BWRAP_BINARY]
        bwrap_managed_mounts: set[str] = {
            self.proc_path.rstrip("/") or "/",
            self.dev_path.rstrip("/") or "/",
        }
        # Drop any bind that would overlay /dev or /proc; see
        # ``_drop_bwrap_managed_binds`` for why this is required.
        ro_binds_in = self._drop_bwrap_managed_binds(
            self.ro_binds, bwrap_managed_mounts, "read-only",
        )
        rw_binds_in = self._drop_bwrap_managed_binds(
            self.rw_binds, bwrap_managed_mounts, "read-write",
        )

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

        if self.die_with_parent:
            # Placed after the namespace flags so it visually groups with
            # other lifecycle controls in ``ps -ef`` output. bwrap accepts
            # the flag at any position before the command.
            args.append("--die-with-parent")

        for cap in self.cap_add:
            args.extend(["--cap-add", cap])
        for cap in self.cap_drop:
            args.extend(["--cap-drop", cap])

        root_ro_binds = [(src, dst) for src, dst in ro_binds_in if dst == "/"]
        root_rw_binds = [(src, dst) for src, dst in rw_binds_in if dst == "/"]
        ro_binds = [(src, dst) for src, dst in ro_binds_in if dst != "/"]
        rw_binds = [(src, dst) for src, dst in rw_binds_in if dst != "/"]
        if root_rw_binds:
            # A read-write root bind already exposes every absolute child path.
            # Re-binding private children such as /home/<user>/.jiuwenswarm can
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

        explicit_dir_paths = {
            self.proc_path,
            self.dev_path,
            *[path for path, _ in self.dir_mounts],
        }
        bind_targets = [*ro_binds, *rw_binds, *self.device_binds]
        auto_dir_paths = _bind_parent_dirs(bind_targets, explicit_dir_paths)
        auto_dir_mounts = [(path, None) for path in auto_dir_paths]
        dir_mounts = [*self.dir_mounts, *auto_dir_mounts]
        dir_mounts.sort(key=lambda item: _path_depth(item[0]))
        remount_ro_paths = set(self.remount_ro)
        read_only_dir_paths = {path for path, _ in dir_mounts if path in remount_ro_paths}
        writable_dir_mounts = [
            (path, permissions)
            for path, permissions in dir_mounts
            if path not in read_only_dir_paths
        ]
        tmpfs_mounts: list[str] = []
        for path in self.tmpfs_mounts:
            _append_unique(tmpfs_mounts, path)
        for path in sorted(read_only_dir_paths, key=_path_depth):
            _append_unique(tmpfs_mounts, path)
        created_paths = {
            "/",
            self.proc_path,
            self.dev_path,
            *[dst for _, dst in root_ro_binds],
            *[dst for _, dst in root_rw_binds],
            *[dst for _, dst in ro_binds],
            *[dst for _, dst in rw_binds],
            *[dst for _, dst in self.device_binds],
            *[path for path, _ in writable_dir_mounts],
            *tmpfs_mounts,
        }

        # create directories before binding over them
        creation_ops = [
            ("dir", path, permissions)
            for path, permissions in writable_dir_mounts
        ]
        creation_ops.extend(("tmpfs", path, None) for path in tmpfs_mounts)
        creation_ops.sort(key=lambda item: _path_depth(item[1]))
        for op, path, permissions in creation_ops:
            if op == "dir":
                if permissions:
                    args.extend(["--perms", permissions])
                args.extend(["--dir", path])
            else:
                args.extend(["--tmpfs", path])

        # read-only binds
        for src, dst in ro_binds:
            args.extend(["--ro-bind", src, dst])

        # read-write binds
        for src, dst in rw_binds:
            args.extend(["--bind", src, dst])

        for src, dst in self.device_binds:
            args.extend(["--dev-bind", src, dst])

        for path in sorted(self.remount_ro, key=_path_depth, reverse=True):
            if path not in created_paths:
                continue
            args.extend(["--remount-ro", path])

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


