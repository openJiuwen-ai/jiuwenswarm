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
    augment_message_with_reference_lines,
    collect_image_references_from_messages,
    collect_provided_image_paths,
    count_image_content_blocks,
    current_multimodal_image_files_from_agent,
    ensure_multimodal_image_filter_window_mutator,
    ensure_manifest_images_referenced,
    inject_image_content_from_references,
    remove_multimodal_image_filter_window_mutator,
)
from jiuwenswarm.agents.harness.common.multimodal.image_history import (
    rewrite_orphan_attached_markers,
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
        image_files: list[dict[str, Any]] | None = None,
        dispatch_hint: bool = False,
    ) -> None:
        super().__init__()
        self._enable_image_multimodal = enable_image_multimodal
        self._max_total_bytes = max(0, int(max_total_bytes or 0))
        self._max_visual_tokens = max(0, int(max_visual_tokens or 0))
        self._max_count = max(0, int(max_count or 0))
        # 装配期绑定的上传清单（None = 未绑定，回退 agent 实例属性）。
        # 只有「rail 在成员 agent 之外计算清单」的装配方（team 成员由 swarm
        # provider 构造）才显式传：清单在 rail 构造时快照，每轮重建即刷新。
        self._bound_image_files: list[dict[str, Any]] | None = (
            [dict(item) for item in image_files if isinstance(item, dict)]
            if image_files is not None
            else None
        )
        # leader 专用提示：把图片交给成员时如何转述引用行（否则成员拿不到像素）。
        self._dispatch_hint = bool(dispatch_hint)
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

    def _resolve_request_image_files(self) -> list[dict[str, Any]] | None:
        """返回本次请求的上传清单；``None`` 表示未绑定、回退 ContextVar。

        team 成员（leader）的清单由 adapter 写进 build context，provider 在
        rail 构造时快照传入——成员 agent 不是 adapter 的 ``self._instance``，
        agent 属性镜像拿不到；返回 ``[]`` 是权威事实（本轮没有新上传），
        不能让 inject 回退到 ContextVar 读到别的轮次残留。
        """
        if self._bound_image_files is not None:
            return [dict(item) for item in self._bound_image_files]
        return current_multimodal_image_files_from_agent(self._deep_agent)

    def _is_team_member_rail(self) -> bool:
        """是否是 team 成员（非 leader）的 rail：绑定清单且无 leader 分发提示。"""

        return self._bound_image_files is not None and not self._dispatch_hint

    def _sync_image_policy_section(self, enabled: bool, images_sent: int) -> None:
        """按「这次到底有没有把像素发出去」写策略段。

        ``enabled=True`` 只说明模型**能**看图，不代表这次真发了。预算 / 引用行解析
        失败时视图里可能一张图都没有，此时再写"图片已作为图像内容直接提供、不要调用
        VQA"就是与真实负载相反的信号——模型会既看不到图、又不敢用兜底工具，最后
        只能拿 OCR 文本作答。因此原生模式下没有像素时不写「已提供 / 别调工具」，
        只写能力事实（模型具备原生视觉）+ 取回路径，既让模型知道自己的能力，又不封
        死视觉工具兜底路径。

        取回路径（需要看图 → read_file）必须写在这里：docx 路径问答等场景没有图片
        引用行（``_sync_image_state_attachment`` 无引用且无像素时直接清除附件），
        read_file 返回提示又只在真调了 read_file 后才出现——能力段是 enabled 会话
        唯一每轮都在的信号，缺了取回路径模型只能靠工具清单直觉选视觉工具。
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
            if self._is_team_member_rail():
                # team 成员：还没有像素，但任务里常只给本地路径。最好走 read_file——
                # read_file 的原生读图 resolver 读 deep_config，会把图片以图像内容返回给
                # 原生模型；不引导的话成员会惯性调用 VQA/OCR（线上 trace 已多次复现）。
                path_cn = (
                    "本次请求可能涉及本地图片。如果上下文中出现图片文件路径，请直接使用 "
                    "read_file 读取该路径：当前模型支持原生图片理解，read_file 会返回图像内容"
                    "（不要调用 visual_question_answering / image_ocr 去读取本地文件）。"
                    "只有远程 URL、或读取失败导致没有像素时，才使用视觉工具兜底。"
                )
                path_en = (
                    "This request may involve local image files. When a local image path appears "
                    "in the context, call read_file on it directly: the current model supports "
                    "native image understanding and read_file returns image content (do not use "
                    "visual_question_answering / image_ocr to read local files). Use a vision tool "
                    "only for remote URLs or when reading failed and no pixels are available."
                )
                try:
                    builder.add_section(PromptSection(
                        name=section_name,
                        content={"cn": path_cn, "en": path_en},
                        priority=96,
                    ))
                except Exception:
                    logger.warning("add %s section failed", section_name, exc_info=True)
                return
            # 主代理 / leader：能力事实 + 取回路径（docx 路径问答等没有引用行的场景，
            # 能力段是 enabled 会话唯一每轮都在的信号，缺了取回路径模型只能靠工具清单
            # 直觉选视觉工具）。
            capability_cn = (
                "系统已确认当前模型支持原生图片输入，你可以直接看图。"
                "需要查看图片文件时（含从文档解包提取的图片），请用 read_file 读取，"
                "图片像素会作为图像内容直接进入你的上下文。"
            )
            capability_en = (
                "The platform has confirmed that the current model supports native "
                "image input; you can see images directly. To inspect an image file "
                "(including images extracted from documents), call read_file on it; "
                "the pixels will enter your context directly as image content."
            )
            try:
                builder.add_section(PromptSection(
                    name=section_name,
                    content={"cn": capability_cn, "en": capability_en},
                    priority=96,
                ))
            except Exception:
                # 提示增强写失败只降级，不影响像素注入主链路。
                logger.warning("add %s section failed", section_name, exc_info=True)
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
        if self._dispatch_hint:
            # team leader 专用：图片要交给成员处理时，必须把引用行原样转述到成员能读到
            # 的消息通道（send_message）。只给绝对路径 / 只写进任务看板都不物化像素。
            native_cn += (
                "\n\n如果要把本次请求中的图片交给团队成员处理，请用 send_message 在消息正文中"
                "原样保留该图的 `[image #n] path=... filename=... mime=...` 引用行——成员据此"
                "直接查看像素。只给文件路径、或只写进任务看板正文时，成员看不到图片内容。"
            )
            native_en += (
                "\n\nTo hand one of these images to a teammate, keep its "
                "`[image #n] path=... filename=... mime=...` reference line verbatim in the "
                "send_message body — the teammate needs that line to see the pixels. A bare "
                "file path, or a path only written into the task board, gives it no image."
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
            if enabled:
                # 原生模型 + 本轮 0 张：与 cn 分支同构。指路 read_file 重读原文件——
                # 若写"call a vision tool"会与能力段（capability_en：native input,
                # call read_file on it）互相矛盾，把模型推回视觉工具老路。
                return (
                    "# Images In This Request\n\n"
                    "- This request carries 0 images as image content (no pixels were attached).\n"
                    "- When you need to inspect an image file (including images referenced in "
                    "earlier messages but omitted by compaction), call read_file on it; the "
                    "pixels will enter your context directly as image content."
                )
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
        if enabled:
            # 原生模型 + 本轮 0 张:像素不在窗口(典型:压缩把旧图搬走、引用行残留)。
            # 必须指路 read_file 重读原文件——若写"请调 VQA"会与能力段(原生直读)
            # 互相矛盾,把模型推回工具老路。
            return (
                "# 本次请求的图片\n\n"
                "- 本次请求附带图片：0 张（图片未作为像素附带）。\n"
                "- 需要查看图片文件时（含历史消息里引用过、但已被压缩省略的图片），"
                "请用 read_file 读取，图片像素会作为图像内容直接进入你的上下文。"
            )
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
        rewrite_orphan_markers = rewrite_orphan_attached_markers

        async def _image_state_sync_window_mutator(_context: Any, window: Any) -> Any:
            context_messages = list(getattr(window, "context_messages", []) or [])
            # 先按消息作用域核对孤儿标记:压缩器搬走像素块时会把 attached 状态行
            # 原样留在摘要里,消费端(附件/护栏)据此误判"已提供、禁止重读",模型
            # 被夹死。改写为 omitted: compacted 后,collect_provided_image_paths 与
            # after_model_call 的护栏重算自动放行 read_file 重读。
            orphan_markers = rewrite_orphan_markers(context_messages)
            if orphan_markers:
                logger.info(
                    "[MultimodalImageRail] rewrote %d orphan attached marker(s) "
                    "to omitted: compacted",
                    orphan_markers,
                )
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
            # 成员侧确定性分发：leader 只把路径写进任务看板 / 只留占位符时，成员上下文
            # 里提到清单图（文件名 / 完整路径）也应直接看像素，而不是回退 VQA。
            if self._is_team_member_rail():
                try:
                    ensured = ensure_manifest_images_referenced(
                        list(messages), self._bound_image_files
                    )
                    if ensured:
                        has_image_references = bool(
                            collect_image_references_from_messages(list(messages))
                        )
                except Exception as exc:  # pragma: no cover - 防御：补引用失败不阻断注入
                    logger.warning(
                        "[MultimodalImageRail] ensure manifest image references failed: %s",
                        exc,
                    )
            try:
                messages, attached = inject_image_content_from_references(
                    list(messages),
                    image_files=self._resolve_request_image_files(),
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

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        """leader 给成员发消息时，把"只提了文件名 / 路径"的上传图补成引用行。

        为什么需要：提示词只要求 leader 用 send_message 保留引用行，但实测它常把
        引用行写进任务看板（tool result，不物化）、消息里只写 "识别 3.jpg"。这里在
        工具执行前按本轮上传清单补引用行——只有消息真正提到的图才补，广播 / 回用户
        不补（避免把清单发给无关成员或用户）。成员侧仍由安全门按清单 ∩ 引用行物化。
        """

        if not self._dispatch_hint:
            return
        try:
            inputs = getattr(ctx, "inputs", None)
            tool_call = getattr(inputs, "tool_call", None)
            tool_name = str(
                getattr(inputs, "tool_name", "") or getattr(tool_call, "name", "") or ""
            )
            if tool_name != "send_message":
                return
            arguments = getattr(tool_call, "arguments", None)
            if not isinstance(arguments, dict):
                return
            target = arguments.get("to")
            if isinstance(target, str):
                if target.strip() in {"", "*", "user"}:
                    return
            elif isinstance(target, (list, tuple)):
                names = [str(item or "").strip() for item in target]
                if not names or any(name in {"", "*", "user"} for name in names):
                    return
            else:
                return
            content = arguments.get("content")
            if not isinstance(content, str) or not content.strip():
                return
            updated = augment_message_with_reference_lines(
                content, self._resolve_request_image_files()
            )
            if updated == content:
                return
            arguments["content"] = updated
            if tool_call is not None:
                tool_call.arguments = arguments
            if inputs is not None:
                inputs.tool_args = arguments
            logger.info(
                "[MultimodalImageRail] dispatch message augmented with image reference lines"
            )
        except Exception as exc:  # pragma: no cover - 旁路失败不阻断工具调用
            logger.warning(
                "[MultimodalImageRail] augment dispatch message failed: %s", exc
            )

