# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""config 域拆分过渡：jiuwenswarm.common.agentos_runtime 是 gateway.common.agentos_runtime 的模块别名。"""

import sys

import gateway.common.agentos_runtime as _real

sys.modules[__name__] = _real
