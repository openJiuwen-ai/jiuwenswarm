# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OfficeAceUserProfileRail — jiuwenswarm product-side rail.

周期拉取云端用户画像（AgentArts Memory D2 memory-overview）到本地缓存，
经 Rail 在 ``before_model_call`` 注入 ``office_ace_user_profile`` prompt 段。

本 rail 住在 jiuwenswarm（非 agent-core），与 ProjectMemoryRail 同构：
* agent-core 保持不动
* jiuwenswarm 可独立演进用户画像加载语义

凭据/endpoint/user_id 由 config.yaml ``memory.office_ace_user_profile`` 段提供，
relay-claw 经 env 覆盖占位符下发（配置替换，同 agentarts memory 模式）。

画像按需拉取：rail 的 ``before_model_call`` 在本地缓存失效时才 fetch，
无进程级后台周期任务。
"""
from __future__ import annotations

from .fetcher import UserProfileConfig, UserProfileFetcher
from .office_ace_user_profile_rail import OfficeAceUserProfileRail
from .section import SECTION_NAME, build_office_ace_user_profile_section

__all__ = [
    "SECTION_NAME",
    "OfficeAceUserProfileRail",
    "UserProfileConfig",
    "UserProfileFetcher",
    "build_office_ace_user_profile_section",
]
