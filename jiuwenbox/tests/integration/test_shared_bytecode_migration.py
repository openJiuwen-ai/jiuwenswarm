"""Real sandbox-owned caches in multiple generic service/tool roots."""

import json
import logging
import sys

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server import shared_paths
from jiuwenbox.supervisor import win_acl, win_setup, win_constants as const

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows cache ownership")


@pytest.mark.parametrize("artifact,denied", [("pyc", False), ("log", False), ("directory", False), ("pyc", True)])
def test_failure_migration_covers_all_declared_roots_without_weakening_denials(
    tmp_path, monkeypatch, caplog, artifact, denied,
):
    import win32api
    import win32con
    import win32security as security
    import pywintypes

    sid = win_setup.get_sandbox_user_sid()
    password = win_setup.get_sandbox_user_password()
    if not sid or not password:
        pytest.skip("requires installed jbx-sandbox")
    roots = [tmp_path / "plugin", tmp_path / "monorepo" / "another-package"]
    caches = []
    for root in roots:
        root.mkdir(parents=True)
        if artifact != "directory":
            (root / "__pycache__").mkdir()
        (root / "worker.py").write_text("VALUE = 1\n")
        caches.append(root / "__pycache__" if artifact == "directory" else
                      root / "__pycache__" / ("worker.cpython-313.pyc" if artifact == "pyc" else "worker.log"))
    win_acl.apply_tool_acl([str(tmp_path)], sid,
                           {"allow_read": [str(tmp_path)], "allow_write": [str(tmp_path)],
                            "deny_read": [], "deny_write": []}, writable_roots=[str(tmp_path)], strict=True)
    host = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        host_sid = security.GetTokenInformation(host, security.TokenUser)[0]
    finally:
        host.Close()
    token = security.LogonUser("jbx-sandbox", None, password,
                              win32con.LOGON32_LOGON_INTERACTIVE, win32con.LOGON32_PROVIDER_DEFAULT)
    password = None
    try:
        security.ImpersonateLoggedOnUser(token)
        try:
            for cache in caches:
                if artifact == "directory":
                    cache.mkdir()
                else:
                    cache.write_bytes(b"sandbox-owned")
                acl = security.ACL()
                acl.AddAccessAllowedAce(security.ACL_REVISION, 0x1F01FF, security.ConvertStringSidToSid(sid))
                acl.AddAccessAllowedAce(security.ACL_REVISION, const.FILE_GENERIC_READ, host_sid)
                sd = security.GetFileSecurity(str(cache), security.DACL_SECURITY_INFORMATION)
                win_acl._write_file_dacl(str(cache), acl, protect=True, sd=sd)
        finally:
            security.RevertToSelf()
    finally:
        token.Close()
    monkeypatch.setenv("JIUWENBOX_TOOL_PATHS", json.dumps({"read_roots": [str(roots[0])]}))
    monkeypatch.setenv("JIUWENBOX_SERVICE_READ_ROOTS", json.dumps([str(roots[1])]))
    monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", "[]")
    for key in ("JIUWENBOX_BUNDLED_PYTHON", "JIUWENBOX_VENV_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(shared_paths, "_shared_acl_cache_path", lambda: tmp_path / "shared-acl.json")
    monkeypatch.setattr(win_acl, "grant_parent_traverse", lambda *a, **kw: None)
    policy = SecurityPolicy(version=1)
    caplog.set_level(logging.INFO, logger="jiuwenbox.server.shared_paths")
    caplog.set_level(logging.DEBUG, logger=win_acl.__name__)
    if denied:
        policy.windows.filesystem.deny_read.extend(map(str, roots))
    if artifact != "pyc" or denied:
        with pytest.raises(pywintypes.error) as error:
            shared_paths.prepare_shared_tool_acl(policy)
        assert error.value.winerror == 5
        assert all(cache.exists() for cache in caches)
        assert not (tmp_path / "shared-acl.json").exists()
        failure = next(record.message for record in caplog.records if "object update failed" in record.message)
        assert f"root={roots[0]} path={caches[0]}" in failure
        assert f"operation=write_dacl owner_sid={sid}" in failure
        assert "winerror=5" in failure
        if denied:
            assert "migration=blocked_by_read_deny" in failure
        else:
            assert "migration=failed_or_ineligible" in failure
            if artifact == "directory":
                assert "object_type=directory" in failure
                assert "eligible=false reason=directory_not_cache" in caplog.text
            else:
                assert "eligible=false reason=not_backed_cpython_cache" in caplog.text
        return
    shared_paths.prepare_shared_tool_acl(policy)
    assert all(f"Shared tool ACL prepared: {root} cache=miss elapsed=" in caplog.text for root in roots)
    assert all(not cache.exists() for cache in caches)
    assert all((root / "worker.py").read_text() == "VALUE = 1\n" for root in roots)

    def unexpected_walk(*args, **kwargs):
        raise AssertionError("warm startup must not scan for caches")

    monkeypatch.setattr(win_acl, "_walk_acl_objects", unexpected_walk)
    shared_paths.prepare_shared_tool_acl(policy)
    assert all(f"Shared tool ACL unchanged, skip tree: {root} cache=hit elapsed=" in caplog.text for root in roots)
