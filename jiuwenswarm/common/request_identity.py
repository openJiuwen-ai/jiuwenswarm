# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（拆仓过渡）：真源已迁 ``gateway.common.request_identity``。"""

import sys

import gateway.common.request_identity as _real

sys.modules[__name__] = _real
