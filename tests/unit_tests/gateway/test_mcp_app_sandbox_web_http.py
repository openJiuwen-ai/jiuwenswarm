# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CR-10997-01 回归：MCP Apps 沙箱路由必须注册在 Gateway Web HTTP app 上。

/api/v1/* 的部署出口是本 app（桌面 app_web 代理、vite dev 代理、生产
VITE_API_BASE 都指向它）；WebChannel 19000 在多数部署形态不可达——
路由只挂 WebChannel 会让前端 loadSandboxUrl 永远 404、永远 srcdoc 回退。
"""

from jiuwenswarm.gateway.channel_manager.web import web_http_app


def test_mcp_app_sandbox_routes_registered_on_web_http_app() -> None:
    app = web_http_app.create_web_http_app(object())
    paths = {route.path for route in app.routes}
    assert "/api/v1/mcp-app/sandbox" in paths
    assert "/mcp-app-sandbox" in paths
