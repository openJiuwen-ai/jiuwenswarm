# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Lite runtime adapter backed by a one-shot sandboxed command engine.

Design reference: LiteRuntime implementation design v4, section 11.

The adapter is the only jiuwenbox code that talks to the engine, and it does
so exclusively through the ``LiteSandboxEngine`` contract. All per-sandbox
runtime state (policy, lock, inflight count, cancel event) lives here; the
SandboxManager never sees it. Four methods (exec + three file helpers plus
search) are the only paths that call ``engine.run()``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import binascii
import ipaddress
import json
import os
import posixpath
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from jiuwenbox.enclio._policy import compile_plan, snapshot_deployment
from jiuwenbox.enclio import (
    CommandRequest,
    Deployment,
    Endpoint,
    LiteSandboxEngine,
    Mount,
    Network,
    RunResult,
    SandboxPolicy,
)
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import (
    BackgroundExecResult,
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    KillBackgroundJobResult,
)
from jiuwenbox.server.runtime.base import (
    RuntimeAdapter,
    RuntimeBackgroundExecRequest,
    RuntimeExecRequest,
    RuntimeFileOpResult,
)
from jiuwenbox.server.runtime.errors import (
    PolicyValidationError,
    SandboxNotFoundError,
    SandboxStateError,
)

# How long cleanup waits for an in-flight run to drain before it gives up and
# records the workspace as pending. Manager calls cleanup outside its lock, so
# this wait does not block other sandboxes.
CLEANUP_WAIT_SECONDS = 5.0
CLEANUP_POLL_SECONDS = 0.02

logger = logging.getLogger(__name__)

# Sandbox-side helper exit codes, aligned with the Manager fast-path fallbacks.
_EXIT_NOT_FOUND = 44
_EXIT_IS_DIRECTORY = 45
_EXIT_TOO_LARGE = 46
_EXIT_PERMISSION_DENIED = 13
# Read helper rejects files above this size before base64 so the engine's
# output cap cannot turn a normal read into resource_limit.
MAX_READ_BYTES = 64 * 1024 * 1024


# The engine's PolicyEngine reserves the whole ~/.jiuwenbox tree (both the
# workspace subtree and the server home) as un-referenceable host paths. Our
# /workspace bind source therefore must live OUTSIDE that tree. The default
# backing root uses the temp dir, scoped by uid, which matches the engine's
# "per-run temporary storage" semantics and is overridable by the operator.
_DEFAULT_BACKING_SUBDIR = "jiuwenbox-lite-runs"


def _default_backing_root() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return str(Path(tempfile.gettempdir()) / f"{_DEFAULT_BACKING_SUBDIR}-{uid}")


def _backing_root(deployment: Deployment | None = None) -> str:
    """Root for per-run engine storage (host facts, not sandbox policy).

    Must stay outside JIUWENBOX_HOME: the engine rejects any policy host path
    inside that tree, and our /workspace bind source derives from here.
    """
    root = getattr(deployment, "backing_root", None) if deployment is not None else None
    return root or os.getenv("JIUWENBOX_LITE_BACKING_ROOT") or _default_backing_root()


def _workspace_of(sandbox_id: str, deployment: Deployment | None = None) -> str:
    """Host backing directory bind-mounted at ``/workspace``.

    Deliberately under the engine backing root, NOT under SANDBOX_WORKSPACE:
    the engine's PolicyEngine rejects bind sources inside the reserved
    workspace subtree before a sandbox can start.
    """
    return str(Path(_backing_root(deployment)) / "workspaces" / sandbox_id)


def _sandbox_backing_root(sandbox_id: str, deployment: Deployment | None = None) -> str:
    """Per-logical-sandbox engine backing root (codex #1).

    The engine resolves ``filesystem_policy.directories/files`` backing paths
    as ``<backing_root>/<encoded sandbox path>`` with NO sandbox id. A shared
    backing root therefore makes two sandboxes declaring ``/home`` or ``/tmp``
    bind-mount the SAME host directory. Scoping the backing root per sandbox
    keeps managed directories isolated while still letting repeated runs of
    one sandbox reuse the same storage.
    """
    return str(Path(_backing_root(deployment)) / "sandboxes" / sandbox_id)


