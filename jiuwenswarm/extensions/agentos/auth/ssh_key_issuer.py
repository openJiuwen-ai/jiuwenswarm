# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：已归 ``jiuwenswarm.gateway.extensions.auth(ssh_key_issuer)``。"""

from jiuwenswarm.gateway.extensions.auth.ssh_key_issuer import (  # noqa: F401
    SshKeyIssuer,
    AgentOSSshKeyIssuer,
)

__all__ = [
    "SshKeyIssuer",
    "AgentOSSshKeyIssuer",
]
