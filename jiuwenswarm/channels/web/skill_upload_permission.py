# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（拆仓过渡）：真源已迁 ``gateway.common.skill_upload_permission``。

该模块为纯 stdlib 无状态组件，按 M1 策略下沉到 ``gateway.common`` 作为唯一
真源（``ALLOWED_CROSS_LAYER_PREFIXES`` 允许母仓反向引用）。本文件保留为
导入兼容入口：``sys.modules[__name__]`` 重定向到真源模块，使既有调用方
（``app_web`` / ``skill_manager`` / 测试）原 ``from jiuwenswarm.channels.web
.skill_upload_permission import ...`` 不变，且共享同一模块实例（含 ``_cache``
等模块级状态，便于 ``reset_cache_for_test`` 生效）。
"""

import sys

import gateway.common.skill_upload_permission as _real

sys.modules[__name__] = _real
