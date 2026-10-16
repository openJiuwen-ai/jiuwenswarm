"""Landlock failure admission, exact inode rules and reported protection."""

import asyncio
import base64
import ctypes
import json
from pathlib import Path
import posixpath
import stat
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from jiuwenbox.models.policy import LandlockPolicy, SecurityPolicy
from jiuwenbox.models.sandbox import LandlockStatus, PolicyMode, SandboxRef
from jiuwenbox.server.policy_engine import PolicyEngine, PolicyValidationError
from jiuwenbox.server.runtime import process
from jiuwenbox.server.sandbox_manager import SandboxManager
from jiuwenbox.supervisor import landlock, landlock_launcher as launcher


def payload(mode="hard_requirement"):
    return {"compatibility": mode, "read_only": ["/usr"], "read_write": ["/work"]}


@pytest.fixture
def kernel(monkeypatch):
    close = Mock()
    calls = []

    def syscall(nr, *args):
        calls.append(nr)
        return 42 if nr == launcher.LANDLOCK_CREATE_RULESET else 0

    monkeypatch.setattr(launcher, "_detect_abi", lambda: 3)
    monkeypatch.setattr(launcher, "prctl", lambda *a: 0)
    monkeypatch.setattr(launcher, "_syscall", syscall)
    monkeypatch.setattr(launcher, "_add_rule", Mock())
    monkeypatch.setattr(launcher, "os", SimpleNamespace(close=close))
    return close, calls


@pytest.mark.parametrize("mode", ["hard_requirement", "best_effort"])
@pytest.mark.parametrize("stage", ["nnp", "create", "add", "restrict"])
def test_initialization_errors_abort_even_in_compatibility_mode(kernel, monkeypatch, mode, stage):
    close, calls = kernel
    if stage == "nnp":
        monkeypatch.setattr(launcher, "prctl", lambda *a: -1)
    elif stage == "add":
        monkeypatch.setattr(launcher, "_add_rule", Mock(side_effect=OSError("rule failed")))
    else:
        fail_nr = (
            launcher.LANDLOCK_CREATE_RULESET
            if stage == "create"
            else launcher.LANDLOCK_RESTRICT_SELF
        )
        monkeypatch.setattr(launcher, "_syscall", lambda nr, *a: -1 if nr == fail_nr else 42)
    with pytest.raises(launcher.LandlockHardRequirementError):
        launcher.apply_landlock(payload(mode))
    assert close.call_count == (1 if stage in {"add", "restrict"} else 0)


@pytest.mark.parametrize("abi", [0, 1, 2])
def test_default_requires_complete_protection(kernel, monkeypatch, abi):
    monkeypatch.setattr(launcher, "_detect_abi", lambda: abi)
    with pytest.raises(launcher.LandlockHardRequirementError, match="Landlock"):
        launcher.apply_landlock(payload())


@pytest.mark.parametrize("abi", [0, 1, 2, 3, 6])
def test_explicit_compatibility_reports_actual_result(kernel, monkeypatch, caplog, abi):
    monkeypatch.setattr(launcher, "_detect_abi", lambda: abi)
    status = launcher.apply_landlock(payload("best_effort"))
    assert status["abi"] == abi
    assert status["state"] == ("enforced" if abi >= 3 else "degraded")
    if abi < 3:
        assert status["reason"] and "Landlock degraded" in caplog.text
    if abi == 0:
        assert not kernel[1], "unsupported kernel must not attempt installing a partial ruleset"


def test_disabled_is_explicit_and_visible(kernel, caplog):
    status = launcher.apply_landlock(payload("disabled"))
    assert status["state"] == "disabled" and status["reason"]
    assert "Landlock disabled" in caplog.text
    assert not kernel[1]


def test_supported_kernel_handles_refer_and_truncate(kernel):
    assert launcher.apply_landlock(payload())["state"] == "enforced"
    ro, rw = launcher._access_masks(3)
    assert not ro & launcher.TRUNCATE
    assert rw & launcher.REFER and rw & launcher.TRUNCATE
    assert ctypes.sizeof(launcher.LandlockPathBeneathAttr) == 12


