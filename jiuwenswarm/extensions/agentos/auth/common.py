# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：已归 ``jiuwenswarm.gateway.extensions.auth(common)``。"""

from jiuwenswarm.gateway.extensions.auth.common import (  # noqa: F401
    headers_to_dict,
    extract_token_from_path_and_headers,
    extract_token,
    extract_headers,
    get_remote_addr,
    take_handshake_auth,
    apply_auth_result_to_ws,
)

__all__ = [
    "headers_to_dict",
    "extract_token_from_path_and_headers",
    "extract_token",
    "extract_headers",
    "get_remote_addr",
    "take_handshake_auth",
    "apply_auth_result_to_ws",
]
