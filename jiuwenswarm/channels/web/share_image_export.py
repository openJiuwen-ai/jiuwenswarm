# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（拆仓过渡）：真源已迁 ``gateway.channel_manager.web.share_image_export``。"""

import sys

import agent_gateway.channel_manager.web.share_image_export as _real

if __name__ == "__main__":
    raise SystemExit(_real.main())

sys.modules[__name__] = _real