@dataclass
class _LiteState:
    """Per-sandbox adapter state. Object identity is the generation token."""

    inflight: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel: asyncio.Event | None = None
    # Once closing, no new command is accepted, but the object stays in
    # _states until cleanup confirms the in-flight run was reclaimed.
    closing: bool = False


class LiteRuntime(RuntimeAdapter):
    """Runtime adapter for one-shot Lite sandboxes."""

    def __init__(
        self,
        deployment: Deployment | None = None,
        engine: LiteSandboxEngine | None = None,
    ) -> None:
        # Host facts are built lazily when no deployment is injected.
        self._deployment = deployment if deployment is not None else build_default_deployment()
        self._injected_engine = engine
        self._policies: dict[str, SandboxPolicy] = {}
        self._states: dict[str, _LiteState] = {}
        # Per-sandbox engine instances. The engine snapshots its deployment
        # (including backing_root) at construction, so isolation requires one
        # engine per logical sandbox (codex #1). Injected engines (tests) are
        # shared deliberately so fake-engine assertions stay valid.
        self._engines: dict[str, LiteSandboxEngine] = {}
        # workspace -> sandbox_id whose previous run was not confirmed reclaimed
        self._pending: dict[str, str] = {}

    def _deployment_for(self, sandbox_id: str) -> Deployment:
        return replace(
            self._deployment,
            backing_root=_sandbox_backing_root(sandbox_id, self._deployment),
        )

    def _engine_for(self, sandbox_id: str) -> LiteSandboxEngine:
        if self._injected_engine is not None:
            return self._injected_engine
        eng = self._engines.get(sandbox_id)
        if eng is None:
            eng = LiteSandboxEngine(self._deployment_for(sandbox_id))
            self._engines[sandbox_id] = eng
        return eng

    # ------------------------------------------------------------------ #
    # 1. create
    # ------------------------------------------------------------------ #
    async def create(
        self,
        sandbox_id: str,
        policy_path: Path,
        env: dict[str, str] | None = None,
    ) -> int | None:
        """Translate the jiuwenbox YAML into an engine policy and open intake.

        No process is started and no directory is created — a fresh sandbox is
        built by ``engine.run()`` per command. Returns ``None`` (no pid).
        """
        spec = SecurityPolicy.model_validate(
            yaml.safe_load(Path(policy_path).read_text(encoding="utf-8"))
        )
        # The engine never applies Landlock (codex #3): fail closed instead of
        # silently running a caller that demanded hard enforcement.
        if spec.landlock.compatibility == "hard_requirement":
            raise PolicyValidationError(
                "Lite cannot honour landlock compatibility=hard_requirement "
                "(the engine does not apply Landlock); use best_effort or disabled"
            )
        # Workspace mount source must derive from the ORIGINAL backing root:
        # _workspace_of() already appends sandbox_id, and the per-sandbox engine
        # adds its own <root>/sandboxes/<id> scope. Passing the derived
        # deployment here would double-nest the path and desync it from the
        # directory actually created below (codex #1 integration fix).
        policy = to_sandbox_policy(spec, sandbox_id, env, self._deployment)
        check_deployment(policy, self._deployment_for(sandbox_id))

        ws = _workspace_of(sandbox_id, self._deployment)
        # The engine bind-mounts this host path at /workspace but does not
        # create it; bwrap requires the source to exist before the first run.
        Path(ws).mkdir(parents=True, exist_ok=True)
        if ws in self._pending:
            raise SandboxStateError(
                f"workspace for sandbox '{sandbox_id}' has an unconfirmed cleanup; "
                "retry after the previous run is reclaimed"
            )
        previous = self._states.get(sandbox_id)
        if previous is not None and previous.inflight > 0:
            raise SandboxStateError(
                f"sandbox '{sandbox_id}' still has {previous.inflight} in-flight "
                "run(s); refusing to open a new generation before reclamation"
            )
        self._policies[sandbox_id] = policy
        self._states[sandbox_id] = _LiteState()
        return None

    # ------------------------------------------------------------------ #
    # 2. stop
    # ------------------------------------------------------------------ #
    async def stop(self, sandbox_id: str, timeout: float = 10.0) -> None:
        """Close intake, cancel, and wait for the in-flight run to drain.

        Manager's restart calls stop() then immediately create(). Without the
        wait here (codex #5) create() sees ``inflight > 0`` and the sandbox is
        left in ERROR until a second Start. Waiting here — with no adapter
        lock held — lets the following create() succeed as soon as the engine
        confirms reclamation. The state object is deliberately KEPT in
        ``_states`` with ``closing`` set so cleanup() can still observe an
        unconfirmed run; on timeout we return without dropping it.
        """
        state = self._states.get(sandbox_id)
        if state is None:
            return
        state.closing = True
        if state.cancel is not None:
            state.cancel.set()
        deadline = time.monotonic() + min(timeout, CLEANUP_WAIT_SECONDS)
        while state.inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(CLEANUP_POLL_SECONDS)

    # ------------------------------------------------------------------ #
    # 3. is_running
    # ------------------------------------------------------------------ #
    async def is_running(self, sandbox_id: str) -> bool:
        """Whether this logical sandbox can still accept commands (memory only)."""
        state = self._states.get(sandbox_id)
        return state is not None and not state.closing

    # ------------------------------------------------------------------ #
    # 4. exec — the only direct engine.run() call site
    # ------------------------------------------------------------------ #
    async def exec(self, sandbox_id: str, request: RuntimeExecRequest) -> ExecResult:
        """Run one command: serialize per sandbox, re-check state, count inflight."""
        state = self._states.get(sandbox_id)
        if state is None or state.closing:
            raise SandboxNotFoundError(f"Sandbox '{sandbox_id}' not found")

        async with state.lock:
            if self._states.get(sandbox_id) is not state or state.closing:
                raise SandboxStateError(
                    f"sandbox '{sandbox_id}' was stopped or replaced while queued"
                )
            ev = asyncio.Event()
            state.cancel = ev
            state.inflight += 1
            try:
                result = await self._engine_for(sandbox_id).run(
                    self._policies[sandbox_id],
                    CommandRequest(
                        argv=tuple(request.command),
                        cwd=request.workdir,
                        env=dict(request.env or {}),
                        stdin=request.stdin_data or b"",
                        timeout_seconds=request.timeout,
                    ),
                    cancel=ev,
                )
                if _status_value(result.status) == "cleanup_error":
                    # Reclamation was NOT confirmed: close intake and mark the
                    # workspace pending so no later run can overlap leftovers.
                    state.closing = True
                    self._pending[_workspace_of(sandbox_id, self._deployment)] = sandbox_id
            finally:
                state.inflight -= 1
                state.cancel = None
        return to_exec_result(result)

    # ------------------------------------------------------------------ #
    # 5-8. background methods — consistent "no background jobs" contract
    # ------------------------------------------------------------------ #
    async def exec_background(
        self,
        sandbox_id: str,
        request: RuntimeBackgroundExecRequest,
    ) -> BackgroundExecResult:
        raise NotImplementedError("lite does not support background jobs")

    async def get_background_job(
        self, sandbox_id: str, job_id: str
    ) -> BackgroundJobStatus:
        raise SandboxNotFoundError(f"background job {job_id!r} not found")

    async def list_background_jobs(
        self, sandbox_id: str, *, running_only: bool = False
    ) -> list[BackgroundJobSummary]:
        return []

    async def kill_background_job(
        self, sandbox_id: str, job_id: str, signum: int = 15
    ) -> KillBackgroundJobResult:
        raise SandboxNotFoundError(f"background job {job_id!r} not found")

    # ------------------------------------------------------------------ #
    # 9. cleanup
    # ------------------------------------------------------------------ #
    async def cleanup(self, sandbox_id: str) -> None:
        """Cancel, wait for inflight to drain, then drop cached state.

        Never deletes the workspace — that is the user's project directory.
        The engine already reclaimed its per-run temp resources. The state is
        removed only once inflight reaches zero; otherwise it is kept so the
        unconfirmed run remains observable via ``_pending``.
        """
        state = self._states.get(sandbox_id)
        if state is not None:
            state.closing = True
            if state.cancel is not None:
                state.cancel.set()
            deadline = time.monotonic() + CLEANUP_WAIT_SECONDS
            while state.inflight > 0 and time.monotonic() < deadline:
                await asyncio.sleep(CLEANUP_POLL_SECONDS)
            if state.inflight > 0:
                self._pending[_workspace_of(sandbox_id, self._deployment)] = sandbox_id
                raise SandboxStateError(
                    f"cleanup for sandbox '{sandbox_id}' unconfirmed: "
                    f"{state.inflight} run(s) still in flight"
                )
            # Reclamation is confirmed: honour create()'s promise that the
            # workspace becomes reusable once the previous run is reclaimed.
            self._pending.pop(_workspace_of(sandbox_id, self._deployment), None)
            self._states.pop(sandbox_id, None)
        self._policies.pop(sandbox_id, None)
        self._engines.pop(sandbox_id, None)

    # ------------------------------------------------------------------ #
    # 11-14. file methods — implemented via exec()/engine.run() in sandbox
    # ------------------------------------------------------------------ #
    async def _run_helper(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        stdin: bytes = b"",
        timeout: float | None = None,
    ) -> ExecResult:
        """Single file-op entry: reuse exec so lock/recheck/inflight apply."""
        return await self.exec(
            sandbox_id,
            RuntimeExecRequest(command=argv, stdin_data=stdin, timeout=timeout),
        )

    async def write_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
        *,
        mkdir_parents: bool = True,
        mode: int | None = None,
    ) -> RuntimeFileOpResult:
        policy = self._policies.get(sandbox_id)
        _check_path(sandbox_path, policy)
        script = (
            'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
            if mkdir_parents
            else 'cat > "$1"'
        )
        if mode is not None:
            script += f' && chmod {mode:o} "$1"'
        try:
            r = await self._run_helper(
                sandbox_id,
                ["bash", "-c", script, "lite-write", sandbox_path],
                stdin=content,
            )
        except SandboxStateError as exc:
            return RuntimeFileOpResult(ok=False, error="sandbox_state", detail=str(exc))
        return _map_write_error(r)

    async def read_file(
        self, sandbox_id: str, sandbox_path: str
    ) -> RuntimeFileOpResult:
        policy = self._policies.get(sandbox_id)
        _check_path(sandbox_path, policy)
        try:
            r = await self._run_helper(
                sandbox_id,
                ["bash", "-c", _READ_FILE_SCRIPT, "lite-read", sandbox_path],
            )
        except SandboxStateError as exc:
            return RuntimeFileOpResult(ok=False, error="sandbox_state", detail=str(exc))
        if r.exit_code != 0:
            return _map_file_error(r)
        if r.stdout and len(r.stdout) > MAX_READ_BYTES * 2:
            return RuntimeFileOpResult(
                ok=False, error="too_large", detail="read result exceeds engine output cap"
            )
        try:
            # GNU base64 folds at 76 columns by default; strip all whitespace
            # before strict decoding so ordinary non-empty files decode.
            content = base64.b64decode("".join(r.stdout.split()), validate=True)
        except binascii.Error:
            return RuntimeFileOpResult(
                ok=False, error="decode_error", detail="invalid base64 from sandbox"
            )
        return RuntimeFileOpResult(ok=True, content=content)

    async def list_dir(
        self,
        sandbox_id: str,
        sandbox_path: str,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        include_files: bool = True,
        include_dirs: bool = True,
    ) -> RuntimeFileOpResult:
        policy = self._policies.get(sandbox_id)
        _check_path(sandbox_path, policy)
        script = _LIST_DIR_SCRIPT
        argv = [
            "python3", "-S", "-c", script, sandbox_path,
            "1" if recursive else "0",
            str(max_depth) if max_depth is not None else "",
            "1" if include_files else "0",
            "1" if include_dirs else "0",
        ]
        try:
            r = await self._run_helper(sandbox_id, argv)
        except SandboxStateError as exc:
            return RuntimeFileOpResult(ok=False, error="sandbox_state", detail=str(exc))
        return _map_list_result(r)

    async def search_files(
        self,
        sandbox_id: str,
        sandbox_path: str,
        patterns: list[str],
        *,
        exclude_patterns: list[str] | None = None,
    ) -> RuntimeFileOpResult:
        policy = self._policies.get(sandbox_id)
        _check_path(sandbox_path, policy)
        argv = [
            "python3", "-S", "-c", _SEARCH_FILES_SCRIPT, sandbox_path,
            json.dumps(list(patterns)),
            json.dumps(list(exclude_patterns or [])),
        ]
        try:
            r = await self._run_helper(sandbox_id, argv)
        except SandboxStateError as exc:
            return RuntimeFileOpResult(ok=False, error="sandbox_state", detail=str(exc))
        return _map_list_result(r)


