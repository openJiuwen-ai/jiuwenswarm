# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""硬件监控模块（当前为 NPU 状态查询）。"""

from .npu_monitor import (
    handle_hardware_npu_status,
    parse_npu_smi_info,
    query_npu_status,
)

__all__ = ["handle_hardware_npu_status", "parse_npu_smi_info", "query_npu_status"]
