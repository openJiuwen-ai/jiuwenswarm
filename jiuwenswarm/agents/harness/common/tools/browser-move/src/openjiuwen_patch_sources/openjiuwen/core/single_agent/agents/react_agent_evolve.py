# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from openjiuwen.core.common.logging import logger
from openjiuwen.core.operator import Operator, LLMCallOperator, ToolCallOperator
from openjiuwen.core.context_engine import ContextEngine, ModelContext
from openjiuwen.core.foundation.llm import AssistantMessage, Model, UserMessage, SystemMessage
from openjiuwen.core.memory import LongTermMemory, MemoryScopeConfig
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.session.stream.base import StreamMode
from openjiuwen.core.single_agent.base import BaseAgent
from openjiuwen.core.single_agent.middleware.base import AgentCallbackEvent
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.core.single_agent.agents.react_agent import ReActAgentConfig


class ReActAgentEvolve(BaseAgent):
    """ReAct paradigm Agent implementation
    ReAct loop: Reasoning -> Acting -> Observation -> Repeat

    Input format (compatible with legacy):
        {"query": "user question", "conversation_id": "session_123"}

    Output format (compatible with legacy):
        invoke: {"output": "response content", "result_type": "answer|error"}
        stream: yields OutputSchema objects

    Note:
        This agent currently does not support Runner.run_agent().
        Use agent.invoke() directly with a session parameter.
    """

    def __init__(
        self,
        card: AgentCard,
    ):
        """Initialize ReActAgent

        Args:
            card: Agent card (required)
        """
        self._config = self._create_default_config()
        self.context_engine = ContextEngine(
            self._config.context_engine_config
        )
        self._llm = None
        # Unified naming: *_op indicates evolvable Operator
        # LLM Operator uses lazy init: model_client_config/model_config_obj may only be ready after configure()
        self._llm_op: Optional[LLMCallOperator] = None
        self._tool_op: Optional[ToolCallOperator] = None
        self._init_memory_scope()
        # Lazy import to avoid circular dependency: skills -> runner -> single_agent -> skills
        from openjiuwen.core.single_agent.skills import SkillUtil

        self._skill_util = SkillUtil(self._config.sys_operation_id)
        super().__init__(card)
        # Operator depends on ability_kit, so placed after BaseAgent initialization
        self._tool_op = ToolCallOperator(
            tool=None,
            tool_call_id="react_tool",
            tool_executor=self.ability_manager.execute,
            tool_registry=self.ability_manager,
        )

    def _init_memory_scope(self) -> None:
        """Initialize memory scope (subclass can override configuration)"""
        if self._config.mem_scope_id:
            LongTermMemory().set_scope_config(self._config.mem_scope_id, MemoryScopeConfig())

    def _create_default_config(self) -> ReActAgentConfig:
        """Create default configuration"""
        return ReActAgentConfig()

    def configure(self, config: ReActAgentConfig) -> 'BaseAgent':
        """Set configuration

        Args:
            config: ReActAgentConfig configuration object

        Returns:
            self (supports chaining)

        Note:
            After config update, context_engine and memory_scope
            will be updated accordingly
        """
        old_config = self._config
        self._config = config

        # Reset LLM if model config changed
        if (old_config.model_provider != config.model_provider or
                old_config.api_key != config.api_key or
                old_config.api_base != config.api_base):
            self._llm = None

        # Update context_engine if context window limit changed
        if old_config.context_engine_config != config.context_engine_config:
            self.context_engine = ContextEngine(
                config.context_engine_config
            )

        # Update memory_scope if memory scope ID changed
        if old_config.mem_scope_id != config.mem_scope_id:
            self._init_memory_scope()

        # Reset sys operation id if changed
        if old_config.sys_operation_id != config.sys_operation_id:
            self.lazy_init_skill()

        return self

    @staticmethod
    def _normalize_user_input(inputs: Any) -> str:
        if isinstance(inputs, dict):
            user_input = inputs.get("query")
            if user_input is None:
                raise ValueError("Input dict must contain 'query'")
            return user_input
        if isinstance(inputs, str):
            return inputs
        raise ValueError("Input must be dict with 'query' or str")

    def _on_llm_parameter_updated(self, target: str, value: Any) -> None:
        # Keep AgentConfig aligned with Operator (especially system_prompt)
        if target == "system_prompt":
            if isinstance(value, list):
                content = value
            else:
                content = [{"role": "system", "content": str(value)}]
            self._config.prompt_template = content

    def _resolve_llm_model_name(self) -> str:
        """Single source of truth: prefer model_name from ModelRequestConfig.

        Consistent with core Model construction.
        """
        model_name_from_obj = (
            getattr(self._config.model_config_obj, "model_name", None)
            if self._config.model_config_obj is not None
            else None
        )
        model_name_from_field = self._config.model_name
        return model_name_from_obj or model_name_from_field

    def _get_llm_op(self) -> LLMCallOperator:
        """LLMCallOperator for self-evolving (react_llm), syncs back to config.prompt_template via callback."""
        if self._llm_op is None:
            llm = self._get_llm()
            model_name = self._resolve_llm_model_name()
            system_prompt = getattr(self._config, "prompt_template", []) or []
            self._llm_op = LLMCallOperator(
                model_name=model_name,
                llm=llm,
                system_prompt=system_prompt,
                user_prompt="{{query}}",
                freeze_system_prompt=False,
                freeze_user_prompt=True,
                llm_call_id="react_llm",
                on_parameter_updated=self._on_llm_parameter_updated,
            )
        else:
            # prompt_template may change after configure/self-evolving:
            # ensure operator internal view aligns with config
            self._llm_op.update_system_prompt(getattr(self._config, "prompt_template", []))
        return self._llm_op

    def _get_skill_messages(self) -> List[SystemMessage]:
        # Skill prompt: injected as additional system message (not written to evolvable system_prompt)
        if self._skill_util is None or not self._skill_util.has_skill():
            return []
        return [SystemMessage(content=self._skill_util.get_skill_prompt())]

    def get_operators(self) -> Dict[str, Operator]:
        """Returns evolvable operator registry (operator_id -> Operator)."""
        ops: Dict[str, Operator] = {}
        if self._tool_op is not None:
            ops[self._tool_op.operator_id] = self._tool_op
        try:
            llm_op = self._get_llm_op()
            ops[llm_op.operator_id] = llm_op
        except Exception:
            # Skip llm operator when model_client_config not configured (remains importable/buildable)
            pass
        return ops

    def _get_llm(self) -> Model:
        """Get LLM instance (lazy initialization)

        Returns:
            Model instance

        Raises:
            ValueError: If model configuration is not configured
        """
        if self._llm is None:
            if self._config.model_client_config is None and self._config.model_config_obj is None:
                raise ValueError("model_client_config is required. Use configure_model_client() to set it.")
            self._llm = Model(
                model_client_config=self._config.model_client_config, model_config=self._config.model_config_obj
            )
        return self._llm

    async def register_skill(self, skill_path: Union[str, List[str]]):
        """Register a skill"""
        self._skill_util.register_skills(skill_path, self)

    async def _init_context(
            self,
            session: Optional[Session]
    ) -> ModelContext:
        if self._config.context_processors:
            from openjiuwen.core.context_engine.token.tiktoken_counter import TiktokenCounter
            context = await self.context_engine.create_context(
                session=session,
                processors=self._config.context_processors,
                token_counter=TiktokenCounter()
            )
        else:
            context = await self.context_engine.create_context(
                session=session
            )
        context_reloader = context.reloader_tool()
        if self._config.context_engine_config.enable_reload:
            self.ability_manager.add(context_reloader.card)
            from openjiuwen.core.runner import Runner
            if not Runner.resource_mgr.get_tool(context_reloader.card.id, tag=self.card.id):
                Runner.resource_mgr.add_tool(context_reloader, tag=self.card.id)
        else:
            self.ability_manager.remove(context_reloader.card.name)
        return context

    async def invoke(self, inputs: Any, session: Optional[Session] = None) -> Dict[str, Any]:
        """Execute ReAct process

        Args:
            inputs: User input, supports the following formats:
                - dict: {"query": "...", "conversation_id": "..."}
                - str: Used directly as query
            session: Session object (required for tool execution)

        Returns:
            Dict with output and result_type
        """
        user_input = self._normalize_user_input(inputs)

        # Hook: before invoke
        await self._execute_callbacks(AgentCallbackEvent.BEFORE_INVOKE, inputs=inputs)

        # Get or create model context
        context = await self._init_context(session)
        # Add user message to context
        await context.add_messages(UserMessage(content=user_input))
        # Get tool info from _ability_manager
        tools = await self.ability_manager.list_tool_info()

        result = None

        # ReAct loop
        for iteration in range(self._config.max_iterations):
            logger.info(f"ReAct iteration {iteration + 1}/{self._config.max_iterations}")

            # Get context window (system_prompt injected by react_llm operator)
            context_window = await context.get_context_window(system_messages=[], tools=tools if tools else None)

            # Hook: before model call
            await self._execute_callbacks(
                AgentCallbackEvent.BEFORE_MODEL_CALL,
                inputs=inputs,
                iteration=iteration + 1,
                messages=context_window.get_messages()
            )

            skill_messages = self._get_skill_messages()

            # Call LLM via Operator (react_llm)
            llm_op = self._get_llm_op()
            history_messages = context_window.get_messages()
            ai_message = await llm_op.invoke(
                inputs={"query": user_input, "messages": [*skill_messages, *history_messages]},
                session=session,
                tools=context_window.get_tools() or None,
            )

            # Hook: after model call
            await self._execute_callbacks(
                AgentCallbackEvent.AFTER_MODEL_CALL,
                inputs=inputs,
                iteration=iteration + 1,
                response=ai_message
            )

            # Add AI message to context
            ai_msg_for_context = AssistantMessage(content=ai_message.content, tool_calls=ai_message.tool_calls)
            await context.add_messages(ai_msg_for_context)

            # Check for tool calls
            if ai_message.tool_calls:
                # Log tool calls
                for tool_call in ai_message.tool_calls:
                    logger.info(f"Executing tool: {tool_call.name} with args: {tool_call.arguments}")

                    # Hook: before tool call
                    await self._execute_callbacks(
                        AgentCallbackEvent.BEFORE_TOOL_CALL,
                        inputs=inputs,
                        iteration=iteration + 1,
                        tool_name=tool_call.name,
                        tool_args=tool_call.arguments
                    )

                # Execute tools via Operator (react_tool)
                tool_op = self._tool_op
                if tool_op is None:
                    raise RuntimeError("react_tool operator is not initialized")
                results = await tool_op.invoke({"tool_calls": ai_message.tool_calls}, session=session)

                # Process results and add tool messages to context
                for idx, (tool_result, tool_msg) in enumerate(results):
                    logger.info(f"Tool result: {tool_result}")
                    await context.add_messages(tool_msg)

                    # Hook: after tool call
                    tool_call = ai_message.tool_calls[idx]
                    await self._execute_callbacks(
                        AgentCallbackEvent.AFTER_TOOL_CALL,
                        inputs=inputs,
                        iteration=iteration + 1,
                        tool_name=tool_call.name,
                        tool_args=tool_call.arguments,
                        tool_result=tool_result
                    )
            else:
                # No tool calls, return AI response
                await self.context_engine.save_contexts(session)
                result = {
                    "output": ai_message.content,
                    "result_type": "answer"
                }
                # Hook: after invoke
                await self._execute_callbacks(
                    AgentCallbackEvent.AFTER_INVOKE,
                    inputs=inputs,
                    result=result
                )
                return result

        # Max iterations reached
        await self.context_engine.save_contexts(session)
        result = {
            "output": "Max iterations reached without completion",
            "result_type": "error"
        }
        # Hook: after invoke
        await self._execute_callbacks(
            AgentCallbackEvent.AFTER_INVOKE,
            inputs=inputs,
            result=result
        )
        return result

    async def stream(
        self,
        inputs: Any,
        session: Optional[Session] = None,
        stream_modes: Optional[List[StreamMode]] = None,
    ) -> AsyncIterator[Any]:
        """Stream execute ReAct process

        Args:
            inputs: User input (required in new version)
            session: Session object (required in new version)
            stream_modes: Stream output modes (optional)

        Yields:
            OutputSchema objects from stream_iterator
        """
        final_result_holder = {"result": None}

        if session is not None:
            await session.pre_run()

        async def stream_process():
            try:
                final_result = await self.invoke(inputs, session)
                final_result_holder["result"] = final_result
                # Write to session stream if available
                if session is not None and hasattr(session, "write_stream"):
                    await session.write_stream(
                        OutputSchema(type="answer", index=0, payload={"output": final_result, "result_type": "answer"})
                    )
            except Exception as e:
                logger.error(f"ReActAgent stream error: {e}")
                final_result_holder["result"] = {"output": str(e), "result_type": "error"}
            finally:
                # Close stream
                if session is not None:
                    await self.context_engine.save_contexts(session)
                    await session.post_run()

        task = asyncio.create_task(stream_process())

        # Read from stream_iterator and yield
        if session is not None and hasattr(session, "stream_iterator"):
            async for result in session.stream_iterator():
                yield result

        await task


__all__ = [
    "ReActAgentEvolve",
]
