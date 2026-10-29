# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（auth 拆分过渡）：auth 域共享 HTTP 工具真源已迁 ``gateway.auth.net``。

server/agents 侧消费方经本路径取用（拍板 #10 方向：母仓反向依赖
gateway 仓）；B3 拆仓时随 shim 收敛退役。
"""

from gateway.auth.net import *  # noqa: F401,F403
from gateway.auth.net import (  # noqa: F401
    Any,
    annotations,
    os,
    requests,
    requests_request,
    ssl_verify_enabled,
)
