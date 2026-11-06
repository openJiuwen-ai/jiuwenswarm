# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-side package management for Extensions V2.

The Gateway shares ``extension_pkgs`` with AgentServer. This package owns the
Gateway half: reading ``package.domain``, copying Gateway zips into the
shared store, publishing and releasing them on the live runtime, and answering
``extensions.v2.*`` requests that address Gateway packages.
"""

from jiuwenswarm.gateway.extensions_v2.management.registry import GatewayPackageManager

__all__ = ["GatewayPackageManager"]
