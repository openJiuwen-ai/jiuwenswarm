# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import copy
from typing import List, Dict, Any, Optional, TYPE_CHECKING

from openjiuwen.core.common.constants.enums import TaskType
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.single_agent.legacy import AgentConfig
from openjiuwen.core.controller.legacy.event.event import Event
from openjiuwen.core.controller.legacy.task.task import Task, TaskInput
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.logging import logger
from openjiuwen.core.common.security.json_utils import JsonUtils
from openjiuwen.core.common.security.user_config import UserConfig
from openjiuwen.core.common.utils.hash_util import generate_key
from openjiuwen.core.context_engine import ContextEngine
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.foundation.llm import ModelConfig, BaseMessage, AssistantMessage, UserMessage, ToolMessage, \
    ModelClientConfig, ModelRequestConfig, Model
from openjiuwen.core.foundation.prompt import PromptTemplate
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.workflow import WorkflowOutput

if TYPE_CHECKING:
    from openjiuwen.core.runner import Runner


class MessageHandlerUtils:

    @staticmethod
    def format_llm_inputs(
            inputs: Any,
            chat_history: List[BaseMessage],
            config: AgentConfig,
            keywords: Optional[dict] = None
    ) -> List[BaseMessage]:
        if isinstance(inputs, InteractiveInput):
            user_fields = {}
        elif isinstance(inputs, dict):
            user_fields = copy.deepcopy(inputs)
        else:
            user_fields = {"query": inputs}

        if keywords:
            user_fields.update(keywords)

        system_prompt = (PromptTemplate(
            content=config.prompt_template
        ).format(user_fields).to_messages())

        return MessageHandlerUtils.concat_system_prompt_with_chat_history(system_prompt, chat_history)

    @staticmethod
    def concat_system_prompt_with_chat_history(system_prompt: List[BaseMessage],
                                               chat_history: List[BaseMessage]) -> List[BaseMessage]:
        result_messages = []

        if not chat_history or chat_history[0].role != "system":
            result_messages.extend(system_prompt)

        result_messages.extend(chat_history)

        return result_messages

    @staticmethod
    def parse_llm_output(response: BaseMessage, config: AgentConfig) -> List[Task]:
        """Parse LLM output, return task list"""
        return MessageHandlerUtils.create_tasks_from_tool_calls(
            response.tool_calls, config
        )

    @staticmethod
    def create_tasks_from_tool_calls(
            tool_calls: List[ToolCall],
            config: AgentConfig
    ) -> List[Task]:
        if not tool_calls:
            return []

        result = []
        for tool_call in tool_calls:
            tool_name = tool_call.name
            for workflow in config.workflows:
                if workflow.name == tool_name:
                    task_type = TaskType.WORKFLOW
                    target_id = f"{workflow.id}_{workflow.version}"
                    arguments = {}
                    try:
                        arguments = JsonUtils.safe_json_loads(tool_call.arguments)
                    except Exception as e:
                        if UserConfig.is_sensitive():
                            logger.error("LLM Agent parse tool call workflow's arguments error")
                            raise build_error(StatusCode.AGENT_CONTROLLER_TOOL_EXECUTION_PROCESS_ERROR,
                                              error_msg="LLM-generated workflow arguments are invalid") from e
                        else:
                            logger.error(f"LLM Agent parse tool call workflow({tool_name})'s arguments error: "
                                         f"{tool_call.arguments}")
                            raise build_error(StatusCode.AGENT_CONTROLLER_TOOL_EXECUTION_PROCESS_ERROR,
                                              error_msg=f"LLM-generated workflow ({tool_name}) arguments "
                                                        f"are invalid: {tool_call.arguments}") from e

                    result.append(Task(
                        task_id=tool_call.id,
                        input=TaskInput(
                            target_id=target_id,
                            target_name=tool_name,
                            arguments=arguments
                        ),
                        task_type=task_type
                    ))
                    break
            for plugin in config.plugins:
                if plugin.name == tool_name:
                    task_type = TaskType.PLUGIN
                    arguments = {}
                    try:
                        arguments = JsonUtils.safe_json_loads(tool_call.arguments)
                    except Exception as e:
                        if UserConfig.is_sensitive():
                            logger.error("LLM Agent parse tool call plugin's arguments error")
                            raise build_error(StatusCode.AGENT_CONTROLLER_TOOL_EXECUTION_PROCESS_ERROR,
                                              error_msg="LLM-generated plugin arguments are invalid") from e
                        else:
                            logger.error(f"LLM Agent parse tool call plugin({tool_name})'s arguments error: "
                                         f"{tool_call.arguments}")
                            raise build_error(StatusCode.AGENT_CONTROLLER_TOOL_EXECUTION_PROCESS_ERROR,
                                              error_msg=f"LLM-generated plugin ({tool_name}) arguments "
                                                        f"are invalid: {tool_call.arguments}") from e
                    result.append(Task(
                        task_id=tool_call.id,
                        input=TaskInput(
                            target_name=tool_name,
                            arguments=arguments
                        ),
                        task_type=task_type
                    ))
                    break
        if not result:
            raise build_error(
                StatusCode.AGENT_TOOL_NOT_FOUND,
                error_msg="failed to create task from tool calls"
            )
        return result

    @staticmethod
    def determine_task_type(tool_name: str, config: AgentConfig) -> TaskType:
        for workflow in config.workflows:
            if tool_name == workflow.name:
                return TaskType.WORKFLOW

        for plugin in config.plugins:
            if tool_name == plugin.name:
                return TaskType.PLUGIN

        raise build_error(StatusCode.AGENT_TOOL_NOT_FOUND, error_msg=f"not find tool call type: {tool_name}")

    @staticmethod
    def is_interaction_result(exec_result: Any) -> bool:
        return (isinstance(exec_result, dict) and
                exec_result.get("error") and
                isinstance(exec_result.get("value"), list))

    @staticmethod
    def create_interrupt_result(e, tool_name: str) -> Dict[str, Any]:
        return {
            "error": True,
            "value": e.message,
            "tool_name": tool_name
        }

    @staticmethod
    def validate_execution_inputs(exec_result: Any, sub_task_result: Any) -> bool:
        return exec_result is not None

    @staticmethod
    def should_add_user_message(query: str, context_engine: ContextEngine, session: Session) -> bool:
        agent_context = context_engine.get_context(session_id=session.get_session_id())
        last_message = agent_context.get_messages(size=1)

        if not last_message:
            return True
        last_message = last_message[0]

        if last_message.role == 'tool':
            logger.info("Skipping user message - post-tool-call request")
            return False

        if last_message.role == 'user' and last_message.content == query:
            logger.info("Skipping duplicate user message")
            return False

        return True

    @staticmethod
    async def add_user_message(query: Any, context_engine: ContextEngine, session: Session):
        if MessageHandlerUtils.should_add_user_message(query, context_engine, session):
            agent_context = context_engine.get_context(session_id=session.get_session_id())
            user_message = UserMessage(content=query)
            await agent_context.add_messages(user_message)
            if UserConfig.is_sensitive():
                logger.info(f"Added user message")
            else:
                logger.info(f"Added user message: {query}")

    @staticmethod
    async def add_ai_message(ai_message: AssistantMessage, context_engine: ContextEngine, session: Session):
        if ai_message:
            agent_context = context_engine.get_context(session_id=session.get_session_id())
            await agent_context.add_messages(ai_message)

    @staticmethod
    async def add_tool_result(event: Event, context_engine: ContextEngine, session: Session):
        if event:
            agent_context = context_engine.get_context(session_id=session.get_session_id())
            tool_result = event.content.task_result.output
            if isinstance(tool_result, OutputSchema):
                payload = tool_result.payload
                if isinstance(payload, dict):
                    tool_result = payload.get("output", "")
            elif isinstance(tool_result, WorkflowOutput):
                tool_result = tool_result.result
            content = JsonUtils.safe_json_dumps(tool_result, str(tool_result), ensure_ascii=False)
            tool_message = ToolMessage(content=content,
                                       tool_call_id=event.context.task_id)
            await agent_context.add_messages(tool_message)

    @staticmethod
    def get_chat_history(context_engine: ContextEngine, session: Session, config: AgentConfig) -> List[BaseMessage]:
        agent_context = context_engine.get_context(session_id=session.get_session_id())
        chat_history = agent_context.get_messages()
        max_rounds = config.constrain.reserved_max_chat_rounds
        return chat_history[-2 * max_rounds:]

    @staticmethod
    def filter_inputs(schema: dict, user_data: dict) -> dict:
        """Filter and validate user input, extract fields by schema"""
        if not schema:
            return {}

        required_fields = {
            k for k, v in schema.items()
            if isinstance(v, dict) and v.get("required") is True
        }

        filtered = {}
        for k in schema:
            if k not in user_data:
                if k in required_fields:
                    raise KeyError(f"missing required parameter: {k}")
                continue
            filtered[k] = user_data[k]

        return filtered

    @staticmethod
    async def add_workflow_message_to_chat_history(message: BaseMessage, workflow_id: str,
                                                   context_engine: ContextEngine, session: Session):
        """Add message to workflow chat history"""
        workflow_context = context_engine.get_context(
            context_id=workflow_id,
            session_id=session.get_session_id()
        )
        workflow_context.add_messages(message)


