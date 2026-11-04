# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill 上传 multipart 处理函数注入注册表（gateway 侧 entry-point 装配点）。

背景（R3 不变量）：``gateway.*`` 禁止 import ``jiuwenswarm.server.*`` /
``jiuwenswarm.channels.*``；但 Skill 上传 multipart 处理函数
（``handle_skills_upload_temp_http`` 等）依赖 ``SkillManager``，属主仓
``server.runtime.skill.*`` 实现，不能下沉到 ``gateway.common``。

装配方式（与 ``gateway.redis`` 同模式）：主仓在 ``pyproject.toml`` 声明
entry-point 组 ``gateway.skill_upload_handlers``，指向一个返回
``SkillUploadHandlers`` 协议实现对象的工厂函数（闭包持有主仓 multipart
handlers 与 ``SkillManager`` 引用）。gateway 进程在 WebChannel 启动期经
``importlib.metadata.entry_points`` 发现并加载，注入到
``channel.skill_upload_handlers``；未声明或加载失败时返回 None，写端点
由 ``attach_skill_upload_routes`` 返回 503，资格门禁仍生效。

本模块仅依赖 stdlib + ``gateway.common``，自身不 import 主仓（R3 不变量）。
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any

logger = logging.getLogger(__name__)

#: entry-point 组名（主仓 pyproject.toml 须声明同名组）。
GATEWAY_SKILL_UPLOAD_HANDLERS_EP_GROUP = "gateway.skill_upload_handlers"
#: 默认 entry-point 名（主仓 pyproject.toml 声明时用此名）。
_DEFAULT_EP_NAME = "default"


def load_skill_upload_handlers() -> Any | None:
    """从 entry-points 加载主仓提供的 SkillUploadHandlers 实现。

    Returns:
        实现 ``SkillUploadHandlers`` 协议的对象（duck-typed），或 None
        （未声明/加载失败）。返回 None 时 ``attach_skill_upload_routes``
        对写端点返回 503；资格门禁不依赖本对象，仍独立生效。
    """
    eps: tuple[Any, ...] = ()
    try:
        eps = tuple(entry_points(group=GATEWAY_SKILL_UPLOAD_HANDLERS_EP_GROUP))
    except Exception as exc:  # noqa: BLE001 — importlib.metadata 在某些环境下可能抛意外
        logger.debug(
            "[skill_upload_handlers] entry_points(group=%s): %s",
            GATEWAY_SKILL_UPLOAD_HANDLERS_EP_GROUP,
            exc,
        )

    for ep in eps:
        # 不同 Python 版本 entry_points entry 的 name 字段一致。
        if getattr(ep, "name", None) != _DEFAULT_EP_NAME:
            continue
        try:
            factory = ep.load()
            handlers = factory()
            logger.info(
                "[skill_upload_handlers] loaded from entry-point %s", getattr(ep, "value", ep)
            )
            return handlers
        except Exception as exc:  # noqa: BLE001 — 主仓装配失败不应阻塞 gateway 启动
            logger.warning(
                "[skill_upload_handlers] entry-point %s load failed: %s; "
                "skill upload write endpoints will return 503",
                getattr(ep, "value", ep),
                exc,
            )
            return None

    logger.debug(
        "[skill_upload_handlers] no entry-point under group=%s name=%s; "
        "skill upload write endpoints will return 503",
        GATEWAY_SKILL_UPLOAD_HANDLERS_EP_GROUP,
        _DEFAULT_EP_NAME,
    )
    return None
