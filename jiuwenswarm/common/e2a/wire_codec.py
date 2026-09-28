# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""转发别名（过渡形态）：实现已迁 ``gateway_protocol.e2a.wire_codec``。"""

from gateway_protocol.e2a.gateway_normalize import (  # noqa: F401
    e2a_response_from_agent_chunk,
)
from gateway_protocol.e2a.wire_codec import (  # noqa: F401
    encode_agent_chunk_for_wire,
    encode_agent_response_for_wire,
    encode_json_parse_error_wire,
    is_e2a_response_wire_dict,
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
