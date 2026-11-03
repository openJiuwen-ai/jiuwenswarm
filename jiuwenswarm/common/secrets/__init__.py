# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（拆仓过渡）：真源已迁 ``gateway.common.secrets``。

请经包级 import 消费（如 ``from jiuwenswarm.common.secrets import
SecretStore``）；子模块级 import 会被别名包解析为 gateway 侧实现。
"""

import sys

import gateway.common.secrets as _real

sys.modules[__name__] = _real
