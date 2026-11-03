# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""实例 Agent 授权表达式：配置写入和请求准入共用解析、校验与求值。

实现已收口到 ``openjiuwen_protocol.match_expressions``（gateway 与 server
两侧共用的唯一来源）。本模块仅 re-export，保留既有导入路径兼容。
"""

from __future__ import annotations

from openjiuwen_protocol.match_expressions import matches, validate_match_expr

__all__ = ("validate_match_expr", "matches")
