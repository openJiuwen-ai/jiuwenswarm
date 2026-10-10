# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Workspace ACL regression tests without changing host permissions."""

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from jiuwenbox.supervisor import win_acl, win_constants as const


class Acl:
    def __init__(self):
        self.aces = []

    def GetAceCount(self):
        return len(self.aces)

    def GetAce(self, index):
        return self.aces[index]

    def AddAccessAllowedAceEx(self, revision, flags, mask, sid):
        self.aces.append(((const.ACCESS_ALLOWED_ACE_TYPE, flags), mask, sid))

    def AddAccessDeniedAceEx(self, revision, flags, mask, sid):
        self.aces.append(((const.ACCESS_DENIED_ACE_TYPE, flags), mask, sid))


class Descriptor:
    def __init__(self):
        self.acl = Acl()
        self.acl.AddAccessAllowedAceEx(2, 0, 0x1F01FF, "host")
        self.control = const.SE_DACL_PROTECTED

    def GetSecurityDescriptorDacl(self):
        return self.acl

    def SetSecurityDescriptorDacl(self, present, acl, defaulted):
        self.acl = acl


@pytest.fixture
def native(monkeypatch, tmp_path):
    descriptors, writes = {}, []

    def get(path, flags):
        return descriptors.setdefault(str(path), Descriptor())

    def set_file(path, flags, sd):
        descriptors[str(path)] = sd
        writes.append(str(path))

    def forbidden(*args):
        pytest.fail("bounded workspace ACL must not use implicit tree propagation")

    api = SimpleNamespace(
        ACL=Acl, GetFileSecurity=get, SetFileSecurity=set_file,
        GetNamedSecurityInfo=forbidden, SetNamedSecurityInfo=forbidden,
        DACL_SECURITY_INFORMATION=4,
    )
    monkeypatch.setattr(win_acl, "_require_windows", lambda: None)
    monkeypatch.setattr(win_acl, "_ensure_pywin32", lambda: (api, None, None))
    monkeypatch.setattr(win_acl, "_resolve_sid", lambda sid: sid)
    monkeypatch.setattr(win_acl, "_sid_dedup_key", lambda sid: sid)
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: "synthetic")
    for name in ("OFFICE_CLAW_DATA_ROOT", "JIUWENCLAW_DATA_DIR_PATH", "JIUWENBOX_HOME"):
        monkeypatch.setattr(win_acl, name, tmp_path / "absent")
    return descriptors, writes


def _rights(sd, sid, kind):
    mask = 0
    for (ace_kind, flags), rights, ace_sid in sd.acl.aces:
        if ace_sid == sid and ace_kind == kind:
            mask |= rights
    return mask