# ---------------------------------------------------------------------- #
# Strategy mapping: SecurityPolicy -> engine SandboxPolicy (design §12.2)
# ---------------------------------------------------------------------- #
def _domain_is_blocked(domain: str, blocked: object) -> bool:
    """Whether a domain is denied by a blocked_domains rule (wildcards allowed)."""
    d = domain.lower().rstrip(".")
    for rule in blocked or ():  # type: ignore[operator]
        r = str(rule).lower().lstrip("*.").rstrip(".")
        if r and (d == r or d.endswith("." + r)):
            return True
    return False


def _ip_is_blocked(address: str, blocked: object) -> bool:
    """Whether an IPv4 host is covered by any blocked_ips CIDR."""
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return True  # unparseable allow entry must not become an allow endpoint
    for rule in blocked or ():  # type: ignore[operator]
        try:
            if addr.version == ipaddress.ip_network(str(rule), strict=False).version \
                    and addr in ipaddress.ip_network(str(rule), strict=False):
                return True
        except ValueError:
            continue
    return False


def _endpoints_from_egress(spec: SecurityPolicy) -> tuple[Endpoint, ...]:
    """Map allow-lists minus explicit denies to the engine's endpoint allow-list.

    The engine's isolated mode is a positive allow-list only. Block rules take
    precedence (codex #2): we subtract blocked ports, domains and IPs from the
    allow set, so nothing explicitly denied by our policy is ever handed to the
    engine as an allowed endpoint. If the whole allow set is neutralised the
    caller drops isolated mode to ``none`` (no egress) instead.
    """
    egress = spec.network.egress
    blocked_ports = set(egress.blocked_ports or ())
    ports = [p for p in (list(egress.allowed_ports or ()) or [443, 80])
             if p not in blocked_ports]
    if not ports:
        return ()
    out: list[Endpoint] = []
    for domain in egress.allowed_domains or ():
        if _domain_is_blocked(str(domain), egress.blocked_domains):
            continue
        out.extend(Endpoint(kind="dns", address=str(domain), port=p) for p in ports)
    for cidr in egress.allowed_ips or ():
        address = str(cidr).split("/", 1)[0]
        if ":" in address or address in ("0.0.0.0", "::"):
            continue  # engine endpoints are IPv4 hosts
        if _ip_is_blocked(address, egress.blocked_ips):
            continue
        out.extend(Endpoint(kind="ip", address=address, port=p) for p in ports)
    if len(out) > 256:
        logger.warning(
            "Lite egress has %d endpoints, engine cap is 256; dropping %d",
            len(out), len(out) - 256,
        )
    return tuple(out[:256])


