# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版北向专家包路由。路径来自公共报文，这里只补 Gateway 北向字段。"""

from __future__ import annotations

from gateway.channel_manager.web.http_routes.mapped import WebHttpMappedRoute
from jiuwenswarm.common.schema.expert_http_routes import expert_http_routes

_TENANT_QUERY = ("group_id", "bot_id", "user_id", "service_id", "agent_id")

# 方法 → 北向说明。路径和动词以公共层为准。
_NORTHBOUND: dict[str, dict] = {
    "agent_templates.list": {
        "summary": "列出可见的专家包",
        "query_keys": ("filter", "ui_language", "organization_id", "department_id") + _TENANT_QUERY,
        "path_to_param": {},
        "accept_body": False,
    },
    "agent_templates.show": {
        "summary": "专家包详情",
        "query_keys": ("ui_language", "organization_id", "department_id") + _TENANT_QUERY,
        "path_to_param": {"id": "id"},
        "accept_body": False,
    },
    "agent_templates.file.list": {
        "summary": "专家包文件列表",
        "query_keys": ("ui_language", "organization_id", "department_id") + _TENANT_QUERY,
        "path_to_param": {"id": "id"},
        "accept_body": False,
    },
    "agent_templates.file.read": {
        "summary": "读取专家包文本文件",
        "query_keys": ("path", "ui_language", "organization_id", "department_id") + _TENANT_QUERY,
        "path_to_param": {"id": "id"},
        "accept_body": False,
    },
    "agent_templates.install": {
        "summary": "安装专家包",
        "query_keys": (),
        "path_to_param": {"id": "id"},
        "accept_body": True,
    },
    "agent_templates.uninstall": {
        "summary": "卸载专家包",
        "query_keys": (),
        "path_to_param": {"id": "id"},
        "accept_body": True,
    },
}


def northbound_routes() -> tuple[WebHttpMappedRoute, ...]:
    """企业版追加到用户面映射表的专家包路由。"""
    routes: list[WebHttpMappedRoute] = []
    for row in expert_http_routes():
        meta = _NORTHBOUND[row.method]
        routes.append(
            WebHttpMappedRoute(
                row.verb,
                row.path,
                row.method,
                "experts",
                meta["summary"],
                path_to_param=meta["path_to_param"],
                query_keys=meta["query_keys"],
                accept_body=meta["accept_body"],
            )
        )
    return tuple(routes)


def pod_http_routes() -> dict[str, tuple[str, str]]:
    """Gateway 发往 Pod 时使用的方法 → (动词, 路径)。"""
    return {row.method: (row.verb, row.path) for row in expert_http_routes()}
