# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Gateway 状态码定义."""

from enum import Enum


class StatusCode(Enum):
    """状态码枚举。"""

    AUTHENTICATION_ERROR = (10010, "Authentication failed!")
    PARAM_CHECK_FAILED_ERROR = (100002, "Error occur when input parameter verification failed")
    STS_DECRYPT_ERROR = (100007, "Sts decrypt data failed!")
    STS_INIT_ERROR = (100009, "Sts init data failed!")