def to_sandbox_policy(
    spec: SecurityPolicy,
    sandbox_id: str,
    env: dict[str, str] | None = None,
    deployment: Deployment | None = None,
) -> SandboxPolicy:
    """Translate a jiuwenbox policy into the colleague engine's policy.

    Uses the engine's own pydantic syscall / cgroup models, fills
    ``Network.endpoints`` for isolated mode, and omits ``landlock`` (the
    engine owns its syscall/landlock policy).
    """
    ws = _workspace_of(sandbox_id, deployment)
    # Existing binds already travel in filesystem_policy; only add our own mount.
    mounts = (Mount(source=ws, target="/workspace", kind="directory", mode="rw"),)

    # The caller network mode is authoritative. Pass it straight through:
    # host shares the host netns with no proxy control (as in ProcessRuntime),
    # isolated keeps its private netns and carries the egress allow-list. The
    # adapter never silently rewrites the requested mode.
    mode = spec.network.mode.value
    endpoints = _endpoints_from_egress(spec) if mode == "isolated" else ()

    environment = dict(spec.environment)
    if env:
        environment.update(env)

    return SandboxPolicy(
        mounts=tuple(mounts),
        default_cwd="/workspace",
        network=Network(mode=mode, endpoints=endpoints),
        syscall=spec.syscall,
        limits=spec.cgroup,
        version=str(spec.version),
        filesystem_policy=spec.filesystem_policy,
        environment=environment,
        process=spec.process,
    )


