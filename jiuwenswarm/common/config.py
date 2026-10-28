# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""config 域拆分过渡：``jiuwenswarm.common.config`` 是 ``gateway.common.config`` 的模块别名。

真源已整体迁至 ``gateway.common.config``（含 get_config 链、update_* 写族、
模板迁移与全部视图构建器）。本文件通过 sys.modules 别名让两个路径指向
**同一个模块对象**——monkeypatch 在任一路径上都作用于同一全局命名空间，
消费方（server 配置面板、agents、gateway 进程）零改写且行为一致。

B3 拆仓时本文件删除，母仓侧改由独立实现或远端接口替代。
"""

import sys

import gateway.common.config as _real

sys.modules[__name__] = _real
