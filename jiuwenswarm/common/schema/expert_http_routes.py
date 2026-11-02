# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""专家包 HTTP 的线上路径。

只保留方法、动词和路径。不读产品形态，不含北向查询参数或说明文字。
"""

from __future__ import annotations

from typing import NamedTuple


class ExpertHttpRoute(NamedTuple):
    """Gateway 与 AgentServer 共用的一条专用网址。"""

    method: str
    verb: str
    path: str


EXPERT_TEMPLATE_HTTP_ROUTES: tuple[ExpertHttpRoute, ...] = (
    ExpertHttpRoute("agent_templates.list", "GET", "/agent-templates"),
    ExpertHttpRoute("agent_templates.show", "GET", "/agent-templates/{id}"),
    ExpertHttpRoute("agent_templates.file.list", "GET", "/agent-templates/{id}/files"),
    ExpertHttpRoute(
        "agent_templates.file.read",
        "GET",
        "/agent-templates/{id}/files/content",
    ),
    ExpertHttpRoute(
        "agent_templates.install",
        "POST",
        "/agent-templates/{id}/actions/install",
    ),
    ExpertHttpRoute(
        "agent_templates.uninstall",
        "POST",
        "/agent-templates/{id}/actions/uninstall",
    ),
)


def expert_http_routes() -> tuple[ExpertHttpRoute, ...]:
    """当前已约定的专家包专用网址。"""
    return EXPERT_TEMPLATE_HTTP_ROUTES