def check_deployment(policy: SandboxPolicy, deployment: Deployment) -> None:
    """Reject policies this host cannot honour, before any command runs."""
    # A bwrap path that merely looks absolute is not enough: a missing
    # binary would surface as setup_error on the first exec (codex #4).
    if not deployment.bwrap or not Path(deployment.bwrap).exists():
        raise PolicyValidationError(
            f"Lite requires bubblewrap; bwrap path {deployment.bwrap!r} does not exist"
        )
    # The engine rejects JIUWENBOX_*/ENCLIO_* env keys on every run; catch
    # that at create too so a READY sandbox cannot be permanently unrunnable.
    try:
        from jiuwenbox.enclio._policy import validate_environment
        validate_environment(policy.environment)
    except ValueError as exc:
        raise PolicyValidationError(f"invalid environment for Lite: {exc}") from exc
    # cgroup limits are enforced by the engine itself (it detects the host
    # backend and verifies applied limits); the engine Deployment has no
    # adapter-side "cgroup parent" knob.
    if not deployment.bwrap:
        raise PolicyValidationError(
            "Lite requires bubblewrap; no bwrap path configured or discovered"
        )
    if policy.network.mode == "isolated" and not policy.network.endpoints:
        # The engine requires isolated mode to carry a non-empty egress
        # allow-list. Fail at create, not on the first exec: a READY sandbox
        # that cannot honour its network policy is worse than a clear refusal.
        raise PolicyValidationError(
            "Lite isolated mode requires a non-empty egress allow-list; "
            "set network.egress.allowed_domains/allowed_ips, or use mode: host "
            "to share the host network without control"
        )
    # P1: the adapter must never hand the engine a policy it will reject on
    # every run. The engine's endpoint() only accepts public IPv4 hosts and
    # multi-label DNS names; private IPs, single-label and wildcard names
    # raise ValueError there. Try the real compile_plan at create time and
    # fail closed with the reason instead of leaving a READY sandbox whose
    # first exec always returns invalid_policy.
    try:
        package_root = str(Path(__file__).resolve().parents[2])
        compile_plan(policy, snapshot_deployment(deployment, package_root))
    except ValueError as exc:
        raise PolicyValidationError(
            f"Lite policy cannot be compiled by the engine: {exc}; "
            "isolated egress endpoints must be public IPv4 hosts or "
            "multi-label DNS names (no private IP, no wildcard, no single-label)"
        ) from exc
    if not policy.limits.is_empty():
        # Fail at create, not at first exec (codex #4): a READY sandbox that
        # cannot establish cgroup limits is worse than a clear refusal.
        try:
            from jiuwenbox.supervisor.cgroup import detect_backend
            backend = detect_backend()
        except Exception:  # non-Linux / import guarded: treat as unavailable
            backend = None
        if backend is None:
            raise PolicyValidationError(
                "cgroup limits are configured but no writable cgroup backend is "
                "available; Lite cannot run sandboxes with resource limits here"
            )


