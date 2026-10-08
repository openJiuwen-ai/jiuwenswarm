# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Check native group argument marshalling without changing Windows accounts."""

import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenbox.supervisor import win_setup


@pytest.mark.parametrize("group_result", [0, 2237, 1379])
def test_add_user_to_group_marshals_names(monkeypatch, group_result):
    group = win_setup.const.SANDBOX_USER_GROUP
    member_name = r"TEST-PC\jbx-sandbox"

    def add_group(server, level, info, error):
        assert ctypes.cast(info, ctypes.POINTER(ctypes.c_wchar_p)).contents.value == group
        assert (server, level, error) == (None, 0, None)
        return group_result

    def add_member(server, group_name, level, info, count):
        assert ctypes.cast(info, ctypes.POINTER(ctypes.c_wchar_p)).contents.value == member_name
        assert (server, group_name, level, count) == (None, group, 3, 1)
        return 0

    api = SimpleNamespace(NetLocalGroupAdd=Mock(side_effect=add_group),
                          NetLocalGroupAddMembers=Mock(side_effect=add_member))
    monkeypatch.setattr(win_setup, "_get_netapi32", lambda: api)
    monkeypatch.setattr(win_setup, "_lookup_user_sid", Mock(side_effect=RuntimeError("use name fallback")))
    monkeypatch.setattr(win_setup, "_sandbox_group_member_names", lambda: [member_name])

    win_setup._add_user_to_group()

    api.NetLocalGroupAdd.assert_called_once()
    api.NetLocalGroupAddMembers.assert_called_once()


def test_group_creation_error_still_stops_install(monkeypatch):
    api = SimpleNamespace(NetLocalGroupAdd=Mock(return_value=5), NetLocalGroupAddMembers=Mock())
    monkeypatch.setattr(win_setup, "_get_netapi32", lambda: api)

    with pytest.raises(RuntimeError, match="NetLocalGroupAdd.*ret=5"):
        win_setup._add_user_to_group()

    api.NetLocalGroupAddMembers.assert_not_called()
