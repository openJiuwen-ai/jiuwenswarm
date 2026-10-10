# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ModelResponseGuardRail - 模型响应语义守卫.

检测两类会被整条链路静默吞掉的模型异常（对应 JiuwenTest 缺陷守护用例
TC-CHAT_SEND-MF-002 / MF-006，[PRODUCTION_BUG_SUSPECT]）：

  - 流不完整（incomplete stream）：流式模型调用结束时未收到任何
    ``finish_reason``（典型场景：SSE 中途断连、网关提前关闭连接）。
    openai 兼容客户端的 ``async for`` 在连接关闭时正常退出，不抛异常，
    截断残句会被当作完整答案。
  - 空响应（empty response）：模型调用协议层完全正常（200 + SSE +
    ``finish_reason=stop`` + ``[DONE]``），但零文本产出。客户端表现为
    「模型失声」却收到成功终止帧。

命中任一检测即抛 ``MODEL_CALL_FAILED``：

  * 异常沿 ``_railed_model_call → _call_model → ReAct loop → invoke``
    传播到 runner 的 ``stream_process``，被转写为
    ``{"result_type": "error"}`` 的 answer 事件；
  * jiuwenswarm 适配器 ``_parse_stream_chunk`` 将其映射为 ``chat.error``
    帧下发给客户端，计费按 FAILED 收口。

由此客户端可以感知「输出被截断 / 模型失声」，而不是把异常响应当作
正常完成。已知豁免：图片输入降级路径（``_build_image_input_unsupported_message``
返回的提示消息同样没有 ``finish_reason``，不能误伤该优雅降级）。
"""

from __future__ import annotations

from typing import Any

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

# 与 LLMRetryRail 相同的 inspector 挂载点（openjiuwen react_agent 消费）。
_STREAM_CHUNK_INSPECTORS_KEY = "_stream_chunk_inspectors"
_GUARD_STATE_KEY = "_model_response_guard_state"


def _messages_contain_image_input(messages: Any) -> bool:
    """近似复刻 ReActAgent._messages_contain_image_input：判断消息是否带图片输入。

    仅用于豁免图片降级路径，采用保守的 JSON 形态扫描；实现保持私有，
    不依赖 openjiuwen 私有方法签名。
    """

    def _contains(value: Any, depth: int = 0) -> bool:
        if depth > 8:
            return False
        if isinstance(value, dict):
            value_type = value.get("type")
            if value_type in ("image", "image_url"):
                return True
            if "image_url" in value or "image" in value:
                return True
            return any(_contains(child, depth + 1) for child in value.values())
        if isinstance(value, (list, tuple)):
            return any(_contains(child, depth + 1) for child in value)
        return False

    if not messages:
        return False
    for message in messages:
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        if _contains(content):
            return True
    return False


class ModelResponseGuardRail(DeepAgentRail):
    """对每次模型调用的结果做流完整性 / 空产出校验，失败即抛 MODEL_CALL_FAILED.

    priority 取负值：callback 框架按 priority 降序执行（大者先跑），
    本 rail 的 ``after_model_call`` 必须最后执行——它命中即抛异常，
    会中断 rail 链，不能抢在其它 rail 的收尾钩子（计费/取消检测等）之前。
    """

    priority = -100

    def __init__(
        self,
        *,
        check_incomplete_stream: bool = True,
        check_empty_response: bool = True,
    ) -> None:
        super().__init__()
        self.check_incomplete_stream = check_incomplete_stream
        self.check_empty_response = check_empty_response
        self._saw_finish_reason = False

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """每次模型调用前重置状态并挂载流块探针（重试 attempt 不重置）."""
        if not getattr(ctx, "retry_attempt", 0):
            self._saw_finish_reason = False
        ctx.extra[_GUARD_STATE_KEY] = {"saw_finish_reason": False}
        inspectors = ctx.extra.get(_STREAM_CHUNK_INSPECTORS_KEY)
        if not isinstance(inspectors, list):
            inspectors = []
        inspectors = [
            inspector
            for inspector in inspectors
            if getattr(inspector, "__self__", None) is not self
        ]
        inspectors.append(self.inspect_stream_chunk)
        ctx.extra[_STREAM_CHUNK_INSPECTORS_KEY] = inspectors

    async def inspect_stream_chunk(self, ctx: AgentCallbackContext, chunk: Any) -> None:
        """记录流中是否出现过 finish_reason（流完整性判定的第一手证据）."""
        if getattr(chunk, "finish_reason", None):
            self._saw_finish_reason = True
            state = ctx.extra.get(_GUARD_STATE_KEY)
            if isinstance(state, dict):
                state["saw_finish_reason"] = True

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """模型调用返回后做语义校验；命中即抛 MODEL_CALL_FAILED."""
        # 本次调用本身已失败（on_model_exception/重试路径会处理），不重复判定；
        # 该分支下 after 钩子的异常也会被 rail 装饰器掩蔽，徒增日志。
        if getattr(ctx, "exception", None) is not None:
            return

        inputs = getattr(ctx, "inputs", None)
        response = getattr(inputs, "response", None) if inputs is not None else None
        if response is None or isinstance(response, (dict, str, bytes)):
            return

        state = ctx.extra.get(_GUARD_STATE_KEY)
        saw_finish_reason = self._saw_finish_reason or (
            bool(state.get("saw_finish_reason")) if isinstance(state, dict) else False
        )
        saw_finish_reason = saw_finish_reason or bool(getattr(response, "finish_reason", None))

        content = getattr(response, "content", None) or ""
        reasoning = getattr(response, "reasoning_content", None) or ""
        tool_calls = getattr(response, "tool_calls", None) or []
        has_output = bool(str(content).strip()) or bool(str(reasoning).strip()) or bool(tool_calls)

        if (
            self.check_incomplete_stream
            and not saw_finish_reason
            and self._is_streaming(ctx)
        ):
            if self._is_image_input_fallback(ctx, response):
                return
            raise build_error(
                StatusCode.MODEL_CALL_FAILED,
                error_msg=(
                    "模型流未正常结束（未收到 finish_reason），响应可能被截断，"
                    "无法确认输出完整性"
                ),
            )

        if self.check_empty_response and not has_output:
            raise build_error(
                StatusCode.MODEL_CALL_FAILED,
                error_msg="模型返回空响应（协议正常完成但无任何文本/工具调用产出）",
            )

    @staticmethod
    def _is_streaming(ctx: AgentCallbackContext) -> bool:
        """仅对流式调用做流完整性判定；非流式响应的 finish_reason 已被规范化."""
        extra = getattr(ctx, "extra", None)
        return bool(extra.get("_streaming")) if isinstance(extra, dict) else False

    @staticmethod
    def _is_image_input_fallback(ctx: AgentCallbackContext, response: Any) -> bool:
        """图片输入优雅降级路径：模型不支持图片时返回的提示消息同样无 finish_reason."""
        content = str(getattr(response, "content", None) or "")
        if "不支持图片输入" not in content:
            return False
        inputs = getattr(ctx, "inputs", None)
        messages = getattr(inputs, "messages", None) if inputs is not None else None
        return _messages_contain_image_input(messages)
