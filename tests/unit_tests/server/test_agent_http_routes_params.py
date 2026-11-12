# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""REST 入口 query 参数类型还原（collect_params）的行为测试。

背景（#5441）：企业版南向 REST 的 query 是字符串空间，Gateway 把
``with_installed=false`` 正确还原为 ``False`` 后再南向序列化；AgentServer
若原样透传字符串，handler 侧 ``bool("False")`` 恒为真，显式 false 被当
true 处理。collect_params 与 Gateway ``_coerce_query_value`` 同表还原。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.agent_http_routes import collect_params


def _request(query: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(method="GET", query_params=query, path_params={})


@pytest.mark.asyncio
async def test_collect_params_restores_bool_query_values():
    params = await collect_params(
        _request({"with_installed": "false", "refresh_marketplaces": "true", "enabled": "False"})
    )
    assert params["with_installed"] is False
    assert params["refresh_marketplaces"] is True
    assert params["enabled"] is False


@pytest.mark.asyncio
async def test_collect_params_restores_on_off_like_as_bool():
    """``on``/``off`` 与同仓 ``_as_bool`` 同集合，不能残留字符串被 bool 判真。"""
    params = await collect_params(
        _request({"with_installed": "off", "refresh_marketplaces": "on", "enabled": "OFF"})
    )
    assert params["with_installed"] is False
    assert params["refresh_marketplaces"] is True
    assert params["enabled"] is False


@pytest.mark.asyncio
async def test_collect_params_restores_numeric_query_as_int():
    params = await collect_params(_request({"limit": "20", "offset": "0", "page": "2"}))
    assert params["limit"] == 20
    assert params["offset"] == 0
    assert params["page"] == 2


@pytest.mark.asyncio
async def test_collect_params_keeps_identifier_query_as_string():
    """标识类参数不参与数字还原：保留前导零，且与 path/header 通道同类型。"""
    params = await collect_params(
        _request({"session_id": "0012", "name": "1", "cursor": "-3", "agent_id": "007"})
    )
    assert params["session_id"] == "0012"
    assert params["name"] == "1"
    assert params["cursor"] == "-3"
    assert params["agent_id"] == "007"


@pytest.mark.asyncio
async def test_text_query_one_stays_string():
    """未登记为布尔的文本参数，``=1`` 不能被吞成 True（如搜索词）。"""
    params = await collect_params(_request({"q": "1", "search_keyword": "0"}))
    assert params["q"] == "1"
    assert params["search_keyword"] == "0"


@pytest.mark.asyncio
async def test_registered_boolean_query_maps_one_and_zero_to_bool():
    """登记的布尔参数 ``=1``/``=0`` 必须与 ``=true``/``=false`` 同义（检视意见 P2）。"""
    params = await collect_params(
        _request(
            {
                "attach_goal": "1",
                "log_as_user": "0",
                "enable_routing": "1",
                "include_team_compatibility": "0",
            }
        )
    )
    assert params["attach_goal"] is True
    assert params["log_as_user"] is False
    assert params["enable_routing"] is True
    assert params["include_team_compatibility"] is False


@pytest.mark.asyncio
async def test_collect_params_strips_and_keeps_text_query_values():
    params = await collect_params(_request({"search_keyword": "  jiuwen  ", "name": "my-skill"}))
    assert params["search_keyword"] == "jiuwen"
    assert params["name"] == "my-skill"


@pytest.mark.asyncio
async def test_collect_params_query_lower_priority_than_path_and_body():
    request = SimpleNamespace(
        method="GET",
        query_params={"session_id": "from-query", "with_installed": "false"},
        path_params={"session_id": "from-path"},
    )
    params = await collect_params(request)
    # path 覆盖 query；query 值仍做类型还原。
    assert params["session_id"] == "from-path"
    assert params["with_installed"] is False


@pytest.mark.asyncio
async def test_gateway_to_agentserver_bool_roundtrip_restores_false():
    """南向序列化 + AgentServer 解析往返：显式 False 不得被字符串化判真（#5441）。"""
    from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.gateway.routing.agent_rest_map import assemble_rest_request

    env = e2a_from_agent_fields(
        request_id="r-5441",
        channel_id="web",
        session_id="s-5441",
        req_method=ReqMethod.SKILLS_LIST,
        params={"with_installed": False},
        is_stream=False,
    )
    assembled = assemble_rest_request(env, base_url="http://127.0.0.1:8766")
    assert assembled.query["with_installed"] == "false"

    params = await collect_params(_request(dict(assembled.query or {})))
    # 修复前：南向 ``str(False)`` → ``"False"``，collect_params 原样透传，
    # handler ``bool("False")`` 为真 → skills.list 意外附带 plugins。
    assert params["with_installed"] is False


@pytest.mark.asyncio
async def test_enable_streaming_one_after_coercion_still_selects_sse():
    """``?enable_streaming=1`` 还原为 bool True 后仍必须判为 SSE，不得静默退化。"""
    from jiuwenswarm.server.agent_http_routes import wants_stream

    params = await collect_params(_request({"enable_streaming": "1"}))
    assert params["enable_streaming"] is True
    request = SimpleNamespace(headers={})
    assert wants_stream(request, params) is True

    off = await collect_params(_request({"enable_streaming": "0"}))
    assert off["enable_streaming"] is False
    assert wants_stream(request, off) is False


def test_wants_stream_accepts_int_and_float_flags():
    from jiuwenswarm.server.agent_http_routes import wants_stream

    request = SimpleNamespace(headers={})
    assert wants_stream(request, {"enable_streaming": 1}) is True
    assert wants_stream(request, {"enable_streaming": 1.0}) is True
    assert wants_stream(request, {"enable_streaming": 0}) is False
    # 字符串分支保持原语义，``true``/``on`` 等仍生效。
    assert wants_stream(request, {"enable_streaming": "on"}) is True
    assert wants_stream(request, {"enable_streaming": "off"}) is False
