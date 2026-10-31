# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway Manager config receiver (registered on unified WEB_PORT)."""

from __future__ import annotations

from .http.app import register_manager_config_routes

__all__ = [
    "register_manager_config_routes",
]
