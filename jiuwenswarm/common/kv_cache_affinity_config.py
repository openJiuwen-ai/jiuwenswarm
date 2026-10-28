# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""config 域拆分过渡：jiuwenswarm.common.kv_cache_affinity_config 是 gateway.common.kv_cache_affinity_config 的模块别名。"""

import sys

import gateway.common.kv_cache_affinity_config as _real

sys.modules[__name__] = _real
