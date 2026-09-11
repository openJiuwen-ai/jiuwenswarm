# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""启动 AgentSSAS HTTP 服务(独立进程,多 Agent 共享)。

演示 HTTP 服务模式: 事件通过 POST /api/v1/events 上报,风险评估以 JSON 返回。

启动方式(AgentSSAS 仓库根目录,需安装 http 扩展依赖):

    uv run --extra http python examples/http_server_demo/start_server.py

服务地址默认 http://127.0.0.1:8443(仅监听本机回环,无需外网)。
可通过环境变量覆盖: SSAS_HTTP_HOST / SSAS_HTTP_PORT / SSAS_HOME。
"""

from __future__ import annotations

import uvicorn

from agent_ssas.core.framework.access_adapter.http_server import create_app
from agent_ssas.core.framework.config.settings import AgentSSASConfig


def main() -> None:
    # demo 默认仅监听本机回环地址;环境变量(SSAS_HTTP_HOST 等)仍可覆盖
    config = AgentSSASConfig.from_dict(
        {
            "http_host": "127.0.0.1",
            "http_port": 8443,
        }
    )
    app = create_app(config)
    print(f"[demo] AgentSSAS HTTP 服务启动: http://{config.http_host}:{config.http_port}")
    print(f"[demo] 事件上报端点: POST http://{config.http_host}:{config.http_port}/api/v1/events")
    print(f"[demo] 存储路径: {config.storage_path}")
    uvicorn.run(app, host=config.http_host, port=config.http_port, log_level="info")


if __name__ == "__main__":
    main()