@pytest.mark.parametrize("kind", ["file", "directory", "device"])
@pytest.mark.parametrize("writable", [True, False])
def test_exact_object_fd_and_access_mask(monkeypatch, kind, writable):
    opened, rules = [], []
    close = Mock()
    mode = {"file": stat.S_IFREG, "directory": stat.S_IFDIR, "device": stat.S_IFCHR}[kind]

    def add(nr, *args):
        rule = launcher.LandlockPathBeneathAttr.from_address(args[2])
        rules.append((rule.parent_fd, rule.allowed_access))
        return 0

    monkeypatch.setattr(
        launcher,
        "os",
        SimpleNamespace(
            open=lambda path, flags: opened.append(path) or 19,
            fstat=lambda fd: SimpleNamespace(st_mode=mode),
            close=close,
            O_PATH=0x200000,
            O_CLOEXEC=0x80000,
            readlink=lambda _: "/etc/exact-file",
            path=SimpleNamespace(realpath=lambda path: path),
        ),
    )
    monkeypatch.setattr(launcher, "_syscall", add)
    ro, rw = launcher._access_masks(3)
    launcher._add_path_rule(42, "/etc/exact-file", rw if writable else ro)
    assert opened == ["/etc/exact-file"], "must not grant /etc"
    expected = rw if writable else ro
    if kind != "directory":
        expected &= launcher.EXECUTE | launcher.READ_FILE | launcher.WRITE_FILE | launcher.TRUNCATE
    assert rules == [(19, expected)]
    close.assert_called_once_with(19)


def test_missing_optional_object_never_grants_parent(monkeypatch):
    add = Mock()
    monkeypatch.setattr(launcher, "_syscall", add)
    monkeypatch.setattr(
        launcher,
        "os",
        SimpleNamespace(
            open=Mock(side_effect=FileNotFoundError()),
            O_PATH=0,
            O_CLOEXEC=0,
        ),
    )
    launcher._add_path_rule(42, "/etc/missing", launcher.READ_FILE)
    add.assert_not_called()


def test_access_denied_is_not_misreported_as_missing(monkeypatch):
    monkeypatch.setattr(
        launcher,
        "os",
        SimpleNamespace(
            open=Mock(side_effect=PermissionError()),
            O_PATH=0,
            O_CLOEXEC=0,
        ),
    )
    with pytest.raises(PermissionError):
        launcher._add_path_rule(42, "/private", launcher.READ_FILE)


@pytest.mark.parametrize("target", ["/jiuwenbox/sandbox-daemon.py", "/"])
def test_opened_fd_cannot_widen_access_after_symlink_change(monkeypatch, target):
    calls, close = [], Mock()
    monkeypatch.setattr(
        launcher,
        "os",
        SimpleNamespace(
            open=lambda *a: 19,
            close=close,
            O_PATH=0,
            O_CLOEXEC=0,
            readlink=lambda _: target,
            path=SimpleNamespace(realpath=lambda path: path),
            fstat=lambda _: SimpleNamespace(st_mode=stat.S_IFDIR),
        ),
    )

    def add(nr, *args):
        calls.append(launcher.LandlockPathBeneathAttr.from_address(args[2]).allowed_access)
        return 0

    monkeypatch.setattr(launcher, "_syscall", add)
    if target == "/":
        launcher._add_path_rule(42, "/changed-symlink", launcher.BASE_READ_WRITE_ACCESS)
        assert calls == [launcher.READ_DIR]
    else:
        with pytest.raises(OSError, match="alias of /jiuwenbox"):
            launcher._add_path_rule(42, "/changed-symlink", launcher.READ_FILE)
        assert not calls
    close.assert_called_once_with(19)


def test_root_grants_exclude_runtime_and_aliases(monkeypatch):
    entries = [SimpleNamespace(path=p) for p in ["/usr", "/data", "/jiuwenbox", "/alias"]]

    class Entries:
        def __enter__(self):
            return entries

        def __exit__(self, *a):
            pass

    paths = []
    monkeypatch.setattr(
        launcher,
        "os",
        SimpleNamespace(
            path=SimpleNamespace(
                normpath=posixpath.normpath, realpath=lambda p: "/jiuwenbox" if p == "/alias" else p
            ),
            scandir=lambda p: Entries(),
        ),
    )
    monkeypatch.setattr(
        launcher, "_add_path_rule", lambda fd, path, access: paths.append((path, access))
    )
    launcher._add_rule(42, "/", launcher.BASE_READ_WRITE_ACCESS)
    assert paths == [
        ("/", launcher.READ_DIR),
        ("/usr", launcher.BASE_READ_WRITE_ACCESS),
        ("/data", launcher.BASE_READ_WRITE_ACCESS),
    ]
    with pytest.raises(OSError, match="/jiuwenbox"):
        launcher._add_rule(42, "/alias", launcher.READ_FILE)


