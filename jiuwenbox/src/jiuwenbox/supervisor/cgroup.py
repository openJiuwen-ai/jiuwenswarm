# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Per-sandbox cgroup resource limits.

This module abstracts the cgroup v2 / v1 differences behind a small set of
operations consumed by :mod:`jiuwenbox.server.runtime.process`:

- :func:`detect_backend` - probe the host once for the preferred backend.
- :func:`setup` - create a per-sandbox cgroup and write the limit files.
- :func:`attach` - place the bwrap host-side PID into ``cgroup.procs``.
- :func:`teardown` - kill stragglers, then rmdir the cgroup.

When neither cgroup v2 nor v1 is writable but :class:`CgroupPolicy` is
non-empty, :func:`setup` raises :class:`CgroupSetupError` so the runtime
can abort sandbox creation rather than silently dropping the limits.

Cgroup writes are performed by the box-server process (typically root) on
the host; the sandbox itself never touches the cgroup tree. PID attachment
happens *after* the bwrap process is spawned, which is sufficient for
cgroup v2 (the migration applies to the existing process and is inherited
by all subsequent forks).
"""

from __future__ import annotations

import enum
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from jiuwenbox.models.policy import CgroupPolicy

logger = logging.getLogger(__name__)


class CgroupSetupError(RuntimeError):
    """Raised when cgroup limits are requested but cannot be applied."""


class CgroupBackend(str, enum.Enum):
    V2 = "v2"
    V1 = "v1"


# Required controllers for the minimal v1/v2 surface we support.
_REQUIRED_CONTROLLERS: tuple[str, ...] = ("memory", "cpu", "pids")

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_JIUWENBOX_ROOT_NAME = "jiuwenbox"

# Maximum number of attempts when rmdir-ing a cgroup that still has
# transient processes attached.
_TEARDOWN_RETRIES = 5
_TEARDOWN_BACKOFF_SECONDS = 0.1


@dataclass
class CgroupHandle:
    """Bookkeeping handed back from :func:`setup` and consumed by
    :func:`attach` / :func:`teardown`.

    For v2 ``paths`` contains exactly one entry (the unified group dir).
    For v1 it contains one entry per controller in ``_REQUIRED_CONTROLLERS``
    that was actually used by the policy.
    """

    backend: CgroupBackend
    sandbox_id: str
    paths: list[Path] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _v2_controllers_enabled_at(path: Path) -> set[str]:
    try:
        text = (path / "cgroup.controllers").read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(text.split())


def _v2_available() -> bool:
    """Return True when cgroup v2 is mounted and our controllers are exposed.

    We require :data:`_CGROUP_ROOT` / cgroup.controllers to exist and to
    expose ``memory``, ``cpu`` and ``pids``. Writability is verified at
    setup time (creating the jiuwenbox subtree) rather than here, so the
    detection is cheap and side-effect-free.
    """
    controllers_file = _CGROUP_ROOT / "cgroup.controllers"
    if not controllers_file.is_file():
        return False
    available = _v2_controllers_enabled_at(_CGROUP_ROOT)
    return all(name in available for name in _REQUIRED_CONTROLLERS)


def _v1_available() -> bool:
    """Return True when cgroup v1 exposes a writable hierarchy for each of
    our required controllers under :data:`_CGROUP_ROOT`.
    """
    for controller in _REQUIRED_CONTROLLERS:
        ctrl_root = _CGROUP_ROOT / controller
        if not ctrl_root.is_dir():
            return False
        # cgroup v1 controller root is always writable by root; we don't
        # touch it here to avoid leaving leftover dirs in detection.
    return True


@lru_cache(maxsize=1)
def detect_backend() -> CgroupBackend | None:
    """Probe the host for a usable cgroup backend.

    Returns ``CgroupBackend.V2`` when cgroup v2 is mounted with our
    controllers exposed, falling back to ``CgroupBackend.V1`` when each
    required controller has its own hierarchy. Returns ``None`` if
    neither is present (the caller decides whether that's fatal).

    Cached because the cgroup hierarchy is fixed for the lifetime of the
    host kernel: every sandbox creation otherwise re-runs ``read_text``
    on ``/sys/fs/cgroup/cgroup.controllers`` (or ``is_dir`` for v1
    controllers) which is wasted work on the hot path.
    """
    if _v2_available():
        return CgroupBackend.V2
    if _v1_available():
        return CgroupBackend.V1
    return None


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _write_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` using ``open(..., "w")`` semantics.

    Cgroup interface files are sysfs-like and require a single write of the
    exact value (no newline appended by the kernel parser); using ``"w"``
    keeps the behaviour predictable across both v2 and v1.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _try_write_file(path: Path, content: str, *, what: str) -> None:
    try:
        _write_file(path, content)
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to write {what} ({path}): {exc.strerror or exc}"
        ) from exc


def _ensure_jiuwenbox_root_v2() -> Path:
    """Create ``/sys/fs/cgroup/jiuwenbox/`` and enable required controllers.

    On cgroup v2 controllers must be enabled in every ancestor's
    ``cgroup.subtree_control`` before the leaf can use them. We enable
    ``+memory +cpu +pids`` at the unified root, then mkdir
    ``/sys/fs/cgroup/jiuwenbox/`` if it doesn't already exist.
    """
    enable_line = " ".join(f"+{c}" for c in _REQUIRED_CONTROLLERS)
    subtree_control = _CGROUP_ROOT / "cgroup.subtree_control"
    try:
        _write_file(subtree_control, enable_line)
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to enable controllers {enable_line!r} in {subtree_control}: "
            f"{exc.strerror or exc}"
        ) from exc

    jiuwenbox_root = _CGROUP_ROOT / _JIUWENBOX_ROOT_NAME
    try:
        jiuwenbox_root.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {jiuwenbox_root}: {exc.strerror or exc}"
        ) from exc

    # Enable controllers on the jiuwenbox subtree as well so per-sandbox
    # children can read/write the limit files.
    try:
        _write_file(jiuwenbox_root / "cgroup.subtree_control", enable_line)
    except OSError as exc:
        # If the jiuwenbox root already has the controllers enabled, this
        # write either succeeds or returns EBUSY; treat any other failure
        # as a real error.
        if getattr(exc, "errno", None) not in (0, None):
            raise CgroupSetupError(
                f"failed to enable controllers in {jiuwenbox_root}: "
                f"{exc.strerror or exc}"
            ) from exc

    return jiuwenbox_root


def _setup_v2(sandbox_id: str, policy: CgroupPolicy) -> CgroupHandle:
    parent = _ensure_jiuwenbox_root_v2()
    group_dir = parent / sandbox_id
    try:
        group_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise CgroupSetupError(
            f"cgroup directory {group_dir} already exists"
        ) from exc
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {group_dir}: {exc.strerror or exc}"
        ) from exc

    try:
        if policy.memory_max is not None:
            _try_write_file(
                group_dir / "memory.max",
                str(policy.memory_max),
                what="memory.max",
            )
            # Also clamp swap to zero so ``memory.max`` behaves as a true
            # hard cap. Without this, a memory-hungry process can simply
            # page out to swap once it hits ``memory.max`` and keep running
            # -- the kernel only invokes the OOM killer when reclaim cannot
            # free pages, and "evict to swap" counts as reclaim. WSL2 and
            # most desktop Linuxes ship with swap enabled, so this is the
            # common case rather than an edge case.
            #
            # The swap controller may be absent on hosts that booted with
            # ``swapaccount=0`` (or without ``CONFIG_MEMCG_SWAP``); missing
            # ``memory.swap.max`` is non-fatal because such hosts have no
            # swap to begin with and ``memory.max`` already behaves as the
            # hard cap users expect.
            swap_max_file = group_dir / "memory.swap.max"
            if swap_max_file.exists():
                try:
                    _write_file(swap_max_file, "0")
                except OSError as exc:
                    logger.warning(
                        "cgroup v2: failed to clamp memory.swap.max=0 for %s: %s",
                        sandbox_id,
                        exc,
                    )
        if policy.cpu_max is not None:
            quota, period = policy.cpu_max
            _try_write_file(
                group_dir / "cpu.max",
                f"{quota} {period}",
                what="cpu.max",
            )
        if policy.pids_max is not None:
            _try_write_file(
                group_dir / "pids.max",
                str(policy.pids_max),
                what="pids.max",
            )
    except CgroupSetupError:
        # Roll back the empty cgroup so we don't leak it on failure.
        try:
            group_dir.rmdir()
        except OSError:
            logger.warning(
                "cgroup: failed to remove %s after setup error; manual cleanup needed",
                group_dir,
            )
        raise

    return CgroupHandle(
        backend=CgroupBackend.V2,
        sandbox_id=sandbox_id,
        paths=[group_dir],
    )


def _setup_v1(sandbox_id: str, policy: CgroupPolicy) -> CgroupHandle:
    """Create one v1 cgroup per controller actually used by the policy.

    Unlike v2 (unified hierarchy), each cgroup v1 controller has its own
    mount under ``/sys/fs/cgroup/<controller>/``. We only create the
    sub-directories for controllers the policy actually constrains so we
    don't leave empty memory/cpu/pids cgroups behind when the user only
    set, e.g., ``pids_max``.
    """
    created_paths: list[Path] = []
    try:
        if policy.memory_max is not None:
            path = _create_v1_group("memory", sandbox_id)
            created_paths.append(path)
            _try_write_file(
                path / "memory.limit_in_bytes",
                str(policy.memory_max),
                what="memory.limit_in_bytes",
            )
            # Cap memory+swap to the same value so the swap controller can't
            # be used to bypass ``memory.limit_in_bytes``. ``memory.memsw``
            # is only present when the kernel was booted with swap accounting
            # enabled (``swapaccount=1`` or built-in default); a missing
            # file is non-fatal -- such hosts can't account swap usage per
            # cgroup anyway, so the user's ``memory_max`` setting is already
            # the hardest cap available.
            memsw_file = path / "memory.memsw.limit_in_bytes"
            if memsw_file.exists():
                try:
                    _write_file(memsw_file, str(policy.memory_max))
                except OSError as exc:
                    logger.warning(
                        "cgroup v1: failed to set memory.memsw.limit_in_bytes "
                        "for %s: %s",
                        sandbox_id,
                        exc,
                    )
        if policy.cpu_max is not None:
            quota, period = policy.cpu_max
            path = _create_v1_group("cpu", sandbox_id)
            created_paths.append(path)
            # ``cpu.cfs_period_us`` must be written before ``cpu.cfs_quota_us``
            # if the new quota exceeds the old period; flipping the order
            # would briefly violate the kernel's quota <= period * cpus
            # invariant. Writing period first is always safe.
            _try_write_file(
                path / "cpu.cfs_period_us",
                str(period),
                what="cpu.cfs_period_us",
            )
            _try_write_file(
                path / "cpu.cfs_quota_us",
                str(quota),
                what="cpu.cfs_quota_us",
            )
        if policy.pids_max is not None:
            path = _create_v1_group("pids", sandbox_id)
            created_paths.append(path)
            _try_write_file(
                path / "pids.max",
                str(policy.pids_max),
                what="pids.max",
            )
    except CgroupSetupError:
        for path in created_paths:
            try:
                path.rmdir()
            except OSError:
                logger.warning(
                    "cgroup v1: failed to remove %s after setup error", path
                )
        raise

    return CgroupHandle(
        backend=CgroupBackend.V1,
        sandbox_id=sandbox_id,
        paths=created_paths,
    )


def _create_v1_group(controller: str, sandbox_id: str) -> Path:
    parent = _CGROUP_ROOT / controller / _JIUWENBOX_ROOT_NAME
    try:
        parent.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {parent}: {exc.strerror or exc}"
        ) from exc
    group_dir = parent / sandbox_id
    try:
        group_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise CgroupSetupError(
            f"cgroup directory {group_dir} already exists"
        ) from exc
    except OSError as exc:
        raise CgroupSetupError(
            f"failed to create {group_dir}: {exc.strerror or exc}"
        ) from exc
    return group_dir


def setup(sandbox_id: str, policy: CgroupPolicy) -> CgroupHandle:
    """Create a cgroup for ``sandbox_id`` and apply ``policy``'s limits.

    Tries cgroup v2 first, then falls back to v1. Raises
    :class:`CgroupSetupError` if neither backend is available or any of
    the writes fails; callers should treat that as a sandbox-create
    failure.
    """
    if policy.is_empty():
        raise ValueError(
            "cgroup.setup called with an empty policy; caller must "
            "check CgroupPolicy.is_empty() and skip cgroup entirely"
        )
    backend = detect_backend()
    if backend is None:
        raise CgroupSetupError(
            "no writable cgroup backend available: cgroup v2 "
            f"({_CGROUP_ROOT}/cgroup.controllers) is missing/incomplete "
            "and cgroup v1 controllers (memory/cpu/pids) are not mounted"
        )
    if backend is CgroupBackend.V2:
        return _setup_v2(sandbox_id, policy)
    return _setup_v1(sandbox_id, policy)


# ---------------------------------------------------------------------------
# Attach
# ---------------------------------------------------------------------------


def attach(handle: CgroupHandle, pid: int) -> None:
    """Migrate ``pid`` into every cgroup directory associated with ``handle``.

    Writing the PID into ``cgroup.procs`` moves both the process and its
    threads; future forks automatically inherit the cgroup, so attaching
    the freshly-spawned bwrap PID is sufficient to constrain the entire
    sandbox subtree.
    """
    if not handle.paths:
        return
    for group_dir in handle.paths:
        procs_file = group_dir / "cgroup.procs"
        try:
            _write_file(procs_file, str(pid))
        except OSError as exc:
            raise CgroupSetupError(
                f"failed to attach pid {pid} to {procs_file}: "
                f"{exc.strerror or exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def _kill_cgroup_processes(group_dir: Path, backend: CgroupBackend) -> None:
    """Best-effort: ask the kernel to kill anything still in the cgroup.

    On v2 ``cgroup.kill`` does this atomically with a single write of
    ``"1"``. On v1 the controller hierarchies don't have ``cgroup.kill``;
    we read ``cgroup.procs`` and ``kill(SIGKILL)`` each PID individually.
    Both paths swallow ``OSError`` since teardown should never block
    sandbox deletion.
    """
    if backend is CgroupBackend.V2:
        kill_file = group_dir / "cgroup.kill"
        if kill_file.exists():
            try:
                _write_file(kill_file, "1")
            except OSError as exc:
                logger.warning(
                    "cgroup teardown: failed to write %s: %s", kill_file, exc
                )
        return
    procs_file = group_dir / "cgroup.procs"
    try:
        raw = procs_file.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid <= 1:
            continue
        try:
            os.kill(pid, 9)  # SIGKILL
        except OSError:
            pass


def _rmdir_with_retry(group_dir: Path) -> bool:
    """Attempt ``rmdir`` with short backoff while EBUSY is reported.

    Returns True on success, False when the cgroup was still busy after
    the configured retries. Callers should only log a warning on failure
    rather than propagating: a stuck cgroup is unfortunate but doesn't
    block sandbox cleanup from the user's perspective.
    """
    for attempt in range(_TEARDOWN_RETRIES):
        try:
            group_dir.rmdir()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt + 1 == _TEARDOWN_RETRIES:
                logger.warning(
                    "cgroup teardown: %s still busy after %d attempts (%s)",
                    group_dir,
                    _TEARDOWN_RETRIES,
                    exc,
                )
                return False
            time.sleep(_TEARDOWN_BACKOFF_SECONDS)
    return False


def teardown(handle: CgroupHandle) -> None:
    """Remove the cgroup(s) associated with ``handle``.

    First nudges out any straggler processes (via ``cgroup.kill`` on v2 or
    a manual SIGKILL loop on v1), then rmdir-s each directory with a
    short retry loop. Failures only emit a warning and never propagate -
    sandbox deletion must remain idempotent and side-effect-free for the
    caller's perspective.
    """
    for group_dir in handle.paths:
        if not group_dir.exists():
            continue
        _kill_cgroup_processes(group_dir, handle.backend)
        # Give the kernel a brief moment to flush the killed processes
        # before we attempt rmdir; the retry loop in _rmdir_with_retry
        # handles the still-EBUSY case.
        time.sleep(_TEARDOWN_BACKOFF_SECONDS)
        if not _rmdir_with_retry(group_dir):
            # Last-ditch effort: try a non-recursive rmtree to clean up
            # if rmdir keeps failing (e.g. leftover sub-cgroups created
            # by some user code inside the sandbox). shutil.rmtree on a
            # cgroup dir is generally a no-op because the interface
            # files cannot be unlinked, but it's harmless on success.
            try:
                shutil.rmtree(group_dir, ignore_errors=True)
            except OSError:
                pass
