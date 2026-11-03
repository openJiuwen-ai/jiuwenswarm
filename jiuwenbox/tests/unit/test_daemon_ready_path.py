"""Daemon startup drops privileges in-process and joins the netns with setns."""

from __future__ import annotations

import io
import ipaddress
import marshal
import os

_TEST_SANDBOX_IP = str(ipaddress.IPv4Address(bytes((100, 64, 0, 2))))


def _call(owner: object, name: str, *args, **kwargs):
    """Call a helper the production code keeps unexported."""
    return getattr(owner, name)(*args, **kwargs)

from jiuwenbox import perf_trace
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.runtime import process as process_mod
from jiuwenbox.server.runtime.process import ProcessRuntime
from jiuwenbox.supervisor import landlock_launcher as launcher
from jiuwenbox.supervisor import network as network_mod
from jiuwenbox.supervisor import sandbox_daemon
from jiuwenbox.supervisor.daemon_ipc import SANDBOX_DAEMON_COMMAND
from jiuwenbox.supervisor.network import ResolvedNetworkRules


def _setenv(args: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    index = 0
    while index < len(args) - 2:
        if args[index] == "--setenv":
            found[args[index + 1]] = args[index + 2]
            index += 3
            continue
        index += 1
    return found


def _bwrap_args(runtime: ProcessRuntime, policy: SecurityPolicy) -> list[str]:
    _call(runtime, "_ensure_launcher_dir", "sb")
    return _call(
        runtime,
        "_build_sandbox_bwrap_args",
        "sb",
        policy,
        list(SANDBOX_DAEMON_COMMAND),
        workdir=None,
        sandbox_env=None,
        netns_attached=True,
        seccomp_fd=None,
        listener_fd=7,
        sandbox_ip=_TEST_SANDBOX_IP,
        daemon_code_fd=9,
    )


def test_privdrop_is_env_not_a_second_python(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        ProcessRuntime,
        "_sandbox_root",
        staticmethod(lambda: tmp_path),
    )
    perf_trace.configure(None)
    runtime = ProcessRuntime()
    policy = SecurityPolicy()
    policy.namespace.user = False

    args = _bwrap_args(runtime, policy)
    env = _setenv(args)
    assert args.count("python3") == 1
    assert "-c" not in args
    assert "netns" not in args
    assert env[process_mod.DROP_UID_ENV].isdigit()
    assert env[process_mod.DROP_GID_ENV].isdigit()
    assert env[process_mod.DROP_UID_ENV] != "0"
    assert env[process_mod.DAEMON_CODE_FD_ENV] == "9"
    assert process_mod.PERF_CHILD_ENV not in env

    policy.namespace.user = True
    kept = _setenv(_bwrap_args(runtime, policy))
    assert process_mod.DROP_UID_ENV not in kept
    assert process_mod.DROP_GID_ENV not in kept

    policy.process.run_as_user = "1000"
    policy.process.run_as_group = "1000"
    info_r, info_w = os.pipe()
    block_r, block_w = os.pipe()
    try:
        _call(runtime, "_ensure_launcher_dir", "sb-map")
        mapped = _call(
            runtime,
            "_build_sandbox_bwrap_args",
            "sb-map",
            policy,
            list(SANDBOX_DAEMON_COMMAND),
            workdir=None,
            sandbox_env=None,
            netns_attached=True,
            seccomp_fd=None,
            listener_fd=7,
            sandbox_ip=_TEST_SANDBOX_IP,
            daemon_code_fd=9,
            id_map_info_fd=info_w,
            id_map_block_fd=block_r,
        )
    finally:
        for fd in (info_r, info_w, block_r, block_w):
            os.close(fd)
    mapped_env = _setenv(mapped)
    assert "--unshare-user" in mapped
    assert "--uid" not in mapped
    assert "--userns-block-fd" in mapped
    assert mapped_env[process_mod.DROP_UID_ENV] == "1000"
    assert mapped_env[process_mod.DROP_GID_ENV] == "1000"


def test_perf_child_env_follows_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ProcessRuntime,
        "_sandbox_root",
        staticmethod(lambda: tmp_path),
    )
    runtime = ProcessRuntime()
    log_path = tmp_path / "create.perf"
    perf_trace.configure(str(log_path))
    try:
        env = _setenv(_bwrap_args(runtime, SecurityPolicy()))
        assert env[process_mod.PERF_CHILD_ENV] == "1"
    finally:
        perf_trace.configure(None)


def test_preexec_setns_opens_netns_file(tmp_path, monkeypatch):
    netns_path = tmp_path / "jbx-sb"
    netns_path.write_bytes(b"")
    seen: dict[str, object] = {}

    def fake_setns(fd: int, flag: int) -> int:
        seen["flag"] = flag
        seen["path"] = os.readlink(f"/proc/self/fd/{fd}")
        return 0

    monkeypatch.setattr(process_mod, "_netns_file", lambda _name: str(netns_path))
    monkeypatch.setattr(getattr(process_mod, "_LIBC"), "setns", fake_setns)
    hook = _call(process_mod, "_child_preexec", "jbx-sb", [], False)
    assert hook is not None
    hook()
    assert seen["flag"] == process_mod.CLONE_NEWNET
    assert seen["path"] == str(netns_path)
    assert _call(process_mod, "_child_preexec", None, [], False) is None


