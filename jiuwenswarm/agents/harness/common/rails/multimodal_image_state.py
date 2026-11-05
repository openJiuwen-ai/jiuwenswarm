# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""原生多模态「本次请求实际附带了几张图」的跨 rail 状态。

为什么需要这个模块：能力位（``deep_config.enable_read_image_multimodal``）只说明
模型**能**看图，不代表**这次**真的把像素发出去了。像素注入可能因预算 / 引用行解析 /
读取失败而产出 0 张，此时模型看不到图，视觉工具（VQA / image_reading）是唯一兜底，
按能力位一律拦截等于把唯一可用路径封死。所以：

- 发送侧（``MultimodalImageRail.before_model_call``）注入后写入实际张数；
- 消费侧（stream rail 的 ``before_tool_call`` 与 progressive rail 的
  ``tools_search`` / ``invoke_tool``）只认这个张数，>0 才认为像素已到模型手里。

写入两个位置，因为消费侧拿到的 ctx 不一样：
- ``ctx.extra``：agent-core 的 tool ctx 与 ReAct ctx 共享同一个 extra dict，
  工具钩子里可直接读；
- agent 属性：``invoke_tool`` 走 ProgressiveToolRail 的工具执行，没有 rail ctx，
  只能从 agent 实例上读。

可见范围：两个位置都是"每轮模型调用刷新 + ``before_invoke`` 归零"，因此不会跨请求
残留（否则上一张图会把这一轮的 VQA 也拦掉）。

同一模块也承载「本次实际物化成功的图片路径」请求级快照
（``PROVIDED_IMAGE_PATHS_KEY`` / ``mark_provided_image_paths`` /
``provided_image_paths``）：read_file 护栏按它判定"这张图已随消息提供像素、别重复读"。
快照只写 ``ctx.extra``（没有跨任务读的 agent 属性需求），且写入点是内层
``before_model_call``（N7），空列表表示"这次确实没有像素"（放行重读）。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.utils import logger

# ctx.extra 键与 agent 属性同名，便于排障时一处 grep。
NATIVE_IMAGES_SENT_KEY = "_jiuwenswarm_native_images_sent"

# ctx.extra 键：本次模型调用**实际物化成功**的图片路径快照（list[str]）。
# 与"上传清单"（ContextVar ``_CURRENT_MULTIMODAL_IMAGE_FILES``）不同：上传了不代表
# 像素进了消息（预算淘汰 / 引用行解析失败 / 读文件失败都可能 0 张），read_file 护栏
# 只有看到这张图真的 attached 才能拒绝重复读。
PROVIDED_IMAGE_PATHS_KEY = "_jiuwenswarm_provided_image_paths"

_AGENT_OWNER_ATTRS = ("_deep_agent", "_config_agent", "_runtime_agent")


def mark_provided_image_paths(
    ctx: Any, paths: list[str] | None, agent: Any = None
) -> list[str]:
    """把「本次模型调用实际附带像素的图片路径」写入 ``ctx.extra`` 快照。

    空列表是**权威事实**（这次确实没有像素），不是"没写"——调用方据此放行 read_file
    重读；``None`` 只表示入参缺省（归一成空列表），不是"未知"。

    与 ``native_images_sent`` 同理由，镜像到 agent 属性：``invoke_tool`` /
    ``tools_search`` 执行时没有 rail ctx，视觉工具护栏只能从 agent 实例上读。
    """

    normalized: list[str] = []
    for item in paths or []:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        extra[PROVIDED_IMAGE_PATHS_KEY] = normalized
    targets = [agent, getattr(ctx, "agent", None)]
    for target in targets:
        if target is None:
            continue
        try:
            setattr(target, PROVIDED_IMAGE_PATHS_KEY, normalized)
        except Exception:  # pragma: no cover - 冻结对象 / 属性拦截时降级为只写 extra
            logger.debug(
                "[multimodal_image_state] set provided paths attr failed: %r", target
            )
    return normalized


def provided_image_paths(ctx: Any = None, agent: Any = None) -> list[str] | None:
    """读取已提供图片路径快照；``None`` 表示本次请求尚未写入（调用方自行回退）。

    空列表与 ``None`` 语义不同：空列表 = 已确认这次没有像素；``None`` = 未知。
    优先读 ``ctx.extra``，读不到再回退 agent 属性（invoke_tool 无 ctx 场景）。
    """

    extra = getattr(ctx, "extra", None) if ctx is not None else None
    if isinstance(extra, dict):
        cached = extra.get(PROVIDED_IMAGE_PATHS_KEY)
        if isinstance(cached, list):
            return [str(item).strip() for item in cached if str(item or "").strip()]
    for target in (agent, getattr(ctx, "agent", None) if ctx is not None else None):
        if target is None:
            continue
        value = _agent_list_attr(target, PROVIDED_IMAGE_PATHS_KEY)
        if value is not None:
            return value
    return None


def mark_native_images_sent(
    ctx: Any = None,
    agent: Any = None,
    count: int = 0,
) -> int:
    """记录本次模型调用实际附带（视图中可见）的图片张数。

    ``count`` 由发送侧按注入后的历史统计，含历史里已有的图片块，因此同一轮第二次
    LLM 调用（``attached=0``）也能得到正确的"视图里有图"结论。
    """

    value = max(0, int(count or 0))
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        extra[NATIVE_IMAGES_SENT_KEY] = value
    targets = [agent, getattr(ctx, "agent", None)]
    for target in targets:
        if target is None:
            continue
        try:
            setattr(target, NATIVE_IMAGES_SENT_KEY, value)
        except Exception:  # pragma: no cover - 冻结对象 / 属性拦截时降级为只写 extra
            logger.debug(
                "[multimodal_image_state] set agent attr failed: %r", target
            )
    return value


def native_images_sent(ctx: Any = None, agent: Any = None) -> int:
    """读取当前请求实际附带的图片张数；未知一律按 0（= 没发出去）。"""

    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        cached = extra.get(NATIVE_IMAGES_SENT_KEY)
        if isinstance(cached, (int, float)):
            return max(0, int(cached))
    for target in (agent, getattr(ctx, "agent", None)):
        value = _agent_attr(target)
        if value is not None:
            return value
    return 0


def _iter_agent_owners(agent: Any) -> list[Any]:
    """把 agent 及其可能持有 deep_config 的包装对象铺平，供属性读取去重遍历。"""

    if agent is None:
        return []
    owners: list[Any] = [agent]
    for attr in _AGENT_OWNER_ATTRS:
        owners.append(getattr(agent, attr, None))
    owners.append(getattr(agent, "react_agent", None))
    owners.append(getattr(agent, "_react_agent", None))
    seen: set[int] = set()
    deduped: list[Any] = []
    for owner in owners:
        if owner is None or id(owner) in seen:
            continue
        seen.add(id(owner))
        deduped.append(owner)
    return deduped


def _agent_attr(agent: Any) -> int | None:
    for owner in _iter_agent_owners(agent):
        value = getattr(owner, NATIVE_IMAGES_SENT_KEY, None)
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return None


def _agent_list_attr(agent: Any, key: str) -> list[str] | None:
    """读 agent 树上的 list 属性（如 provided image paths），命中即返回归一列表。"""

    for owner in _iter_agent_owners(agent):
        value = getattr(owner, key, None)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item or "").strip()]
    return None
