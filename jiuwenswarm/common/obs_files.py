# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（拆仓过渡）：真源已迁 ``gateway.common.obs_files``。"""

import sys

import gateway.common.obs_files as _real

sys.modules[__name__] = _real
