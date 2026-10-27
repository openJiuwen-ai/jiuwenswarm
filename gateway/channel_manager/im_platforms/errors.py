# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""IM 平台附件处理公共异常。

``AttachmentPersistError`` 表示附件字节已成功从平台下载、但落盘到目标
AgentServer 注入目录失败（AgentServer 不可达 / E2A 落盘失败）。按决策 D6，
该错误必须穿透「下载失败降级为文本」的兜底，整条消息按可重试错误失败，
不得做「附件暂缺」降级。
"""

from __future__ import annotations


class AttachmentPersistError(RuntimeError):
    """附件落盘失败（目标 AgentServer 不可达），按可重试错误失败整条消息。"""