def test_daemon_bytecode_round_trip():
    fd = _call(process_mod, "_open_daemon_code_memfd")
    try:
        blob = b""
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            blob += chunk
    finally:
        os.close(fd)
    code = marshal.loads(blob)
    assert isinstance(code, type(compile("x=1", "<t>", "exec")))
    assert code.co_filename.endswith("sandbox-daemon.py")


def test_launcher_loads_memfd_and_skips_disk(tmp_path, monkeypatch):
    missing = tmp_path / "missing-daemon.py"
    source = "def main():\n    return 4\n"
    code = compile(source, "daemon.py", "exec")
    fd = os.memfd_create("test-daemon", os.MFD_CLOEXEC)
    os.write(fd, marshal.dumps(code))
    os.lseek(fd, 0, os.SEEK_SET)
    monkeypatch.setenv(launcher.DAEMON_CODE_FD_ENV, str(fd))
    loaded = _call(launcher, "_load_daemon_code", str(missing))
    assert loaded is not None
    assert loaded.co_consts
    assert launcher.DAEMON_CODE_FD_ENV not in os.environ
    assert not missing.exists()


def test_launcher_drop_privileges_uses_env(monkeypatch):
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("groups", tuple(groups))))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("gid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("uid", uid)))
    monkeypatch.delenv(launcher.DROP_UID_ENV, raising=False)
    monkeypatch.delenv(launcher.DROP_GID_ENV, raising=False)
    assert _call(launcher, "_drop_privileges") is True
    assert calls == []

    monkeypatch.setenv(launcher.DROP_UID_ENV, "1000")
    monkeypatch.setenv(launcher.DROP_GID_ENV, "1001")
    assert _call(launcher, "_drop_privileges") is True
    assert calls == [("groups", ()), ("gid", 1001), ("uid", 1000)]
    assert launcher.DROP_UID_ENV not in os.environ
    assert launcher.DROP_GID_ENV not in os.environ


def test_launcher_perf_marks_only_when_requested(tmp_path, monkeypatch, capsys):
    script = tmp_path / "daemon.py"
    script.write_text("def main():\n    return 0\n", encoding="utf-8")
    payload = {"compatibility": "disabled", "read_only": [], "read_write": []}
    monkeypatch.delenv(launcher.PERF_CHILD_ENV, raising=False)
    assert _call(launcher, "_run_daemon_in_process", str(script), payload) == 0
    assert "JIUWENBOX_PERF" not in capsys.readouterr().err

    monkeypatch.setenv(launcher.PERF_CHILD_ENV, "1")
    assert _call(launcher, "_run_daemon_in_process", str(script), payload) == 0
    err = capsys.readouterr().err
    for step in (
        "daemon.privdrop",
        "daemon.load",
        "daemon.landlock",
        "daemon.enter_main",
    ):
        assert f"JIUWENBOX_PERF\t{step}\t" in err


def test_child_perf_lines_are_appended_only_when_enabled(tmp_path):
    log_path = tmp_path / "create.perf"
    startup = io.BytesIO(
        b"JIUWENBOX_PERF\tdaemon.load\t12.500\n"
        b"JIUWENBOX_DAEMON_READY\n"
    )
    perf_trace.configure(None)
    _call(process_mod, "_record_child_perf", "sb", startup)
    assert not log_path.exists()

    perf_trace.configure(str(log_path))
    try:
        startup.seek(0)
        _call(process_mod, "_record_child_perf", "sb", startup)
    finally:
        perf_trace.configure(None)
    body = [
        line for line in log_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert len(body) == 1
    _start, elapsed, sandbox_id, step = body[0].split("\t")
    assert elapsed == "12.500"
    assert sandbox_id == "sb"
    assert step == "daemon.load"


def test_command_modules_load_after_ready_marker():
    assert sandbox_daemon.shutil is None
    assert sandbox_daemon.subprocess is None
    text = open(sandbox_daemon.__file__, encoding="utf-8").read()
    ready_at = text.index("JIUWENBOX_DAEMON_READY")
    load_at = text.index("_load_deferred_modules()", ready_at)
    accept_at = text.index("_accept_loop(", load_at)
    assert ready_at < load_at < accept_at
    _call(sandbox_daemon, "_load_deferred_modules")
    assert sandbox_daemon.subprocess.Popen.__name__ == "Popen"


def test_default_allow_skips_ipv6_restore(monkeypatch):
    calls: list[int] = []

    def record(*_args, ip_version: int, **_kwargs) -> None:
        calls.append(ip_version)

    monkeypatch.setattr(network_mod, "_can_iptables_restore", lambda _version: True)
    monkeypatch.setattr(network_mod, "_ip6tables_available", lambda _namespace: True)
    monkeypatch.setattr(network_mod, "_run_iptables_restore", record)
    allow = ResolvedNetworkRules(default_deny=False)
    network_mod.apply_iptables_rules(allow, allow, namespace="ns", bring_up_loopback=False)
    assert calls == [4]

    calls.clear()
    deny = ResolvedNetworkRules(default_deny=True)
    network_mod.apply_iptables_rules(deny, allow, namespace="ns", bring_up_loopback=False)
    assert calls == [4, 6]

    calls.clear()
    blocked = ResolvedNetworkRules(default_deny=False, blocked_ports=[22])
    network_mod.apply_iptables_rules(allow, blocked, namespace="ns", bring_up_loopback=False)
    assert calls == [4, 6]