@pytest.mark.parametrize("root", ["/", "//", "///", "/tmp/.."])
def test_default_root_is_listing_only_and_file_mount_stays_exact(root):
    policy = SecurityPolicy.model_validate(
        {
            "filesystem_policy": {
                "read_only": ["/", "/usr"],
                "bind_mounts": [
                    {"host_path": "/host/config", "sandbox_path": "/etc/config", "mode": "ro"}
                ],
            }
        }
    )
    # Model paths follow the host OS; exercise Linux payloads on Windows too.
    policy.filesystem_policy.read_only = [root, "/usr"]
    policy.filesystem_policy.bind_mounts[0].sandbox_path = "/etc/config"
    decoded = json.loads(base64.urlsafe_b64decode(landlock.encode_landlock_payload(policy)))
    assert "/" not in decoded["read_only"]
    assert decoded["directory_only"] == ["/"]
    assert "/etc/config" in decoded["read_only"] and "/etc" not in decoded["read_only"]
    assert "/jiuwenbox" not in decoded["read_only"] + decoded["read_write"]


def test_bind_root_expansion_matches_bwrap_exclusions(tmp_path):
    (tmp_path / "allowed").write_text("ok")
    (tmp_path / "secret").write_text("secret")
    (tmp_path / ".hidden").write_text("hidden")
    policy = SecurityPolicy.model_validate(
        {
            "filesystem_policy": {
                "bind_root_entries": [
                    {
                        "host_root": str(tmp_path),
                        "sandbox_path": "/data",
                        "mode": "ro",
                        "exclude": ["secret"],
                    }
                ]
            }
        }
    )
    policy.filesystem_policy.bind_root_entries[0].sandbox_path = "/data"
    decoded = json.loads(base64.urlsafe_b64decode(landlock.encode_landlock_payload(policy)))
    assert "/data/allowed" in decoded["read_only"]
    assert "/data" not in decoded["read_only"]
    assert (
        "/data/secret" not in decoded["read_only"] and "/data/.hidden" not in decoded["read_only"]
    )


@pytest.mark.parametrize(
    "filename",
    ["default-policy.yaml", "enterprise-policy.yaml", "java-policy.yaml", "code-agent-policy.yaml"],
)
def test_bundled_linux_policies_are_mandatory(filename):
    path = Path(landlock.__file__).parents[1] / "configs" / filename
    policy = SecurityPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert policy.landlock.compatibility == "hard_requirement"
    assert LandlockPolicy().compatibility == "hard_requirement"


@pytest.mark.parametrize("mode", [PolicyMode.APPEND, PolicyMode.OVERRIDE])
@pytest.mark.parametrize(
    "base,requested",
    [
        ("hard_requirement", "best_effort"),
        ("hard_requirement", "disabled"),
        ("best_effort", "disabled"),
    ],
)
def test_request_cannot_weaken_deployment_floor(monkeypatch, tmp_path, mode, base, requested):
    from jiuwenbox.server import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "sys", SimpleNamespace(platform="linux"))
    manager = SandboxManager.__new__(SandboxManager)
    manager.policy = SecurityPolicy(landlock={"compatibility": base})
    manager.policy_engine = PolicyEngine(policies_dir=tmp_path)
    with pytest.raises(PolicyValidationError, match="deployment policy"):
        manager._resolve_effective_policy({"landlock": {"compatibility": requested}}, mode)


@pytest.mark.parametrize("server_mode", ["best_effort", "disabled", "hard_requirement"])
@pytest.mark.parametrize("mode", [PolicyMode.APPEND, PolicyMode.OVERRIDE])
def test_omitted_mode_inherits_explicit_deployment_choice(monkeypatch, tmp_path, server_mode, mode):
    from jiuwenbox.server import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "sys", SimpleNamespace(platform="linux"))
    manager = SandboxManager.__new__(SandboxManager)
    manager.policy = SecurityPolicy(landlock={"compatibility": server_mode})
    manager.policy_engine = PolicyEngine(policies_dir=tmp_path)
    assert manager._resolve_effective_policy({}, mode).landlock.compatibility == server_mode
    assert (
        manager._resolve_effective_policy(SecurityPolicy(), mode).landlock.compatibility
        == server_mode
    )


