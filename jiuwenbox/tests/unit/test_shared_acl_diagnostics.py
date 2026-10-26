"""Preparation failures retain their cause and never publish a success stamp."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server import shared_paths
from jiuwenbox.supervisor import win_acl, win_setup


@pytest.mark.parametrize("operation", ["stat_runtime_metadata", "read_root_dacl", "protect_root_dacl"])
def test_shared_root_preparation_failure_has_context_without_marking_success(
    tmp_path, monkeypatch, caplog, operation,
):
    root = tmp_path / "runtime"
    root.mkdir()
    cache = tmp_path / "shared-acl.json"
    failure = PermissionError("original preparation failure")
    owner_queries = []

    def get(path, flags):
        if flags == 1:
            owner_queries.append(path)
            return SimpleNamespace(GetSecurityDescriptorOwner=lambda: "runtime-owner-sid")
        if operation == "read_root_dacl":
            raise failure
        return SimpleNamespace(GetSecurityDescriptorDacl=lambda: None)

    security = SimpleNamespace(
        OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4, GetFileSecurity=get,
        ConvertSidToStringSid=str, ConvertSecurityDescriptorToStringSecurityDescriptor=lambda *a: "dacl",
    )
    monkeypatch.setattr(shared_paths, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("JIUWENBOX_SERVICE_READ_ROOTS", json.dumps([str(root)]))
    monkeypatch.setenv("JIUWENBOX_TOOL_PATHS", "{}")
    monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", "[]")
    for key in ("JIUWENBOX_VENV_DIR", "JIUWENBOX_BUNDLED_PYTHON"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(shared_paths, "_shared_acl_cache_path", lambda: cache)
    monkeypatch.setattr(win_setup, "get_sandbox_user_sid", lambda: "sandbox")
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: "synthetic")
    monkeypatch.setattr(win_acl, "grant_parent_traverse", lambda *a, **kw: None)
    monkeypatch.setattr(win_acl, "apply_tool_acl", Mock())
    monkeypatch.setattr(win_acl, "_ensure_pywin32", lambda: (security, None, None))
    if operation == "stat_runtime_metadata":
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == root / "node.exe":
                raise failure
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
    if operation == "protect_root_dacl":
        monkeypatch.setattr(win_acl, "_write_file_dacl", Mock(side_effect=failure))
    with pytest.raises(PermissionError) as error:
        shared_paths.prepare_shared_tool_acl(SecurityPolicy(version=1))
    assert error.value is failure
    assert not cache.exists()
    failed_path = root / "node.exe" if operation == "stat_runtime_metadata" else root
    assert owner_queries == [str(failed_path)]
    assert f"root={root} path={failed_path} operation={operation} owner_sid=runtime-owner-sid" in caplog.text
