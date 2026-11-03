# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 企业配置库轮询：多副本从共享库同步 Gateway 自身配置。"""

from .scheduler import ConfigPollScheduler, get_config_poll_scheduler

__all__ = ("ConfigPollScheduler", "get_config_poll_scheduler")
