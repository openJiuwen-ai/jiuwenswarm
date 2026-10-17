# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""backend_client/openjiuwen - 对接 openjiuwen (agent-core) 的 AgentSSASSecurityRail 插桩代码。

此目录下的代码仅在 jiuwenswarm 通过 try/except 显式导入时加载。
如果 openjiuwen 未安装,导入会失败(ImportError),
但 agent-ssas 核心能力不受影响。
"""