# ---------------------------------------------------------------------- #
# Result normalization: RunResult -> ExecResult or exception
# ---------------------------------------------------------------------- #
def _status_value(status) -> str:
    """Engine reports plain strings; tolerate enum-like values in tests."""
    return getattr(status, "value", status)


def to_exec_result(r: RunResult) -> ExecResult:
    """Collapse the engine's rich status into the 3-field adapter result."""
    status = _status_value(r.status)
    if status == "exited":
        return ExecResult(
            exit_code=r.exit_code,
            stdout=r.stdout.decode("utf-8", "replace"),
            stderr=r.stderr.decode("utf-8", "replace"),
        )
    if status == "signalled":
        return ExecResult(
            exit_code=-(r.term_signal or 0),
            stdout=r.stdout.decode("utf-8", "replace"),
            stderr=r.stderr.decode("utf-8", "replace"),
        )
    if status == "timed_out":
        raise TimeoutError(f"run {r.run_id} timed out")
    if status == "cancelled":
        raise SandboxStateError(f"run {r.run_id} cancelled")
    if status == "resource_limit":
        raise SandboxStateError(f"run {r.run_id} hit {r.limit_reason or 'resource limit'}")
    if status == "cleanup_error":
        raise SandboxStateError(
            f"run {r.run_id} cleanup failed after {r.initial_status or 'unknown status'}"
        )
    cwd_setup_error = status == "setup_error" and r.error_stage == "cwd"
    cwd_startup_error = r.error_code in {
        "workdir_not_found", "workdir_permission_denied",
        "workdir_not_directory", "workdir_unavailable",
    }
    cleaned_before_start = r.command_started is False and r.cleanup_complete
    if cwd_setup_error and cwd_startup_error and cleaned_before_start:
        # Match ProcessRuntime's command-start failure instead of HTTP 409.
        return ExecResult(
            exit_code=127,
            stdout=r.stdout.decode("utf-8", "replace"),
            stderr=r.stderr.decode("utf-8", "replace") or f"failed to start command: {r.error_code}",
        )
    # setup_error / invalid_policy / invalid_request
    raise SandboxStateError(
        f"run {r.run_id} {status} at {r.error_stage or 'engine'}: "
        f"{r.error_code or r.stderr.decode('utf-8', 'replace')}"
    )


