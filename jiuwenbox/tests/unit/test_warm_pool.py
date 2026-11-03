"""Warm pool matching, numeric run-as ids, and the launcher park protocol."""

from __future__ import annotations

import base64
import json
import os
import selectors
import subprocess
import sys

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.runtime.warm_pool import (
    WarmPool,
    warm_pool_compatible,
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


def test_warm_pool_allows_filesystem_and_run_as_to_differ():
    template = SecurityPolicy()
    request = template.model_copy(deep=True)
    request.process.run_as_user = "app"
    request.process.run_as_group = "app"
    request.filesystem_policy.read_write.append("/app/build")
    assert warm_pool_compatible(template, request)


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


def test_warm_pool_size_defaults_to_four(monkeypatch):
    monkeypatch.delenv("JIUWENBOX_WARM_POOL", raising=False)
    assert warm_pool_size() == 4
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
