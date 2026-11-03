# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：会话级模型选择存储已下沉 ``jiuwenswarm.common.model_selection_store``。"""

from jiuwenswarm.common.model_selection_store import (  # noqa: F401
    get_session_model_selection,
    set_session_model_selection,
)

__all__ = ["get_session_model_selection", "set_session_model_selection"]
