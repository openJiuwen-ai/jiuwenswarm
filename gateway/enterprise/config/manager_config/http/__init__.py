# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP helpers for Manager config (registered on unified WEB_PORT)."""

from .app import register_manager_config_routes

__all__ = [
    "register_manager_config_routes",
]
