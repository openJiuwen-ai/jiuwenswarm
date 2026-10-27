"""Real Windows registry ACLs keep runtime pins outside the sandbox's authority."""

import hashlib
import os
import sys
import uuid

import pytest

from jiuwenswarm.server.runtime import python_base_pin as pins

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry ACLs")


@pytest.fixture
def pin_registry(tmp_path, monkeypatch):
    import winreg

    root = rf"Software\JiuwenSwarm\TestRuntimePins-{uuid.uuid4().hex}"
    monkeypatch.setattr(pins, "_PIN_ROOT", root)
    yield root
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root, 0, winreg.KEY_READ) as key:
            children = []
            for index in range(winreg.QueryInfoKey(key)[0]):
                children.append(winreg.EnumKey(key, index))
        for child in children:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, root + "\\" + child)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, root)
    except FileNotFoundError:
        pass


def test_host_pin_roundtrip_and_protected_registry_dacl(tmp_path, pin_registry):
    import win32security

    managed = tmp_path / "managed"
    managed.mkdir()
    base = tmp_path / "base" / "python.exe"
    base.parent.mkdir()
    base.touch()
    assert pins.read_python_base_pin(managed) is None
    pins.write_python_base_pin(managed, base)
    assert pins.read_python_base_pin(managed) == base

    with pins._open_pin_key(managed) as key:
        descriptor = win32security.GetSecurityInfo(int(key), win32security.SE_REGISTRY_KEY,
                                                 win32security.DACL_SECURITY_INFORMATION)
    assert descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    assert descriptor.GetSecurityDescriptorDacl().GetAceCount() == 3
    other = tmp_path / "other" / "python.exe"
    other.parent.mkdir()
    other.touch()
    with pytest.raises(pins.RuntimePythonTrustError, match="already differs"):
        pins.write_python_base_pin(managed, other)
    assert pins.read_python_base_pin(managed) == base
    pins.write_python_base_pin(managed, other, replace=True)
    assert pins.read_python_base_pin(managed) == other
    other.unlink()
    # Retain the old identity so provisioning can detect and repair a removed runtime.
    assert pins.read_python_base_pin(managed) == other
    pins.write_python_base_pin(managed, base, replace=True)
    assert pins.read_python_base_pin(managed) == base


def test_actual_sandbox_account_cannot_write_host_pin(tmp_path, pin_registry):
    import winreg
    import win32api
    import win32con
    import win32security
    from jiuwenbox.supervisor import win_setup

    password = win_setup.get_sandbox_user_password()
    if not password:
        pytest.skip("requires installed jbx-sandbox account")
    managed = tmp_path / "managed"
    managed.mkdir()
    base = tmp_path / "base" / "python.exe"
    base.parent.mkdir()
    base.touch()
    pins.write_python_base_pin(managed, base)
    host = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        host_sid = win32security.ConvertSidToStringSid(
            win32security.GetTokenInformation(host, win32security.TokenUser)[0])
    finally:
        host.Close()
    name = hashlib.sha256(os.path.normcase(str(managed.absolute())).encode("utf-8")).hexdigest()
    path = host_sid + "\\" + pin_registry + "\\" + name
    token = win32security.LogonUser("jbx-sandbox", None, password,
                                 win32con.LOGON32_LOGON_INTERACTIVE, win32con.LOGON32_PROVIDER_DEFAULT)
    try:
        password = None
        win32security.ImpersonateLoggedOnUser(token)
        try:
            with pytest.raises(PermissionError):
                with winreg.OpenKey(winreg.HKEY_USERS, path, 0, winreg.KEY_SET_VALUE):
                    pytest.fail("sandbox can modify the host pin")
        finally:
            win32security.RevertToSelf()
    finally:
        token.Close()
    assert pins.read_python_base_pin(managed) == base