def test_existing_files_denials_and_revoke_without_tree_propagation(tmp_path, native):
    root = tmp_path / "workspace"
    skill = root / "skills" / "sample" / "SKILL.md"
    module = root / "skills" / "sample" / "node_modules" / "package" / "index.js"
    readonly = root / "readonly" / "config.yaml"
    secret = root / "secret" / "private.txt"
    exception = root / "secret" / "public" / "ok.txt"
    for path in (skill, module, readonly, secret, exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing", encoding="utf-8")
    applied = win_acl.apply_sandbox_acl(
        str(root), [str(root)], [str(readonly.parent)],
        allow_read=[str(root), str(exception.parent)], deny_read=[str(secret.parent)],
        sandbox_user_sid="sandbox", bounded_roots=[str(root)],
    )
    descriptors, writes = native
    assert applied == [str(root)]
    assert {str(skill), str(module), str(readonly), str(secret), str(exception)} <= set(writes)
    for sid in ("synthetic", "sandbox"):
        for path in (skill, module, exception):
            assert _rights(descriptors[str(path)], sid, const.ACCESS_ALLOWED_ACE_TYPE) & const.FILE_READ_DATA
        assert _rights(descriptors[str(readonly)], sid, const.ACCESS_DENIED_ACE_TYPE) & const.FILE_WRITE_DATA
        assert not _rights(descriptors[str(readonly)], sid, const.ACCESS_ALLOWED_ACE_TYPE) & const.FILE_WRITE_DATA
        assert _rights(descriptors[str(secret)], sid, const.ACCESS_DENIED_ACE_TYPE) & const.FILE_READ_DATA
        assert not _rights(descriptors[str(secret)], sid, const.ACCESS_ALLOWED_ACE_TYPE) & const.FILE_READ_DATA
    for sd in descriptors.values():
        assert sd.control == const.SE_DACL_PROTECTED
        assert _rights(sd, "host", const.ACCESS_ALLOWED_ACE_TYPE) == 0x1F01FF
    win_acl.revoke_sandbox_acl(applied, sandbox_user_sid="sandbox")
    for sd in descriptors.values():
        assert all(ace[2] == "host" for ace in sd.acl.aces)


def _fail_once(monkeypatch, target):
    original = win_acl._write_file_dacl
    failed = False

    def fail_child(path, acl, **kwargs):
        nonlocal failed
        if path == str(target) and not failed:
            failed = True
            raise PermissionError("protected file")
        return original(path, acl, **kwargs)

    monkeypatch.setattr(win_acl, "_write_file_dacl", fail_child)


def test_bounded_deny_failure_cleans_partial_permissions(tmp_path, native, monkeypatch):
    root = tmp_path / "workspace"
    secret = root / "secret" / "file"
    secret.parent.mkdir(parents=True)
    secret.write_text("existing", encoding="utf-8")
    _fail_once(monkeypatch, secret)
    with pytest.raises(PermissionError):
        win_acl.apply_sandbox_acl(
            str(root), [str(root)], [], deny_read=[str(secret.parent)], bounded_roots=[str(root)],
        )
    assert all(ace[2] == "host" for sd in native[0].values() for ace in sd.acl.aces)


def test_unlistable_child_does_not_abort_bounded_acl(tmp_path, native, monkeypatch, caplog):
    root = tmp_path / "workspace"
    hidden = root / "sandbox-owned"
    visible = root / "visible.txt"
    hidden.mkdir(parents=True)
    (hidden / "secret.txt").write_text("hidden", encoding="utf-8")
    visible.write_text("ok", encoding="utf-8")
    real_scandir = win_acl.os.scandir

    def scandir(path):
        if win_acl.os.path.normcase(str(path)) == win_acl.os.path.normcase(str(hidden)):
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(win_acl.os, "scandir", scandir)
    with caplog.at_level("WARNING"):
        applied = win_acl.apply_sandbox_acl(str(root), [str(root)], [], bounded_roots=[str(root)])
    assert applied == [str(root)]
    assert str(visible) in native[1]
    assert str(hidden / "secret.txt") not in native[1]
    assert any("skip unlistable" in record.message and str(hidden) in record.message for record in caplog.records)


def test_bounded_allow_failure_degrades_without_failing_create(tmp_path, native, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "protected").write_text("existing", encoding="utf-8")
    (root / "other").write_text("existing", encoding="utf-8")
    _fail_once(monkeypatch, root / "protected")
    applied = win_acl.apply_sandbox_acl(str(root), [str(root)], [], bounded_roots=[str(root)])
    assert applied == [str(root)]
    assert _rights(native[0][str(root / "other")], "synthetic", const.ACCESS_ALLOWED_ACE_TYPE)


def test_reapply_skips_unchanged_objects(tmp_path, native):
    root = tmp_path / "workspace"
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("existing", encoding="utf-8")
    descriptors, writes = native
    args = (str(root), [str(root)], [])
    win_acl.apply_sandbox_acl(*args, sandbox_user_sid="sandbox", bounded_roots=[str(root)])
    first = len(writes)
    assert first
    assert win_acl.apply_sandbox_acl(
        *args, sandbox_user_sid="sandbox", bounded_roots=[str(root)],
    ) == [str(root)]
    assert len(writes) == first


def test_office_claw_cleanup_keeps_profile_deny_without_propagation(tmp_path, native, monkeypatch):
    office = tmp_path / ".office-claw"
    office.mkdir()
    descriptors, _ = native
    sd = descriptors.setdefault(str(office), Descriptor())
    sd.acl.AddAccessDeniedAceEx(2, const.RECURSIVE_ACE_FLAGS, const.FILE_GENERIC_READ, "sandbox")
    api = win_acl._ensure_pywin32()[0]
    monkeypatch.setattr(api, "GetNamedSecurityInfo", lambda path, *args: descriptors[str(path)])
    monkeypatch.setattr(win_acl, "OFFICE_CLAW_DATA_ROOT", office)
    root = tmp_path / "workspace"
    root.mkdir()
    win_acl.apply_sandbox_acl(
        str(root), [str(root)], [], sandbox_user_sid="sandbox", bounded_roots=[str(root)],
    )
    assert _rights(descriptors[str(office)], "sandbox", const.ACCESS_DENIED_ACE_TYPE)


def test_stream_logging_does_not_block_on_stalled_pipe(monkeypatch):
    import logging
    from jiuwenbox import logging_config

    stalled, release = threading.Event(), threading.Event()

    class StalledStream:
        def write(self, _text):
            stalled.set()
            release.wait(5)

        def flush(self):
            pass

    for name in ("", "uvicorn", "uvicorn.error"):
        monkeypatch.setattr(logging.getLogger(name), "handlers", [])
    logger = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler(StalledStream())
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logging_config, "_STREAM_QUEUE_MAX_RECORDS", 2)
    monkeypatch.setattr(logging_config, "_stream_queue", None)
    try:
        logging_config.make_stream_logging_nonblocking()
        assert isinstance(logger.handlers[0], logging_config._NonBlockingStreamHandler)
        logger.warning("first")
        assert stalled.wait(1)
        for _ in range(100):
            logger.warning("must not block the caller")
    finally:
        release.set()


