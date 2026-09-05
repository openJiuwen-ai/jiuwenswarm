from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import NODE_DISPLAY_NAMES
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.pipeline_init import PipelineInitNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.intent_classify import IntentClassifyNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.requirement_collect import RequirementCollectNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.document_parse import DocumentParseNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.template_context import TemplateContextNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.content_plan import ContentPlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.outline_review import OutlineReviewNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.deep_research import DeepResearchNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.style_prepare import StylePrepareNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.image_prepare import ImagePrepareNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_page_gen import PPTPageGenNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_export import PPTExportNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.delivery import DeliveryNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.speaker_notes import SpeakerNotesNode

logger = logging.getLogger(__name__)

_P3_SKIP_MESSAGE = "未检测到待解析文档，跳过文档解析"
_P3_SKIP_FIELDS = {
    "doc_parse_ok": False,
    "doc_parse_error": None,
    "topic": "",
    "topic_inferred": False,
}


def _document_parse_failed(inputs: dict[str, Any]) -> bool:
    return bool(inputs.get("has_documents")) and inputs.get("doc_parse_ok") is not True


def _document_parse_failure_result(
    node: str,
    inputs: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    error = inputs.get("doc_parse_error") or "未知原因"
    return {
        "node": node,
        "status": "error",
        "message": f"文档解析失败，已停止生成 PPT：{error}",
        "result": inputs,
        "steps": results,
    }


_MERGE_SKIP_KEYS = frozenset(
    {
        "node",
        "status",
        "message",
        "content",
        "skipped",
        "resume_skip",
    }
)


def _merge_subplan_result(inputs: dict[str, Any], result: Any) -> None:
    if not isinstance(result, dict):
        return
    for key, value in result.items():
        if key in _MERGE_SKIP_KEYS:
            continue
        inputs[key] = value


def _result_status(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("status", "ok"))
    return "ok"


def _append_subplan_step(
    results: list[dict[str, Any]],
    subplan: PlanNode,
    result: Any,
) -> None:
    results.append(
        {
            "node": subplan.plan_name,
            "status": _result_status(result),
            "result": result,
        }
    )


class PPTGenRootNode(PlanNode):
    # 节点显示名映射：供 Executor 读取，将内部 plan_name 转为前端展示的中文名。
    display_names: dict[str, str] = NODE_DISPLAY_NAMES

    def __init__(self) -> None:
        self._p0 = PipelineInitNode()
        self._p1 = IntentClassifyNode()
        self._p2 = RequirementCollectNode()
        self._p3 = DocumentParseNode()
        self._p3_5 = TemplateContextNode()
        self._speaker_notes = SpeakerNotesNode()
        self._delivery = DeliveryNode()
        self._tail_plans = [
            ContentPlanNode(),
            OutlineReviewNode(),
            DeepResearchNode(),
            StylePrepareNode(),
            ImagePrepareNode(),
            PPTPageGenNode(),
            PPTExportNode(),
            # Stage 8: 演讲备注（仅 need_speaker_notes=True 时执行，best-effort 不阻塞交付）
            self._speaker_notes,
            self._delivery,
        ]
        super().__init__(
            plan_name="ppt_gen_root",
            instruction="PPT生成任务流根节点，串联P0-P10全流程（含Stage 8演讲备注）",
            # P3 固定保留在任务列表；无附件时运行时 skip 并标记 completed。
            sub_plans=[
                self._p0,
                self._p1,
                self._p3,
                self._p2,
                self._p3_5,
                *self._tail_plans,
            ],
        )

    async def _run_subplan(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> None:
        result = await self.execute_subplan(subplan, inputs)
        _merge_subplan_result(inputs, result)
        _append_subplan_step(results, subplan, result)

    async def _skip_p3_subplan(
        self,
        inputs: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> None:
        if await self.should_skip_subplan(self._p3, inputs):
            result = await self.execute_subplan(self._p3, inputs)
            _merge_subplan_result(inputs, result)
            _append_subplan_step(results, self._p3, result)
            return

        result = await self.skip_subplan(
            self._p3,
            inputs,
            message=_P3_SKIP_MESSAGE,
            extra=_P3_SKIP_FIELDS,
        )
        _merge_subplan_result(inputs, result)
        _append_subplan_step(results, self._p3, result)

    async def _run_p3_and_p2(self, inputs: dict[str, Any], results: list[dict[str, Any]]) -> None:
        if inputs.get("has_documents"):
            await self._run_subplan(self._p3, inputs, results)
        else:
            await self._skip_p3_subplan(inputs, results)
        if _document_parse_failed(inputs):
            return
        await self._run_subplan(self._p2, inputs, results)

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """非流式执行 PPT 生成全流程，并把共享上下文透传给所有子节点。"""
        results: list[dict[str, Any]] = []

        await self._run_subplan(self._p0, inputs, results)
        await self._run_subplan(self._p1, inputs, results)
        await self._run_p3_and_p2(inputs, results)

        if _document_parse_failed(inputs):
            return _document_parse_failure_result(self.plan_name, inputs, results)

        # P3.5 模板叙事上下文预处理（条件执行，节点内部判断 style_mode）
        await self._run_subplan(self._p3_5, inputs, results)

        for subplan in self._tail_plans:
            await self._run_subplan(subplan, inputs, results)

        return {
            "node": self.plan_name,
            "status": "ok",
            "message": "PPT生成任务流执行完成",
            "result": inputs,
            "steps": results,
        }

    async def _silent_resume_skip_subplan_stream(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
        results: list[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        """Resume 重放时已 completed 的 stage：不广播进度，仍走 subplan 回调链。"""
        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(subplan, inputs)
        skipped = await self._maybe_skip_subplan_execute(subplan, inputs)
        if skipped is not None:
            _merge_subplan_result(inputs, skipped)
            _append_subplan_step(results, subplan, skipped)
            return
            yield  # pragma: no cover - async generator marker

        # should_skip_subplan 与 _maybe_skip_subplan_execute 二次判定不一致时
        # （理论上不应发生），回退真实执行，避免 subplan 被静默丢弃。
        logger.warning(
            "[PPTGenRoot] resume skip disagreement for %s: "
            "entered silent skip path but skip execute declined; falling back to run_stream",
            subplan.plan_name,
        )
        last_chunk: Any = None
        hitl_interrupt = False
        try:
            async for chunk in subplan.run_stream(inputs):
                last_chunk = chunk
                yield chunk
        except AbortError:
            hitl_interrupt = True
            raise
        finally:
            if self._after_subplan_execute is not None and not hitl_interrupt:
                await self._after_subplan_execute(subplan, inputs, last_chunk)
        _merge_subplan_result(inputs, last_chunk)
        _append_subplan_step(results, subplan, last_chunk)

    def _stage_progress_banner(
        self,
        subplan: PlanNode,
        *,
        index: int,
        total_steps: int,
        done: bool,
    ) -> dict[str, Any]:
        verb = "完成执行" if done else "开始执行"
        return {
            "node": self.plan_name,
            "status": "progress",
            "message": f"{verb} {subplan.plan_name}（{index}/{total_steps}）",
            "current_node": subplan.plan_name,
            "step": index,
            "total_steps": total_steps,
            # 横幅属于主回答气泡，不属于 stage taskRuns。Executor 识别该标记后
            # 立即发送，并把 task.start 推迟到横幅之后，避免双协程排空时
            # task.start 反超横幅、左下气泡停滞在旧 Stage。
            "_bubble_progress": True,
            "_bubble_progress_done": bool(done),
        }

    async def _run_subplan_stream(
        self,
        subplan: PlanNode,
        inputs: dict[str, Any],
        results: list[dict[str, Any]],
        *,
        index: int,
        total_steps: int,
    ) -> AsyncIterator[dict[str, Any]]:
        if await self.should_skip_subplan(subplan, inputs):
            async for chunk in self._silent_resume_skip_subplan_stream(
                subplan, inputs, results
            ):
                yield chunk
            return

        # 横幅刻意放在 task.start 之前、task.complete 之后：它们应进入左下
        # 主回答气泡，而 stage 内部输出仍由 task 上下文路由到左上任务区域。
        if not await self.should_suppress_subplan_start_banner(subplan, inputs):
            yield self._stage_progress_banner(
                subplan, index=index, total_steps=total_steps, done=False
            )

        last_chunk: Any = None
        stream = self.execute_subplan_stream(subplan, inputs)
        try:
            async for chunk in stream:
                last_chunk = chunk
                yield chunk
        finally:
            await stream.aclose()

        _merge_subplan_result(inputs, last_chunk)
        _append_subplan_step(results, subplan, last_chunk)
        yield self._stage_progress_banner(
            subplan, index=index, total_steps=total_steps, done=True
        )

    async def _skip_p3_subplan_stream(
        self,
        inputs: dict[str, Any],
        results: list[dict[str, Any]],
        *,
        index: int,
        total_steps: int,
    ) -> AsyncIterator[dict[str, Any]]:
        if await self.should_skip_subplan(self._p3, inputs):
            async for chunk in self._silent_resume_skip_subplan_stream(
                self._p3, inputs, results
            ):
                yield chunk
            return

        skip_result: dict[str, Any] = {
            "node": self._p3.plan_name,
            "status": "ok",
            "message": _P3_SKIP_MESSAGE,
            "skipped": True,
            **_P3_SKIP_FIELDS,
        }
        if not await self.should_suppress_subplan_start_banner(self._p3, inputs):
            yield self._stage_progress_banner(
                self._p3, index=index, total_steps=total_steps, done=False
            )

        if self._before_subplan_execute is not None:
            await self._before_subplan_execute(self._p3, inputs)
        try:
            yield skip_result
        finally:
            if self._after_subplan_execute is not None:
                await self._after_subplan_execute(self._p3, inputs, skip_result)

        _merge_subplan_result(inputs, skip_result)
        _append_subplan_step(results, self._p3, skip_result)
        yield self._stage_progress_banner(
            self._p3, index=index, total_steps=total_steps, done=True
        )

    async def _run_p3_and_p2_stream(
        self,
        inputs: dict[str, Any],
        results: list[dict[str, Any]],
        *,
        total_steps: int,
    ) -> AsyncIterator[dict[str, Any]]:
        if inputs.get("has_documents"):
            async for chunk in self._run_subplan_stream(
                self._p3, inputs, results, index=3, total_steps=total_steps
            ):
                yield chunk
        else:
            async for chunk in self._skip_p3_subplan_stream(
                inputs, results, index=3, total_steps=total_steps
            ):
                yield chunk

        if _document_parse_failed(inputs):
            return

        async for chunk in self._run_subplan_stream(
            self._p2, inputs, results, index=4, total_steps=total_steps
        ):
            yield chunk

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """流式执行 PPT 生成全流程，逐个透传子节点进度与结果。"""
        results: list[dict[str, Any]] = []
        total_steps = len(self.sub_plans)

        # HITL resume 重放时首段 stage 往往已是 completed；不再广播「全流程开始」，
        # 避免对话框重复刷 Stage 1–N 的假进度。
        if not await self.should_skip_subplan(self._p0, inputs):
            yield {
                "node": self.plan_name,
                "status": "progress",
                "message": "PPT生成任务流开始执行",
            }

        for index, subplan in enumerate([self._p0, self._p1], start=1):
            async for chunk in self._run_subplan_stream(
                subplan,
                inputs,
                results,
                index=index,
                total_steps=total_steps,
            ):
                yield chunk

        async for chunk in self._run_p3_and_p2_stream(inputs, results, total_steps=total_steps):
            yield chunk

        if _document_parse_failed(inputs):
            yield _document_parse_failure_result(self.plan_name, inputs, results)
            return

        # P3.5 模板叙事上下文预处理（条件执行，节点内部判断 style_mode）
        async for chunk in self._run_subplan_stream(
            self._p3_5, inputs, results, index=5, total_steps=total_steps,
        ):
            yield chunk

        for index, subplan in enumerate(self._tail_plans, start=6):
            async for chunk in self._run_subplan_stream(
                subplan,
                inputs,
                results,
                index=index,
                total_steps=total_steps,
            ):
                yield chunk

        # 交付总结骨架不在此处流式发出：
        # 1) 此时 P10 after_subplan 已清空 task_id，骨架会与「PPT生成任务流执行完成」等
        #    无 stream_source_id 的过程尾落入同一缓冲桶，主气泡第一行变成过程尾；
        # 2) 该 chat.delta 早于外层 skill_acceleration_exec 的 tool_result，进不了
        #    RelayClaw 的 pptTurboSummary 收集窗口（tool_result 时还会被清空）。
        # 骨架由 P10 写入 artifact → skill_turbo_tools 挂 ContextVar →
        # SkillTurboDeliverySummaryRail 在外层 tool_result 之后再发 llm_output。
        yield {
            "node": self.plan_name,
            "status": "ok",
            "message": "PPT生成任务流执行完成",
            "result": inputs,
            "steps": results,
        }


root = PPTGenRootNode()
