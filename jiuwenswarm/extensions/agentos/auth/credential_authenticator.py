# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：已归 ``gateway.extensions.auth(credential_authenticator)``。"""

from gateway.extensions.auth.credential_authenticator import (  # noqa: F401
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)

__all__ = [
    "AuthContext",
    "AuthResult",
    "CredentialAuthenticator",
]
