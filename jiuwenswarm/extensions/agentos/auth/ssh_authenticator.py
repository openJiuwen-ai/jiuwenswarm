# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：已归 ``jiuwenswarm.gateway.extensions.auth(ssh_authenticator)``。"""

from jiuwenswarm.gateway.extensions.auth.ssh_authenticator import (  # noqa: F401
    SshPublicKeyAuthenticator,
)

__all__ = [
    "SshPublicKeyAuthenticator",
]
