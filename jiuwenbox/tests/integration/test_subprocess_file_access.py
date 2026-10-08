# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native file permission checks on temporary fixtures."""

import sys

import pytest

from jiuwenbox.supervisor import win_acl, win_constants as const


@pytest.mark.skipif(sys.platform != "win32", reason="uses real NTFS ACLs on temporary files")
def test_desktop_grants_keep_yaml_denies_and_old_explicit_allows(tmp_path, monkeypatch):
    import win32security

    # A synthetic SID has no host user membership; this test cannot deny the
    # user's own account access to its temporary files.
    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    root = tmp_path / "desktop"
    denied = root / "agent" / "skills" / "private"
    allowed = denied / "public"
    logs = root / "logs"
    for directory in (denied, allowed, logs):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "file.txt").write_text("test", encoding="utf-8")
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: sid)
    monkeypatch.setattr(win_acl, "grant_current_user_write_dac", lambda *a, **k: None)
    # Reproduce existing installations with explicit grants below a later deny.
    win_acl.apply_desktop_data_rw(str(root), preserve_write_roots=[str(denied)])
    win_acl.grant_ace(str(denied / "file.txt"), sid, rights=const.ALLOW_WRITE_RIGHTS, mode="ALLOW")
    policy = {
        "allow_read": [str(root), str(allowed)],
        "allow_write": [str(root)],
        "deny_read": [str(denied)],
        "deny_write": [str(logs)],
    }
    win_acl.apply_desktop_data_rw(
        str(root), preserve_write_roots=[str(root / "agent")], filesystem_policy=policy,
    )

    def masks(path):
        acl = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION,
        ).GetSecurityDescriptorDacl()
        deny = allow = 0
        for i in range(acl.GetAceCount()):
            (kind, _), mask, trustee = acl.GetAce(i)
            if win32security.ConvertSidToStringSid(trustee) == sid:
                if kind == const.ACCESS_DENIED_ACE_TYPE:
                    deny |= mask
                elif kind == const.ACCESS_ALLOWED_ACE_TYPE:
                    allow |= mask
        return deny, allow

    for path in (denied, denied / "file.txt"):
        deny, allow = masks(path)
        assert deny & const.DENY_READ_RIGHTS == const.DENY_READ_RIGHTS
        assert not allow & const.DENY_READ_RIGHTS
    deny, allow = masks(allowed / "file.txt")
    assert not deny & const.DENY_READ_RIGHTS
    assert allow & const.FILE_GENERIC_READ == const.FILE_GENERIC_READ
    deny, allow = masks(logs / "file.txt")
    assert deny & const.DENY_WRITE_RIGHTS == const.DENY_WRITE_RIGHTS
    assert not allow & const.DENY_WRITE_RIGHTS
    assert allow & const.FILE_GENERIC_READ == const.FILE_GENERIC_READ
    # Ancestor traversal must not remove either the deny or the explicit exception.
    monkeypatch.setattr(win_acl, "_is_host_dacl_frozen", lambda p: p == tmp_path)
    before = masks(denied), masks(allowed)
    win_acl.grant_parent_traverse(str(allowed), sid, filesystem_policy=policy)
    assert (masks(denied), masks(allowed)) == before


@pytest.fixture(params=["ro-root", "rw-root-ro-child", "rw-root-remount-child"])
def policy_workspace(request, tmp_path):
    if request.param == "ro-root":
        yield tmp_path, request.param
    else:
        from pathlib import Path
        import tempfile

        # A private path under the real home exercises opening mount sources
        # after entering userns; /tmp alone does not cover this regression.
        with tempfile.TemporaryDirectory(prefix=".jbx-test-", dir=Path.home()) as directory:
            root = Path(directory) / ".jiuwenswarm"
            root.mkdir(mode=0o700)
            yield root, request.param


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux mount namespaces and bwrap")
def test_readonly_child_is_inherited_by_subprocesses(policy_workspace):
    import shutil
    import subprocess

    from jiuwenbox.supervisor.bwrap import BwrapConfig

    tmp_path, layout = policy_workspace
    binary = shutil.which("bwrap")
    if binary is None:
        pytest.skip("bwrap is not installed")
    # Only a prerequisite probe may skip. A failure of the actual policy test fails.
    probe = subprocess.run(
        [binary, "--ro-bind", "/", "/", "--", sys.executable, "-c", "pass"],
        capture_output=True, text=True, timeout=10,
    )
    if probe.returncode:
        pytest.skip(f"mount namespaces unavailable: {probe.stderr.strip()}")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secret = private / "secret.txt"
    secret.write_text("SECRET")
    script = tmp_path / "access.py"
    script.write_text(
        "import errno, pathlib, subprocess, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "depth = int(sys.argv[1])\n"
        "if depth:\n"
        "    assert (root / 'private/secret.txt').read_text() == 'SECRET'\n"
        "    (root / f'allowed-{depth}').write_text('ok')\n"
        "    try: (root / 'private/secret.txt').write_text('escaped')\n"
        "    except OSError as exc: assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM)\n"
        "    else: raise AssertionError('readonly child became writable')\n"
        "if depth < 2:\n"
        "    subprocess.run([sys.executable, __file__, str(depth + 1)], check=True)\n",
        encoding="utf-8",
    )
    cfg = BwrapConfig(
        ro_binds=[(str(private), str(private))],
        rw_binds=[(str(tmp_path), str(tmp_path))],
        command=[sys.executable, str(script), "0"],
    )
    if layout == "ro-root":
        cfg.ro_binds.insert(0, ("/", "/"))
    else:
        cfg.rw_binds.insert(0, ("/", "/"))
        if layout == "rw-root-remount-child":
            cfg.ro_binds.clear()
            cfg.rw_binds.append((str(private), str(private)))
            cfg.remount_ro.append(str(private))
    result = subprocess.run(cfg.to_args(), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert secret.read_text() == "SECRET"
    assert [(tmp_path / f"allowed-{depth}").read_text() for depth in (1, 2)] == ["ok", "ok"]
