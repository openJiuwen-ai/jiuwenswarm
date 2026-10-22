# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：AgentOS 认证套件已归 ``jiuwenswarm.gateway.extensions.auth``。

gateway 为认证套件的宿主（SSH 协议通道）；本 shim 仅服务于母仓侧
agentos_router 的过渡引用，gateway 独立成仓时随之处理归属。
"""

from jiuwenswarm.gateway.extensions.auth.credential_authenticator import (  # noqa: F401
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)
from jiuwenswarm.gateway.extensions.auth.ssh_authenticator import (  # noqa: F401
    SshPublicKeyAuthenticator,
)
from jiuwenswarm.gateway.extensions.auth.ssh_key_issuer import (  # noqa: F401
    AgentOSSshKeyIssuer,
    SshKeyIssuer,
)
from jiuwenswarm.gateway.extensions.auth.ssh_key_registry import (  # noqa: F401
    KeyRegistry,
    KeyRegistryEntry,
)
