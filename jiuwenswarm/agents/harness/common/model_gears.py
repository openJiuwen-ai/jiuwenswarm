# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""四档模式（model gear）关键字 → 具体模型 id 的唯一映射源。

前端把 ``fast`` / ``balanced`` / ``extreme`` / ``auto`` 这四个「档位关键字」当作
``model_name`` 下发，由 :class:`ModelRoutingRail` 在**单 agent 主链路**里翻成具体
模型 id（读 request param 注入的 ``run_context.extra["model_selection"]``）。

但档位关键字还有第二条通道：relay 把它原样写进 ``OFFICE_CLAW_EFFECTIVE_MODEL`` →
sidecar 的 ``MODEL_NAME`` env → 租户 tip / ``config.yaml``。**这条通道不经过任何
rail**。于是任何只吃 env 派生配置、没有 rail 也没有 request params 的消费方
（典型：DeepResearch 隔离子进程）会拿到裸档位关键字，当真实模型名直送 MaaS →
``ModelArts.81009 Invalid model``。

本模块是那张表的唯一出处：rail 与 deepresearch 共用，保证两侧同表同源不漂移。
放在 ``common/`` 顶层是为了让 deepresearch 侧能干净地 import ——
``rails/`` 包的 ``__init__`` 会连带拉起 team/symphony 等重依赖，不能进隔离链路。
"""

from __future__ import annotations

# 模式名 → 固定模型名。与 relay 前端 GEAR_MODEL_MAP 对齐市场 id。
MODE_MODEL_MAP: dict[str, str] = {
    "fast": "deepseek-v4.1-flash",
    "balanced": "deepseek-v4.1-flash",
    "extreme": "glm-5.2",
    "auto": "deepseek-v4.1-flash",
}

# 模式名 → 思考深度。
MODE_THINKING_MAP: dict[str, str] = {
    "fast": "off",  # 关闭思考
    "balanced": "medium",  # 中等思考
    "extreme": "deep",  # 深度思考
    "auto": "medium",  # 中等思考
}

# 四档写死映射里的「官方模型」标记：relay 写 models.json 时给 maas binding 打
# model_provider='huawei_maas'。四档查找优先命中此标，避免撞到用户自定义的同名模型。
MODE_PREFERRED_PROVIDER = "huawei_maas"


def is_model_gear(name: object) -> bool:
    """``name`` 是否为档位关键字（忽略大小写与首尾空白）。"""
    return isinstance(name, str) and name.strip().lower() in MODE_MODEL_MAP


def resolve_model_gear(name: str) -> str:
    """把档位关键字翻成具体模型 id；不是档位则原样返回（含原始空白）。

    刻意比 rail 的 ``_MODE_MODEL_MAP.get(selection)`` 宽松一档（忽略大小写）：
    这里的入参来自 env，大小写不由本进程控制，宽松匹配只可能多翻、不会漏翻。
    """
    if not isinstance(name, str):
        return name
    return MODE_MODEL_MAP.get(name.strip().lower(), name)
