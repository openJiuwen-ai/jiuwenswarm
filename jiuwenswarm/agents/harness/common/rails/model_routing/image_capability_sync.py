# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""主对话换模后，把 ``deep_config.enable_read_image_multimodal`` 同步到新模型能力。

根因：换模有两条路径——adapter 的 ``interface_deep._apply_model_to_react_agent``
会同步看图能力位，但 ``ModelRoutingRail._emit_decision`` 的自动路由在 ``before_invoke``
阶段直接 ``set_llm``，只换模型对象与名称，**不**同步能力位。入口模型声明支持图片、
自动路由随后切到文本模型时，``enable_read_image_multimodal`` 仍沿用旧值 True，导致
MultimodalImageRail 误注入像素 + 摘掉 VQA，最终文本模型收到不支持的输入。

因此这里提供单一入口，所有换模路径（adapter / 自动路由）都必须经它收敛，避免再漏
新入口。看图能力只由逐模型 ``modalities``（是否含 ``image``）决定（未声明即不支持），
与 ``model_factory._resolve_enable_read_image_multimodal`` 同口径。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.tools.model_capabilities import (
    supports_native_image,
)


def sync_agent_image_capability(host: Any, model: Any) -> bool:
    """把 ``host`` 的 ``deep_config.enable_read_image_multimodal`` 对齐到模型能力。

    ``host`` 是挂着 ``_deep_config`` / ``deep_config`` 的 DeepAgent（或包装对象），
    ``model`` 是 ``set_llm`` 之后生效的新模型。返回是否真正写入了能力位。

    自动路由构造的新模型若未显式携带 ``modalities``（``Model`` 构造默认无此字段），
    ``supports_native_image`` 归 False → 按"不支持"处理，对文本目标（如 glm-5.3）是
    正确结论；视觉目标若走该路径，须由构造方显式带上 ``modalities``（含 ``image``）。
    """

    if host is None or model is None:
        return False
    deep_config = getattr(host, "_deep_config", None) or getattr(host, "deep_config", None)
    if deep_config is None:
        return False
    enabled = supports_native_image(getattr(model, "modalities", None))
    try:
        deep_config.enable_read_image_multimodal = enabled
    except Exception:
        return False
    return True
