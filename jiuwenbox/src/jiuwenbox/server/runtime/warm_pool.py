# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Background pool of sandboxes parked before the request filesystem is applied.

Network, cgroup, seccomp and the server default filesystem are applied when
a slot is parked. Checkout bind-mounts any additional request filesystem and
only then drops privileges and applies Landlock. ``JIUWENBOX_WARM_POOL`` is
the number of parked sandboxes. ``0`` turns the pool off. The default is 8.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

from jiuwenbox.models.policy import ProcessPolicy, SecurityPolicy
from jiuwenbox.supervisor.bwrap import BwrapConfig

if TYPE_CHECKING:
    from jiuwenbox.server.runtime.process import ProcessRuntime

logger = logging.getLogger(__name__)

ENV_WARM_POOL = "JIUWENBOX_WARM_POOL"
DEFAULT_WARM_POOL_SIZE = 8


def warm_pool_size() -> int:
    """Return the configured pool depth. Unset means 8. ``0`` disables it."""
    raw = os.environ.get(ENV_WARM_POOL)
    if raw is None or not raw.strip():
        return DEFAULT_WARM_POOL_SIZE
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", ENV_WARM_POOL, raw)
        return 0


_COMPARED_POLICIES = ("network", "cgroup", "namespace", "syscall")
_MISMATCH_VALUE_LIMIT = 180


def warm_pool_mismatches(template: SecurityPolicy, request: SecurityPolicy) -> list[str]:
    """Return why ``request`` cannot take a slot parked from ``template``.

    An empty list means the request is compatible. Each entry names one
    differing field and both values. Filesystem, run-as and environment are
    applied at checkout and are not compared.
    """
    reasons: list[str] = []
    request_landlock = request.landlock.compatibility
    template_landlock = template.landlock.compatibility
    if request_landlock == "disabled" or request_landlock != template_landlock:
        note = ""
        if request_landlock == "disabled":
            note = " (warm pool cannot serve a sandbox with Landlock disabled)"
        reasons.append(
            f"landlock.compatibility request={request_landlock!r} "
            f"template={template_landlock!r}{note}"
        )
    for name in _COMPARED_POLICIES:
        reasons.extend(
            _policy_diffs(name, getattr(template, name), getattr(request, name)),
        )
    return reasons


def warm_pool_compatible(template: SecurityPolicy, request: SecurityPolicy) -> bool:
    """True when a parked slot can serve this request.

    Filesystem, run-as (a name or a number) and environment are applied at
    checkout. The slot's user namespace already identity-maps every id in
    ``0 .. 65535``, so ``setuid`` to the resolved user succeeds.
    Landlock must stay enabled: the parked process already mounted the
    server default filesystem, and without Landlock those paths would stay
    visible.
    """
    return not warm_pool_mismatches(template, request)


def _policy_diffs(path: str, template: object, request: object) -> list[str]:
    if template == request:
        return []
    fields = getattr(type(template), "model_fields", None)
    if fields and type(template) is type(request):
        found: list[str] = []
        for name in fields:
            found.extend(_policy_diffs(
                f"{path}.{name}",
                getattr(template, name),
                getattr(request, name),
            ))
        if found:
            return found
    return [
        f"{path} request={_brief(request)} template={_brief(template)}",
    ]


def _brief(value: object) -> str:
    rendered = getattr(value, "value", None)
    text = repr(rendered if isinstance(rendered, str) else value)
    if len(text) <= _MISMATCH_VALUE_LIMIT:
        return text
    return text[: _MISMATCH_VALUE_LIMIT - 3] + "..."


def boot_policy(template: SecurityPolicy) -> SecurityPolicy:
    """Policy used to start a parked process.

    The slot keeps the filesystem of the server default policy, so every
    bind mount loaded with jiuwenbox is present before checkout. The process
    stays uid 0 inside an identity user-id map until checkout setuid/setgid.
    """
    return template.model_copy(deep=True, update={
        "process": ProcessPolicy(run_as_user="0", run_as_group="0"),
    })


def mount_specs(policy: SecurityPolicy, extra_binds: list[dict[str, str]]) -> list[dict]:
    """Host binds the checkout helper must attach, parents before children."""
    config = BwrapConfig.from_policy(policy, ["true"])
    specs: list[dict] = []
    for src, dst in config.ro_binds:
        specs.append({"src": src, "dst": dst, "ro": True})
    for src, dst in [*config.rw_binds, *config.device_binds]:
        specs.append({"src": src, "dst": dst, "ro": False})
    for entry in extra_binds:
        specs.append({
            "src": entry["host_path"],
            "dst": entry["sandbox_path"],
            "ro": False,
        })
    specs.sort(key=lambda item: item["dst"].count("/"))
    return specs


