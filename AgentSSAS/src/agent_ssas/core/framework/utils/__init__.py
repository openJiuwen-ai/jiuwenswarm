# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 公共能力模块。

提供 ID 生成等公共工具函数。
"""

from agent_ssas.core.framework.utils.id_utils import new_data_id, new_event_id, new_uuid

__all__ = ["new_data_id", "new_event_id", "new_uuid"]
