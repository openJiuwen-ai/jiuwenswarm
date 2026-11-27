# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent HTTP 入口从 ``X-*`` 头重建 ``metadata.agent_identity``.

Gateway 到 AgentServer 的 REST 正文只有业务参数，展示身份走独立头；
重建结果供 ``MultiTenantStrategy.bind_request`` 写入租户工作区。
"""

from __future__ import annotations

from starlette.requests import Request

from jiuwenswarm.server.agent_http_routes import _rebuild_identity_from_headers

_AGENT_ID = "EyMFedy4mZ0b8btF1gxJFjsq4FOl7Pj6m2EYP3Ecmu7"


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
        }
    )


def test_rebuilds_agent_identity_from_display_headers() -> None:
    user_id, metadata = _rebuild_identity_from_headers(
        _request(
            {
                "X-User-Id": "u",
                "X-Group-Id": "g",
                "X-Bot-Id": "b",
                "X-Agent-Identity-Id": _AGENT_ID,
                "X-Runtime-Template-Id": "rt-1",
                "X-Agent-Template-Id": "tpl-1",
            }
        )
    )

    assert user_id == "u"
    assert metadata["agent_identity"] == {
        "agent_id": _AGENT_ID,
        "runtime_template_id": "rt-1",
        "agent_template_id": "tpl-1",
    }


def test_routing_agent_id_is_not_treated_as_display_identity() -> None:
    """``X-Agent-Id`` 是路由层 Agent id，不能当成展示身份。"""
    _user_id, metadata = _rebuild_identity_from_headers(
        _request({"X-User-Id": "u", "X-Agent-Id": "agent.work.normal"})
    )

    assert "agent_identity" not in (metadata or {})
    assert metadata["agent_id"] == "agent.work.normal"
