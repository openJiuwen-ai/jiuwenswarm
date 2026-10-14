# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentOS authentication modules."""

from jiuwenswarm.gateway.extensions.auth.credential_authenticator import (
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)
from jiuwenswarm.gateway.extensions.auth.ssh_authenticator import SshPublicKeyAuthenticator
from jiuwenswarm.gateway.extensions.auth.ssh_key_issuer import (
    AgentOSSshKeyIssuer,
    SshKeyIssuer,
)
from jiuwenswarm.gateway.extensions.auth.ssh_key_registry import (
    KeyRegistry,
    KeyRegistryEntry,
)

__all__ = [
    "AuthContext",
    "AuthResult",
    "CredentialAuthenticator",
    "KeyRegistry",
    "KeyRegistryEntry",
    "SshKeyIssuer",
    "AgentOSSshKeyIssuer",
    "SshPublicKeyAuthenticator",
]