# ---------------------------------------------------------------------- #
# Path light-validation and file-op result mapping
# ---------------------------------------------------------------------- #
def _check_path(sandbox_path: str, policy: SandboxPolicy | None) -> None:
    """Light checks only; the sandbox mount view is the real boundary."""
    if not sandbox_path.startswith("/"):
        raise PolicyValidationError(f"sandbox path must be absolute: {sandbox_path!r}")
    normalized = posixpath.normpath(sandbox_path)
    if ".." in normalized.split("/"):
        raise PolicyValidationError(f"sandbox path must not contain '..': {sandbox_path!r}")
    if policy is not None:
        targets = [m.target.rstrip("/") or "/" for m in policy.mounts]
        filesystem = policy.filesystem_policy
        if filesystem is not None:
            targets.extend(mount.sandbox_path.rstrip("/") or "/" for mount in filesystem.bind_mounts)
            # Engine mounts these as rw binds, but they are not in policy.mounts
            # (codex #6). Accept them so /home and /tmp file ops can reach the
            # engine instead of being rejected at the adapter boundary.
            for entry in (*filesystem.directories, *filesystem.files):
                path = entry if isinstance(entry, str) else getattr(entry, "path", None)
                if path:
                    targets.append(str(path).rstrip("/") or "/")
        if not any(
            t == "/" or normalized == t or normalized.startswith(t.rstrip("/") + "/")
            for t in targets
        ):
            raise PolicyValidationError(
                f"path {sandbox_path!r} is outside declared mounts"
            )


def _map_write_error(r: ExecResult) -> RuntimeFileOpResult:
    if r.exit_code == 0:
        return RuntimeFileOpResult(ok=True)
    return _map_file_error(r)


def _map_file_error(r: ExecResult) -> RuntimeFileOpResult:
    code = r.exit_code
    if code == _EXIT_NOT_FOUND:
        return RuntimeFileOpResult(ok=False, error="not_found", errno=2, detail=r.stderr)
    if code == _EXIT_IS_DIRECTORY:
        return RuntimeFileOpResult(ok=False, error="is_directory", errno=21, detail=r.stderr)
    if code == _EXIT_TOO_LARGE:
        return RuntimeFileOpResult(ok=False, error="too_large", detail=r.stderr)
    if code == _EXIT_PERMISSION_DENIED:
        return RuntimeFileOpResult(ok=False, error="permission_denied", errno=13, detail=r.stderr)
    return RuntimeFileOpResult(
        ok=False, error="io_error", errno=code, detail=r.stderr or r.stdout
    )


def _map_list_result(r: ExecResult) -> RuntimeFileOpResult:
    if r.exit_code != 0:
        return _map_file_error(r)
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError:
        return RuntimeFileOpResult(
            ok=False, error="decode_error", detail="invalid JSON from sandbox helper"
        )
    return RuntimeFileOpResult(ok=True, items=items)


# ---------------------------------------------------------------------- #
# Sandbox-side helper scripts (python3 -S, stdlib only)
# ---------------------------------------------------------------------- #
# Explicit exit-code protocol for read: 44 missing, 45 is-directory. The
# caller strips base64 whitespace before strict decoding (GNU base64 folds
# at 76 columns by default).
_READ_FILE_SCRIPT = (
    f'if [ ! -e "$1" ]; then exit 44; fi; '
    'if [ -d "$1" ]; then exit 45; fi; '
    'if [ ! -r "$1" ]; then exit 13; fi; '
    'if [ $(stat -c %s "$1") -gt 67108864 ]; then exit 46; fi; '
    'base64 -w 0 < "$1"'
)

