# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 呈现模块的插件接口协议。

定义 PresentationPlugin,作为呈现插件接口。
输入威胁分析报告(简化格式),输出形式由插件自行决定(日志、文件、推送等)。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PresentationPlugin(Protocol):
    """呈现插件接口。

    输入威胁分析报告(简化格式),基于 trace_id 关联
    构建完整 OCSF 格式报告并输出。
    """

    name: str

    async def render(self, report: dict) -> None:
        """呈现威胁分析报告。

        参数 report 为威胁分析报告(简化格式),包含风险检测结果
        及 aux_ids(trace_id、session_id 等关联字段)。
        """
        ...
