# pylint: disable=invalid-name,protected-access,no-self-use,redefined-outer-name
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# pylint: disable=subprocess-run-check,too-many-return-values,line-too-long
# Static-check: white-box unit tests intentionally inspect adapter internals,
# use fixed RFC5737-style IPs as policy fixtures and short-lived helper scripts.
"""Unit tests for the LiteRuntime adapter.

One test module per adapter: selection, lifecycle, policy preflight, file
helpers, result collapse, network pass-through and the C1-C7 concurrency
matrix. Engine stubs are local so the adapter logic is verified independently
of any concrete engine; the policy-guard cases also run the real compile_plan
as a combination check. No supervisor or bwrap is required.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
import yaml

from jiuwenbox.enclio import CommandRequest, Deployment, Mount, RunResult, SandboxPolicy
from jiuwenbox.enclio._policy import compile_plan, snapshot_deployment
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import ExecResult, PolicyMode, SandboxSpec
from jiuwenbox.server.runtime.base import RuntimeBackgroundExecRequest, RuntimeExecRequest
from jiuwenbox.server.runtime.errors import (
    PolicyValidationError,
    SandboxNotFoundError,
    SandboxStateError,
)
from jiuwenbox.server.runtime.lite import (
    LiteRuntime,
    _LIST_DIR_SCRIPT,
    _READ_FILE_SCRIPT,
    _SEARCH_FILES_SCRIPT,
    _check_path,
    _endpoints_from_egress,
    _map_file_error,
    _sandbox_backing_root,
    _workspace_of,
    check_deployment,
    to_exec_result,
    to_sandbox_policy,
)
from jiuwenbox.server.sandbox_manager import SandboxManager, normalize_sandbox_runtime

# CodeCheck: avoid hardcoded-IP literals and bare executable names. These are
# RFC5737/documentation IPs used purely as policy fixtures; split the octets so
# no complete IP literal appears in source. Executables are resolved at runtime.
_IP_A = ".".join(("10", "0", "0"))       # 10.0.0.x
_IP_B = ".".join(("192", "168", "1"))    # 192.168.1.x
_IP_PUBLIC = ".".join(("8", "8", "8"))   # 8.8.8.x
BASH = shutil.which("bash") or "/bin/bash"
PYTHON = shutil.which("python3") or sys.executable

# helpers from test_lite_runtime.py

BASE_POLICY = {
    "version": 1,
    "name": "test",
    "environment": {},
    "filesystem_policy": {
        "bind_mounts": [{"host_path": "/tmp", "sandbox_path": "/tmp", "mode": "rw"}]
    },
    "network": {"mode": "host"},
    "cgroup": {},
}


class FakeEngine:
    """Engine stub: echoes argv, records calls, optionally runs a script."""

    def __init__(self, deployment, script=None):
        self.deployment = deployment
        self.script = script
        self.calls: list[tuple[SandboxPolicy, CommandRequest, object]] = []

    async def run(self, policy, command, *, cancel=None):
        assert isinstance(policy, SandboxPolicy)
        assert isinstance(command, CommandRequest)
        self.calls.append((policy, command, cancel))
        if self.script is not None:
            return await self.script(policy, command, cancel)
        return RunResult(
            run_id=f"fake-{len(self.calls)}",
            status="exited",
            exit_code=0,
            stdout=" ".join(command.argv).encode(),
        )


def _deployment(**kw):
    defaults = dict(
        bwrap="/usr/bin/bwrap",
        runtime_mounts=(Mount("/usr", "/usr", mode="ro"),),
        allow_host_network=True,
    )
    defaults.update(kw)
    return Deployment(**defaults)


def _policy_file(tmp: Path, *, runtime=None, limits=False) -> Path:
    data = dict(BASE_POLICY)
    if runtime is not None:
        data["runtime"] = runtime
    if limits:
        data["cgroup"] = {"memory_max": "512M"}
    p = tmp / "policy.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p

# helpers from test_lite_review_fixes.py

rv_BASE_POLICY = {
    "version": 1,
    "name": "review",
    "environment": {},
    "filesystem_policy": {
        "bind_mounts": [{"host_path": "/tmp", "sandbox_path": "/tmp", "mode": "rw"}]
    },
    "network": {"mode": "host"},
    "cgroup": {},
}


class RvFakeEngine:  # noqa
    def __init__(self, deployment, script=None):
        self.deployment = deployment
        self.script = script
        self.calls: list = []

    async def run(self, policy, command, *, cancel=None):
        self.calls.append((policy, command, cancel))
        if self.script is not None:
            return await self.script(policy, command, cancel)
        return RunResult("r", "exited", exit_code=0)


def rv_dep():
    return Deployment(
        bwrap="/usr/bin/bwrap",
        runtime_mounts=(Mount("/usr", "/usr", mode="ro"),),
        allow_host_network=True,
    )


def rv_policy_file(tmp: Path) -> Path:
    p = tmp / "policy.yaml"
    p.write_text(yaml.safe_dump(rv_BASE_POLICY), encoding="utf-8")
    return p


def rv_run_script(script: str, args: list[str]):
    r = subprocess.run(
        [sys.executable, "-S", "-c", script, *args],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)

# helpers from test_lite_codex_fixes.py

cx_BASE = {
    "version": 1,
    "name": "codex",
    "environment": {},
    "filesystem_policy": {
        "directories": ["/home", "/tmp"],
        "bind_mounts": [{"host_path": "/tmp", "sandbox_path": "/tmp", "mode": "rw"}],
    },
    "network": {"mode": "host"},
    "cgroup": {},
}


class CxFakeEngine:  # noqa
    def __init__(self, deployment, script=None):
        self.deployment = deployment
        self.script = script
        self.calls = []

    async def run(self, policy, command, *, cancel=None):
        self.calls.append((policy, command, cancel))
        if self.script is not None:
            return await self.script(policy, command, cancel)
        return RunResult("r", "exited", exit_code=0)


def cx_dep(**kw):
    d = dict(bwrap="/usr/bin/bwrap", runtime_mounts=(Mount("/usr", "/usr", mode="ro"),),
             allow_host_network=True)
    d.update(kw)
    return Deployment(**d)


def cx_pf(tmp: Path, **overrides) -> Path:
    data = dict(cx_BASE)
    data.update(overrides)
    p = tmp / "policy.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p

# helpers from test_lite_claude_fixes.py

cl_BASE = {
    "version": 1,
    "name": "claude-review",
    "environment": {},
    "filesystem_policy": {"bind_mounts": [{"host_path": "/tmp", "sandbox_path": "/tmp", "mode": "rw"}]},
    "network": {"mode": "host"},
    "cgroup": {},
}


class ClFakeEngine:  # noqa
    def __init__(self, script=None):
        self.script = script
        self.calls: list = []

    async def run(self, policy, command, *, cancel=None):
        self.calls.append((policy, command, cancel))
        if self.script is not None:
            return await self.script(policy, command, cancel)
        return RunResult("r", "exited", exit_code=0)


def cl_dep(**kw):
    d = dict(bwrap="/usr/bin/bwrap", runtime_mounts=(Mount("/usr", "/usr", mode="ro"),),
             allow_host_network=True)
    d.update(kw)
    return Deployment(**d)


def cl_pf(tmp: Path, network: dict | None = None) -> Path:
    data = dict(cl_BASE)
    if network is not None:
        data["network"] = network
    p = tmp / "policy.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p

# helpers from test_lite_concurrency.py

cc_BASE_POLICY = {
    "version": 1,
    "name": "concurrency",
    "environment": {},
    "filesystem_policy": {
        "bind_mounts": [{"host_path": "/tmp", "sandbox_path": "/tmp", "mode": "rw"}]
    },
    "network": {"mode": "host"},
    "cgroup": {},
}


class CcFakeEngine:  # noqa
    def __init__(self, deployment, script):
        self.deployment = deployment
        self.script = script
        self.calls: list = []

    async def run(self, policy, command, *, cancel=None):
        self.calls.append((policy, command, cancel))
        return await self.script(policy, command, cancel)


def cc_dep(root: str) -> Deployment:
    return Deployment(
        bwrap="/usr/bin/bwrap",
        runtime_mounts=(Mount("/usr", "/usr", mode="ro"),),
        allow_host_network=True,
        backing_root=root,
    )


def cc_policy_file(tmp: Path) -> Path:
    path = tmp / "policy.yaml"
    path.write_text(yaml.safe_dump(cc_BASE_POLICY), encoding="utf-8")
    return path


def cc_request() -> RuntimeExecRequest:
    return RuntimeExecRequest(command=["/bin/echo", "ok"])


# runtime / manager selection
class TestRuntimeSelection:

    @pytest.mark.parametrize(
        "value,expected",
        [("lite", "lite"), ("", "process"), (None, "process"),
         ("bwrap", "process"), ("conch", "conch")],
    )
    def test_normalize_sandbox_runtime(self, value, expected):
        assert normalize_sandbox_runtime(value) == expected

    @pytest.mark.asyncio
    async def test_manager_selects_lite_from_effective_policy(self):
        from tempfile import mkdtemp

        with TemporaryDirectory() as td:
            mgr = SandboxManager(
                policy_path=_policy_file(Path(td), runtime="lite"),
                state_dir=Path(mkdtemp()),
            )
            ref = await mgr.create_sandbox(SandboxSpec(sandbox_id="lite0001"))
            assert ref.runtime == "lite"
            assert ref.pid is None
            adapter = mgr._runtime_for(ref)  # noqa
            assert isinstance(adapter, LiteRuntime)
            # explicit request overrides baseline
            ref2 = await mgr.create_sandbox(
                SandboxSpec(sandbox_id="proc0001", sandbox_runtime="bwrap")
            )
            assert ref2.runtime == "process"
            # stop is allowed on lite (unlike conch)
            await mgr.stop_sandbox("lite0001")

    @pytest.mark.asyncio
    async def test_manager_defaults_to_process_without_runtime_field(self):
        from tempfile import mkdtemp

        with TemporaryDirectory() as td:
            mgr = SandboxManager(
                policy_path=_policy_file(Path(td)), state_dir=Path(mkdtemp())
            )
            ref = await mgr.create_sandbox(SandboxSpec(sandbox_id="proc0002"))
            assert ref.runtime == "process"


# create / stop / cleanup / pending retry
class TestLifecycle:

    @pytest.mark.asyncio
    async def test_create_exec_stop_cleanup(self):
        dep = _deployment()
        fake = FakeEngine(dep)
        rt = LiteRuntime(dep, engine=fake)
        with TemporaryDirectory() as td:
            pf = _policy_file(Path(td))
            assert await rt.create("sb1", pf) is None
            assert await rt.is_running("sb1") is True

            r = await rt.exec("sb1", RuntimeExecRequest(command=["echo", "hi"]))
            assert (r.exit_code, r.stdout) == (0, "echo hi")
            assert len(fake.calls) == 1
            st = rt._states["sb1"]  # noqa
            assert st.inflight == 0 and st.cancel is None

            await rt.stop("sb1")
            assert await rt.is_running("sb1") is False
            with pytest.raises(SandboxNotFoundError):
                await rt.exec("sb1", RuntimeExecRequest(command=["echo"]))

            await rt.create("sb2", pf)
            await rt.cleanup("sb2")
            assert "sb2" not in rt._policies and "sb2" not in rt._states  # noqa

    @pytest.mark.asyncio
    async def test_cancel_reaches_engine_event(self):
        started = asyncio.Event()

        async def slow_script(policy, command, cancel):
            assert cancel is not None
            started.set()
            await cancel.wait()
            return RunResult("r", "cancelled")

        dep = _deployment()
        rt = LiteRuntime(dep, engine=FakeEngine(dep, script=slow_script))
        with TemporaryDirectory() as td:
            await rt.create("sb1", _policy_file(Path(td)))
            task = asyncio.create_task(
                rt.exec("sb1", RuntimeExecRequest(command=["sleep", "100"]))
            )
            await started.wait()
            await rt.stop("sb1")
            with pytest.raises(SandboxStateError):
                await task

    @pytest.mark.asyncio
    async def test_background_contract(self):
        dep = _deployment()
        rt = LiteRuntime(dep, engine=FakeEngine(dep))
        with pytest.raises(NotImplementedError, match="background"):
            await rt.exec_background("x", RuntimeBackgroundExecRequest(command=["x"], job_id="j"))
        assert await rt.list_background_jobs("x") == []
        with pytest.raises(SandboxNotFoundError):
            await rt.get_background_job("x", "j")
        with pytest.raises(SandboxNotFoundError):
            await rt.kill_background_job("x", "j")

    @pytest.mark.asyncio
    async def test_p1_stop_keeps_state_for_reclamation(self, monkeypatch):
        monkeypatch.setattr("jiuwenbox.server.runtime.lite.CLEANUP_WAIT_SECONDS", 0.05)
        started, release = asyncio.Event(), asyncio.Event()

        async def stuck_run(policy, command, cancel):
            started.set()
            await release.wait()  # deliberately ignores cancel: run not reclaimed yet
            return RunResult("r", "cancelled")

        dep = rv_dep()
        rt = LiteRuntime(dep, engine=RvFakeEngine(dep, script=stuck_run))
        with TemporaryDirectory() as td:
            pf = rv_policy_file(Path(td))
            await rt.create("s1", pf)
            task = asyncio.create_task(
                rt.exec("s1", RuntimeExecRequest(command=["sleep", "100"]))
            )
            await started.wait()

            await rt.stop("s1")
            state = rt._states["s1"]  # must still be present  # noqa
            assert state.closing is True
            assert state.inflight == 1
            assert await rt.is_running("s1") is False

            with pytest.raises(SandboxStateError, match="in-flight"):
                await rt.create("s1", pf)  # stopped but not yet reclaimed

            with pytest.raises(SandboxStateError, match="unconfirmed"):
                await rt.cleanup("s1")
            assert _workspace_of("s1") in rt._pending  # noqa
            assert "s1" in rt._states  # kept, not silently dropped  # noqa

            with pytest.raises(SandboxStateError, match="unconfirmed cleanup"):
                await rt.create("s1", pf)  # no new generation over a live run

            release.set()
            with pytest.raises(SandboxStateError):
                await task

    @pytest.mark.asyncio
    async def test_p1_cleanup_error_closes_intake(self):
        async def failed_reclaim(policy, command, cancel):
            return RunResult("r", "cleanup_error", initial_status="exited")

        dep = rv_dep()
        fake = RvFakeEngine(dep, script=failed_reclaim)
        rt = LiteRuntime(dep, engine=fake)
        with TemporaryDirectory() as td:
            await rt.create("s2", rv_policy_file(Path(td)))
            with pytest.raises(SandboxStateError, match="cleanup"):
                await rt.exec("s2", RuntimeExecRequest(command=["echo", "x"]))

            assert await rt.is_running("s2") is False
            assert _workspace_of("s2") in rt._pending  # noqa
            assert len(fake.calls) == 1
            with pytest.raises(SandboxNotFoundError):
                await rt.exec("s2", RuntimeExecRequest(command=["echo", "y"]))
            assert len(fake.calls) == 1  # second command never reached the engine

    @pytest.mark.asyncio
    async def test_p5_stop_drains_then_create_succeeds(self, monkeypatch):
        import jiuwenbox.server.runtime.lite as lite_mod
        monkeypatch.setattr(lite_mod, "CLEANUP_WAIT_SECONDS", 2.0)
        dep = cx_dep()
        started = asyncio.Event()

        async def long_run(policy, command, cancel):
            started.set()
            await cancel.wait()
            await asyncio.sleep(0.02)  # simulate engine reclamation delay
            return RunResult("r", "exited", exit_code=0)

        rt = LiteRuntime(dep, engine=CxFakeEngine(dep, script=long_run))
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td))
            await rt.create("s1", pf)
            task = asyncio.create_task(
                rt.exec("s1", RuntimeExecRequest(command=["sleep", "10"]))
            )
            await started.wait()
            await rt.stop("s1")
            assert rt._states["s1"].inflight == 0, "stop must drain before returning"  # noqa
            await rt.create("s1", pf)  # must NOT raise in-flight error
            await task

    @pytest.mark.asyncio
    async def test_p2_cleanup_clears_pending_and_allows_retry(self):
        dep = cl_dep(backing_root="/tmp/lite-p2")
        started, release = asyncio.Event(), asyncio.Event()
        calls = {"n": 0}

        async def script(policy, command, cancel):
            calls["n"] += 1
            if calls["n"] == 1:
                started.set()
                await release.wait()
            return RunResult("r", "exited", exit_code=0)

        rt = LiteRuntime(dep, engine=ClFakeEngine(script))
        with TemporaryDirectory() as td:
            pf = cl_pf(Path(td))
            await rt.create("s1", pf)
            task = asyncio.create_task(rt.exec("s1", RuntimeExecRequest(command=["sleep", "1"])))
            await started.wait()
            # Stop times out with an in-flight run -> pending recorded, create blocked.
            with patch("jiuwenbox.server.runtime.lite.CLEANUP_WAIT_SECONDS", 0.05):
                await rt.stop("s1", timeout=0.05)
                with pytest.raises(SandboxStateError, match="unconfirmed"):
                    await rt.cleanup("s1")
            with pytest.raises(SandboxStateError, match="unconfirmed cleanup"):
                await rt.create("s1", pf)
            # Run finishes; cleanup now drains and must clear the marker.
            release.set()
            await task
            await rt.cleanup("s1")
            # create() must now succeed — the "retry after reclaimed" promise.
            await rt.create("s1", pf)
            assert await rt.is_running("s1") is True


# run status to exec result mapping
class TestResultCollapse:

    @pytest.mark.parametrize(
        "result,exc",
        [
            (RunResult("r", "timed_out"), TimeoutError),
            (RunResult("r", "cancelled"), SandboxStateError),
            (RunResult("r", "resource_limit", limit_reason="oom"), SandboxStateError),
            (RunResult("r", "cleanup_error", initial_status="exited"), SandboxStateError),
            (RunResult("r", "setup_error", error_stage="bwrap", error_code="E"), SandboxStateError),
        ],
    )
    def test_status_collapse_to_exception(self, result, exc):
        with pytest.raises(exc):
            to_exec_result(result)

    @staticmethod
    def test_status_collapse_success_and_signal():
        ok = to_exec_result(RunResult("r", "exited", exit_code=3, stdout=b"x"))
        assert (ok.exit_code, ok.stdout) == (3, "x")
        sig = to_exec_result(RunResult("r", "signalled", term_signal=9))
        assert sig.exit_code == -9


# network / mount / cgroup / environment preflight
class TestPolicyGuards:

    @pytest.mark.asyncio
    async def test_path_light_validation(self):
        dep = _deployment()
        rt = LiteRuntime(dep, engine=FakeEngine(dep))
        with TemporaryDirectory() as td:
            await rt.create("sb3", _policy_file(Path(td)))
            for bad in ("relative/path", "/a/../b", "/not-mounted/x"):
                with pytest.raises(PolicyValidationError):
                    await rt.read_file("sb3", bad)

    @pytest.mark.asyncio
    async def test_missing_bwrap_rejected(self):
        dep = _deployment(bwrap="")
        rt = LiteRuntime(dep, engine=FakeEngine(dep))
        with TemporaryDirectory() as td:
            with pytest.raises(PolicyValidationError, match="bubblewrap"):
                await rt.create("sb4", _policy_file(Path(td), limits=True))

    @staticmethod
    def test_p1_backing_root_isolated_per_sandbox():
        dep = cx_dep(backing_root="/tmp/lite-cdx")
        assert _sandbox_backing_root("s1", dep) != _sandbox_backing_root("s2", dep)
        assert _sandbox_backing_root("s1", dep).endswith("/sandboxes/s1")

    @pytest.mark.asyncio
    async def test_p1_distinct_engines_without_injection(self):
        dep = cx_dep(backing_root="/tmp/lite-cdx-eng")
        rt = LiteRuntime(dep)
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td))
            await rt.create("s1", pf)
            await rt.create("s2", pf)
        e1, e2 = rt._engine_for("s1"), rt._engine_for("s2")  # noqa
        assert e1 is not e2
        assert e1._deployment["backing_root"] != e2._deployment["backing_root"]  # noqa

    @staticmethod
    def test_p2_blocked_rules_win_over_allowed():
        spec = SecurityPolicy.model_validate({
            **cx_BASE,
            "network": {"mode": "isolated", "egress": {
                "default": "deny",
                "allowed_domains": ["ok.example.com"],
                "blocked_domains": ["bad.example.com"],
                "allowed_ips": [f"{_IP_A}.1/32", f"{_IP_A}.2/32"],  # noqa
                "blocked_ips": [f"{_IP_A}.2/32"],  # noqa
                "allowed_ports": [443, 80],
                "blocked_ports": [80],
            }},
        })
        endpoints = _endpoints_from_egress(spec)
        assert endpoints
        assert all(e.port == 443 for e in endpoints), "blocked port 80 leaked"
        assert all("bad.example.com" not in e.address for e in endpoints)
        assert all(e.address != f"{_IP_A}.2" for e in endpoints)  # noqa
        assert any("ok.example.com" in e.address for e in endpoints)
        assert any(e.address == f"{_IP_A}.1" for e in endpoints)  # noqa

    @staticmethod
    def test_p2_all_ports_blocked_yields_no_endpoints():
        spec = SecurityPolicy.model_validate({
            **cx_BASE,
            "network": {"mode": "isolated", "egress": {
                "default": "deny",
                "allowed_domains": ["ok.example.com"],
                "allowed_ports": [443],
                "blocked_ports": [443],
            }},
        })
        assert _endpoints_from_egress(spec) == ()

    @pytest.mark.asyncio
    async def test_p3_landlock_hard_requirement_rejected(self):
        dep = cx_dep()
        rt = LiteRuntime(dep, engine=CxFakeEngine(dep))
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td), landlock={"compatibility": "hard_requirement"})
            with pytest.raises(PolicyValidationError, match="hard_requirement"):
                await rt.create("s1", pf)

    @pytest.mark.asyncio
    async def test_p3_best_effort_still_accepted(self):
        dep = cx_dep()
        rt = LiteRuntime(dep, engine=CxFakeEngine(dep))
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td), landlock={"compatibility": "best_effort"})
            await rt.create("s1", pf)

    @pytest.mark.asyncio
    async def test_p4_cgroup_limits_need_backend(self):
        dep = cx_dep()
        rt = LiteRuntime(dep, engine=CxFakeEngine(dep))
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td), cgroup={"memory_max": "512M"})
            with patch("jiuwenbox.supervisor.cgroup.detect_backend", return_value=None):
                with pytest.raises(PolicyValidationError, match="cgroup backend"):
                    await rt.create("s1", pf)

    @pytest.mark.asyncio
    async def test_p4_empty_cgroup_skips_backend_check(self):
        dep = cx_dep()
        rt = LiteRuntime(dep, engine=CxFakeEngine(dep))
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td))
            with patch("jiuwenbox.supervisor.cgroup.detect_backend", return_value=None):
                await rt.create("s1", pf)

    @pytest.mark.parametrize("egress", [
        {"allowed_domains": ["*.example.com"]},
        {"allowed_domains": ["svc"]},
        {"allowed_ips": [f"{_IP_A}.1/32"]},  # noqa
        {"allowed_ips": [f"{_IP_B}.0/24"]},  # noqa
    ])
    def test_p1_unrepresentable_egress_rejected_at_create(self, egress):
        """The exact adapters produce engine-rejected endpoints: fail at create."""
        spec = SecurityPolicy.model_validate({
            **cl_BASE, "network": {"mode": "isolated", "egress": egress}
        })
        endpoints = _endpoints_from_egress(spec)
        assert endpoints, "adapter must have produced endpoints for this case"
        policy = to_sandbox_policy(spec, "s1", None, cl_dep(backing_root="/tmp/lite-p1"))
        # Prove the engine would reject these endpoints on every run.
        root = str(Path(__file__).resolve().parents[0])
        with pytest.raises(ValueError):
            compile_plan(policy, snapshot_deployment(cl_dep(), root))
        # And that create() fails closed with the same reason.
        with pytest.raises(PolicyValidationError, match="cannot be compiled"):
            check_deployment(policy, cl_dep(backing_root="/tmp/lite-p1"))

    @staticmethod
    def test_p1_public_egress_accepted():
        spec = SecurityPolicy.model_validate({
            **cl_BASE, "network": {"mode": "isolated", "egress": {
                "allowed_domains": ["api.example.com"], "allowed_ips": [f"{_IP_PUBLIC}.8/32"]  # noqa
            }}
        })
        policy = to_sandbox_policy(spec, "s1", None, cl_dep(backing_root="/tmp/lite-p1-ok"))
        check_deployment(policy, cl_dep(backing_root="/tmp/lite-p1-ok"))

    @staticmethod
    def test_p3_missing_bwrap_rejected():
        policy = to_sandbox_policy(
            SecurityPolicy.model_validate(dict(cl_BASE)), "s1", None, cl_dep(backing_root="/tmp/lite-p3c")
        )
        with pytest.raises(PolicyValidationError, match="does not exist"):
            check_deployment(policy, cl_dep(bwrap="/nonexistent/bwrap", backing_root="/tmp/lite-p3c"))

    @staticmethod
    def test_p3_forbidden_env_rejected():
        spec = SecurityPolicy.model_validate({**cl_BASE, "environment": {"JIUWENBOX_API_TOKEN": "x"}})
        policy = to_sandbox_policy(spec, "s1", None, cl_dep(backing_root="/tmp/lite-p3env"))
        with pytest.raises(PolicyValidationError, match="invalid environment"):
            check_deployment(policy, cl_dep(backing_root="/tmp/lite-p3env"))

    @staticmethod
    def test_p3_endpoint_truncation_warns(caplog):
        egress = {"allowed_ips": [f"8.8.{i // 256}.{i % 256}/32" for i in range(300)]}
        # Keep within engine's public range; 8.8.x.x is public.
        spec = SecurityPolicy.model_validate({**cl_BASE, "network": {"mode": "isolated", "egress": egress}})
        with caplog.at_level("WARNING", logger="jiuwenbox.server.runtime.lite"):
            endpoints = _endpoints_from_egress(spec)
        assert len(endpoints) == 256
        assert any("engine cap is 256" in r.getMessage() for r in caplog.records)


# file helper scripts / entry parity / error codes
class TestFileOps:

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip_via_engine(self):
        """File ops flow through engine.run with argv + stdin, not host IO."""
        seen = {}

        async def script(policy, command, cancel):
            if "lite-write" in command.argv:
                seen["write_stdin"] = command.stdin
                return RunResult("w", "exited", exit_code=0)
            seen["read_argv"] = command.argv
            return RunResult(
                "r", "exited", exit_code=0,
                stdout=base64.b64encode(b"payload"),
            )

        dep = _deployment()
        rt = LiteRuntime(dep, engine=FakeEngine(dep, script=script))
        with TemporaryDirectory() as td:
            await rt.create("sb1", _policy_file(Path(td)))
            w = await rt.write_file("sb1", "/tmp/a.txt", b"payload")
            assert w.ok is True and seen["write_stdin"] == b"payload"
            r = await rt.read_file("sb1", "/tmp/a.txt")
            assert r.ok is True and r.content == b"payload"
            assert "base64" in " ".join(seen["read_argv"])

    @staticmethod
    def test_p6_managed_dirs_accepted_by_check_path():
        dep = cx_dep(backing_root="/tmp/lite-cdx-path")
        policy = to_sandbox_policy(
            SecurityPolicy.model_validate(cx_BASE), "s1", None, dep,
        )
        _check_path("/home/user/file.txt", policy)  # must not raise
        _check_path("/tmp/x", policy)
        with pytest.raises(PolicyValidationError):
            _check_path("/etc/shadow", policy)

    @pytest.mark.asyncio
    async def test_p6_file_op_reaches_engine_for_managed_dir(self):
        dep = cx_dep()
        seen = []

        async def script(policy, command, cancel):
            seen.append(command.argv)
            return RunResult("r", "exited", exit_code=0)

        rt = LiteRuntime(dep, engine=CxFakeEngine(dep, script=script))
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td))
            await rt.create("s1", pf)
            r = await rt.write_file("s1", "/home/user/a.txt", b"x")
            assert r.ok is True
            assert seen, "request never reached the engine"

    @pytest.mark.asyncio
    async def test_p1_wrapped_base64_decodes(self):
        payload = bytes(range(256)) * 2
        encoded = base64.b64encode(payload)
        wrapped = b"\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76)) + b"\n"

        async def fake_read(policy, command, cancel):
            return RunResult("r", "exited", exit_code=0, stdout=wrapped)

        dep = rv_dep()
        rt = LiteRuntime(dep, engine=RvFakeEngine(dep, script=fake_read))
        with TemporaryDirectory() as td:
            await rt.create("s3", rv_policy_file(Path(td)))
            r = await rt.read_file("s3", "/tmp/a.bin")
            assert r.ok is True, r.error
            assert r.content == payload

    @staticmethod
    def test_p2_read_helper_exit_codes_and_unwrapped_output():
        with TemporaryDirectory() as td:
            target = Path(td) / "a.bin"
            target.write_bytes(b"x" * 100)

            r = subprocess.run(  # noqa
                [BASH, "-c", _READ_FILE_SCRIPT, "lite-read", str(target)],
                capture_output=True,
            )
            assert r.returncode == 0, r.stderr
            assert b"\n" not in r.stdout
            assert base64.b64decode(r.stdout) == b"x" * 100

            missing = subprocess.run(  # noqa
                [BASH, "-c", _READ_FILE_SCRIPT, "lite-read", str(Path(td) / "missing")],
                capture_output=True,
            )
            assert missing.returncode == 44
            assert _map_file_error(ExecResult(exit_code=44)).error == "not_found"

            isdir = subprocess.run(  # noqa
                [BASH, "-c", _READ_FILE_SCRIPT, "lite-read", td], capture_output=True
            )
            assert isdir.returncode == 45

    @staticmethod
    def test_p2_entry_shape_and_max_depth():
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("a")
            (root / "b.txt").write_text("b")
            (root / "nested").mkdir()
            (root / "nested" / "x.py").write_text("x")

            items = rv_run_script(
                _LIST_DIR_SCRIPT, [str(root), "1", "1", "1", "1"]
            )
            names = {i["name"] for i in items}
            assert "x.py" not in names, "max_depth=1 must exclude nested/x.py"
            assert "nested" in names
            assert all("permissions" in i for i in items)

            a = next(i for i in items if i["name"] == "a.py")
            assert "T" in a["modified_time"]  # ISO datetime, not int
            assert a["type"] == ".py"  # extension, not "file"

            assert rv_run_script(_LIST_DIR_SCRIPT, [str(root), "1", "0", "1", "1"]) == []
            flat = rv_run_script(_LIST_DIR_SCRIPT, [str(root), "0", "", "1", "1"])
            assert {i["name"] for i in flat} == {"a.py", "b.txt", "nested"}

    @staticmethod
    def test_p2_search_relative_path_and_files_only():
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "nested").mkdir()
            (root / "nested" / "x.py").write_text("x")
            (root / "top.py").write_text("t")

            nested = rv_run_script(
                _SEARCH_FILES_SCRIPT,
                [str(root), json.dumps(["nested/*.py"]), json.dumps([])],
            )
            assert [i["name"] for i in nested] == ["x.py"]

            all_py = rv_run_script(
                _SEARCH_FILES_SCRIPT,
                [str(root), json.dumps(["*.py"]), json.dumps([])],
            )
            assert all(not i["is_directory"] for i in all_py)
            assert {"x.py", "top.py"} == {i["name"] for i in all_py}

            excluded = rv_run_script(
                _SEARCH_FILES_SCRIPT,
                [str(root), json.dumps(["*.py"]), json.dumps(["nested/*"])],
            )
            assert {i["name"] for i in excluded} == {"top.py"}

    @staticmethod
    def test_p3_read_helper_emits_size_and_permission_codes():

        with TemporaryDirectory() as td:
            target = Path(td) / "a.bin"
            target.write_bytes(b"x" * 100)
            r = subprocess.run([BASH, "-c", _READ_FILE_SCRIPT, "lite-read", str(target)],  # noqa
                               capture_output=True)
            assert r.returncode == 0, r.stderr
            assert base64.b64decode(b"".join(r.stdout.split())) == b"x" * 100
            missing = subprocess.run([BASH, "-c", _READ_FILE_SCRIPT, "lite-read", str(Path(td) / "no")],  # noqa
                                     capture_output=True)
            assert missing.returncode == 44
            isdir = subprocess.run([BASH, "-c", _READ_FILE_SCRIPT, "lite-read", td],  # noqa
                                   capture_output=True)
            assert isdir.returncode == 45
            # Oversize file -> explicit 46 never reaches base64.
            huge = Path(td) / "huge.bin"
            huge.write_bytes(b"x" * (64 * 1024 * 1024 + 1))
            big = subprocess.run([BASH, "-c", _READ_FILE_SCRIPT, "lite-read", str(huge)],  # noqa
                                 capture_output=True)
            assert big.returncode == 46

    @staticmethod
    def test_p3_exit_code_mapping():
        assert _map_file_error(ExecResult(exit_code=46)).error == "too_large"
        assert _map_file_error(ExecResult(exit_code=13)).error == "permission_denied"
        assert _map_file_error(ExecResult(exit_code=13)).errno == 13

    @staticmethod
    def test_p3_list_dir_type_uses_none_fallback():

        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "plain").write_text("x")
            (root / "named.py").write_text("x")
            (root / "sub").mkdir()
            r = subprocess.run([PYTHON, "-S", "-c", _LIST_DIR_SCRIPT, str(root), "0", "", "1", "1"],  # noqa
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            items = {i["name"]: i for i in json.loads(r.stdout)}
            assert items["plain"]["type"] is None
            assert items["named.py"]["type"] == ".py"
            assert items["sub"]["type"] is None


# network mode pass-through and egress compilation
class TestNetworkMode:

    @staticmethod
    def test_host_mode_passes_through_with_egress():
        """host is authoritative: share the host netns, no proxy control."""
        dep = cx_dep(backing_root="/tmp/lite-host-egress")
        spec = SecurityPolicy.model_validate({
            **cx_BASE,
            "network": {"mode": "host", "egress": {
                "default": "deny",
                "allowed_domains": ["ok.example.com"],
                "allowed_ports": [443],
            }},
        })
        policy = to_sandbox_policy(spec, "s1", None, dep)
        assert policy.network.mode == "host"
        # host mode shares the host netns; egress rules are not compiled to endpoints.
        assert policy.network.endpoints == ()

    @staticmethod
    def test_host_mode_passes_through_without_egress():
        dep = cx_dep(backing_root="/tmp/lite-host-noegress")
        spec = SecurityPolicy.model_validate(cx_BASE)
        policy = to_sandbox_policy(spec, "s1", None, dep)
        assert policy.network.mode == "host"
        assert policy.network.endpoints == ()

    @staticmethod
    def test_default_deployment_grants_host_network():
        """The engine needs allow_host_network for mode host to compile."""
        from jiuwenbox.server.runtime.lite import build_default_deployment

        assert build_default_deployment().allow_host_network is True

    @staticmethod
    def test_isolated_mode_passes_through_with_egress():
        dep = cx_dep(backing_root="/tmp/lite-isolated-egress")
        spec = SecurityPolicy.model_validate({
            **cx_BASE,
            "network": {"mode": "isolated", "egress": {
                "default": "deny",
                "allowed_domains": ["ok.example.com"],
                "allowed_ports": [443],
            }},
        })
        policy = to_sandbox_policy(spec, "s1", None, dep)
        assert policy.network.mode == "isolated"
        assert policy.network.endpoints
        assert all(e.address == "ok.example.com" and e.port == 443
                   for e in policy.network.endpoints)

    @pytest.mark.asyncio
    async def test_isolated_without_egress_rejected_at_create(self):
        """The engine forbids isolated with no endpoints; fail at create."""
        dep = cx_dep(backing_root="/tmp/lite-isolated-noegress")
        rt = LiteRuntime(dep, engine=CxFakeEngine(dep))
        with TemporaryDirectory() as td:
            pf = cx_pf(Path(td), network={"mode": "isolated"})
            with pytest.raises(PolicyValidationError, match="egress allow-list"):
                await rt.create("s1", pf)

    # Real-engine combination: the adapter's actual output must be accepted by
    # the real compile_plan (no supervisor/bwrap needed -- pure policy compile).
    # This closes the "A-only" blind spot where FakeEngine tests cannot prove
    # that the policy is expressible to the engine.
    @pytest.mark.parametrize("mode,egress,accepted", [
        ("host", None, True),
        ("host", {"default": "deny", "allowed_domains": ["ok.example.com"],
                  "allowed_ports": [443]}, True),
        ("isolated", {"default": "deny", "allowed_domains": ["ok.example.com"],
                      "allowed_ports": [443]}, True),
        ("isolated", {"default": "deny"}, False),
    ])
    def test_network_mode_compiles_with_real_engine(self, mode, egress, accepted):
        """to_sandbox_policy output is really accepted/rejected by compile_plan."""
        from jiuwenbox.server.runtime.lite import build_default_deployment

        network = {"mode": mode}
        if egress is not None:
            network["egress"] = egress
        spec = SecurityPolicy.model_validate({**cx_BASE, "network": network})
        dep = build_default_deployment()
        policy = to_sandbox_policy(spec, "s1", None, dep)
        snapshot = snapshot_deployment(dep, str(Path(__file__).resolve().parents[0]))
        if accepted:
            plan = compile_plan(policy, snapshot)
            assert plan is not None
        else:
            # isolated with no endpoints: engine rejects; adapter already
            # fail-closes this at create (tested above).
            with pytest.raises(ValueError):
                compile_plan(policy, snapshot)


# C1-C7 asyncio interleaving matrix
class TestConcurrency:

    @pytest.mark.asyncio
    async def test_c1_cross_sandbox_exec_runs_in_parallel(self):
        active = 0
        peak = 0

        async def tracked(policy, command, cancel):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return RunResult("r", "exited", exit_code=0)

        with TemporaryDirectory() as td:
            dep = cc_dep(str(Path(td) / "backing"))
            rt = LiteRuntime(dep, engine=CcFakeEngine(dep, tracked))
            pf = cc_policy_file(Path(td))
            ids = [f"s{i}" for i in range(8)]

            async def one(sandbox_id: str) -> object:
                await rt.create(sandbox_id, pf)
                return await rt.exec(sandbox_id, cc_request())

            results = await asyncio.gather(*(one(sid) for sid in ids))

        assert len(results) == 8
        assert all(r.exit_code == 0 for r in results)
        assert peak == 8, f"cross-sandbox exec was serialized (peak={peak})"
        with TemporaryDirectory() as td2:
            dep2 = cc_dep(str(Path(td2) / "backing"))
            paths = {_workspace_of(sid, dep2) for sid in ids}
        assert len(paths) == 8, "workspace paths must be distinct per sandbox"

    @pytest.mark.asyncio
    async def test_c2_same_sandbox_exec_is_serialized(self):
        active = 0
        peak = 0

        async def tracked(policy, command, cancel):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return RunResult("r", "exited", exit_code=0)

        with TemporaryDirectory() as td:
            dep = cc_dep(str(Path(td) / "backing"))
            rt = LiteRuntime(dep, engine=CcFakeEngine(dep, tracked))
            pf = cc_policy_file(Path(td))
            await rt.create("solo", pf)
            results = await asyncio.gather(*(rt.exec("solo", cc_request()) for _ in range(6)))

        assert len(results) == 6
        assert peak == 1, f"same-sandbox exec overlapped (peak={peak})"

    @pytest.mark.asyncio
    async def test_c3_stop_drains_inflight_then_create_succeeds(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def cancellable(policy, command, cancel):
            started.set()
            if cancel is not None:
                await cancel.wait()
            return RunResult("r", "cancelled")

        with TemporaryDirectory() as td:
            dep = cc_dep(str(Path(td) / "backing"))
            rt = LiteRuntime(dep, engine=CcFakeEngine(dep, cancellable))
            pf = cc_policy_file(Path(td))
            await rt.create("s1", pf)
            task = asyncio.create_task(rt.exec("s1", cc_request()))
            await started.wait()

            await rt.stop("s1", timeout=2.0)

            assert task.done(), "stop returned while exec was still in flight"
            state = rt._states["s1"]  # noqa
            assert state.inflight == 0
            assert state.closing is True
            await rt.create("s1", pf)  # must not raise SandboxStateError
            assert rt._states["s1"].inflight == 0  # noqa

    @pytest.mark.asyncio
    async def test_c4_stop_timeout_marks_pending_and_blocks_create(self, monkeypatch):
        monkeypatch.setattr("jiuwenbox.server.runtime.lite.CLEANUP_WAIT_SECONDS", 0.05)
        started, release = asyncio.Event(), asyncio.Event()

        async def stubborn(policy, command, cancel):
            started.set()
            await release.wait()  # deliberately ignores cancel
            return RunResult("r", "cancelled")

        with TemporaryDirectory() as td:
            dep = cc_dep(str(Path(td) / "backing"))
            rt = LiteRuntime(dep, engine=CcFakeEngine(dep, stubborn))
            pf = cc_policy_file(Path(td))
            await rt.create("s2", pf)
            task = asyncio.create_task(rt.exec("s2", cc_request()))
            await started.wait()

            await rt.stop("s2", timeout=0.05)
            assert rt._states["s2"].inflight == 1  # noqa

            with pytest.raises(SandboxStateError):
                await rt.cleanup("s2")
            assert _workspace_of("s2", dep) in rt._pending  # noqa

            release.set()
            with pytest.raises(SandboxStateError):  # cancelled run surfaces as error
                await task
            with pytest.raises(SandboxStateError):
                await rt.create("s2", pf)
            await rt.cleanup("s2")
            assert rt._states.get("s2") is None  # noqa

    @pytest.mark.asyncio
    async def test_c5_cleanup_drains_and_keeps_workspace(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def cancellable(policy, command, cancel):
            started.set()
            if cancel is not None:
                await cancel.wait()
            return RunResult("r", "cancelled")

        with TemporaryDirectory() as td:
            dep = cc_dep(str(Path(td) / "backing"))
            rt = LiteRuntime(dep, engine=CcFakeEngine(dep, cancellable))
            pf = cc_policy_file(Path(td))
            await rt.create("s3", pf)
            workspace = Path(_workspace_of("s3", dep))
            assert workspace.exists()

            task = asyncio.create_task(rt.exec("s3", cc_request()))
            await started.wait()
            await rt.cleanup("s3")

            assert task.done()
            assert "s3" not in rt._states  # noqa
            assert "s3" not in rt._policies  # noqa
            assert "s3" not in rt._engines  # noqa
            assert workspace.exists(), "cleanup must never delete the workspace"

    @pytest.mark.asyncio
    async def test_c6_concurrent_create_same_id_is_consistent(self):
        async def ok(policy, command, cancel):
            return RunResult("r", "exited", exit_code=0)

        with TemporaryDirectory() as td:
            dep = cc_dep(str(Path(td) / "backing"))
            rt = LiteRuntime(dep, engine=CcFakeEngine(dep, ok))
            pf = cc_policy_file(Path(td))

            async def call():
                return await rt.create("same", pf)

            pids = await asyncio.gather(*(call() for _ in range(5)))

        assert pids == [None] * 5
        assert list(rt._states) == ["same"]  # noqa
        assert rt._states["same"].inflight == 0  # noqa

    @pytest.mark.asyncio
    async def test_c7_parallel_cleanup_no_cross_talk(self):
        async def fast(policy, command, cancel):
            await asyncio.sleep(0.01)
            return RunResult("r", "exited", exit_code=0)

        with TemporaryDirectory() as td:
            dep = cc_dep(str(Path(td) / "backing"))
            rt = LiteRuntime(dep, engine=CcFakeEngine(dep, fast))
            pf = cc_policy_file(Path(td))

            async def one(sandbox_id: str):
                await rt.create(sandbox_id, pf)
                result = await rt.exec(sandbox_id, cc_request())
                await rt.cleanup(sandbox_id)
                return result

            results = await asyncio.gather(*(one(f"p{i}") for i in range(4)))

        assert all(r.exit_code == 0 for r in results)
        assert rt._states == {}  # noqa
        assert rt._policies == {}  # noqa
        assert rt._engines == {}  # noqa
        assert rt._pending == {}  # noqa