class ReasonerUtils:
    @staticmethod
    def get_chat_history(context_engine: ContextEngine, session: Session,
                         chat_history_max_turn: int) -> List[BaseMessage]:
        """Get history by max conversation rounds"""
        agent_context = context_engine.get_context(session_id=session.get_session_id())
        chat_history = agent_context.get_messages()
        return chat_history[-2 * chat_history_max_turn:]

    @staticmethod
    async def get_model(model_config: ModelConfig):
        """Get model instance by config"""
        from openjiuwen.core.runner import Runner
        model_id = generate_key(
            model_config.model_info.api_key,
            model_config.model_info.api_base,
            model_config.model_provider
        )

        model = await Runner.resource_mgr.get_model(model_id=model_id)

        if model is None:
            model_client_config = ModelClientConfig(
                client_id=model_id,
                client_provider=model_config.model_provider,
                api_key=model_config.model_info.api_key,
                api_base=model_config.model_info.api_base,
                timeout=model_config.model_info.timeout,
                verify_ssl=False,
                ssl_cert=None,
            )
            model_request_config = ModelRequestConfig(
                model=model_config.model_info.model_name,
                temperature=model_config.model_info.temperature,
                top_p=model_config.model_info.top_p,
                **(model_config.model_info.model_extra or {})
            )

            def create_model():
                return Model(model_client_config=model_client_config, model_config=model_request_config)

            Runner.resource_mgr.add_model(model_id=model_id, model=create_model)

            model = await Runner.resource_mgr.get_model(model_id=model_id)

        return model
