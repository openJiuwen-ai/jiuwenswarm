# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""logging_config Codec。"""

from jiuwenswarm.gateway.config.logging.repository import LOGGING_LEVEL_FIELDS
from jiuwenswarm.gateway.config.section import DbFlatSectionCodec, YamlSectionCodec


def db_logging_codec() -> DbFlatSectionCodec:
    return DbFlatSectionCodec(LOGGING_LEVEL_FIELDS)


__all__ = ["DbFlatSectionCodec", "YamlSectionCodec", "db_logging_codec"]
