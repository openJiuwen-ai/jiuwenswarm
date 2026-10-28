# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（security 域拆分过渡）：真源已迁 ``gateway.extensions.base_crypto``。

CryptoProvider 注入点的实现与装配（``_RegistryCryptoBridge`` 的注册）都在
gateway 的 extensions 治理层；母仓消费方（config 凭据加解密、config_panel、
agents memory rail）经本路径取用，B3 拆仓时随 shim 收敛退役。
"""

from gateway.extensions.base_crypto import *  # noqa: F401,F403
from gateway.extensions.base_crypto import (  # noqa: F401
    CryptoProvider,
    get_crypto_provider,
    set_crypto_provider,
)
