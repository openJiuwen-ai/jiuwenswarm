# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""File-policy precedence and Linux mount ordering regressions."""

import pytest
from unittest.mock import MagicMock

from jiuwenbox.supervisor import win_acl, win_constants as const
from jiuwenbox.supervisor.bwrap import BwrapConfig


@pytest.mark.parametrize("axis,mask", [
    ("read", const.DENY_READ_RIGHTS), ("write", const.DENY_WRITE_RIGHTS),
])
def test_yaml_deny_boundaries_and_explicit_exceptions(tmp_path, axis, mask):
    denied = tmp_path / "private"
    policy = {
        f"deny_{axis}": [str(denied)],
        f"allow_{axis}": [str(tmp_path), str(denied / "public")],
    }
    assert win_acl._policy_deny_rights(denied / "file", policy) == mask
    assert win_acl._policy_deny_rights(tmp_path / "private-other", policy) == 0
    assert win_acl._policy_deny_rights(denied / "public" / "file", policy) == 0
    policy[f"deny_{axis}"].append(str(denied / "public"))
    assert win_acl._policy_deny_rights(denied / "public" / "file", policy) == mask


def test_write_allow_keeps_existing_read_implication(tmp_path):
    policy = {"deny_read": [str(tmp_path)], "allow_write": [str(tmp_path / "exception")]}
    assert win_acl._policy_deny_rights(tmp_path / "exception" / "file", policy) == 0


@pytest.mark.parametrize("root_mode", [None, "rw", "ro"])
def test_readonly_child_survives_writable_parent(root_mode):
    cfg = BwrapConfig(
        ro_binds=[("/data/private", "/data/private")],
        rw_binds=[("/data", "/data")],
    )
    if root_mode == "rw":
        cfg.rw_binds.append(("/", "/"))
    elif root_mode == "ro":
        cfg.ro_binds.append(("/", "/"))
    args = cfg.to_args()
    mounts = [tuple(args[i:i + 3]) for i, arg in enumerate(args)
              if arg in {"--bind", "--ro-bind"}]
    child = ("--ro-bind", "/data/private", "/data/private")
    parent = ("--bind", "/", "/") if root_mode == "rw" else ("--bind", "/data", "/data")
    assert child in mounts
    assert mounts.index(parent) < mounts.index(child)
    assert cfg.to_args() == args, "serializing policy must not consume restrictions"


def test_deny_remount_keeps_its_mountpoint_under_writable_root():
    cfg = BwrapConfig(
        rw_binds=[("/", "/"), ("/data/locked", "/data/locked")],
        remount_ro=["/data/locked"],
    )
    args = cfg.to_args()
    bind = next(i for i in range(len(args) - 2)
                if args[i:i + 3] == ["--bind", "/data/locked", "/data/locked"])
    remount = args.index("--remount-ro")
    assert args[remount + 1] == "/data/locked"
    assert bind < remount


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["read_only", "ro_bind"])
async def test_readonly_policy_reaches_acl_without_denying_read(tmp_path, monkeypatch, restriction):
    import jiuwenbox.supervisor as supervisor
    from jiuwenbox.models.policy import SecurityPolicy
    from jiuwenbox.server.runtime.process import ProcessRuntime

    root = str(tmp_path)
    denied = str(tmp_path / "deny")
    fs = {
        "read_write": [root],
        "bind_mounts": [
            {"host_path": root, "sandbox_path": root, "mode": "rw"},
            {"host_path": denied, "sandbox_path": denied,
             "mode": "rw" if restriction == "read_only" else "ro"},
        ],
        "read_only": [denied] if restriction == "read_only" else [],
    }
    policy = SecurityPolicy.model_validate({"version": 1, "filesystem_policy": fs})

    class AclReached(Exception):
        """Stop before launching a real runner or touching host ACLs."""

    acl = MagicMock()
    acl.resolve_desktop_data_dir.return_value = None
    acl.apply_sandbox_acl.side_effect = AclReached
    monkeypatch.setattr(supervisor, "win_acl", acl)
    monkeypatch.setattr(supervisor, "win_setup", MagicMock(), raising=False)
    runtime = ProcessRuntime()
    monkeypatch.setattr(runtime, "_load_policy", lambda _: policy)
    monkeypatch.setattr(runtime, "_win_workspace_for", lambda *a: root)
    with pytest.raises(AclReached):
        await runtime._create_windows("probe", tmp_path / "policy.yaml", {})
    args, kwargs = acl.apply_sandbox_acl.call_args
    assert root in args[1]  # writable parent
    assert denied in args[2]  # explicit write denial survives the parent grant
    assert denied in kwargs["allow_read"]
    assert denied not in kwargs["deny_read"]
