# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：provider 方言兼容补丁已下沉 ``jiuwenswarm.common.llm_provider_compat_patch``。"""

from jiuwenswarm.common.llm_provider_compat_patch import (  # noqa: F401
    apply_provider_compat_patches,
)

__all__ = ["apply_provider_compat_patches"]