def test_windows_policy_keeps_existing_compatibility(monkeypatch, tmp_path):
    from jiuwenbox.server import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "sys", SimpleNamespace(platform="win32"))
    manager = SandboxManager.__new__(SandboxManager)
    manager.policy = SecurityPolicy()
    manager.policy_engine = PolicyEngine(policies_dir=tmp_path)
    result = manager._resolve_effective_policy(
        {"landlock": {"compatibility": "disabled"}}, PolicyMode.OVERRIDE
    )
    assert result.landlock.compatibility == "disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,state,abi",
    [
        ("hard_requirement", "enforced", 3),
        ("best_effort", "degraded", 0),
        ("disabled", "disabled", 0),
    ],
)
async def test_startup_verifies_and_reports_actual_status(monkeypatch, mode, state, abi, caplog):
    runtime = process.ProcessRuntime()
    runtime._runtime_policies["sb"] = SecurityPolicy(landlock={"compatibility": mode})
    monkeypatch.setattr(process, "DAEMON_STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(runtime, "_verify_daemon_alive", Mock())
    monkeypatch.setattr(
        runtime,
        "_read_landlock_status",
        lambda _: LandlockStatus(
            mode=mode,
            state=state,
            abi=abi,
            reason="kernel unavailable" if state != "enforced" else None,
        ),
    )
    await runtime._wait_daemon_ready("sb", Mock(), None)
    assert runtime._daemon_socket_ready["sb"] is True
    status = runtime._landlock_statuses["sb"]
    ref = SandboxRef(id="sb")
    manager = SandboxManager.__new__(SandboxManager)
    manager.runtime = SimpleNamespace(get_landlock_status=lambda _: status)
    manager._refresh_landlock_status(ref)
    assert ref.model_dump(mode="json")["landlock_status"]["state"] == state
    assert bool(ref.security_warnings) == (state != "enforced")
    if state != "enforced":
        assert "Landlock" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        None,
        {"mode": "hard_requirement", "state": "degraded", "abi": 0},
        {"mode": "best_effort", "state": "enforced", "abi": 3},
    ],
)
async def test_unverified_or_weakened_status_prevents_ready(monkeypatch, report):
    runtime = process.ProcessRuntime()
    runtime._runtime_policies["sb"] = SecurityPolicy()
    monkeypatch.setattr(process, "DAEMON_STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(runtime, "_verify_daemon_alive", Mock())
    monkeypatch.setattr(
        runtime, "_read_landlock_status", lambda _: LandlockStatus.model_validate(report)
    )
    stop, cleanup = AsyncMock(), Mock()
    monkeypatch.setattr(runtime, "stop", stop)
    monkeypatch.setattr(runtime, "_cleanup_sandbox_artifacts", cleanup)
    with pytest.raises(RuntimeError, match="Landlock startup verification failed"):
        await runtime._wait_daemon_ready("sb", Mock(), None)
    assert not runtime._daemon_socket_ready.get("sb")
    stop.assert_awaited_once()
    cleanup.assert_called_once()


def test_failed_launcher_never_executes_daemon(monkeypatch, tmp_path):
    marker = tmp_path / "executed"
    script = tmp_path / "daemon.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef main(): return 0\n"
    )
    monkeypatch.setattr(
        launcher,
        "apply_landlock",
        Mock(side_effect=launcher.LandlockHardRequirementError("blocked")),
    )
    assert launcher._run_daemon_in_process(str(script), payload()) == 126
    assert not marker.exists()


def test_launcher_passes_trusted_result_in_memory(monkeypatch, tmp_path):
    script = tmp_path / "daemon.py"
    script.write_text(
        "LANDLOCK_STATUS=None\ndef main():\n    assert LANDLOCK_STATUS['state']=='enforced'\n    return 0\n"
    )
    monkeypatch.setattr(launcher, "apply_landlock", lambda _: {"state": "enforced"})
    assert launcher._run_daemon_in_process(str(script), payload()) == 0


def test_windows_has_no_landlock_status(monkeypatch):
    monkeypatch.setattr(process, "sys", SimpleNamespace(platform="win32"))
    assert process.ProcessRuntime().get_landlock_status("desktop") is None


