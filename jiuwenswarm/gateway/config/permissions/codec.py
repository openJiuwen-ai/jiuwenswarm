# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""permissions_config Codec 选型说明。

个人版与企业版均使用 :class:`YamlSectionCodec`（``config.yaml`` ``/permissions`` 段）。
企业 Agent 策略走 ``permissions_template`` 槽位，不再使用实例级 DB 表。
"""

from jiuwenswarm.gateway.config.section import YamlSectionCodec

__all__ = ["YamlSectionCodec"]
