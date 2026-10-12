# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：实现已迁 ``gateway_protocol.e2a.wire_codec``。

Team Organization delta 身份保留已下沉协议包：``encode_agent_chunk_for_wire``
/ ``parse_agent_server_wire_chunk`` 经内部转换函数在 chat.delta 分支内置
保留团队路由标识，本模块不再持有包装实现。
"""

from gateway_protocol.e2a.wire_codec import (  # noqa: F401
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
    is_e2a_response_wire_dict,
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)

__all__ = [
    "encode_agent_chunk_for_wire",
    "encode_agent_response_for_wire",
    "encode_json_parse_error_wire",
    "is_e2a_response_wire_dict",
    "parse_agent_server_wire_chunk",
    "parse_agent_server_wire_unary",
]