@pytest.mark.asyncio
async def test_cancelled_startup_cleans_up_and_preserves_cancellation(monkeypatch):
    runtime = process.ProcessRuntime()
    runtime._runtime_policies["sb"] = SecurityPolicy()
    monkeypatch.setattr(process, "DAEMON_STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(runtime, "_verify_daemon_alive", Mock())
    monkeypatch.setattr(
        runtime, "_read_landlock_status", Mock(side_effect=asyncio.CancelledError())
    )
    stop, cleanup = AsyncMock(), Mock()
    monkeypatch.setattr(runtime, "stop", stop)
    monkeypatch.setattr(runtime, "_cleanup_sandbox_artifacts", cleanup)
    with pytest.raises(asyncio.CancelledError):
        await runtime._wait_daemon_ready("sb", Mock(), None)
    stop.assert_awaited_once()
    cleanup.assert_called_once()
    assert not runtime._daemon_socket_ready.get("sb")


def test_security_status_uses_authenticated_ipc_and_ignores_request_claims(monkeypatch):
    from jiuwenbox.supervisor import sandbox_daemon as daemon
    from jiuwenbox.supervisor.daemon_ipc import CONTROL_TOKEN_BYTES

    client, server = socket.socketpair()
    token = b"t" * CONTROL_TOKEN_BYTES
    status = {"mode": "best_effort", "state": "degraded", "abi": 0, "reason": "unsupported"}
    monkeypatch.setattr(daemon, "LANDLOCK_STATUS", status)

    class ConnectedSocket:
        def connect(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            client.close()

        def __getattr__(self, name):
            return getattr(client, name)

    monkeypatch.setattr(
        process,
        "socket",
        SimpleNamespace(
            socket=lambda *a: ConnectedSocket(),
            AF_UNIX=1,
            SOCK_STREAM=1,
        ),
    )
    runtime = process.ProcessRuntime()
    path = Path("control.sock")
    runtime._daemon_control_tokens[path] = token
    monkeypatch.setattr(runtime, "_control_socket_host_path", lambda _: path)
    thread = threading.Thread(
        target=daemon._handle_connection, args=(server, daemon.DaemonState(token))
    )
    thread.start()
    try:
        assert runtime._read_landlock_status("sb").model_dump() == status
    finally:
        client.close()
        thread.join(timeout=3)
        server.close()
    assert not thread.is_alive()


def test_status_handler_does_not_trust_client_claims(monkeypatch):
    from jiuwenbox.supervisor import sandbox_daemon as daemon

    actual = {"state": "degraded", "abi": 0, "reason": "unavailable"}
    monkeypatch.setattr(daemon, "LANDLOCK_STATUS", actual)
    send = Mock()
    monkeypatch.setattr(daemon, "_send_response", send)
    connection = Mock()
    daemon._handle_security_status(connection, {"landlock": {"state": "enforced", "abi": 9}})
    assert send.call_args.args[1]["landlock"] == actual


@pytest.mark.asyncio
async def test_manager_returns_and_persists_degradation_after_create_and_restart(
    monkeypatch, tmp_path
):
    from jiuwenbox.models.sandbox import SandboxSpec
    from jiuwenbox.server import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "sys", SimpleNamespace(platform="linux"))
    status = LandlockStatus(
        mode="best_effort", state="degraded", abi=0, reason="kernel unavailable"
    )
    runtime = SimpleNamespace(
        create=AsyncMock(return_value=123),
        get_landlock_status=lambda _: status,
        is_running=AsyncMock(return_value=False),
    )
    manager = SandboxManager.__new__(SandboxManager)
    manager.runtime = runtime
    manager.policy = SecurityPolicy(landlock={"compatibility": "best_effort"})
    manager.policy_engine = PolicyEngine(policies_dir=tmp_path)
    manager.audit = SimpleNamespace(log=Mock())
    manager._lock = asyncio.Lock()
    manager._sandboxes, manager._policies = {}, {}
    manager._save_state = Mock()
    ref = await manager.create_sandbox(SandboxSpec())
    assert ref.phase.value == "ready"
    assert ref.landlock_status == status and ref.security_warnings
    assert (await manager.get_sandbox(ref.id)).security_warnings
    ref = await manager.start_sandbox(ref.id)
    assert ref.landlock_status == status and ref.security_warnings
    assert manager._save_state.call_args.args[0].landlock_status == status
    runtime.create.side_effect = RuntimeError("Landlock initialization failed")
    failed = await manager.start_sandbox(ref.id)
    assert failed.phase.value == "error" and failed.pid is None
    assert failed.landlock_status is None and not failed.security_warnings
    assert "Landlock" in failed.error_message
