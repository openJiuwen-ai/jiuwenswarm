# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Multimodal image input adaptation for Core model calls."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.llm import SystemMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentKind
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.multimodal import (
    collect_image_references_from_messages,
    collect_provided_image_paths,
    count_image_content_blocks,
    current_multimodal_image_files_from_agent,
    ensure_multimodal_image_filter_window_mutator,
    inject_image_content_from_references,
    remove_multimodal_image_filter_window_mutator,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_state import (
    mark_native_images_sent,
    mark_provided_image_paths,
)
from jiuwenswarm.common.utils import logger

# 运行时附件里承载「本次模型调用到底带了几张图」的 section。系统提示里的策略段落在
# 提示尾部、是否渲染不可观测，而运行时附件每次调用重建且模型必读，用它兜住负载事实。
_IMAGE_STATE_ATTACHMENT_SECTION = "runtime.image_state"
_IMAGE_STATE_ATTACHMENT_SOURCE = "jiuwenswarm.multimodal_image_rail"
_IMAGE_STATE_SYNC_WINDOW_MUTATOR_ATTR = "_jiuwenswarm_multimodal_image_state_sync_mutator"


class MultimodalImageRail(DeepAgentRail):
    """Prepare image attachments for native multimodal model input.

    发送侧唯一决策点：``before_model_call`` 在 agent-core 构建窗口（含压缩处理器）
    **之前**把本轮引用行物化成历史里的 ``image_url`` 块，因此新上传的图片从第一次
    LLM 调用起就按视觉 token 计入窗口，压缩判定看到的是含图上下文；模型不支持
    原生看图时历史不动，只在视图副本上剥离（由 agent-core 发送侧策略兜底）。

    时序约束（priority=82）：agent-core 按 priority **降序**执行 rail 回调
    （``framework.py`` ``sort(..., reverse=True)``；``AgentRail.priority`` 文档
    "higher = first"）。本 rail 是"本次到底发了几张像素"的唯一写入方，所有读该
    事实的消费者都必须在它之后跑：``ProgressiveToolRail`` 的 ``before_model_call``
    要按张数把视觉工具从 schema 摘掉、``JiuSwarmStreamEventRail`` 的工具钩子要按
    张数放行/拦截 read_file 与视觉工具。若本 rail 排在它们之后，首次 LLM 调用时
    ``_jiuwenswarm_native_images_sent`` 仍是 ``before_invoke`` 归零后的 0，像素虽
    随后注入成功，视觉工具第一次也不会被摘除（模型会白调一轮 VQA/OCR）。82 的取值
    避开 deepresearch(81) 与 task_execution(85)，确保严格中间位、无并列不确定顺序。
    """

    priority = 82

    def __init__(
        self,
        enable_image_multimodal: bool | None = None,
        *,
        max_total_bytes: int = 0,
        max_visual_tokens: int = 0,
        max_count: int = 0,
    ) -> None:
        super().__init__()
        self._enable_image_multimodal = enable_image_multimodal
        self._max_total_bytes = max(0, int(max_total_bytes or 0))
        self._max_visual_tokens = max(0, int(max_visual_tokens or 0))
        self._max_count = max(0, int(max_count or 0))
        self._deep_agent: Any | None = None
        self._system_prompt_builder: Any | None = None
        self._attachment_manager: Any | None = None

    def init(self, agent: Any) -> None:
        self._deep_agent = agent
        self._system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._attachment_manager = getattr(agent, "prompt_attachment_manager", None)

    def _read_image_multimodal_enabled(self) -> bool:
        if self._enable_image_multimodal is not None:
            return self._enable_image_multimodal
        deep_config = (
            getattr(self._deep_agent, "deep_config", None)
            or getattr(self._deep_agent, "_deep_config", None)
        )
        return bool(getattr(deep_config, "enable_read_image_multimodal", False))

    def _sync_image_policy_section(self, enabled: bool, images_sent: int) -> None:
        """按「这次到底有没有把像素发出去」写策略段。

        ``enabled=True`` 只说明模型**能**看图，不代表这次真发了。预算 / 引用行解析
        失败时视图里可能一张图都没有，此时再写"图片已作为图像内容直接提供、不要调用
        VQA"就是与真实负载相反的信号——模型会既看不到图、又不敢用兜底工具，最后
        只能拿 OCR 文本作答。原生模式下没有像素时干脆不写本段，让视觉工具兜底。
        """

        builder = self._system_prompt_builder
        if builder is None:
            return
        section_name = "multimodal_image_policy"
        try:
            builder.remove_section(section_name)
        except Exception:
            # 段不存在或 builder 未实现移除时不影响本轮注入，仅记录便于定位。
            logger.warning("remove %s section failed", section_name, exc_info=True)
        if enabled and images_sent <= 0:
            return
        native_cn = (
            "本次请求可能包含用户上传的图片。当前模型支持原生图片理解，"
            "图片已作为图像内容直接提供；请直接使用你的视觉能力查看图片，"
            "不要调用 visual_question_answering 或 image_reading 工具。"
        )
        native_en = (
            "This request may contain user-uploaded images. The current model supports "
            "native image understanding and the images are provided as image content. "
            "Use your vision capability directly; do not call visual_question_answering "
            "or image_reading tools."
        )
        fallback_cn = (
            "本次请求可能包含用户上传的图片。当前模型不支持原生图片理解；"
            "如需理解图片内容，请调用 visual_question_answering 或 image_reading 工具。"
        )
        fallback_en = (
            "This request may contain user-uploaded images. The current model does not "
            "support native image understanding; to understand image content, call the "
            "visual_question_answering or image_reading tool."
        )
        try:
            builder.add_section(PromptSection(
                name=section_name,
                content={"cn": native_cn if enabled else fallback_cn,
                         "en": native_en if enabled else fallback_en},
                priority=96,
            ))
        except Exception:
            # 策略段是提示增强，写失败只降级（模型仍能靠运行时附件拿到负载事实），
            # 不得让 before_model_call 整体失败——那会连像素注入一起丢。
            logger.warning("add %s section failed", section_name, exc_info=True)

    def _mark_images_sent(self, ctx: AgentCallbackContext, count: int) -> None:
        """把「本次实际附带张数」写给工具护栏（stream rail / progressive rail）。"""

        mark_native_images_sent(ctx, self._deep_agent, count)

    def _snapshot_provided_image_paths(
        self, ctx: AgentCallbackContext, messages: list[Any] | None
    ) -> list[str]:
        """把「本次实际随消息提供像素的图片路径」固化到 ``ctx.extra``（N7）。

        与上传清单不同：预算淘汰 / 引用行解析失败 / 读文件失败都可能让上传的图
        一张都没进消息，此时 read_file 必须放行（模型只能靠重读拿图）。因此快照
        按注入后的历史实际 ``attached`` 状态计算，``messages=None``（读历史失败 /
        无 context）与解析异常一律写空列表——"这次确实没有像素"是权威事实。
        """

        paths: list[str] = []
        if messages is not None:
            try:
                paths = collect_provided_image_paths(list(messages))
            except Exception:  # pragma: no cover - 防御：解析异常按"没有像素"处理
                logger.warning(
                    "[MultimodalImageRail] collect provided image paths failed",
                    exc_info=True,
                )
                paths = []
        return mark_provided_image_paths(ctx, paths, agent=self._deep_agent)

    def _attachment_language(self) -> str:
        return str(getattr(self._system_prompt_builder, "language", "") or "cn")

    def _build_image_state_attachment(
        self,
        *,
        enabled: bool,
        images_sent: int,
        provided_paths: list[str] | None = None,
    ) -> str:
        """构造「本次请求图片负载」附件正文。

        只陈述事实 + 与该事实一致的下一步：有像素就直说可以直接看、别再调视觉/OCR
        工具；没像素就直说这次没图、需要时用视觉工具。经验库里的 TIP 是按旧模型写的，
        模型只有看到这条事实才不用靠经验猜（B_01：切原生后首轮仍调 VQA，白花一轮）。

        重复读取限制只落在「实际随请求提供像素」的图片上（``provided_paths`` 非空时才
        写），与 read_file 按目标放行护栏（vision_tool_targets_all_provided）保持一致，
        避免劝模型"别重读"却没限定范围，导致没进消息的图既看不到又不被重读。
        """

        provided = [p for p in (provided_paths or []) if p]
        if self._attachment_language() == "en":
            if enabled and images_sent > 0:
                parts = [
                    "# Images In This Request",
                    "",
                    f"- This request carries {images_sent} image(s), already provided as image "
                    "content: inspect the pixels directly to answer.",
                    "- Do not call visual_question_answering / image_reading / image_ocr to look "
                    "again.",
                ]
                if provided:
                    parts.append(
                        f"- Do not read_file these {images_sent} provided image(s) again; "
                        "use read_file for any other image you need to inspect."
                    )
                parts.append(
                    "- Read text, watermarks and IDs in the image from the pixels you received."
                )
                return "\n".join(parts)
            return (
                "# Images In This Request\n\n"
                "- This request carries 0 images as image content (no pixels were attached).\n"
                "- To understand image content, call a vision tool such as "
                "visual_question_answering; do not guess from context."
            )
        if enabled and images_sent > 0:
            parts = [
                "# 本次请求的图片",
                "",
                f"- 本次请求附带图片：{images_sent} 张，已作为图像内容直接提供，可直接查看像素作答。",
                "- 不要调用 visual_question_answering / image_reading / image_ocr 再「看一遍」。",
            ]
            if provided:
                parts.append(
                    f"- 也不要 read_file 重新读取这 {images_sent} 张已随请求提供像素的图片；"
                    "其它图片如需查看请正常使用 read_file。"
                )
            parts.append("- 图中文字、水印、编号以你收到的像素为准。")
            return "\n".join(parts)
        return (
            "# 本次请求的图片\n\n"
            "- 本次请求附带图片：0 张（图片未作为像素附带）。\n"
            "- 需要理解图片内容时请调用 visual_question_answering 等图片理解工具，"
            "不要凭上下文猜测。"
        )

    async def _sync_image_state_attachment(
        self,
        ctx: AgentCallbackContext,
        *,
        enabled: bool,
        images_sent: int,
        has_image_references: bool,
        provided_paths: list[str] | None = None,
    ) -> None:
        """把负载事实写进运行时附件（best-effort，写失败不阻断模型调用）。"""

        manager = self._attachment_manager
        if manager is None:
            # DeepAgent 把同一个 PromptAttachmentManager 同时挂到内层 ReActAgent，两处
            # 取到的实例一致；优先用 init 时捕获的那份（与 RuntimePromptRail 同源）。
            manager = getattr(ctx.agent, "prompt_attachment_manager", None)
        if manager is None:
            return
        try:
            writer = manager.bind_context(ctx)
            if not has_image_references and images_sent <= 0:
                # 附件按会话存续：本轮没有图必须清掉上一轮的行，否则模型读到过期负载。
                await writer.clear_section(_IMAGE_STATE_ATTACHMENT_SECTION)
                return
            await writer.add_section(
                _IMAGE_STATE_ATTACHMENT_SECTION,
                self._build_image_state_attachment(
                    enabled=enabled,
                    images_sent=images_sent,
                    provided_paths=provided_paths,
                ),
                PromptAttachmentKind.RUNTIME,
                _IMAGE_STATE_ATTACHMENT_SOURCE,
                priority=96,
                content_kind="text/markdown",
            )
        except ValueError as exc:
            logger.warning(
                "[MultimodalImageRail] skip image state attachment (no session id): %s", exc
            )
        except Exception as exc:  # pragma: no cover - 旁路失败不阻断
            logger.warning("[MultimodalImageRail] write image state attachment failed: %s", exc)

    def _remove_image_state_sync_window_mutator(self, ctx: AgentCallbackContext) -> bool:
        """摘掉上一轮安装的最终窗口同步 mutator（跨请求复用需按当前 ctx 重装）。"""

        mutators = getattr(ctx.context, "_window_mutators", None)
        if not isinstance(mutators, list):
            return False
        before = len(mutators)
        mutators[:] = [
            mutator
            for mutator in mutators
            if not bool(getattr(mutator, _IMAGE_STATE_SYNC_WINDOW_MUTATOR_ATTR, False))
        ]
        return len(mutators) != before

    def _install_image_state_sync_window_mutator(
        self, ctx: AgentCallbackContext, enabled: bool
    ) -> bool:
        """安装压缩后按最终窗口重写图片状态提示（策略段 + runtime.image_state）。

        ``before_model_call`` 在 ``get_context_window`` 压缩**之前**按完整历史写策略段
        与附件；若 FullCompact 随后把含图历史搬走，系统提示与附件仍宣称"已提供 N 张、
        别调 VQA"，模型实际收到 0 张。context 引擎的 window mutator 在压缩之后、附件
        注入之前执行，这里借它按最终窗口重算，并重建系统提示 / 重写附件，保证发给模型
        的提示与实际负载一致。工具护栏（``mark_native_images_sent`` / provided paths）
        仍由 ``after_model_call`` 按最终窗口归位，本 mutator 不重复写。
        """

        mutators = getattr(ctx.context, "_window_mutators", None)
        if not isinstance(mutators, list):
            return False
        # 幂等：每轮 before_model_call 重装，替换旧闭包（旧 ctx / enabled 已过期）。
        self._remove_image_state_sync_window_mutator(ctx)

        # 在方法作用域捕获 bound helper：闭包运行在 context 引擎的 window-mutator
        # 循环里，不直接触碰 rail 的受保护成员（G.CLS.11）。
        sync_policy = self._sync_image_policy_section
        sync_attachment = self._sync_image_state_attachment
        system_prompt_builder = self._system_prompt_builder

        async def _image_state_sync_window_mutator(_context: Any, window: Any) -> Any:
            context_messages = list(getattr(window, "context_messages", []) or [])
            final_images_sent = count_image_content_blocks(context_messages)
            final_has_refs = bool(
                collect_image_references_from_messages(context_messages)
            )
            final_provided = collect_provided_image_paths(context_messages)

            # 策略段按最终窗口重写：0 张删段（放行视觉工具兜底），>0 张写"别调 VQA"。
            sync_policy(enabled, final_images_sent)
            builder = system_prompt_builder
            if builder is not None:
                new_system = builder.build()
                model_copy = getattr(window, "model_copy", None)
                if callable(model_copy):
                    window = model_copy(
                        update={"system_messages": [SystemMessage(content=new_system)]}
                    )

            await sync_attachment(
                ctx,
                enabled=enabled,
                images_sent=final_images_sent,
                has_image_references=final_has_refs,
                provided_paths=final_provided,
            )
            return window

        setattr(
            _image_state_sync_window_mutator,
            _IMAGE_STATE_SYNC_WINDOW_MUTATOR_ATTR,
            True,
        )
        mutators.append(_image_state_sync_window_mutator)
        return True

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        enabled = self._read_image_multimodal_enabled()
        logger.info(
            "[MultimodalImageRail] before_model_call ctx_present=%s context_present=%s enabled=%s",
            ctx is not None,
            ctx is not None and ctx.context is not None,
            enabled,
        )
        if ctx.context is None:
            logger.info("[MultimodalImageRail] skip install: ctx.context is None")
            self._mark_images_sent(ctx, 0)
            # 无 context 时无法确认历史里的像素负载：按"这次没有像素"写快照，
            # read_file 重读放行（模型手里没有图，不能既不给图又禁止取图）。
            self._snapshot_provided_image_paths(ctx, None)
            self._sync_image_policy_section(enabled, 0)
            await self._sync_image_state_attachment(
                ctx, enabled=enabled, images_sent=0, has_image_references=False
            )
            return
        if enabled:
            # 先摘掉上一轮按非视觉模型安装的视图过滤 mutator：同一 ContextEngine
            # 跨模型热切换时不撤销，会把下面刚注入的像素在视图层再剥掉，模型实际
            # 收不到图（表现为绕过原生能力去调 VQA / read_file）。
            if remove_multimodal_image_filter_window_mutator(ctx.context):
                logger.info(
                    "[MultimodalImageRail] removed stale image filter mutator "
                    "after switching to native image input"
                )
            # 装最终窗口同步 mutator：压缩后按实际发给模型的消息重写策略段 + runtime
            # image_state 附件，避免压缩搬走含图历史后提示词还宣称"已提供 N 张"。
            self._install_image_state_sync_window_mutator(ctx, enabled)
            # 在窗口构建之前就地写入历史：像素进入本次请求的 token 计量，压缩判定
            # 才能看到本轮新上传的图片（需求「压缩之前按视觉 token 把图片计入窗口」）。
            try:
                messages = ctx.context.get_messages()
            except Exception as exc:  # pragma: no cover - defensive context guard
                logger.warning("[MultimodalImageRail] read messages failed: %s", exc)
                self._mark_images_sent(ctx, 0)
                self._snapshot_provided_image_paths(ctx, None)
                self._sync_image_policy_section(enabled, 0)
                await self._sync_image_state_attachment(
                    ctx, enabled=enabled, images_sent=0, has_image_references=False
                )
                return
            has_image_references = bool(
                collect_image_references_from_messages(list(messages))
            )
            try:
                messages, attached = inject_image_content_from_references(
                    list(messages),
                    image_files=current_multimodal_image_files_from_agent(
                        self._deep_agent
                    ),
                    max_total_bytes=self._max_total_bytes,
                    max_visual_tokens=self._max_visual_tokens,
                    max_count=self._max_count,
                )
            except Exception as exc:  # pragma: no cover - 失败降级不阻断（N9）
                logger.warning("[MultimodalImageRail] image injection failed: %s", exc)
                self._mark_images_sent(ctx, 0)
                # 注入中途失败：无法确认哪几张写进了历史，按"没有像素"处理（放行重读），
                # 比误判"已提供"导致图既看不到又读不到安全。
                self._snapshot_provided_image_paths(ctx, None)
                self._sync_image_policy_section(enabled, 0)
                await self._sync_image_state_attachment(
                    ctx,
                    enabled=enabled,
                    images_sent=0,
                    has_image_references=has_image_references,
                )
                return
            # attached 只是本轮新增；策略段 / 视觉工具护栏要的是「视图里有没有像素」，
            # 同一轮第二次 LLM 调用 attached=0 但历史里像素仍在，所以按实际块计数。
            images_sent = count_image_content_blocks(messages)
            self._mark_images_sent(ctx, images_sent)
            # read_file 护栏按"实际物化成功"的路径集合判定重复读：上传了但没进消息的
            # 图（预算淘汰 / 读取失败）不能被当成已提供，否则模型既看不到像素又被禁止
            # 重读，图彻底不可得。
            provided_paths = self._snapshot_provided_image_paths(ctx, messages)
            self._sync_image_policy_section(enabled, images_sent)
            await self._sync_image_state_attachment(
                ctx,
                enabled=enabled,
                images_sent=images_sent,
                has_image_references=has_image_references,
                provided_paths=provided_paths,
            )
            logger.info(
                "[MultimodalImageRail] injected attached=%d visible=%d provided=%s "
                "budget_bytes=%s budget_visual_tokens=%s budget_count=%s",
                attached,
                images_sent,
                provided_paths,
                self._max_total_bytes,
                self._max_visual_tokens,
                self._max_count,
            )
            return

        self._remove_image_state_sync_window_mutator(ctx)
        self._mark_images_sent(ctx, 0)
        # 非原生分支不向消息写像素（历史里的 image 块只在视图副本上剥离），快照写空。
        self._snapshot_provided_image_paths(ctx, None)
        self._sync_image_policy_section(enabled, 0)
        has_image_references = False
        try:
            has_image_references = bool(
                collect_image_references_from_messages(list(ctx.context.get_messages()))
            )
        except Exception as exc:  # pragma: no cover - defensive context guard
            logger.warning("[MultimodalImageRail] read messages failed: %s", exc)
        await self._sync_image_state_attachment(
            ctx,
            enabled=enabled,
            images_sent=0,
            has_image_references=has_image_references,
        )
        installed = ensure_multimodal_image_filter_window_mutator(ctx.context)
        logger.info("[MultimodalImageRail] filter mutator installed=%s", installed)
        if installed:
            logger.info(
                "Installed multimodal image filter window mutator (view-level)"
            )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """窗口（含压缩）+ 发送侧策略之后、本轮工具调用之前，按最终窗口重算图片负载。

        ``before_model_call`` 在 ``get_context_window`` 之前按**完整历史**算
        ``images_sent`` / ``provided_paths``；若压缩随后把含图历史搬走（FullCompact），
        缓存仍宣称"模型已收到图"——模型既看不到像素、read_file 又被重复读护栏拒绝，
        图片彻底不可得。``ctx.inputs.messages`` 在 ``_railed_model_call`` 里已被更新为
        实际发出去的消息（压缩 + ``_apply_image_input_policy`` 之后），这里按它重算，
        让工具钩子（read_file 重复读 / 视觉工具拦截）看到最终窗口的事实。
        """

        if not self._read_image_multimodal_enabled():
            # 非原生分支从未物化像素，before_model_call 已写 0 / 空，无需重算。
            return
        messages = getattr(getattr(ctx, "inputs", None), "messages", None)
        if not isinstance(messages, list):
            return
        try:
            images_sent = count_image_content_blocks(list(messages))
            self._mark_images_sent(ctx, images_sent)
            self._snapshot_provided_image_paths(ctx, messages)
        except Exception as exc:  # pragma: no cover - 防御：读不到最终窗口时保留较早快照
            logger.warning(
                "[MultimodalImageRail] recompute final-window image state failed: %s",
                exc,
            )
            return
        logger.info(
            "[MultimodalImageRail] after_model_call final_window_images=%d", images_sent
        )
