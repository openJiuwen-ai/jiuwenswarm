# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-side utils facade: path contracts + logger.

路径推导真源在 ``gateway.common.paths``（!9120 建立）；本模块只做再导出并
提供 ``logger``。不拷贝母仓 ``jiuwenswarm.common.utils`` 的运行时函数——
logging 级别热更、tenant key 等与母仓运行时/跨进程契约耦合的实现继续从
母仓导入，待架构化拆分（E2A 化）时统一处理。
"""

from __future__ import annotations

import logging

from gateway.common.paths import (  # noqa: F401
    get_agent_home_dir,
    get_agent_root_dir,
    get_agent_sessions_dir,
    get_agent_workspace_dir,
    get_checkpoint_dir,
    get_config_dir,
    get_config_file,
    get_env_file,
    get_logs_dir,
    get_root_dir,
    get_user_home,
    get_user_workspace_dir,
    set_user_home,
)

logger = logging.getLogger(__name__)
