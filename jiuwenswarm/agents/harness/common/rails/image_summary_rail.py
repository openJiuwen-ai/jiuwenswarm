# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Queue background summaries for images read into context during a turn."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.context_engine.image_refs import (
    ImageRef,
    collect_image_refs,
    image_key,
    set_image_summary_provider,
)
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.image_summary import (
    ImageSummaryScheduler,
    SessionImageSummaryProvider,
)
from jiuwenswarm.common.utils import logger


class ImageSummaryRail(DeepAgentRail):
    """Summarize read_file images in the background, for offloader folding.

    两个触发点：
    - ``after_tool_call``（快路径）：read_file 返回 multimodal image item 即
      有 base64——图片已进模型上下文的直接证据（read_file 的
      enable_image_multimodal 生效态），立即按 source_path + image_key 调度，
      不依赖声明判定，declared 缺声明（models.json 未接线等）时不误伤。
    - ``after_invoke``（兜底）：扫上下文收集遗漏的图（用户附件注入等），
      只有多模态主模型才需要摘要——门控用 ``_jiuwenswarm_declared_image_support``，
      由 ``model_capabilities.resolve_image_support`` 按「model 的 input 声明里
      是否带 image」刷新（True = 多模态；False = 明确非多模态；None = 未知，保守跳过）。
    摘要模型端点与主对话同通道：按主模型 api_base 分流，np:// 时走命名管道。
    """

    priority = 70

    def __init__(self) -> None:
        super().__init__()
        provider = SessionImageSummaryProvider()
        self._scheduler = ImageSummaryScheduler(provider)
        set_image_summary_provider(provider)
        # DeepAgent 引用：AFTER_TOOL_CALL 桥接到内层 ReActAgent 触发，
        # 届时 ctx.agent 是 ReActAgent（无 deep_config），凭证必须从这里取。
        self._deep_agent: Any | None = None

    def init(self, agent: Any) -> None:
        self._deep_agent = agent

    def _resolve_agent(self, ctx: AgentCallbackContext) -> Any:
        """桥接事件（after_tool_call）的 ctx.agent 是内层 ReActAgent，优先用 DeepAgent。"""
        return self._deep_agent or ctx.agent

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """read_file 返回图片后立即调度摘要（multimodal item 即 base64 在场）。"""
        context = ctx.context
        agent = self._resolve_agent(ctx)
        if context is None:
            return
        try:
            tool_result = getattr(ctx.inputs, "tool_result", None)
            data = getattr(tool_result, "data", None)
            items = data.get("multimodal") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return
            refs: list[ImageRef] = []
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "image":
                    continue
                data_url = item.get("data_url")
                if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
                    continue
                path = str(item.get("source_path") or "").strip()
                if not path:
                    continue
                # key 与 react_agent build_image_entry 写入 metadata 的一致
                # （sha1(data_url)），折叠渲染摘要时按 key 命中。
                refs.append(
                    ImageRef(
                        path=path,
                        key=image_key(data_url),
                        width=item.get("width"),
                        height=item.get("height"),
                    )
                )
            if not refs:
                return
            model_credential = self._model_credential_from_agent(agent)
            self._scheduler.schedule(
                context.session_id(), refs, model_credential=model_credential
            )
        except Exception as exc:
            logger.warning(
                "[ImageSummaryRail] failed to queue image summaries from tool result: %s", exc
            )

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        context = ctx.context
        agent = self._resolve_agent(ctx)
        if context is None:
            return
        try:
            refs = [
                ref for ref in collect_image_refs(context.get_messages()) if ref.key
            ]
            if not refs:
                return
            # 仅当轮主模型被判定为多模态（model 的 input 声明带 image，见
            # model_capabilities.resolve_image_support）才调度摘要；非多模态
            # 模型图片会被剥出模型上下文，折叠与摘要均无意义。
            declared = getattr(agent, "_jiuwenswarm_declared_image_support", None)
            if declared is not True:
                logger.info(
                    "[ImageSummaryRail] skip %d image(s): model not multimodal (declared=%s)",
                    len(refs),
                    declared,
                )
                return
            model_credential = self._model_credential_from_agent(agent)
            self._scheduler.schedule(
                context.session_id(), refs, model_credential=model_credential
            )
        except Exception as exc:
            logger.warning(
                "[ImageSummaryRail] failed to queue image summaries: %s", exc
            )

    @staticmethod
    def _model_credential_from_agent(agent: Any) -> Any | None:
        """Build a VisionModelConfig from the agent's current main model.

        经 np 管道调摘要时只需 ``model``（代理按它路由上游）；``api_key`` 允许为空
        （桌面形态令牌经密钥包下发，由 image_summary._proxy_api_key 回退）；凭证缺
        base_url/model_name 时返回 None——没有可路由的模型就跳过摘要。
        """
        from openjiuwen.harness import VisionModelConfig

        deep_config = getattr(agent, "deep_config", None) or getattr(
            agent, "_deep_config", None
        )
        model = getattr(deep_config, "model", None)
        if model is None:
            return None
        client_config = getattr(model, "model_client_config", None)
        request_config = getattr(model, "model_config", None)
        api_key = str(getattr(client_config, "api_key", "") or "").strip()
        base_url = str(getattr(client_config, "api_base", "") or "").strip()
        model_name = str(getattr(request_config, "model_name", "") or "").strip()
        # 经 np 管道调摘要时只需 base_url 与 model_name（代理按它们路由上游；
        # api_key 允许为空串，由 image_summary._proxy_api_key 回退到 proxyKey）。
        missing = [
            name
            for name, value in (("base_url", base_url), ("model_name", model_name))
            if not value
        ]
        if missing:
            logger.warning(
                "[ImageSummaryRail] main-model credential incomplete, summary skipped, missing: %s",
                ", ".join(missing),
            )
            return None
        return VisionModelConfig(api_key=api_key, base_url=base_url, model=model_name)
