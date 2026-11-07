# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# agent_ssas/core/modes/http/server.py
"""AgentSSAS HTTP 服务模式启动入口。

通过 uvicorn 启动 FastAPI 应用,监听事件上报请求。

启动方式:
    python -m agent_ssas.core.modes.http.server

环境变量:
    SSAS_MODE=http            (触发 HTTP 模式)
    SSAS_HTTP_HOST=0.0.0.0   (监听地址)
    SSAS_HTTP_PORT=8443       (监听端口)
"""

from __future__ import annotations

import uvicorn

from agent_ssas.core.framework.access_adapter.http_server import create_app
from agent_ssas.core.framework.config.settings import AgentSSASConfig


def main() -> None:
    """启动 AgentSSAS HTTP 服务端。"""
    config = AgentSSASConfig()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.http_host,
        port=config.http_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
