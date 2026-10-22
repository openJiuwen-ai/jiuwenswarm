# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：MCP 包清单解析已下沉 ``jiuwenswarm.common.mcp_package_manifest``。"""

from jiuwenswarm.common.mcp_package_manifest import (  # noqa: F401
    McpPackageError,
    McpPackageManifest,
    load_mcp_package,
    iter_mcp_packages,
    resolve_mcp_package,
)

__all__ = [
    "McpPackageError",
    "McpPackageManifest",
    "load_mcp_package",
    "iter_mcp_packages",
    "resolve_mcp_package",
]