def test_reparse_point_is_not_followed(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    child = root / "junction"
    child.mkdir()
    (child / "outside").write_text("must not touch", encoding="utf-8")
    import os
    original = os.stat

    def stat(path, **kwargs):
        if str(path) == str(child):
            return SimpleNamespace(st_file_attributes=0x400)
        return original(path, **kwargs)

    monkeypatch.setattr(win_acl.os, "stat", stat)
    assert list(win_acl._walk_acl_objects(str(root))) == [str(root)]


@pytest.mark.asyncio
async def test_acl_worker_keeps_loop_responsive_and_serializes_mutations():
    from jiuwenbox.server.runtime.process import ProcessRuntime

    runtime = ProcessRuntime()
    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    def first():
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(2)

    task = asyncio.create_task(runtime._run_windows_acl(first))
    other = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        other = asyncio.create_task(runtime._run_windows_acl(second_entered.set))
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not second_entered.is_set(), "cancelled native calls must retain the mutation lock"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await other
    assert second_entered.is_set()


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows ACL APIs")
def test_native_acl_preserves_protection_and_covers_old_and_new_files(tmp_path, monkeypatch):
    security = pytest.importorskip("win32security")
    synthetic = "S-1-5-21-1001-1002-1003-2001"
    sandbox = "S-1-5-21-1001-1002-1003-2002"
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: synthetic)
    for name in ("OFFICE_CLAW_DATA_ROOT", "JIUWENCLAW_DATA_DIR_PATH", "JIUWENBOX_HOME"):
        monkeypatch.setattr(win_acl, name, tmp_path / "absent")
    root = tmp_path / "workspace"
    skill = root / "skills" / "sample" / "node_modules" / "package" / "index.js"
    readonly = root / "readonly" / "config.yaml"
    secret = root / "secret" / "private.txt"
    public = root / "secret" / "public" / "ok.txt"
    for path in (skill, readonly, secret, public):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing", encoding="utf-8")
    sd = security.GetFileSecurity(str(skill), security.DACL_SECURITY_INFORMATION)
    sd.SetSecurityDescriptorControl(security.SE_DACL_PROTECTED, security.SE_DACL_PROTECTED)
    security.SetFileSecurity(str(skill), security.DACL_SECURITY_INFORMATION, sd)

    def masks(path, sid):
        sd = security.GetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION)
        allowed, denied = 0, 0
        acl = sd.GetSecurityDescriptorDacl()
        for index in range(acl.GetAceCount()):
            (kind, flags), rights, ace_sid = acl.GetAce(index)
            if security.ConvertSidToStringSid(ace_sid) == sid:
                if kind == const.ACCESS_DENIED_ACE_TYPE:
                    denied |= rights
                else:
                    allowed |= rights
        return sd, allowed, denied

    applied = win_acl.apply_sandbox_acl(
        str(root), [str(root)], [str(readonly.parent)],
        allow_read=[str(root), str(public.parent)], deny_read=[str(secret.parent)],
        sandbox_user_sid=sandbox, bounded_roots=[str(root)],
    )
    try:
        future = readonly.parent / "new.yaml"
        future.write_text("new", encoding="utf-8")
        for sid in (synthetic, sandbox):
            sd, allowed, denied = masks(skill, sid)
            assert allowed & const.FILE_READ_DATA
            assert sd.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED
            for path in (readonly, future):
                sd, allowed, denied = masks(path, sid)
                assert allowed & const.FILE_READ_DATA
                assert denied & const.FILE_WRITE_DATA
            assert masks(secret, sid)[2] & const.FILE_READ_DATA
            assert not masks(public, sid)[2] & const.FILE_READ_DATA
            assert masks(public, sid)[1] & const.FILE_READ_DATA
    finally:
        win_acl.revoke_sandbox_acl(applied, sandbox_user_sid=sandbox)
    for path in (skill, readonly, future, secret, public):
        for sid in (synthetic, sandbox):
            assert masks(path, sid)[1:] == (0, 0)


@pytest.mark.asyncio
async def test_windows_create_dispatches_acl_to_worker(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    import jiuwenbox.supervisor as supervisor
    from jiuwenbox.models.policy import SecurityPolicy
    from jiuwenbox.server.runtime.process import ProcessRuntime

    class AclReached(Exception):
        pass

    main_thread = threading.get_ident()

    def apply(*args, **kwargs):
        assert threading.get_ident() != main_thread
        assert str(tmp_path) in kwargs["bounded_roots"]
        raise AclReached

    acl = MagicMock()
    acl.resolve_desktop_data_dir.return_value = None
    acl.apply_sandbox_acl.side_effect = apply
    monkeypatch.setattr(supervisor, "win_acl", acl)
    monkeypatch.setattr(supervisor, "win_setup", MagicMock(), raising=False)
    runtime = ProcessRuntime()
    monkeypatch.setattr(runtime, "_load_policy", lambda _: SecurityPolicy(version=1))
    monkeypatch.setattr(runtime, "_win_workspace_for", lambda *args: str(tmp_path))
    with pytest.raises(AclReached):
        await runtime._create_windows("probe", tmp_path / "policy.yaml", {})
