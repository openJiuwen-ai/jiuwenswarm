# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compatibility entry point for the Gateway-owned ADX extension."""

from gateway.extensions.adx.extension import register_extensions

__all__ = ["register_extensions"]
