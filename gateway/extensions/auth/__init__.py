# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentOS authentication modules."""

from gateway.extensions.auth.credential_authenticator import (
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)
from gateway.extensions.auth.ssh_authenticator import SshPublicKeyAuthenticator
from gateway.extensions.auth.ssh_key_issuer import (
    AgentOSSshKeyIssuer,
    SshKeyIssuer,
)
from gateway.extensions.auth.ssh_key_registry import (
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
