# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""兼容别名：宿主服务注册表已下沉 ``jiuwenswarm.common.host_services``。

该模块是 gateway（install）与 agents 工具（get）共享的进程内单例总线，
必须全仓唯一模块实例，故归共享层 common。
"""

from jiuwenswarm.common.host_services import (  # noqa: F401
    RuntimeHostPushTransport,
    RuntimeXiaoyiChannelProvider,
    get_runtime_xiaoyi_channel,
    install_runtime_push_handler,
    install_runtime_wake_handler,
    install_runtime_xiaoyi_channel_provider,
    restore_runtime_push_handler,
    restore_runtime_wake_handler,
    restore_runtime_xiaoyi_channel_provider,
    send_runtime_push,
)
