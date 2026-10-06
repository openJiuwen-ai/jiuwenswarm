# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""ACP 子进程环境构造已下沉 ``jiuwenswarm.common.acp.subprocess_env``。

此处 re-export 保持既有 import 路径兼容；本模块将在阶段 3 随 ``jiuwenswarm/acp/`` 一并删除。
"""

from __future__ import annotations

from jiuwenswarm.common.acp.subprocess_env import build_acp_subprocess_env

__all__ = ["build_acp_subprocess_env"]