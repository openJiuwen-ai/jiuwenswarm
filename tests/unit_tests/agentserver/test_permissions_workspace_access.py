# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``permissions.file_guard.workspace.access.get/set`` RPC 单测。

覆盖：枚举登记、frozenset 注册、GET 读三轴、SET 全量/部分更新、
非法 level 校验（dispatch 回 BAD_REQUEST + 函数层抛 ValueError）。
对齐 ``test_permissions_tools_list.py`` 的 dispatch 测试写法，
通过 monkeypatch ``config._effective_permissions`` / ``_persist_permissions``
在内存里跑，不落盘。
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_config_rpc


def _access_request(method: ReqMethod, params: dict[str, Any]) -> AgentRequest:
    return AgentRequest(
        request_id="access-1",
        channel_id="web",
        session_id="default",
        req_method=method,
        params=params,
    )


def _install_fake_permissions(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """注入内存态 permissions，避开 file_guard 的落盘与 config_loader 缓存。"""
    state: dict[str, Any] = {"permissions": {}}

    monkeypatch.setattr(config, "_effective_permissions", lambda: state["permissions"])

    def _fake_persist(mutate_fn) -> dict[str, Any]:
        mutate_fn(state["permissions"])
        return state["permissions"]

    monkeypatch.setattr(config, "_persist_permissions", _fake_persist)
    return state


def test_workspace_access_methods_are_declared() -> None:
    assert getattr(ReqMethod, "PERMISSIONS_WORKSPACE_ACCESS_GET", None) is not None
    assert getattr(ReqMethod, "PERMISSIONS_WORKSPACE_ACCESS_SET", None) is not None


def test_workspace_access_methods_are_registered() -> None:
    methods = permissions_config_rpc.get_permissions_config_req_methods()
    assert ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET in methods
    assert ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_SET in methods


def test_workspace_access_get_returns_current_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_permissions(monkeypatch)
    state["permissions"] = {
        "file_guard": {"workspace": {"read": "allow", "write": "ask", "exec": "deny"}}
    }

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET, {})
    )

    assert resp.ok is True
    assert resp.payload == {"read": "allow", "write": "ask", "exec": "deny"}


def test_workspace_access_get_defaults_to_ask_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_permissions(monkeypatch)
    state["permissions"] = {"file_guard": {"workspace": {}}}

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET, {})
    )

    assert resp.ok is True
    assert resp.payload == {"read": "ask", "write": "ask", "exec": "ask"}


def test_workspace_access_get_defaults_when_file_guard_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_permissions(monkeypatch)
    # _effective_permissions 返回空 dict（无 file_guard）

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET, {})
    )

    assert resp.ok is True
    assert resp.payload == {"read": "ask", "write": "ask", "exec": "ask"}


@pytest.mark.parametrize(
    "bad_value",
    ["maybe", True, None, 1, "ALLOW"],
    ids=["str-invalid", "bool", "none", "int", "wrong-case"],
)
def test_workspace_access_get_sanitizes_invalid_levels(
    monkeypatch: pytest.MonkeyPatch,
    bad_value,
) -> None:
    state = _install_fake_permissions(monkeypatch)
    state["permissions"] = {
        "file_guard": {"workspace": {"read": bad_value, "write": "allow", "exec": "ask"}}
    }

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET, {})
    )

    assert resp.ok is True
    # 非法 read 被清洗为 ask；合法 write/exec 原样透传
    assert resp.payload == {"read": "ask", "write": "allow", "exec": "ask"}


def test_workspace_access_get_sanitizes_all_invalid_axes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_permissions(monkeypatch)
    state["permissions"] = {
        "file_guard": {"workspace": {"read": "maybe", "write": True, "exec": 1}}
    }

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_GET, {})
    )

    assert resp.ok is True
    assert resp.payload == {"read": "ask", "write": "ask", "exec": "ask"}


def test_workspace_access_set_writes_all_axes(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_permissions(monkeypatch)

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(
            ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_SET,
            {"access": {"read": "allow", "write": "allow", "exec": "ask"}},
        )
    )

    assert resp.ok is True
    assert resp.payload == {"read": "allow", "write": "allow", "exec": "ask"}

    ws = state["permissions"]["file_guard"]["workspace"]
    assert ws == {"read": "allow", "write": "allow", "exec": "ask"}


def test_workspace_access_set_partial_update_keeps_other_axes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_permissions(monkeypatch)
    state["permissions"] = {
        "file_guard": {"workspace": {"read": "allow", "write": "allow", "exec": "ask"}}
    }

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(
            ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_SET,
            {"access": {"write": "ask"}},
        )
    )

    assert resp.ok is True
    # 只改 write，read/exec 不动
    assert resp.payload == {"read": "allow", "write": "ask", "exec": "ask"}
    ws = state["permissions"]["file_guard"]["workspace"]
    assert ws == {"read": "allow", "write": "ask", "exec": "ask"}


def test_workspace_access_set_rejects_invalid_level(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_permissions(monkeypatch)

    resp = permissions_config_rpc.dispatch_permissions_config_request(
        _access_request(
            ReqMethod.PERMISSIONS_WORKSPACE_ACCESS_SET,
            {"access": {"read": "maybe"}},
        )
    )

    assert resp.ok is False
    assert resp.payload.get("code") == "BAD_REQUEST"
    assert "read" in resp.payload.get("error", "")


def test_update_access_function_raises_on_invalid_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_permissions(monkeypatch)

    with pytest.raises(ValueError):
        config.update_permissions_file_guard_workspace_access_in_config(
            {"write": "sometimes"}
        )