class _MountClient:
    """Talks to the long-lived single-threaded mount helper."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._next_id = 0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "jiuwenbox.supervisor.mount_helper"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    def apply(self, pid: int, user_ns: bool, mounts: list[dict]) -> None:
        proc = self._proc
        if not self._helper_ready(proc):
            raise RuntimeError("mount helper is not running")
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            proc.stdin.write(json.dumps({
                "id": request_id,
                "pid": pid,
                "user_ns": user_ns,
                "mounts": mounts,
            }))
            proc.stdin.write("\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
        if not line:
            raise RuntimeError("mount helper closed its stdout")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("error") or "mount helper failed"))

    @staticmethod
    def _helper_ready(proc: subprocess.Popen[str] | None) -> bool:
        if proc is None or proc.poll() is not None:
            return False
        return proc.stdin is not None and proc.stdout is not None


class WarmPool:
    """Keeps ``size`` parked sandboxes and refills after each checkout.

    ``replace_template`` installs a new default policy. Parked sandboxes are
    kept when network, cgroup, namespace, syscall and Landlock still match;
    otherwise they are destroyed and the filler parks new ones from the new
    template.
    """

    def __init__(self, runtime: ProcessRuntime, template: SecurityPolicy, size: int) -> None:
        self._runtime = runtime
        self._template = template
        self._size = size
        self._epoch = 0
        self._slots: queue.Queue[str] = queue.Queue()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._helper = _MountClient()
        self._thread = threading.Thread(
            target=self._fill_loop,
            name="jiuwenbox-warm-pool",
            daemon=True,
        )

    def start(self) -> None:
        self._helper.start()
        self._runtime.attach_mount_client(self._helper)
        self._thread.start()
        logger.info("Warm pool started with depth %d", self._size)

    def stop(self) -> None:
        self._stop.set()
        with self._state_lock:
            self._epoch += 1
        self._thread.join(timeout=8)
        for slot_id in self._drain_slots():
            self._runtime.destroy_warm_slot(slot_id)
        self._helper.close()

    def replace_template(self, template: SecurityPolicy) -> None:
        """Point the pool at ``template`` and drop parked sandboxes that cannot serve it."""
        with self._state_lock:
            reusable = warm_pool_compatible(self._template, template)
            self._template = template
            if reusable:
                logger.info("Warm pool template updated; parked sandboxes still match")
                return
            self._epoch += 1
            stale = self._drain_slots_unlocked()
        logger.info(
            "Warm pool template changed; discarding %d parked sandbox(es)",
            len(stale),
        )
        for slot_id in stale:
            try:
                self._runtime.destroy_warm_slot(slot_id)
            except Exception:
                logger.exception("Failed to discard warm slot %s", slot_id)

    def stage_slot(self, slot_id: str) -> None:
        """Queue a slot id. Tests use this instead of the internal queue."""
        with self._state_lock:
            self._slots.put(slot_id)

    @property
    def depth(self) -> int:
        return self._size

    def mismatch_reasons(self, request: SecurityPolicy) -> list[str]:
        """Name the template fields that keep ``request`` off this pool."""
        with self._state_lock:
            template = self._template
        return warm_pool_mismatches(template, request)

    def acquire(self, policy: SecurityPolicy) -> tuple[bool, str | None]:
        """Claim a parked sandbox when ``policy`` matches the current template.

        The first value is false when the pool must not serve this policy.
        It is true with a null slot when the template matches but the queue is empty.
        """
        with self._state_lock:
            if not warm_pool_compatible(self._template, policy):
                return False, None
            try:
                return True, self._slots.get_nowait()
            except queue.Empty:
                return True, None

    def _drain_slots(self) -> list[str]:
        with self._state_lock:
            return self._drain_slots_unlocked()

    def _drain_slots_unlocked(self) -> list[str]:
        stale: list[str] = []
        while True:
            try:
                stale.append(self._slots.get_nowait())
            except queue.Empty:
                return stale

    def _fill_loop(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                if self._slots.qsize() >= self._size:
                    template = None
                    epoch = self._epoch
                else:
                    template = self._template
                    epoch = self._epoch
            if template is None:
                self._stop.wait(0.5)
                continue
            try:
                slot_id = self._runtime.spawn_warm_slot(template)
            except Exception:
                logger.exception("Warm pool failed to park a sandbox")
                self._stop.wait(1.0)
                continue
            discard = False
            with self._state_lock:
                if self._stop.is_set() or epoch != self._epoch:
                    discard = True
                else:
                    self._slots.put(slot_id)
            if discard:
                self._runtime.destroy_warm_slot(slot_id)
                if self._stop.is_set():
                    return
