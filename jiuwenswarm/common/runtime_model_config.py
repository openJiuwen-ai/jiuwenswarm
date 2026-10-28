# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""config 域拆分过渡：jiuwenswarm.common.runtime_model_config 是 gateway.common.runtime_model_config 的模块别名。"""

import sys

import gateway.common.runtime_model_config as _real

sys.modules[__name__] = _real