# Entry shape matches Manager/Conch: ISO modified_time, extension-based type,
# permissions field. Callers filter by extension, so "file" breaks them.
_LIST_DIR_SCRIPT = r'''
import datetime, json, os, sys
root, recursive, max_depth, want_files, want_dirs = (
    sys.argv[1], sys.argv[2] == "1",
    int(sys.argv[3]) if sys.argv[3] else None,
    sys.argv[4] == "1", sys.argv[5] == "1",
)
if not os.path.exists(root):
    sys.exit(44)
if not os.path.isdir(root):
    sys.exit(45)
def entry(p, isdir, st):
    name = os.path.basename(p)
    return {
        "name": name, "path": p,
        "size": 0 if isdir else st.st_size,
        "is_directory": isdir,
        "modified_time": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(),
        "type": None if isdir else (os.path.splitext(name)[1] or None),
        "permissions": None,
    }
out = []
if recursive:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            rel_parts = os.path.relpath(p, root).split(os.sep)
            if max_depth is not None and len(rel_parts) > max_depth:
                continue
            isdir = os.path.isdir(p)
            if (isdir and not want_dirs) or (not isdir and not want_files):
                continue
            try:
                out.append(entry(p, isdir, os.stat(p)))
            except OSError:
                continue
else:
    for name in os.listdir(root):
        if max_depth is not None and max_depth < 1:
            continue
        p = os.path.join(root, name)
        isdir = os.path.isdir(p)
        if (isdir and not want_dirs) or (not isdir and not want_files):
            continue
        try:
            out.append(entry(p, isdir, os.stat(p)))
        except OSError:
            continue
out.sort(key=lambda e: e["path"])
print(json.dumps(out, ensure_ascii=False))
'''

# Search matches BOTH basename and relative path (like Conch), returns files
# only, and keeps the same entry shape as list_dir.
_SEARCH_FILES_SCRIPT = r'''
import datetime, fnmatch, json, os, sys
root, patterns, excludes = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
if not os.path.exists(root):
    sys.exit(44)
if not os.path.isdir(root):
    sys.exit(45)
def matched(name, rel):
    if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in excludes):
        return False
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in patterns)
out = []
for dirpath, dirnames, filenames in os.walk(root):
    for name in filenames:
        p = os.path.join(dirpath, name)
        rel = os.path.relpath(p, root)
        if not matched(name, rel):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({
            "name": name, "path": p, "size": st.st_size,
            "is_directory": False,
            "modified_time": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(),
            "type": os.path.splitext(name)[1] or None,
            "permissions": None,
        })
out.sort(key=lambda e: e["path"])
print(json.dumps(out, ensure_ascii=False))
'''


# ---------------------------------------------------------------------- #
# Service-level deployment facts (host paths and budgets, not policy)
# ---------------------------------------------------------------------- #
def build_default_deployment() -> Deployment:
    """Build the engine Deployment from process facts and env (design §12.1)."""
    bwrap = os.getenv("JIUWENBOX_LITE_BWRAP") or shutil.which("bwrap") or "/usr/bin/bwrap"
    sandbox_python = os.getenv("JIUWENBOX_LITE_SANDBOX_PYTHON") or "/usr/bin/python3"
    runtime_mounts = tuple(
        Mount(source=src, target=src, kind="directory", mode="ro")
        for src in ("/usr", "/lib", "/lib64", "/etc/ssl/certs")
    )
    return Deployment(
        runtime_mounts=runtime_mounts,
        bwrap=bwrap,
        python=sys.executable,
        sandbox_python=sandbox_python,
        service_uid=os.getuid() if hasattr(os, "getuid") else 0,
        service_gid=os.getgid() if hasattr(os, "getgid") else 0,
        # The policy is the source of truth: mode host shares the host netns
        # without proxy control, matching ProcessRuntime. This is the engine
        # Deployment capability flag, not a new policy-selection switch.
        allow_host_network=True,
        backing_root=_backing_root(),
    )
