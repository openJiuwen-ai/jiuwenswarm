# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer 企业平面 · 能力包（工作区配额等）.

与 ``config/``、``tenant/`` 并列；本包内模块为运行时能力，按需懒加载，
勿在包 ``__init__`` 中拉起全家桶。与 Gateway ``gateway.enterprise.capabilities`` 同构。
"""
