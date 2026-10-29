# Gateway 侧副本，源文件 jiuwenswarm/common/config_panel/models_handlers.py；改动请同步两侧。
# 本副本仅保留 gateway 实际引用的符号；同步母仓时请勿整体覆盖。
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Web 侧 models/config 域 handler 下沉实现（自 gateway app_web_handlers 迁出）。

gateway 拆分：ConfigAdapter（AgentServer 进程）复用这些成熟实现时不再 import
``gateway.channel_manager.web.app_web_handlers``。gateway 侧 ``_register_web_handlers``
通过 ``gateway.common.config_panel.models_handlers`` 间接消费同一实现，
保持单一实现源（过渡期 gateway 仓持 common 副本）。

对外入口：
- ``register_models_handlers(channel, *, on_config_saved=None, agent_client=None)``：
  在给定 channel 上注册 ``models.list`` / ``models.replace_all`` /
  ``config.validate_model`` / ``models.validate``（含 AgentOS 多用户 E2A 代理
  分叉语义——由 gateway 侧 ``_register_config_proxy`` 包装，本模块仅提供
  本地 handler）。
- 纯函数（``build_models_defaults_from_frontend`` 等）供单测与 TUI/Web 通道
  模块复用。
"""

from __future__ import annotations

import logging
from typing import Any


from gateway.common.config import (
    get_config,
    get_default_models,
)
from gateway.common.reasoning_injector import (
    core_has_context_window_field,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 纯工具函数（自 app_web_handlers 迁出，与 TUI 侧实现保持契约一致）
# --------------------------------------------------------------------------- #


# 供旧调用方/单测引用的兼容别名（原 app_web_handlers 私有符号）


def resolve_model_config_obj_for_validate(
    model_name: str, params: dict[str, Any]
) -> dict[str, Any]:
    """从热更新后的配置中查找对应模型的 ``model_config_obj``。

    按 ``model_name`` 或 ``alias`` 匹配 ``models.defaults`` 中的条目，
    返回该条目的 ``model_config_obj``；找不到或读取失败时 fallback 到
    空字典（让模型使用自身默认参数，避免 ``temperature=0`` 对部分
    模型不兼容）。前端传入的 ``reasoning_level`` 可覆盖配置值。
    """
    model_config_obj: dict[str, Any] = {}
    try:
        _cfg = get_config()
        _models = get_default_models(_cfg)
        for entry in _models:
            if not isinstance(entry, dict):
                continue
            mcc = entry.get("model_client_config") or {}
            entry_model_name = str(mcc.get("model_name", "")).strip()
            entry_alias = str(entry.get("alias", "")).strip()
            if entry_model_name == model_name or entry_alias == model_name:
                obj = entry.get("model_config_obj")
                if isinstance(obj, dict):
                    model_config_obj = dict(obj)
                # context_window（模型支持的上下文总长度）可配在任意模型条目的
                # model_config_obj 里（defaults / agentos / video / audio / vision /
                # image_gen 均可），供 core 从 ModelRequestConfig 取值。是否在出口
                # 清掉取决于 core 是否已把 context_window 加为 ModelRequestConfig
                # 正式字段（见 reasoning_injector.core_has_context_window_field）：
                # - core 未加字段（过渡期）：context_window 进 extra 会被
                #   base_model_client 经 model_dump 透传给厂商 SDK 报 unexpected
                #   keyword argument -> 需清。
                # - core 已加字段：context_window 作正式字段，core 自行 exclude
                #   不发厂商、可读 -> 不清（否则切掉 core 想读的值）。
                # 不再守 _source=="agentos"：所有条目一视同仁，defaults 配了
                # context_window 同样需要过渡期清防发厂商。_source 标记本身由
                # reasoning_injector._build_model_request_kwargs 统一 pop；此处与
                # 公共出口同口径，覆盖绕过 build_model_from_entry 的 validate 路径。
                if not core_has_context_window_field():
                    model_config_obj.pop("context_window", None)
                logger.info(
                    "[config.validate_model] loaded model_config_obj for '%s' "
                    "(matched_by=%s): %s",
                    model_name,
                    "model_name" if entry_model_name == model_name else "alias",
                    model_config_obj,
                )
                break
        else:
            logger.info(
                "[config.validate_model] no model_config_obj found for '%s', using empty default",
                model_name,
            )
    except Exception as exc:
        logger.warning(
            "[config.validate_model] failed to read model_config_obj from config, using default. %s",
            exc,
        )
    if "reasoning_level" in params:
        model_config_obj["reasoning_level"] = params.get("reasoning_level")
    return model_config_obj


def _get_crypto_provider() -> Any:
    """经 common 钩子取 crypto provider（未注册/未就绪返回 None）。

    gateway 进程由 ``extensions.registry`` 在 create_instance 时安装桥接；
    AgentServer 进程无 crypto 扩展时 api_key 以明文落库（与原实现一致）。
    """
    try:
        from gateway.extensions.base_crypto import get_crypto_provider

        return get_crypto_provider()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# handler 实现（channel 语义与 app_web_handlers 原实现一致）
# --------------------------------------------------------------------------- #
