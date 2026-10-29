# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-owned response projection and durable history lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing
from dataclasses import replace
from typing import Any, AsyncIterator

from jiuwenswarm.agents.harness.common.recommendation.prompt_suggestion import USER_INTERACTION_EVENTS
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.common.chat_final import ensure_final_mode_inplace
from jiuwenswarm.server.runtime.agent_adapter.execution_driver import ExecutionDriver


logger = logging.getLogger(__name__)


class StreamResponsePipeline:
    """One turn owns its projection state, durable buffers and cleanup barrier."""

    def __init__(
        self,
        *,
        request,
        adapter,
        session_id,
        inputs,
        memory_mode,
        user_turn,
        is_team_mode,
        ports,
        hooks,
        queue_capacity,
        retry_call,
        completion,
    ) -> None:
        self.request = request
        self.completion = completion
        self.adapter = adapter
        self.session_id = session_id
        self.rid = request.request_id
        self.cid = request.channel_id
        self.inputs = inputs
        self.memory_mode = memory_mode
        self.user_turn = user_turn
        self.is_team_mode = is_team_mode
        self.ports = ports
        self.hooks = hooks
        self.driver = ExecutionDriver(
            lambda: self.adapter.process_message_stream_impl(self.request, self.inputs),
            capacity=queue_capacity,
        )
        self.stream_queue = self.driver.queue
        self.stream_done = self.driver.done
        from jiuwenswarm.server.runtime.agent_adapter.history_committer import (
            HistoryCommitter,
        )
        from jiuwenswarm.server.runtime.agent_adapter.stream_projector import (
            StreamProjector,
        )

        self.projector = StreamProjector(
            block_buffer=self.ports.TeamA2UIBlockBuffer(),
            split_probe=self.ports.split_a2ui_stream_content,
            extend_probe=self.ports.extend_a2ui_stream_probe,
        )

        async def write_stream_history(**record) -> None:
            await self.ports.run_history_io(self.ports.append_history_record, wait_for_write=True, **record)

        self.history_committer = HistoryCommitter(
            params=self.request.params,
            session_id=self.session_id,
            request_id=self.rid,
            channel_id=self.cid,
            write_record=write_stream_history,
            decorate_web=self.ports.with_web_agent_template_metadata,
            decorate_heartbeat=self.ports.with_heartbeat_history_metadata,
            decorate_cross_session=self.ports.with_cross_session_history_metadata,
            resolve_final_timestamp=self.ports.resolve_final_record_timestamp,
        )

        self.repair_call = getattr(self.adapter, "repair_model_response", None)
        self.retry_without_a2ui_call = retry_call

        # proactive 请求：source 标记在 request.params 里，由外层 proactive_adapter
        # 消费流时注入到 chunk。这里（生产端）收不到注入后的 source，只能自己按同一
        # 规则判定"话术轮"：工具轮总结 final / 延续轮 final 不带 proactive 标记落盘，
        # 只有最后一个非工具轮非空 final（=推荐话术）落盘带 source/rec_id——否则刷新
        # 后 history 里每一条 chat.final 都带 rec_id，被渲染成多张推荐卡片（bug）。
        # "最后一个"要等流结束才能确定，故缓存话术轮候选、流末补落。
        self.is_proactive_request = bool(
            self.request.params.get("source") == "proactive_recommendation"
        )
        self.proactive_pending_final: dict[str, Any] | None = None
        # 自上一个 chat.final 以来是否出现过 chat.tool_call（用于识别工具轮总结 final）。
        self.saw_proactive_tool_call_since_final = False

    async def _persist_proactive_final_record(
        self,
        *,
        content: str,
        payload: dict[str, Any],
        marked: bool,
    ) -> None:
        """落盘 proactive 流的一条 chat.final 记录。

        marked=True（话术轮/最后一个非工具轮非空 final）时带
        source/proactive_rec_id，刷新后前端渲染成推荐卡片；否则为普通
        气泡（工具轮总结、延续轮文本等不参与卡片渲染）。
        """
        extra_fields: dict[str, Any] = {
            k: v
            for k, v in payload.items()
            if k not in ("event_type", "event", "content")
        }
        if marked:
            # 只给话术轮补 proactive 标记（请求级参数仅这一条记录消费）。
            for pk in (
                "source",
                "proactive_type",
                "proactive_target",
                "automation",
                "proactive_rec_id",
            ):
                if pk not in extra_fields and pk in self.request.params:
                    extra_fields[pk] = self.request.params[pk]
        extra_fields = (
            self.history_committer.attach_reasoning_content(extra_fields) or {}
        )
        extra_fields = (
            self.ports.with_cross_session_history_metadata(
                self.ports.with_heartbeat_history_metadata(
                    self.ports.with_web_agent_template_metadata(
                        extra_fields,
                        self.request.params,
                        self.cid,
                        event_type="chat.final",
                    ),
                    self.request.params,
                ),
                self.request.params,
            )
            or {}
        )
        await self.ports.run_history_io(
            self.ports.append_history_record,
            wait_for_write=True,
            session_id=self.session_id,
            request_id=self.rid,
            channel_id=self.cid,
            role="assistant",
            event_type="chat.final",
            content=content,
            timestamp=self.ports.resolve_final_record_timestamp(
                event_type="chat.final",
                segment_started_at=None,
                extra_fields=extra_fields,
            ),
            extra=extra_fields if extra_fields else None,
            mode=self.request.params.get("mode", "unknown"),
        )

    async def _finalize_team_a2ui_block(
        self, payload: dict[str, Any], decision: Any
    ) -> None:
        try:
            finalized = await self.ports.finalize_assistant_response_if_a2ui(
                decision.raw_block,
                channel=self.cid,
                user_query=self.user_turn.text,
                request_id=f"{self.rid}:{decision.key[0]}:{decision.key[1]}",
                repair_call=self.repair_call,
                retry_without_a2ui_call=self.retry_without_a2ui_call,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Team A2UI local finalization failed: request_id=%s round=%s member=%s",
                self.rid,
                decision.key[0],
                decision.key[1],
            )
            finalized = decision.raw_block
        await self.stream_queue.put(
            (
                "team_a2ui_finalized",
                (payload, decision, finalized),
            )
        )

    def _schedule_team_a2ui_block(self, payload: dict[str, Any], decision: Any) -> None:
        logger.info(
            "Team A2UI block finalization scheduled: request_id=%s round=%s member=%s",
            self.rid,
            decision.key[0],
            decision.key[1],
        )
        self.projector.team_a2ui_tasks[decision.key] = asyncio.create_task(
            self._finalize_team_a2ui_block(payload, decision)
        )

    async def run(self) -> AsyncIterator[AgentResponseChunk]:
        self.driver.start()
        try:
            async with aclosing(self._consume()) as stream:
                async for chunk in stream:
                    self.completion.observe(chunk)
                    yield chunk
        finally:
            try:
                await self.history_committer.persist_team_member_pending()
                # proactive 请求：流结束时把缓存的话术轮 final 补落（带 source/rec_id）。
                # 流里的非空 final 都被延迟到此刻按序落盘，只有最后一个非工具轮 final
                # （=推荐话术）带 proactive 标记，其余（工具轮总结/延续轮）为普通气泡。
                if (
                    self.is_proactive_request
                    and self.proactive_pending_final is not None
                ):
                    await self._persist_proactive_final_record(
                        content=str(self.proactive_pending_final["content"]),
                        payload=self.proactive_pending_final["payload"],
                        marked=True,
                    )
                # Goal 还在跑时这条流不会收到收尾的 chat.final，气泡里已经展示的正文
                # 也就没有任何一处落盘。补一次，否则重新打开历史记录时这段回答凭空
                # 消失，和实时看到的不是一回事。非 Goal 流不进这里。
                if self.history_committer.saw_goal_stream_output:
                    await self.history_committer.persist_pending_final_text()
                else:
                    # 非 Goal 异常结束（LLM 错误/断连，或流结束无收尾事件）时，pending
                    # reasoning 不会随 tool_call/final 落盘；补落一条，刷新后思考内容
                    # 与耗时终点（末帧时刻）都能恢复。正文 final 故意不落——半截回答
                    # 不该当 chat.final 写进历史。
                    await self.history_committer.persist_pending_reasoning()
            finally:
                # The adapter producer owns RuntimeOutputStream.  Cancelling and
                # awaiting it releases the runtime output lease and aborts the
                # in-flight round when the outer WebSocket consumer disappears.
                unfinished_a2ui_tasks = [
                    task
                    for task in self.projector.team_a2ui_tasks.values()
                    if not task.done()
                ]
                for task in unfinished_a2ui_tasks:
                    task.cancel()
                if unfinished_a2ui_tasks:
                    await asyncio.gather(*unfinished_a2ui_tasks, return_exceptions=True)
                await self.driver.close()

        self.driver.raise_if_cancelled()
        async with aclosing(self._finish()) as stream:
            async for chunk in stream:
                yield chunk

    async def _consume(self) -> AsyncIterator[AgentResponseChunk]:
        _yielded_from_queue = 0
        logger.info(
            "[JiuWenSwarm] consumer loop starting: request_id=%s is_team=%s",
            self.rid,
            self.is_team_mode,
        )
        while (
            not self.stream_done.is_set()
            or not self.stream_queue.empty()
            or bool(self.projector.team_a2ui_tasks)
        ):
            try:
                item = await asyncio.wait_for(self.stream_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            event_type, data = item
            if event_type == "team_a2ui_finalized":
                original_payload, decision, finalized = data
                self.projector.team_a2ui_tasks.pop(decision.key, None)
                pending_final = self.projector.team_a2ui_pending_finals.pop(
                    decision.key, None
                )
                if pending_final is not None:
                    self.projector.team_a2ui_blocks.remember_finalized(
                        decision.key,
                        decision.raw_block,
                        finalized,
                    )
                    replay = self.projector.team_a2ui_blocks.consume(
                        pending_final,
                        "chat.final",
                        str(pending_final.get("content") or ""),
                    )
                    if replay is not None and replay.raw_block:
                        self._schedule_team_a2ui_block(pending_final, replay)
                        continue
                    replay_content = replay.replacement if replay is not None else None
                    output_payload = {
                        **pending_final,
                        "content": replay_content or finalized,
                        "session_id": self.session_id,
                    }
                elif decision.finalize_whole_event:
                    output_payload = {
                        **original_payload,
                        "event_type": "chat.final",
                        "content": finalized,
                        "session_id": self.session_id,
                    }
                else:
                    self.projector.team_a2ui_blocks.remember_finalized(
                        decision.key,
                        decision.raw_block,
                        finalized,
                    )
                    output_payload = {
                        **original_payload,
                        "content": f"{finalized}{decision.trailing}",
                    }
                output_payload["_team_a2ui_finalized"] = True
                data = AgentResponseChunk(
                    request_id=self.rid,
                    channel_id=self.cid,
                    payload=output_payload,
                    is_complete=False,
                )
                event_type = "chunk"
            _yielded_from_queue += 1
            if _yielded_from_queue <= 3:
                _pl = getattr(data, "payload", None) if event_type == "chunk" else None
                _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""
                logger.info(
                    "[JiuWenSwarm] consumer loop yield #%s: request_id=%s event_type=%s item_type=%s",
                    _yielded_from_queue,
                    self.rid,
                    _et,
                    event_type,
                )

            if event_type == "error":
                if isinstance(data, asyncio.CancelledError):
                    logger.info("[JiuWenSwarm] 流式处理被中断: request_id=%s", self.rid)
                    raise data
                # Surface exception class so consumers can classify
                # failures structurally instead of regexing the message.
                error_type = (
                    type(data).__name__ if isinstance(data, BaseException) else ""
                )
                error_payload: dict[str, Any] = {
                    "event_type": "chat.error",
                    "error": str(data),
                }
                if error_type:
                    error_payload["error_type"] = error_type
                await self.ports.run_history_io(
                    self.ports.append_history_record,
                    wait_for_write=True,
                    session_id=self.session_id,
                    request_id=self.rid,
                    channel_id=self.cid,
                    role="assistant",
                    event_type="chat.error",
                    content=str(data),
                    timestamp=time.time(),
                    mode=self.request.params.get("mode", "unknown"),
                    extra=self.ports.with_heartbeat_history_metadata(
                        {"error_type": error_type} if error_type else None,
                        self.request.params,
                    ),
                )
                yield AgentResponseChunk(
                    request_id=self.rid,
                    channel_id=self.cid,
                    payload=error_payload,
                    is_complete=False,
                )
            else:
                if isinstance(data, AgentResponseChunk):
                    async with aclosing(self._project_chunk(data)) as stream:
                        async for output in stream:
                            yield output
                elif isinstance(data, dict) and isinstance(data.get("event_type"), str):
                    async with aclosing(self._project_mapping(data)) as stream:
                        async for output in stream:
                            yield output

    async def _project_chunk(
        self, data: AgentResponseChunk
    ) -> AsyncIterator[AgentResponseChunk]:
        phase_payload = data.payload if isinstance(data.payload, dict) else {}
        phase_event = phase_payload.get("event_type")
        if phase_event == "chat.input_received":
            await self.history_committer.persist_input_boundary(phase_payload)
            self.history_committer.output_phase_metadata["output_suppressed"] = True
            yield data
            return
        if phase_event == "chat.output_phase":
            # An automatic Goal attempt changes model phase, not
            # the visible bubble. Only consumed user input starts
            # a new segment and closes the suppressed old tail.
            if phase_payload.get("applied_input_ids"):
                await self.history_committer.persist_pending_final_text()
                await self.history_committer.persist_pending_reasoning()
                self.history_committer.output_phase_metadata = {}
                self.history_committer.final_content = ""
            self.history_committer.output_phase_metadata["output_phase_id"] = (
                phase_payload["output_phase_id"]
            )
        elif phase_payload.get("output_phase_id"):
            # A stream-end final is visible even if steering was
            # never consumed. Persist buffered hidden output with
            # its own visibility before accepting that control.
            if self.history_committer.output_phase_metadata.get(
                "output_suppressed"
            ) and not phase_payload.get("output_suppressed"):
                await self.history_committer.persist_pending_final_text()
                await self.history_committer.persist_pending_reasoning()
            self.history_committer.output_phase_metadata = {
                key: phase_payload[key]
                for key in ("output_phase_id", "output_suppressed")
                if key in phase_payload
            }
        if self.projector.suppress_a2ui_stream:
            data = self.ports.normalize_nested_stream_chunk(data)
            if data is None:
                return
        if isinstance(data.payload, dict):
            payload_event_type = data.payload.get("event_type")
            enriched_payload = self.ports.with_web_agent_template_payload(
                data.payload,
                self.request.params,
                self.cid,
                event_type=(
                    payload_event_type if isinstance(payload_event_type, str) else None
                ),
            )
            if enriched_payload is not data.payload:
                data = replace(data, payload=enriched_payload)
        if isinstance(data.payload, dict) and isinstance(
            data.payload.get("event_type"), str
        ):
            et = str(data.payload.get("event_type"))
            self.history_committer.note_goal_stream_payload(et, data.payload)
            should_record = et.startswith("chat.") or et == "context.usage"
            final_segment_started_at: float | None = None
            final_segment_order = None
            if not should_record and et == EventType.TEAM_MESSAGE.value:
                should_record = True
            if et == "context.compression_state":
                await self.ports.run_history_io(
                    self.ports.append_compact_history_from_payload,
                    payload=data.payload,
                    session_id=self.session_id,
                    request_id=self.rid,
                    channel_id=self.cid,
                    mode=self.request.params.get("mode", "unknown"),
                )

            payload_content = str(data.payload.get("content", ""))
            locally_finalized = bool(data.payload.get("_team_a2ui_finalized"))
            if locally_finalized:
                next_payload = dict(data.payload)
                next_payload.pop("_team_a2ui_finalized", None)
                data = replace(data, payload=next_payload)
            if (
                self.is_team_mode
                and not locally_finalized
                and et in {"chat.delta", "chat.final"}
            ):
                direct_payloads, next_payload = self.projector.process_team_payload(
                    data.payload, self._schedule_team_a2ui_block
                )
                for direct_payload in direct_payloads:
                    yield replace(
                        data,
                        payload=direct_payload,
                        is_complete=False,
                    )
                if next_payload is None:
                    return
                data = replace(data, payload=next_payload)
                et = str(next_payload.get("event_type") or et)
                payload_content = str(next_payload.get("content", ""))
            a2ui_split = None
            if (
                self.ports.should_probe_a2ui_stream(is_team_mode=self.is_team_mode)
                and et in {"chat.delta", "chat.final"}
                and payload_content
            ):
                a2ui_split = self.projector.probe(payload_content)
            if self.ports.should_defer_a2ui_processing_status(
                suppress_a2ui_stream=self.projector.suppress_a2ui_stream,
                event_type=et,
                payload=data.payload,
            ):
                logger.info(
                    "A2UI processing_status=false deferred until finalization: "
                    "request_id=%s",
                    self.rid,
                )
                return
            if self.is_team_mode and await self.history_committer.handle_team_member_event(data.payload):
                yield data
                return
            if et == "chat.delta":
                self.projector.final_answer_chunks.append(payload_content)
                if self.projector.suppress_a2ui_stream or a2ui_split is not None:
                    first_a2ui_suppression = not self.projector.suppress_a2ui_stream
                    if first_a2ui_suppression:
                        logger.info(
                            "A2UI stream suppression activated: request_id=%s event_type=%s",
                            self.rid,
                            et,
                        )
                    self.projector.suppress_a2ui_stream = True
                    if a2ui_split is not None and a2ui_split[0]:
                        prefix_payload = dict(data.payload)
                        prefix_payload["content"] = a2ui_split[0]
                        yield AgentResponseChunk(
                            request_id=data.request_id,
                            channel_id=data.channel_id,
                            payload=prefix_payload,
                            is_complete=False,
                        )
                    if (
                        first_a2ui_suppression
                        and not self.projector.a2ui_pending_render_sent
                    ):
                        yield self.ports.make_a2ui_pending_render_chunk(
                            request_id=self.rid, channel_id=self.cid
                        )
                        self.projector.a2ui_pending_render_sent = True
                    return
                self.history_committer.note_durable_pending_final_delta(
                    payload_content, data.payload
                )
                should_record = False
            elif et == "chat.reasoning":
                self.history_committer.note_durable_reasoning_delta(data.payload)
                self.history_committer.pending_reasoning_chunks.append(payload_content)
                should_record = False
            elif et == "chat.tool_call" or et in USER_INTERACTION_EVENTS:
                if self.is_proactive_request and et == "chat.tool_call":
                    self.saw_proactive_tool_call_since_final = True
                await self.history_committer.persist_pending_final_text()
                if et in USER_INTERACTION_EVENTS:
                    await self.history_committer.persist_pending_reasoning()
            elif et == "chat.final":
                if isinstance(data.payload, dict):
                    ensure_final_mode_inplace(data.payload)
                if self.projector.suppress_a2ui_stream or a2ui_split is not None:
                    first_a2ui_suppression = not self.projector.suppress_a2ui_stream
                    if first_a2ui_suppression:
                        logger.info(
                            "A2UI stream suppression activated: request_id=%s event_type=%s",
                            self.rid,
                            et,
                        )
                    self.projector.suppress_a2ui_stream = True
                    if (
                        first_a2ui_suppression
                        and not self.projector.a2ui_pending_render_sent
                    ):
                        yield self.ports.make_a2ui_pending_render_chunk(
                            request_id=self.rid, channel_id=self.cid
                        )
                        self.projector.a2ui_pending_render_sent = True
                    if payload_content:
                        self.projector.final_answer_content = payload_content
                        self.projector.final_answer_chunks.clear()
                    self.history_committer.reset_durable_pending_final()
                    return
                if self.is_proactive_request and payload_content:
                    # proactive 话术轮判定（与 proactive_adapter 同规则）：
                    # - 工具轮总结 final（该轮有 tool_call）：立即按普通气泡
                    #   落盘，不带 proactive 标记；
                    # - 第一个非工具轮非空 final（=推荐话术）：缓存为话术轮
                    #   候选，等流结束才带 source/rec_id 落盘（否则工具轮
                    #   总结/延续轮文本会带着 rec_id 落盘，刷新后被渲染成
                    #   第二张推荐卡片——bug）；其后的非工具轮 final（如话术
                    #   后的纯文本延续）不是交付物，立即落盘为普通气泡。
                    self.history_committer.reset_durable_pending_final()
                    final_segment_started_at = None
                    self.projector.final_answer_content = payload_content
                    self.projector.final_answer_chunks.clear()
                    self.history_committer.final_content = payload_content
                    if self.saw_proactive_tool_call_since_final:
                        self.saw_proactive_tool_call_since_final = False
                        await self._persist_proactive_final_record(
                            content=payload_content,
                            payload=(
                                dict(data.payload)
                                if isinstance(data.payload, dict)
                                else {}
                            ),
                            marked=False,
                        )
                    else:
                        # 第一个非工具轮非空 final = 话术轮候选，缓存等
                        # 流末带标记落盘；其后的非工具轮 final（如话术后的
                        # 纯文本延续）不是交付物，立即落盘为普通气泡，
                        # 不覆盖候选。
                        if self.proactive_pending_final is None:
                            self.proactive_pending_final = {
                                "content": payload_content,
                                "payload": (
                                    dict(data.payload)
                                    if isinstance(data.payload, dict)
                                    else {}
                                ),
                            }
                        else:
                            await self._persist_proactive_final_record(
                                content=payload_content,
                                payload=(
                                    dict(data.payload)
                                    if isinstance(data.payload, dict)
                                    else {}
                                ),
                                marked=False,
                            )
                    should_record = False
                # 先记住本段起始时刻：下面的 reset/flush 会把它清掉。
                final_segment_started_at = (
                    self.history_committer.pending_final_started_at
                )
                final_segment_order = self.history_committer.pending_final_order
                if payload_content:
                    self.history_committer.reset_durable_pending_final()
                else:
                    # 空 final 只是收尾/拆气泡信号（Goal 中间态 final 被降级成
                    # chat.delta、流末尾的兜底 final），气泡里留下的正文就是前面
                    # 那些 delta。这里必须落盘同一份，否则历史里整段回答会消失。
                    await self.history_committer.persist_pending_final_text()
                    final_segment_started_at = None

            if should_record:
                payload_dict = dict(data.payload)
                extra_fields = {
                    k: v
                    for k, v in payload_dict.items()
                    if k not in ("event_type", "content")
                }
                if payload_dict.get("output_phase_id"):
                    # The stream clock is milliseconds. History owns its
                    # seconds timestamp and first-delta position below.
                    stream_timestamp = extra_fields.pop("timestamp")
                    if et == "chat.final":
                        extra_fields["completed_at"] = stream_timestamp / 1000
                if et == "chat.final" and final_segment_order:
                    extra_fields["output_order"] = final_segment_order
                if et == EventType.TEAM_MESSAGE.value and "event" in payload_dict:
                    event_data = payload_dict.get("event", {})
                    if isinstance(event_data, dict):
                        for k, v in event_data.items():
                            if k not in ("type", "timestamp", "content"):
                                extra_fields[k] = v
                if et in {"chat.final", "chat.tool_call"}:
                    extra_fields = self.history_committer.attach_reasoning_content(
                        extra_fields
                    )
                # 透传 proactive 标记——刷新页面时前端靠 source 识别卡片
                for pk in (
                    "source",
                    "proactive_type",
                    "proactive_target",
                    "automation",
                    "proactive_rec_id",
                ):
                    if pk not in extra_fields and pk in self.request.params:
                        extra_fields[pk] = self.request.params[pk]
                if not isinstance(extra_fields, dict):
                    extra_fields = {}
                extra_fields = (
                    self.ports.with_cross_session_history_metadata(
                        self.ports.with_heartbeat_history_metadata(
                            self.ports.with_web_agent_template_metadata(
                                extra_fields,
                                self.request.params,
                                self.cid,
                                event_type=et,
                                payload=payload_dict,
                            ),
                            self.request.params,
                        ),
                        self.request.params,
                    )
                    or {}
                )
                record_timestamp = self.ports.resolve_final_record_timestamp(
                    event_type=et,
                    segment_started_at=(
                        final_segment_started_at if et == "chat.final" else None
                    ),
                    extra_fields=extra_fields,
                )
                await self.ports.run_history_io(
                    self.ports.append_history_record,
                    wait_for_write=True,
                    session_id=self.session_id,
                    request_id=self.rid,
                    channel_id=self.cid,
                    role="assistant",
                    event_type=et,
                    content=data.payload.get("content")
                    or data.payload.get("error")
                    or "",
                    timestamp=record_timestamp,
                    extra=extra_fields if extra_fields else None,
                    mode=self.request.params.get("mode", "unknown"),
                )
                if et == "chat.final":
                    self.history_committer.final_content = str(
                        data.payload.get("content", "")
                    )
            if et == "chat.final":
                next_final_content = str(data.payload.get("content", ""))
                if next_final_content:
                    self.projector.final_answer_content = next_final_content
                    self.projector.final_answer_chunks.clear()
        yield data

    async def _project_mapping(
        self, data: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        data = (
            self.ports.with_web_agent_template_payload(
                data,
                self.request.params,
                self.cid,
            )
            or data
        )
        et = str(data.get("event_type"))
        self.history_committer.note_goal_stream_payload(et, data)
        should_record = et.startswith("chat.") or et == "context.usage"
        final_segment_started_at = None
        if not should_record and et == EventType.TEAM_MESSAGE.value:
            should_record = True
        if et == "context.compression_state":
            await self.ports.run_history_io(
                self.ports.append_compact_history_from_payload,
                payload=data,
                session_id=self.session_id,
                request_id=self.rid,
                channel_id=self.cid,
                mode=self.request.params.get("mode", "unknown"),
            )

        payload_content = str(data.get("content", ""))
        locally_finalized = bool(data.get("_team_a2ui_finalized"))
        if locally_finalized:
            data = dict(data)
            data.pop("_team_a2ui_finalized", None)
        if (
            self.is_team_mode
            and not locally_finalized
            and et in {"chat.delta", "chat.final"}
        ):
            direct_payloads, next_payload = self.projector.process_team_payload(
                data, self._schedule_team_a2ui_block
            )
            for direct_payload in direct_payloads:
                yield AgentResponseChunk(
                    request_id=self.rid,
                    channel_id=self.cid,
                    payload=direct_payload,
                    is_complete=False,
                )
            if next_payload is None:
                return
            data = next_payload
            et = str(next_payload.get("event_type") or et)
            payload_content = str(next_payload.get("content", ""))
        a2ui_split = None
        if (
            self.ports.should_probe_a2ui_stream(is_team_mode=self.is_team_mode)
            and et in {"chat.delta", "chat.final"}
            and payload_content
        ):
            a2ui_split = self.projector.probe(payload_content)
        if self.ports.should_defer_a2ui_processing_status(
            suppress_a2ui_stream=self.projector.suppress_a2ui_stream,
            event_type=et,
            payload=data,
        ):
            logger.info(
                "A2UI processing_status=false deferred until finalization: "
                "request_id=%s",
                self.rid,
            )
            return
        if et == "chat.final":
            ensure_final_mode_inplace(data)
        if self.is_team_mode and await self.history_committer.handle_team_member_event(data):
            yield AgentResponseChunk(
                request_id=self.rid, channel_id=self.cid, payload=data, is_complete=False,
            )
            return
        if et == "chat.delta":
            self.projector.final_answer_chunks.append(payload_content)
            if self.projector.suppress_a2ui_stream or a2ui_split is not None:
                first_a2ui_suppression = not self.projector.suppress_a2ui_stream
                if first_a2ui_suppression:
                    logger.info(
                        "A2UI stream suppression activated: request_id=%s event_type=%s",
                        self.rid,
                        et,
                    )
                self.projector.suppress_a2ui_stream = True
                if a2ui_split is not None and a2ui_split[0]:
                    prefix_payload = dict(data)
                    prefix_payload["content"] = a2ui_split[0]
                    yield AgentResponseChunk(
                        request_id=self.rid,
                        channel_id=self.cid,
                        payload=prefix_payload,
                        is_complete=False,
                    )
                if (
                    first_a2ui_suppression
                    and not self.projector.a2ui_pending_render_sent
                ):
                    yield self.ports.make_a2ui_pending_render_chunk(
                        request_id=self.rid, channel_id=self.cid
                    )
                    self.projector.a2ui_pending_render_sent = True
                return
            self.history_committer.note_durable_pending_final_delta(
                payload_content, data
            )
            should_record = False
        elif et == "chat.reasoning":
            self.history_committer.note_durable_reasoning_delta(data)
            self.history_committer.pending_reasoning_chunks.append(payload_content)
            should_record = False
        elif et == "chat.tool_call" or et in USER_INTERACTION_EVENTS:
            if self.is_proactive_request and et == "chat.tool_call":
                self.saw_proactive_tool_call_since_final = True
            await self.history_committer.persist_pending_final_text()
            if et in USER_INTERACTION_EVENTS:
                await self.history_committer.persist_pending_reasoning()
        elif et == "chat.final":
            if self.projector.suppress_a2ui_stream or a2ui_split is not None:
                first_a2ui_suppression = not self.projector.suppress_a2ui_stream
                if first_a2ui_suppression:
                    logger.info(
                        "A2UI stream suppression activated: request_id=%s event_type=%s",
                        self.rid,
                        et,
                    )
                self.projector.suppress_a2ui_stream = True
                if (
                    first_a2ui_suppression
                    and not self.projector.a2ui_pending_render_sent
                ):
                    yield self.ports.make_a2ui_pending_render_chunk(
                        request_id=self.rid, channel_id=self.cid
                    )
                    self.projector.a2ui_pending_render_sent = True
                if payload_content:
                    self.projector.final_answer_content = payload_content
                    self.projector.final_answer_chunks.clear()
                self.history_committer.reset_durable_pending_final()
                return
            if self.is_proactive_request and payload_content:
                # 与第一分支（AgentResponseChunk）相同的 proactive
                # 话术轮判定与延迟落盘（见上）。
                self.history_committer.reset_durable_pending_final()
                final_segment_started_at = None
                self.projector.final_answer_content = payload_content
                self.projector.final_answer_chunks.clear()
                self.history_committer.final_content = payload_content
                if self.saw_proactive_tool_call_since_final:
                    self.saw_proactive_tool_call_since_final = False
                    await self._persist_proactive_final_record(
                        content=payload_content,
                        payload=dict(data),
                        marked=False,
                    )
                else:
                    # 同第一分支：第一个非工具轮 final 缓存为话术轮候选，
                    # 其后的非工具轮 final 立即落盘为普通气泡，不覆盖。
                    if self.proactive_pending_final is None:
                        self.proactive_pending_final = {
                            "content": payload_content,
                            "payload": dict(data),
                        }
                    else:
                        await self._persist_proactive_final_record(
                            content=payload_content,
                            payload=dict(data),
                            marked=False,
                        )
                should_record = False
            final_segment_started_at = self.history_committer.pending_final_started_at
            if payload_content:
                self.history_committer.reset_durable_pending_final()
            else:
                # 同上：空 final 收尾时把气泡正文落盘，别丢历史。
                await self.history_committer.persist_pending_final_text()
                final_segment_started_at = None

        if should_record:
            extra_fields = {
                k: v for k, v in data.items() if k not in ("event_type", "content")
            }
            if et == EventType.TEAM_MESSAGE.value and "event" in data:
                event_data = data.get("event", {})
                if isinstance(event_data, dict):
                    for k, v in event_data.items():
                        if k not in ("type", "timestamp", "content"):
                            extra_fields[k] = v
            if et in {"chat.final", "chat.tool_call"}:
                extra_fields = self.history_committer.attach_reasoning_content(
                    extra_fields
                )
            # 透传 proactive 标记——刷新页面时前端靠 source 识别卡片
            for pk in (
                "source",
                "proactive_type",
                "proactive_target",
                "automation",
                "proactive_rec_id",
            ):
                if pk not in extra_fields and pk in self.request.params:
                    extra_fields[pk] = self.request.params[pk]
            if not isinstance(extra_fields, dict):
                extra_fields = {}
            extra_fields = (
                self.ports.with_cross_session_history_metadata(
                    self.ports.with_heartbeat_history_metadata(
                        self.ports.with_web_agent_template_metadata(
                            extra_fields,
                            self.request.params,
                            self.cid,
                            event_type=et,
                            payload=data,
                        ),
                        self.request.params,
                    ),
                    self.request.params,
                )
                or {}
            )
            record_timestamp = self.ports.resolve_final_record_timestamp(
                event_type=et,
                segment_started_at=(
                    final_segment_started_at if et == "chat.final" else None
                ),
                extra_fields=extra_fields,
            )
            await self.ports.run_history_io(
                self.ports.append_history_record,
                wait_for_write=True,
                session_id=self.session_id,
                request_id=self.rid,
                channel_id=self.cid,
                role="assistant",
                event_type=et,
                content=data.get("content") or data.get("error") or "",
                timestamp=record_timestamp,
                extra=extra_fields if extra_fields else None,
                mode=self.request.params.get("mode", "unknown"),
            )
            if et == "chat.final":
                self.history_committer.final_content = str(data.get("content", ""))
        if et == "chat.final":
            next_final_content = str(data.get("content", ""))
            if next_final_content:
                self.projector.final_answer_content = next_final_content
                self.projector.final_answer_chunks.clear()
        yield AgentResponseChunk(
            request_id=self.rid,
            channel_id=self.cid,
            payload=data,
            is_complete=False,
        )

    async def _finish(self) -> AsyncIterator[AgentResponseChunk]:
        assistant_message = self.projector.assistant_message
        finalized_assistant_message = (
            await self.ports.finalize_assistant_response_if_a2ui(
                assistant_message,
                channel=self.cid,
                user_query=self.user_turn.text,
                request_id=self.rid or "",
                repair_call=self.repair_call,
                retry_without_a2ui_call=self.retry_without_a2ui_call,
            )
        )
        if finalized_assistant_message and (
            finalized_assistant_message != assistant_message
            or self.projector.suppress_a2ui_stream
        ):
            history_metadata: dict[str, Any] = {}
            for key in (
                "source",
                "proactive_type",
                "proactive_target",
                "automation",
                "proactive_rec_id",
            ):
                if key in self.request.params:
                    history_metadata[key] = self.request.params[key]
            await self.ports.run_history_io(
                self.ports.append_history_record,
                wait_for_write=True,
                session_id=self.session_id,
                request_id=self.rid,
                channel_id=self.cid,
                role="assistant",
                event_type="chat.final",
                content=finalized_assistant_message,
                timestamp=time.time(),
                extra=self.ports.with_heartbeat_history_metadata(
                    self.ports.with_web_agent_template_metadata(
                        self.history_committer.attach_reasoning_content(
                            history_metadata
                        ),
                        self.request.params,
                        self.cid,
                        event_type="chat.final",
                    ),
                    self.request.params,
                ),
                mode=self.request.params.get("mode", "unknown"),
            )
            self.projector.final_answer_content = finalized_assistant_message
            self.projector.final_answer_chunks = []
            self.completion.complete()
            final_chunk = self.ports.make_a2ui_final_chunk(
                request_id=self.rid,
                channel_id=self.cid,
                session_id=self.session_id,
                content=finalized_assistant_message,
            )
            enriched_payload = self.ports.with_web_agent_template_payload(
                final_chunk.payload,
                self.request.params,
                self.cid,
            )
            if enriched_payload is not final_chunk.payload:
                final_chunk = replace(final_chunk, payload=enriched_payload)
            yield final_chunk

        self.completion.complete()

        # cloud memory: after chat hook
        if self.memory_mode == "cloud":
            assistant_message = self.projector.assistant_message
            await self.hooks.after_memory(self.request, assistant_message)

        # auto memory: extract memories after conversation ends
        # 需要 auto_memory_enabled 和 memory.enabled 都为 true 才触发
        mode = (
            self.request.params.get("mode", "code")
            if isinstance(self.request.params, dict)
            else "code"
        )
        config = self.ports.get_config()
        if self.ports.is_auto_memory_enabled(
            mode, config
        ) and self.ports.is_memory_enabled(mode, config):
            self.ports.trigger_auto_memory_extraction(
                self.adapter, self.request, self.session_id, is_stream=True
            )

        await self.completion.wait()
        yield AgentResponseChunk(
            request_id=self.rid,
            channel_id=self.cid,
            payload={"is_complete": True},
            is_complete=True,
        )
