# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""
Legacy ReActAgent - Backward compatible version

This is the legacy version of ReActAgent for backward compatibility.
For new code, use openjiuwen.core.single_agent.agents.react_agent.ReActAgent

Will be removed in v1.0.0
"""

import asyncio
import json
from typing import Dict, Any, AsyncIterator, List

from pydantic import ValidationError

from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.utils.hash_util import generate_key
from openjiuwen.core.common.utils.message_utils import MessageUtils
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.logging import logger
from openjiuwen.core.session import Config
from openjiuwen.core.session.workflow import Session as WorkflowSession
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.interaction.interaction import SimpleAgentInteraction
from openjiuwen.core.session.internal.agent import AgentSession as InternalAgentSession
from openjiuwen.core.session.internal.wrapper import StateSession
from openjiuwen.core.session.tracer import Tracer
from openjiuwen.core.single_agent.legacy.agent import BaseAgent
from openjiuwen.core.single_agent.legacy.config import (
    LegacyReActAgentConfig,
)
from openjiuwen.core.workflow import Workflow
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, ModelConfig, ModelClientConfig, \
    ModelRequestConfig, Model
from openjiuwen.core.foundation.prompt import PromptTemplate
from openjiuwen.core.foundation.tool import Tool


class TaskSession(StateSession):
    """
    deprecated
    """
    def __init__(self, session_id: str = None, config: Config = None, resource_mgr=None, card=None):
        if config is None:
            config = Config()
        super().__init__(InternalAgentSession(session_id, config, resource_mgr, card=card))
        self._interaction = None

    async def trace(self, data: dict):
        pass

    async def trace_error(self, error: Exception):
        pass

    async def interact(self, value):
        if self._interaction is None:
            self._interaction = SimpleAgentInteraction(self._inner)
        await self._interaction.wait_user_inputs(value)

    def get_inner_session(self):
        return self._inner

    def stream_iterator(self) -> AsyncIterator[Any]:
        return self._inner.stream_writer_manager().stream_output()

    async def post_run(self):
        if isinstance(self._inner, InternalAgentSession):
            await self._inner.stream_writer_manager().stream_emitter().close()
            await self._inner.checkpointer().post_agent_execute(self._inner)

    def tracer(self) -> Tracer:
        return self._inner.tracer()

    def get_envs(self):
        return getattr(self._inner.config(), "_env")

    def create_workflow_session(self) -> WorkflowSession:
        return WorkflowSession(parent=self._inner, session_id=self.session_id())

    def get_session_id(self):
        return self.session_id()


class AgentSession:
    """
    deprecated
    """

    def __init__(self, config: Config = None):
        self._config = config if config is not None else Config()
        self._checkpointer = CheckpointerFactory.get_checkpointer()

    async def pre_run(self, **kwargs) -> Session:
        session_id = kwargs.get("session_id")
        if session_id is None:
            session_id = kwargs.get("trace_id")
        inputs = kwargs.get("inputs")
        session = TaskSession(session_id=session_id, config=self._config)
        await self._checkpointer.pre_agent_execute(session.base(), inputs)
        return session

    async def release(self, session_id: str):
        await self._checkpointer.release(session_id)


class LegacyReActAgent(BaseAgent):
    """Legacy ReAct Agent for backward compatibility
    
    For new code, use openjiuwen.core.single_agent.agents.react_agent.ReActAgent
    
    Will be removed in v1.0.0
    """

    def __init__(
            self,
            agent_config: LegacyReActAgentConfig,
            workflows: List[Workflow] = None,
            tools: List[Tool] = None
    ):
        """Initialize Legacy ReActAgent
        
        Args:
            agent_config: ReAct config
            workflows: Workflow list
            tools: Tool list
        """
        super().__init__(agent_config)
        # 2. Create Session
        self._session = AgentSession(config=self._config)
        self._llm = None

        if tools:
            self.add_tools(tools)
        if workflows:
            self.add_workflows(workflows)

    def _get_llm(self):
        """Get LLM instance"""
        if self._llm is None:
            model_id = generate_key(
                self.agent_config.model.model_info.api_key,
                self.agent_config.model.model_info.api_base,
                self.agent_config.model.model_provider
            )

            model_client_config = ModelClientConfig(
                client_id=model_id,
                client_provider=self.agent_config.model.model_provider,
                api_key=self.agent_config.model.model_info.api_key,
                api_base=self.agent_config.model.model_info.api_base,
                verify_ssl=False,
                ssl_cert=None,
            )
            model_request_config = ModelRequestConfig(
                model=self.agent_config.model.model_info.model_name,
                temperature=self.agent_config.model.model_info.temperature,
                top_p=self.agent_config.model.model_info.top_p,
                **(self.agent_config.model.model_info.model_extra or {})
            )
            self._llm = Model(model_client_config=model_client_config, model_config=model_request_config)

        return self._llm

    async def call_model(self, user_input: str, session: Session, is_first_call: bool = False):
        """Call LLM for reasoning"""
        if is_first_call:
            await MessageUtils.add_user_message(user_input, self.context_engine, session)

        chat_history = MessageUtils.get_chat_history(
            self.context_engine, session,
            self.agent_config
        )

        messages = []
        try:
            system_prompt = PromptTemplate(content=self.agent_config.prompt_template).to_messages()
            for prompt in system_prompt:
                prompt_dict = prompt.model_dump(exclude_none=True)
                messages.append(prompt_dict)
        except ValidationError as e:
            raise build_error(
                StatusCode.AGENT_PROMPT_PARAM_ERROR,
                error_msg=str(e),
                cause=e
            ) from e

        for msg in chat_history:
            msg_dict = msg.model_dump(exclude_none=True)
            messages.append(msg_dict)
        from openjiuwen.core.runner import Runner
        tools = await Runner.resource_mgr.get_tool_infos(tag=self.agent_config.id)
        llm = self._get_llm()
        llm_output = await llm.invoke(
            messages,
            tools=tools,
            model=self.agent_config.model.model_info.model_name
        )

        ai_message = AssistantMessage(
            content=llm_output.content,
            tool_calls=llm_output.tool_calls
        )
        await MessageUtils.add_ai_message(ai_message, self.context_engine, session)
        return llm_output

    async def _execute_tool_call(self, tool_call, session: Session) -> Any:
        """Execute single tool call"""
        tool_name = tool_call.name
        try:
            tool_args = json.loads(tool_call.arguments) if isinstance(tool_call.arguments, str) else tool_call.arguments
        except (json.JSONDecodeError, AttributeError):
            tool_args = {}
        from openjiuwen.core.runner import Runner
        tool = Runner.resource_mgr.get_tool(tool_id=tool_name, tag=self.agent_config.id)
        if not tool:
            raise ValueError(f"Tool not found: {tool_name}")

        result = await tool.invoke(tool_args)

        tool_message = ToolMessage(
            content=str(result),
            tool_call_id=tool_call.id
        )
        await MessageUtils.add_tool_message(tool_message, self.context_engine, session)
        return result

    async def invoke(self, inputs: Dict, session: Session = None) -> Dict:
        """Sync call - Complete ReAct loop"""
        session_id = inputs.get("conversation_id", "default_session")
        session_created = False
        if session is None:
            session = await self._session.pre_run(session_id=session_id, inputs=inputs)
            session_created = True
        await self.context_engine.create_context(session=session)

        try:
            user_input = inputs.get("query", "")
            if not user_input:
                return {"output": "No query provided", "result_type": "error"}

            iteration = 0
            max_iteration = self.agent_config.constrain.max_iteration
            is_first_call = True

            while iteration < max_iteration:
                iteration += 1
                logger.info(f"ReAct iteration {iteration}")

                llm_output = await self.call_model(
                    user_input,
                    session,
                    is_first_call=is_first_call
                )
                is_first_call = False

                if not llm_output.tool_calls:
                    logger.info("No tool calls, task completed")
                    return {
                        "output": llm_output.content,
                        "result_type": "answer"
                    }

                for tool_call in llm_output.tool_calls:
                    tool_name = tool_call.name
                    logger.info(f"Executing tool: {tool_name}")
                    result = await self._execute_tool_call(tool_call, session)
                    logger.info(f"Tool {tool_name} completed with result: {result}")

            logger.warning(f"Exceeded max iteration {max_iteration}")
            return {
                "output": "Exceeded max iteration",
                "result_type": "error"
            }
        finally:
            if session_created:
                await session.post_run()

    async def stream(self, inputs: Dict, session: Session = None) -> AsyncIterator[Any]:
        """Stream call - minimal version"""
        session_id = inputs.get("conversation_id", "default_session")
        if session is None:
            agent_session = await self._session.pre_run(
                session_id=session_id, inputs=inputs
            )
            need_cleanup = True
            own_stream = True
        else:
            agent_session = session
            need_cleanup = False
            own_stream = False
            from openjiuwen.core.runner import Runner
            if hasattr(self, '_tools') and self._tools:
                tools_to_add = [(tool.card.name, tool) for tool in self._tools]
                Runner.resource_mgr.add_tool(tool=tools_to_add, tag=self.agent_config.id)
        await self.context_engine.create_context(session=agent_session)

        final_result_holder = {"result": None}

        async def stream_process():
            try:
                final_result = await self.invoke(inputs, agent_session)
                final_result_holder["result"] = final_result
                await agent_session.write_stream(OutputSchema(
                    type="answer",
                    index=0,
                    payload={"output": final_result, "result_type": "answer"}
                ))
            except Exception as e:
                logger.error(f"ReActAgent stream error: {e}")
            finally:
                if need_cleanup:
                    await agent_session.post_run()

        task = asyncio.create_task(stream_process())

        if own_stream:
            async for result in agent_session.stream_iterator():
                yield result

        await task

        if not own_stream and final_result_holder["result"] is not None:
            yield final_result_holder["result"]


def create_react_agent_config(
        agent_id: str,
        agent_version: str,
        description: str,
        model: ModelConfig,
        prompt_template: List[Dict]
) -> LegacyReActAgentConfig:
    """Create ReAct Agent config

    Args:
        agent_id: Agent ID
        agent_version: Agent version
        description: Agent description
        model: Model config
        prompt_template: Prompt template

    Returns:
        LegacyReActAgentConfig instance
    
    Deprecated:
        This function is deprecated and will be removed in v1.0.0.
        Use ReActAgentConfig directly instead.
    """
    import warnings
    warnings.warn(
        "create_react_agent_config() is deprecated and will be removed "
        "in the future. Please use ReActAgentConfig() constructor instead.",
        DeprecationWarning,
        stacklevel=2
    )
    return LegacyReActAgentConfig(
        id=agent_id,
        version=agent_version,
        description=description,
        model=model,
        prompt_template=prompt_template
    )
