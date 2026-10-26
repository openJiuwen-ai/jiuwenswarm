# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ReqMethod 协议契约：转发方法串必须能被协议枚举解析。

Gateway 发往 AgentServer 的方法串会在服务端直接走 ``ReqMethod(str)``
（``server/agent_ws_server._payload_to_request``、``server/front/protocol``、
``server/ipc/protocol``），未知串会抛 ``ValueError`` 导致整个请求失败。
因此凡是进入转发名单、或作为 Gateway 出站请求携带的方法串，都必须存在于
``openjiuwen-protocol`` 的 ``ReqMethod`` 中。

不覆盖 ``channel.register_method(...)`` 注册的本地方法：``cron.*`` /
``heartbeat.*`` / ``vendors.*`` / ``external_cli.*`` 等只由 Gateway 本地
handler 处理，不跨进程发送，也不参与线上枚举解析。需要跨进程的方法应登记到
``_FORWARD_REQ_METHODS`` / ``CLI_FORWARD_REQ_METHODS``，本测试会自动纳入。
"""

from __future__ import annotations

import ast
from pathlib import Path

import openjiuwen_protocol.message as protocol_message

from jiuwenswarm.common.schema import agent as schema_agent
from jiuwenswarm.common.schema import message as schema_message
from jiuwenswarm.common.schema.message import ReqMethod
from gateway.channel_manager.tui.tui_connect import CLI_FORWARD_REQ_METHODS
from gateway.channel_manager.web.app_web_handlers import (
    _FORWARD_REQ_METHODS,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REQ_METHOD_VALUES = frozenset(method.value for method in ReqMethod)


def _gateway_req_method_literals() -> set[str]:
    """收集 gateway 源码中 ``req_method="..."`` 关键字实参字面量。"""
    found: set[str] = set()
    gateway_root = _REPO_ROOT / "gateway"
    assert gateway_root.is_dir(), f"Gateway source directory does not exist: {gateway_root}"
    for path in gateway_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "req_method"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    found.add(keyword.value.value)
    return found


def _contract_method_strings() -> set[str]:
    return (
        set(_FORWARD_REQ_METHODS)
        | set(CLI_FORWARD_REQ_METHODS)
        | _gateway_req_method_literals()
    )


def test_forwarded_methods_exist_in_protocol_req_method() -> None:
    contract_methods = _contract_method_strings()
    assert contract_methods, "未收集到任何转发方法串，检查常量来源是否变更"

    unknown = sorted(contract_methods - _REQ_METHOD_VALUES)
    assert not unknown, (
        "以下方法串会转发到 AgentServer，但 openjiuwen-protocol 的 ReqMethod "
        f"未定义，服务端 ReqMethod(str) 会抛 ValueError：{unknown}。"
        "请在 agent-protocol 仓补齐后更新 pyproject/uv.lock 的 pin。"
    )


def test_gateway_literal_scan_is_not_empty() -> None:
    assert _gateway_req_method_literals(), "Gateway literal scan must inspect the current source directory"


def test_req_method_reexports_are_identical() -> None:
    """schema.message / schema.agent 必须复用协议枚举，避免双重定义。"""
    assert schema_message.ReqMethod is protocol_message.ReqMethod
    assert schema_agent.ReqMethod is protocol_message.ReqMethod
    assert ReqMethod is protocol_message.ReqMethod
