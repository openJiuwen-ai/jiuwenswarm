# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
import os
import platform
from pathlib import Path
import socket


def get_container_ip():
    hostname = socket.gethostname()
    return socket.gethostbyname(hostname)


class BaseConfig:
    container_ip = get_container_ip()
    websocket_port: int = 8855
    websocket_host: str = "127.0.0.1"
    websocket_server_uri: str = f"ws://{websocket_host}:{websocket_port}"
    ENV = os.getenv('RUNTIME_MICROSERVICE_ENVIRONMENT', 'cn_dev_default')
    if platform.system() == "Windows":
        LOCAL_FLAG = True
        HTTP_SERVER_URL: str = f"http://localhost:8080"
    else:
        LOCAL_FLAG = False
        HTTP_SERVER_URL: str = f"https://{container_ip}:8080"
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
    LOCAL_FLAG = True
