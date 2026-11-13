# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 提示词段正文数据层（从各 flash rail 抽离的段正文常量 + 纯函数）。

模块按域组织（safety/response/persona/tools/memory/todo/subagent/relay），
每个模块只含数据与纯函数、不 import 任何 flash rail / ``DeepAgentRail``，可被
``flash.prompt.offline`` 离线组装器与各 rail 双向复用。命名沿用原 rail 内的
下划线/公开常量名，保证 rail re-import 零行为变化。
"""
