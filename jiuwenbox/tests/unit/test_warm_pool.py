"""Warm pool matching, numeric run-as ids, and the launcher park protocol."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import selectors
import subprocess
import sys
from pathlib import Path

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.runtime.process import ProcessRuntime
from jiuwenbox.server.runtime.warm_pool import (
    WarmPool,
    boot_policy,
    warm_pool_compatible,
    warm_pool_mismatches,
    warm_pool_size,
)
from jiuwenbox.supervisor.bwrap import BwrapConfig
from jiuwenbox.supervisor.landlock_launcher import WARM_COMMIT_FD_ENV, WARM_PARKED_MARKER
from jiuwenbox.supervisor.posix_id import resolve_posix_id


def test_numeric_run_as_is_the_id_itself():
    assert resolve_posix_id("1000", kind="user") == 1000
    assert resolve_posix_id("1000", kind="group") == 1000


def test_bwrap_uses_numeric_run_as():
    policy = SecurityPolicy.model_validate({
        "process": {"run_as_user": "1000", "run_as_group": "1000"},
    })
    config = BwrapConfig.from_policy(policy, ["true"])
    assert config.uid == 1000
    assert config.gid == 1000


def test_boot_policy_keeps_the_loaded_filesystem_and_stays_root():
    template = SecurityPolicy.model_validate({
        "filesystem_policy": {
            "bind_mounts": [
                {"host_path": "/opt", "sandbox_path": "/opt", "mode": "ro"},
            ],
        },
        "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
    })

    booted = boot_policy(template)

    assert [
        (mount.host_path, mount.sandbox_path, mount.mode)
        for mount in booted.filesystem_policy.bind_mounts
    ] == [("/opt", "/opt", "ro")]
    assert booted.process.run_as_user == "0"
    assert booted.process.run_as_group == "0"
    assert template.process.run_as_user == "sandbox"


def test_warm_pool_allows_filesystem_and_run_as_to_differ():
    template = SecurityPolicy()
    request = template.model_copy(deep=True)
    request.process.run_as_user = "app"
    request.process.run_as_group = "app"
    request.filesystem_policy.read_write.append("/app/build")
    assert warm_pool_compatible(template, request)


def test_warm_pool_mismatches_name_each_incompatible_field():
    template = SecurityPolicy()
    request = template.model_copy(deep=True)
    request.network.mode = "host"
    request.network.egress.allowed_domains = ["example.com"]
    request.cgroup.memory_max = 1024
    request.namespace.pid = False
    request.syscall.x86_64.blocked = ["clone"]
    request.landlock.compatibility = "disabled"

    reasons = warm_pool_mismatches(template, request)

    assert reasons == [
        "landlock.compatibility request='disabled' template='best_effort' "
        "(warm pool cannot serve a sandbox with Landlock disabled)",
        "network.mode request='host' template='isolated'",
        "network.egress.allowed_domains request=['example.com'] template=[]",
        "cgroup.memory_max request=1024 template=None",
        "namespace.pid request=False template=True",
        "syscall.x86_64.blocked request=['clone'] template=[]",
    ]
    assert not warm_pool_compatible(template, request)


def test_warm_pool_mismatches_landlock_mode_without_disabling_it():
    template = SecurityPolicy()
    request = template.model_copy(deep=True)
    request.landlock.compatibility = "hard_requirement"

    assert warm_pool_mismatches(template, request) == [
        "landlock.compatibility request='hard_requirement' template='best_effort'",
    ]


def test_warm_pool_rejects_network_or_disabled_landlock():
    template = SecurityPolicy()
    assert warm_pool_compatible(template, template)

    other_network = template.model_copy(deep=True)
    other_network.network.mode = "host"
    assert not warm_pool_compatible(template, other_network)

    disabled = template.model_copy(deep=True)
    disabled.landlock.compatibility = "disabled"
    assert not warm_pool_compatible(template, disabled)


class _PoolRuntime:
    def __init__(self) -> None:
        self.destroyed: list[str] = []

    def destroy_warm_slot(self, slot_id: str) -> None:
        self.destroyed.append(slot_id)


def test_replace_template_keeps_slots_when_the_new_policy_still_matches():
    runtime = _PoolRuntime()
    template = SecurityPolicy()
    pool = WarmPool(runtime, template, 1)
    pool.stage_slot("w-keep")
    updated = template.model_copy(deep=True)
    updated.filesystem_policy.read_write.append("/tmp")

    pool.replace_template(updated)

    assert runtime.destroyed == []
    assert pool.acquire(updated) == (True, "w-keep")


def test_replace_template_discards_slots_that_cannot_serve_the_new_policy():
    runtime = _PoolRuntime()
    template = SecurityPolicy()
    pool = WarmPool(runtime, template, 1)
    pool.stage_slot("w-old")
    updated = template.model_copy(deep=True)
    updated.network.mode = "host"

    pool.replace_template(updated)

    assert runtime.destroyed == ["w-old"]
    assert pool.acquire(template) == (False, None)
    assert pool.acquire(updated) == (True, None)


def test_create_logs_why_the_warm_pool_was_not_used(caplog, monkeypatch):
    runtime = ProcessRuntime()
    policy = SecurityPolicy()
    with caplog.at_level(logging.INFO):
        adopted = asyncio.run(runtime.claim_warm_sandbox("sb-off", policy, Path("policy.yaml"), None))
    assert adopted is None
    assert "Warm pool not used for sb-off: pool was not started" in caplog.text

    caplog.clear()
    monkeypatch.setenv("JIUWENBOX_WARM_POOL", "0")
    with caplog.at_level(logging.INFO):
        runtime.start_warm_pool(policy)
        adopted = asyncio.run(
            runtime.claim_warm_sandbox("sb-disabled", policy, Path("policy.yaml"), None),
        )
    assert adopted is None
    assert "Warm pool disabled (JIUWENBOX_WARM_POOL='0')" in caplog.text
    assert "Warm pool not used for sb-disabled: disabled (JIUWENBOX_WARM_POOL='0')" in caplog.text

    class _Pool:
        depth = 4

        @staticmethod
        def acquire(request):
            del request
            return False, None

        @staticmethod
        def mismatch_reasons(request):
            return warm_pool_mismatches(policy, request)

    request = policy.model_copy(deep=True)
    request.network.mode = "host"
    runtime.attach_warm_pool(_Pool())
    caplog.clear()
    with caplog.at_level(logging.INFO):
        adopted = asyncio.run(
            runtime.claim_warm_sandbox("sb-policy", request, Path("policy.yaml"), None),
        )
    assert adopted is None
    assert (
        "Warm pool not used for sb-policy: policy incompatible: "
        "network.mode request='host' template='isolated'"
    ) in caplog.text

    class _EmptyPool(_Pool):
        @staticmethod
        def acquire(request):
            del request
            return True, None

    runtime.attach_warm_pool(_EmptyPool())
    caplog.clear()
    with caplog.at_level(logging.INFO):
        adopted = asyncio.run(
            runtime.claim_warm_sandbox("sb-empty", policy, Path("policy.yaml"), None),
        )
    assert adopted is None
    assert "Warm pool not used for sb-empty: no parked sandbox available (depth=4)" in caplog.text


def test_warm_pool_size_defaults_to_eight(monkeypatch):
    monkeypatch.delenv("JIUWENBOX_WARM_POOL", raising=False)
    assert warm_pool_size() == 8
    monkeypatch.setenv("JIUWENBOX_WARM_POOL", "0")
    assert warm_pool_size() == 0


def test_launcher_parks_until_commit(tmp_path):
    daemon = tmp_path / "daemon.py"
    daemon.write_text(
        "def main():\n"
        "    import sys\n"
        "    sys.stdout.write('READY\\n')\n"
        "    sys.stdout.flush()\n"
        "    return 0\n",
        encoding="utf-8",
    )
    payload = {
        "compatibility": "disabled",
        "read_only": [],
        "read_write": [],
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    env = os.environ.copy()
    env[WARM_COMMIT_FD_ENV] = str(read_fd)
    launcher = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "src",
        "jiuwenbox",
        "supervisor",
        "landlock_launcher.py",
    )
    proc = subprocess.Popen(
        [sys.executable, launcher, encoded, "--daemon", str(daemon)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    assert proc.stderr is not None
    watcher = selectors.DefaultSelector()
    watcher.register(proc.stderr, selectors.EVENT_READ)
    captured = b""
    while WARM_PARKED_MARKER.encode() not in captured:
        if not watcher.select(timeout=5):
            proc.kill()
            raise AssertionError(captured)
        captured += os.read(proc.stderr.fileno(), 4096)
    commit = {"landlock_b64": encoded, "env": {"JIUWENBOX_WARM_TEST": "1"}}
    os.write(write_fd, json.dumps(commit).encode())
    os.close(write_fd)
    assert proc.wait(timeout=5) == 0
    assert proc.stdout is not None
    assert b"READY" in proc.stdout.read()
