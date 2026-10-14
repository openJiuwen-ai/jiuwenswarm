# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：应用插件宿主已归 ``jiuwenswarm.gateway.extensions.application_host``。"""

from jiuwenswarm.gateway.extensions.application_host import (  # noqa: F401
    APPLICATION_PLUGIN_API_PREFIX,
    application_plugin_manifest,
    iter_websocket_routes,
    mount_application_plugin_http_routes,
)

__all__ = [
    "APPLICATION_PLUGIN_API_PREFIX",
    "application_plugin_manifest",
    "iter_websocket_routes",
    "mount_application_plugin_http_routes",
]
