# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved
"""LLMController - ReAct style controller based on BaseController"""

from dataclasses import dataclass
from datetime import datetime, timezone

from typing import Dict, Optional, List, Any

from openjiuwen.core.common.exception.errors import build_error, BaseError
from openjiuwen.core.single_agent.legacy import (
    LegacyReActAgentConfig as ReActAgentConfig, PluginSchema,
)
from openjiuwen.core.controller.legacy import BaseController, Event, EventType, Task, TaskResult, TaskStatus
from openjiuwen.core.controller.legacy.utils import MessageHandlerUtils
from openjiuwen.core.common.utils.message_utils import MessageUtils
from openjiuwen.core.common.constants.enums import TaskType
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.logging import logger
from openjiuwen.core.common.security.json_utils import JsonUtils
from openjiuwen.core.single_agent import Session
from openjiuwen.core.common.security.user_config import UserConfig
from openjiuwen.core.common.utils.hash_util import generate_key
from openjiuwen.core.common.constants import constant as const
from openjiuwen.core.runner import Runner
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.session.stream import OutputSchema
from openjiuwen.core.workflow import WorkflowExecutionState, WorkflowOutput
from openjiuwen.core.foundation.llm import ModelRequestConfig, ModelClientConfig, AssistantMessage, ToolMessage, Model
from openjiuwen.core.memory.long_term_memory import LongTermMemory
from openjiuwen.core.foundation.llm import ToolCall


@dataclass
class TaskInterruptionState:
    """Encapsulates all data related to task interruption

    This dataclass groups related parameters that describe the complete state
    when a task is interrupted, making the API cleaner and more maintainable.
    """
    task: Task
    session: Session
    ai_message: AssistantMessage
    remaining_tasks: list[Task]
    interaction_data: Optional[list] = None
    current_iteration: Optional[int] = None


def convert_timestamp(utc_timestamp: str) -> str:
    local_dt = utc_timestamp
    if utc_timestamp:
        try:
            utc_dt = datetime.strptime(utc_timestamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            local_dt = utc_dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        except ValueError:
            logger.warning(f"timestamp format invalid: {utc_timestamp}, skip convert")
    return local_dt


class LLMController(BaseController):
    """LLM Controller

    Core responsibilities:
    1. Receive user input and invoke LLM reasoning to generate tasks
    2. Execute tasks (plugin/workflow)
    3. After task completion, invoke LLM reasoning again to decide whether to continue
    4. Loop until problem solved or max iteration reached
    """

    def __init__(
            self,
            config: ReActAgentConfig,
            context_engine,
            session
    ):
        super().__init__(config, context_engine, session)
        self.config = config
        self._enable_long_term_mem = self.config.agent_memory_config.enable_long_term_mem
        self._enable_mem_variables = len(self.config.agent_memory_config.mem_variables) > 0
        self._enable_memory = (self.config.memory_scope_id and
                               (self._enable_long_term_mem or self._enable_mem_variables))
        self._long_term_memory_instance = LongTermMemory()
        self._memory_inited = False

    async def handle_event(self, event: Event, session: Session) -> Optional[Dict]:
        """Handle Message - only handles user input

        Notes:
        - BaseController.invoke() only creates USER_INPUT type messages
        - Task execution results are handled directly in _execute_tasks_loop

        Args:
            event: Event object (only USER_INPUT type)
            session: Session context

        Returns:
            Final result
        """
        if event.event_type != EventType.USER_INPUT:
            logger.warning(f"Unexpected event type: {event.event_type}, expected USER_INPUT")
            raise build_error(
                StatusCode.AGENT_CONTROLLER_USER_INPUT_PROCESS_ERROR,
                error_msg=f"{event.event_type} is unexpected event type, should be USER_INPUT"
            )

        try:
            return await self._handle_user_input(event, session)
        except Exception as e:
            logger.error(f"Error in handling message: {e}")
            if isinstance(e, BaseError):
                raise e
            else:
                raise build_error(
                    StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                    error_msg=str(e),
                    cause=e
                ) from e

    async def _handle_user_input(self, event: Event, session: Session) -> Optional[Dict]:
        """Handle user input - ReAct core: LLM reasoning to generate plan

        Process:
        1. Add user message to conversation history
        2. Call LLM reasoning to generate task plan
        3. Execute tasks
        4. If workflow task, check if needs to resume interrupted task
        5. Loop until completion
        """

        # Add user message to conversation history
        await MessageUtils.add_user_message(event.get_display_content(), self._context_engine, session)

        # 0. Fast path: Check if message has InteractiveInput with node_id - directly resume workflow
        interactive_input = getattr(event.content, 'interactive_input', None)
        if interactive_input is not None and interactive_input.user_inputs:
            resume_result = self._find_interrupted_task_by_node_id(
                interactive_input, session
            )
            if resume_result:
                # Unpack new return values
                ai_message, remaining_tasks, saved_iteration = resume_result

                logger.info(
                    f"Resuming interrupted workflow from InteractiveInput, "
                    f"remaining tasks: {len(remaining_tasks)}, "
                    f"saved_iteration: {saved_iteration}"
                )

                # Directly use saved ai_message (no need to manually construct)
                await MessageUtils.add_ai_message(ai_message, self._context_engine, session)

                # Update first task (interrupted one) with user input
                interrupted_task = remaining_tasks[0]
                interrupted_task.input.arguments = interactive_input
                interrupted_task.status = TaskStatus.INTERRUPTED

                # Resume execution: execute all remaining tasks
                initial_iteration = (saved_iteration + 1) if saved_iteration is not None else 1

                return await self._execute_react_loop(
                    remaining_tasks,
                    session,
                    initial_iteration=initial_iteration,
                    ai_message=ai_message
                )
            logger.warning("Given Interactive input, but no interrupted task found, " \
                           "falling through to normal LLM detection")

        # 1. Normal path: Call LLM model to generate plans
        tasks, llm_output = await self._generate_plan_from_llm(event, session)

        if not tasks:
            logger.info("ReAct Iteration: 1 end, No task is generated")
            final_result = await self._send_final_stream(llm_output.content, session)
            return self._unwrap_result(final_result)

        # Check if planned task is workflow task
        initial_iteration = 1
        workflow_task = self._resolve_workflow_from_tasks(tasks)
        if workflow_task:
            # Check if needs to resume interrupted task
            resume_result = self._find_interrupted_task(workflow_task, session)
            if resume_result[0]:
                # Unpack new return values
                saved_ai_message, remaining_tasks, saved_iteration, component_ids = resume_result

                logger.info(
                    f"Resuming interrupted workflow task: {workflow_task.input.target_name}, "
                    f"last iteration: {saved_iteration}"
                )

                # LLM generated a new ai_message (llm_output) with new tool_call_id in _generate_plan_from_llm.
                # To ensure tool responses match the ai_message in context, we update the saved task's
                # task_id to use LLM's new tool_call_id. This way the tool response will correctly
                # correspond to the ai_message already in context.
                if llm_output and llm_output.tool_calls and len(llm_output.tool_calls) > 0:
                    new_tool_call_id = llm_output.tool_calls[0].id
                    old_task_id = remaining_tasks[0].task_id
                    remaining_tasks[0].task_id = new_tool_call_id
                    logger.info(f"Updated task_id from {old_task_id} to {new_tool_call_id} for resume")

                # Update first task (interrupted one) with user input
                interrupted_task = remaining_tasks[0]

                # Build correct InteractiveInput for workflow resume
                if event.content.interactive_input is not None:
                    interactive_input = event.content.interactive_input
                else:
                    # Create InteractiveInput with component_ids
                    user_query = event.get_display_content()
                    interactive_input = InteractiveInput()
                    if component_ids:
                        # Use first component_id to bind user input
                        for comp_id in component_ids:
                            interactive_input.update(comp_id, user_query)
                    else:
                        # Fallback to raw_inputs
                        interactive_input = InteractiveInput(raw_inputs=user_query)
                    logger.info(
                        f"Created InteractiveInput with component_ids: {component_ids}, user_query: {user_query}")

                interrupted_task.input.arguments = interactive_input
                interrupted_task.status = TaskStatus.INTERRUPTED

                # Resume execution
                initial_iteration = saved_iteration + 1 if saved_iteration is not None else 1

                return await self._execute_react_loop(
                    remaining_tasks,
                    session,
                    initial_iteration=initial_iteration,
                    ai_message=saved_ai_message
                )
            else:
                logger.info(f"Creating new workflow task: {workflow_task.input.target_name}")

        # Execute new tasks
        return await self._execute_react_loop(
            tasks,
            session,
            initial_iteration=initial_iteration,
            ai_message=llm_output
        )

    async def _post_task_completion(
            self,
            task: Task,
            output: Any,
            workflow_id: Optional[str],
            session: Session
    ):
        """Post-processing after task completion: add tool_msg, clear state

        Args:
            task: Completed task
            output: Task output (stream data)
            workflow_id: Workflow ID (if applicable)
            session: Session context
        """
        # Add tool_msg for completed task
        if output and len(output) > 0:
            if output[0].type in ("plugin_final", "workflow_final"):
                temp_event = Event.create_task_completed(
                    conversation_id=session.get_session_id(),
                    task_id=task.task_id,
                    task_result=task.result,
                    workflow_id=workflow_id,
                    stream_data=output
                )
                await MessageHandlerUtils.add_tool_result(temp_event, self._context_engine, session)
                logger.info(f"Added tool_message for completed task: {task.task_id}")

        # Clear workflow interrupted state (if any)
        if workflow_id:
            self._clear_interrupted_state(task, session)
            logger.info(f"Cleared interrupted state for workflow: {workflow_id}")

    async def _generate_next_plan(
            self,
            task: Task,
            workflow_id: Optional[str],
            output: Any,
            session: Session
    ) -> tuple[list[Task], Any]:
        """Generate next plan after task completion

        Args:
            task: Completed task
            workflow_id: Workflow ID (if applicable)
            output: Task output (stream data)
            session: Session context

        Returns:
            tuple: (tasks, llm_output)
        """
        # Create temporary Event for LLM reasoning (maintain compatibility)
        temp_event = Event.create_task_completed(
            conversation_id=session.get_session_id(),
            task_id=task.task_id,
            task_result=task.result,
            workflow_id=workflow_id,
            stream_data=output
        )

        return await self._generate_plan_from_llm(temp_event, session)

    async def _handle_task_completed(
            self,
            task: Task,
            execution_result: TaskResult,
            session: Session
    ) -> tuple[Optional[Dict], list[Task], Optional[AssistantMessage]]:
        """Handle task completion - generate next plan, check if done

        Args:
            task: Completed task
            execution_result: Task execution result
            session: Session context

        Returns:
            tuple: (final_result_if_done, new_tasks, new_ai_message)
            - If final_result is not None, ReAct loop should end
            - If final_result is None, continue with new_tasks and new_ai_message
        """
        workflow_id = execution_result.metadata.get("workflow_id")

        # Generate next plan
        tasks, llm_output = await self._generate_next_plan(
            task=task,
            workflow_id=workflow_id,
            output=execution_result.output,
            session=session
        )

        # Check if done
        if not tasks:
            logger.info("No new tasks generated, ReAct loop completed")
            final_result = await self._send_final_stream(llm_output.content, session)
            return self._unwrap_result(final_result), [], None

        # Continue loop with new tasks and new ai_message
        return None, tasks, llm_output

    async def _handle_task_interrupted(
            self,
            interruption_state: TaskInterruptionState,
            output: List
    ) -> Optional[Dict]:
        """Handle task interruption

        Save complete ai_message and remaining tasks for resumption

        Args:
            interruption_state: Complete interruption state encapsulated in dataclass
            output: Interaction output data
        """
        # Save interruption state with complete information
        await self.interrupt_task(interruption_state)

        # Add mock tool_msg
        mock_tool_msg = ToolMessage(
            content="[INTERRUPTED - Waiting for user input]",
            tool_call_id=interruption_state.task.task_id
        )
        agent_context = self._context_engine.get_context(
            session_id=interruption_state.session.get_session_id()
        )
        await agent_context.add_messages(mock_tool_msg)

        # 流式返回：只返回第一个中断
        first_interrupt = self._get_first_interrupt(output)
        logger.info(
            f"Task has {self._count_interactions(output)} "
            f"interrupts, returning only the first one for streaming"
        )

        # Write interrupted task stream data (only first interrupt)
        await self._write_message_stream_data(first_interrupt, interruption_state.session)

        # Return interruption result (only first interrupt)
        return self._unwrap_result(first_interrupt)

    async def _handle_task_error(
            self,
            error_msg: str,
            session: Session,
            task: Task
    ) -> Optional[Dict]:
        """Handle task error

        - Add tool message and send error stream data
        - Return None to continue executing next task
        """
        logger.error(f"Task execution error: {error_msg}")

        # Add tool message (similar to interrupted task handling)
        error_content = f"[FAILED - {error_msg}]"
        mock_tool_msg = ToolMessage(
            content=error_content,
            tool_call_id=task.task_id
        )
        agent_context = self._context_engine.get_context(session_id=session.get_session_id())
        await agent_context.add_messages(mock_tool_msg)
        logger.info(f"Added tool_message for failed task: {task.task_id}")

        # Send error stream data for user notification
        await self._send_error_stream(error_msg, session)

        # Return None to allow continuing to next task
        return None

    async def _execute_react_loop(self, tasks: list[Task], session: Session,
                                  initial_iteration: int = 1,
                                  ai_message: Optional[AssistantMessage] = None) -> Optional[Dict]:
        """Execute ReAct loop with explicit iteration - NO RECURSION

        Main loop:
        1. Execute all tasks sequentially
        2. If interrupted, return immediately, if failed, add tool message and execute next task
        3. If all completed, generate next plan from LLM
        4. If no new tasks or max iteration reached, return final result
        5. Otherwise, continue loop with new tasks

        Args:
            tasks: Initial task list
            session: Session context
            initial_iteration: Initial iteration
            ai_message: Complete AI message with all tool_calls

        Returns:
            Final result dictionary
        """
        iteration = initial_iteration
        while tasks and iteration <= self.config.constrain.max_iteration:
            logger.info(f"ReAct Iteration: {iteration} / {self.config.constrain.max_iteration}")

            # Execute all tasks sequentially
            for idx, task in enumerate(tasks):
                logger.info(f"Executing task {task.task_id}, type: {task.task_type}, index: {idx + 1}/{len(tasks)}")

                # Execute task
                execution_result = await self._execute_task(task, session)

                # Handle interruption - stop immediately, wait for user input
                if execution_result.status == TaskStatus.INTERRUPTED:
                    logger.info(f"Task interrupted at index {idx + 1}, stopping ReAct loop")

                    # Calculate remaining uncompleted tasks (including current interrupted one)
                    remaining_tasks = tasks[idx:]

                    # Create interruption state
                    interruption_state = TaskInterruptionState(
                        task=task,
                        session=session,
                        ai_message=ai_message,
                        remaining_tasks=remaining_tasks,
                        interaction_data=execution_result.output,
                        current_iteration=iteration
                    )

                    return await self._handle_task_interrupted(
                        interruption_state=interruption_state,
                        output=execution_result.output
                    )

                # Handle error - add mock tool message and continue to next task
                if execution_result.status == TaskStatus.FAILED:
                    logger.error(f"Task failed: {execution_result.error}")
                    await self._handle_task_error(
                        error_msg=execution_result.error,
                        session=session,
                        task=task
                    )
                    # Continue to next task

                # Task completed successfully - do post-processing
                if execution_result.status == TaskStatus.SUCCESS:
                    await self._post_task_completion(
                        task=task,
                        output=execution_result.output,
                        workflow_id=execution_result.metadata.get("workflow_id"),
                        session=session
                    )
                    # Continue to next task

            # All tasks completed, generate next plan
            logger.info("All tasks completed successfully, generate next plan using LLM")
            last_task = tasks[-1]
            final_result, new_tasks, new_ai_message = await self._handle_task_completed(
                task=last_task,
                execution_result=execution_result,
                session=session
            )

            if final_result is not None:
                logger.info(f"ReAct loop completed at iteration: {iteration}")
                return final_result

            # Update loop variables
            tasks = new_tasks
            ai_message = new_ai_message
            iteration += 1

        # Exceeded max iteration count
        logger.warning(
            f"Exceeded max iteration {self.config.constrain.max_iteration}, stopping ReAct loop"
        )
        return {"output": "Maximum iteration reached", "result_type": "answer"}

    async def _execute_task(self, task: Task, session: Session) -> TaskResult:
        """Execute single task - return execution task result
        """
        try:
            if task.task_type == TaskType.WORKFLOW:
                return await self._execute_workflow_task(task, session)
            elif task.task_type == TaskType.PLUGIN:
                return await self._execute_plugin_task(task, session)
            else:
                logger.warning(f"Unknown task type: {task.task_type}")
                raise build_error(
                    StatusCode.AGENT_TASK_NOT_SUPPORT,
                    error_msg=str(task.task_type)
                )
        except Exception as e:
            logger.error(f"Error executing task {task.task_id}: {e}")
            return TaskResult(
                status=TaskStatus.FAILED,
                error=str(e)
            )

    async def _execute_workflow_task(self, task: Task, session: Session) -> TaskResult:
        """Execute workflow task - return result dictionary

        - If task status is INTERRUPTED, it's a resume task
        - Change status to RUNNING before execution
        - Determine if interrupted or completed based on execution result
        """
        try:
            workflow_id = task.input.target_id
            workflow = await self._find_workflow_by_id(workflow_id, session)
            if not workflow:
                raise ValueError(f"Workflow not found: {workflow_id}")
            workflow_session = session.create_workflow_session()

            # Record task execution information
            is_resume = task.status == TaskStatus.INTERRUPTED
            logger.info(
                f"Executing workflow: {task.input.target_name}, "
                f"workflow_id: {task.input.target_id}, "
                f"is_resume={is_resume}, "
                f"input_type={type(task.input.arguments)}"
            )

            # Update task status to RUNNING
            task.status = TaskStatus.RUNNING

            # Execute workflow
            result = await Runner.run_workflow(
                workflow,
                inputs=task.input.arguments,
                session=workflow_session,
                context=await self._context_engine.create_context(
                    context_id=workflow_id, session=session
                )
            )

            # Prepare stream data
            output_stream_data = self._prepare_workflow_stream_data(result)

            # Check result status
            is_interrupted = self._is_workflow_interrupted(result)
            result_state = "NO STATE"
            if hasattr(result, 'state'):
                result_state = result.state
            logger.info(
                f"Executing workflow result state: {result_state}, "
                f"interrupted: {is_interrupted}"
            )
            if is_interrupted:
                task.result = TaskResult(
                    status=TaskStatus.INTERRUPTED,
                    output=result,
                    metadata={"state": result.state.value}
                )
                logger.info(f"Workflow {task.input.target_name} interrupted, waiting for user input")

                return TaskResult(
                    status=TaskStatus.INTERRUPTED,
                    output=output_stream_data,
                    metadata={"workflow_id": task.input.target_id}
                )
            else:
                task.result = TaskResult(
                    status=TaskStatus.SUCCESS,
                    output=result,
                    metadata={"state": result.state.value if hasattr(result, 'state') else "completed"}
                )
                await self._write_workflow_stream_output(result, session)
                logger.info(f"Workflow {task.input.target_name} completed successfully")

                return TaskResult(
                    status=TaskStatus.SUCCESS,
                    output=output_stream_data,
                    metadata={"workflow_id": task.input.target_id}
                )
        except BaseError as e:
            logger.error(f"Error executing workflow task {task.input.target_name}: {e}")
            raise build_error(
                StatusCode.AGENT_WORKFLOW_EXECUTION_ERROR,
                error_msg=e.message,
                cause=e
            ) from e
        except Exception as e:
            logger.error(f"Error executing workflow {task.input.target_name}: {e}")
            raise build_error(
                StatusCode.AGENT_WORKFLOW_EXECUTION_ERROR,
                error_msg=str(e),
                cause=e
            ) from e

    async def _execute_plugin_task(self, task: Task, session: Session) -> TaskResult:
        """Execute plugin task - return result dictionary"""
        tool = None
        tool_id = self._find_plugin_id_by_name(task.input.target_name)
        if tool_id:
            tool = Runner.resource_mgr.get_tool(tool_id=tool_id, tag=self.config.id)
        if not tool:
            logger.error("Tool not found")
            raise build_error(
                StatusCode.AGENT_TOOL_NOT_FOUND,
                error_msg=f"tool '{task.input.target_name}' is not registered in session"
            )
        try:
            result = await tool.invoke(task.input.arguments)

            # Prepare stream data
            payload = {"output": result, "result_type": "answer"}
            output_stream_data = [OutputSchema(type="plugin_final", index=0, payload=payload)]

            # update task result
            task.result = TaskResult(
                status=TaskStatus.SUCCESS,
                output=output_stream_data,
                metadata={"tool_name": task.input.target_name}
            )
            return TaskResult(
                status=TaskStatus.SUCCESS,
                output=output_stream_data,
                metadata={"tool_name": task.input.target_name}
            )
        except BaseError as e:
            logger.error(f"Error executing plugin task {task.input.target_name}: {e}")
            raise build_error(
                StatusCode.AGENT_TOOL_EXECUTION_ERROR,
                error_msg=e.message,
                cause=e
            ) from e
        except Exception as e:
            logger.error(f"Error executing plugin task {task.input.target_name}: {e}")
            raise build_error(
                StatusCode.AGENT_TOOL_EXECUTION_ERROR,
                error_msg=str(e),
                cause=e
            ) from e

    async def _generate_plan_from_llm(self, event: Event, session: Session):
        """Call LLM to generate plan - ReAct core method"""
        inputs = event.get_display_content()
        user_id = event.source.user_id
        tools = await Runner.resource_mgr.get_tool_infos(tag=self.config.id)
        logger.info(f"Loaded {len(tools)} Tool(s) for generating plans")
        system_prompt_keywords = await self._get_system_prompt_keywords(inputs, user_id)
        chat_history = MessageUtils.get_chat_history(self._context_engine, session, self.config)
        llm_inputs = MessageHandlerUtils.format_llm_inputs(inputs, chat_history, self.config, system_prompt_keywords)

        if UserConfig.is_sensitive():
            logger.info(f"React llm inputs")
        else:
            logger.info(f"React llm inputs: {llm_inputs}")

        try:
            model = await self._get_model(session=session)
            llm_output = await self._call_llm_get_output(
                model,
                self.config.model.model_info.model_name,
                llm_inputs,
                tools,
                session
            )

            if UserConfig.is_sensitive():
                logger.info(f"React llm output")
            else:
                logger.info(f"React llm output: {llm_output}")

            tasks = MessageHandlerUtils.parse_llm_output(llm_output, self.config)
            # Add LLM output to CE conversation history
            await MessageUtils.add_ai_message(llm_output, self._context_engine, session)

        except Exception as e:
            logger.error(f"Failed to invoke model, {e}")
            if isinstance(e, BaseError):
                raise e
            else:
                raise build_error(
                    StatusCode.AGENT_CONTROLLER_INVOKE_CALL_FAILED,
                    error_msg=str(e),
                    cause=e
                ) from e

        return tasks, llm_output

    async def _call_llm_get_output(
            self,
            model,
            model_name: str,
            llm_inputs: Any,
            tools: List[Any],
            session: Session
    ) -> AssistantMessage:
        """ Stream LLM invocation and output chunks in real-time

        Args:
            model: Model instance
            model_name: Model name
            llm_inputs: LLM input messages
            tools: Available tools
            session: Session context for streaming output

        Returns:
            AIMessage: Accumulated complete message from all chunks

        Raises:
            BaseError: If LLM returns empty response or invocation fails
        """
        accumulated_chunk = None
        stream_index = 0

        try:
            async for chunk in model.stream(llm_inputs, tools=tools, model=model_name):
                # Accumulate chunks using AIMessageChunk's __add__ method
                if accumulated_chunk is None:
                    accumulated_chunk = chunk
                else:
                    accumulated_chunk = accumulated_chunk + chunk

                # Stream output for reasoning content
                if chunk.reasoning_content:
                    stream_output = OutputSchema(
                        type="llm_reasoning",
                        index=stream_index,
                        payload={
                            "output": chunk.reasoning_content,
                            "result_type": "answer"
                        }
                    )
                    await session.write_stream(stream_output)
                    stream_index += 1

                # Stream output for response content
                if chunk.content:
                    stream_output = OutputSchema(
                        type="llm_output",
                        index=stream_index,
                        payload={
                            "output": chunk.content,
                            "result_type": "answer"
                        }
                    )
                    await session.write_stream(stream_output)
                    stream_index += 1

            # Check for empty response
            if accumulated_chunk is None:
                raise build_error(
                    StatusCode.AGENT_CONTROLLER_INVOKE_CALL_FAILED,
                    error_msg="LLM returned empty response"
                )

            # Convert accumulated chunk to AIMessage
            return AssistantMessage(
                role=accumulated_chunk.role or "assistant",
                content=accumulated_chunk.content or "",
                tool_calls=accumulated_chunk.tool_calls or [],
                usage_metadata=accumulated_chunk.usage_metadata,
                finish_reason=accumulated_chunk.finish_reason,
                parser_content=accumulated_chunk.parser_content,
                reasoning_content=accumulated_chunk.reasoning_content,
            )

        except Exception as e:
            logger.error(f"Failed to stream LLM output: {e}")
            raise

    async def _get_model(self, session=None):
        """Get model instance"""
        model_id = generate_key(
            self.config.model.model_info.api_key,
            self.config.model.model_info.api_base,
            self.config.model.model_provider
        )

        model = await Runner.resource_mgr.get_model(model_id=model_id, session=session)

        if model is None:
            model_client_config = ModelClientConfig(
                client_id=model_id,
                client_provider=self.config.model.model_provider,
                api_key=self.config.model.model_info.api_key,
                api_base=self.config.model.model_info.api_base,
                timeout=self.config.model.model_info.timeout,
                verify_ssl=False,
                ssl_cert=None,
            )
            model_request_config = ModelRequestConfig(
                model=self.config.model.model_info.model_name,
                temperature=self.config.model.model_info.temperature,
                top_p=self.config.model.model_info.top_p,
                **(self.config.model.model_info.model_extra or {})
            )

            def model_provider():
                return Model(model_client_config=model_client_config, model_config=model_request_config)

            Runner.resource_mgr.add_model(model_id=model_id, model=model_provider)

        return await Runner.resource_mgr.get_model(model_id=model_id, session=session)

    def _get_workflow_id_from_schema(self, workflow_name: str) -> Optional[str]:
        """Get workflow_id from workflow schema by name

        Args:
            workflow_name: Workflow name

        Returns:
            Workflow ID in format {id}_{version}, or None if not found
        """
        for workflow_schema in self.config.workflows:
            if workflow_schema.name == workflow_name:
                return f"{workflow_schema.id}_{workflow_schema.version}"
        return None

    def _ensure_workflow_id(self, task: Task) -> str:
        """Ensure task has valid workflow_id, construct from schema if needed

        Args:
            task: Task object

        Returns:
            Workflow ID

        Raises:
            ValueError: If workflow_id cannot be determined
        """
        # If task already has target_id, use it
        if task.input.target_id:
            return task.input.target_id

        # Otherwise, construct from workflow schema
        workflow_id = self._get_workflow_id_from_schema(task.input.target_name)
        if workflow_id:
            task.input.target_id = workflow_id
            logger.info(
                f"Set workflow_id={workflow_id} for workflow: {task.input.target_name}"
            )
            return workflow_id

        raise ValueError(
            f"Cannot determine workflow_id for task: {task.input.target_name}"
        )

    def _resolve_workflow_from_tasks(self, tasks: list[Task]) -> Optional[Task]:
        """Find workflow task from task list"""
        for task in tasks:
            if task.task_type == TaskType.WORKFLOW:
                return task
        return None

    def _find_interrupted_task(self, workflow_task: Task, session: Session):
        """Find interrupted task

        Find interrupted task from session.state:
        state["llm_controller"]["interrupted_tasks"][workflow_id]

        Find interrupted task for specified workflow from session.state

        Returns:
            tuple: (ai_message, remaining_tasks, saved_iteration, component_ids) or (None, None, None, None)
        """
        state = session.get_state("llm_controller")
        if not state:
            logger.info("No llm_controller state found, don't have interrupted tasks")
            return None, None, None, None

        interrupted_tasks = state.get("interrupted_tasks", {})
        logger.info(f"find interrupted_tasks {list(interrupted_tasks.keys())} from llm_controller")

        # Get workflow_id from schema (unified method)
        workflow_id = self._get_workflow_id_from_schema(workflow_task.input.target_name)
        if not workflow_id:
            logger.warning(f"Workflow schema not found for {workflow_task.input.target_name}")
            return None, None, None, None

        state_key = workflow_id.replace('.', '_')

        # Try to find interrupted task with normalized key
        if state_key in interrupted_tasks:
            logger.info(f"Found interrupted task for {workflow_task.input.target_name} (key: {state_key})")
            task_info = interrupted_tasks[state_key]

            # Restore complete information
            ai_message = AssistantMessage.model_validate(task_info["ai_message"])
            remaining_tasks = [Task.model_validate(t) for t in task_info["remaining_tasks"]]
            saved_iteration = task_info["iteration"]
            component_ids = task_info.get("component_ids", [])

            logger.info(
                f"Restored: {len(ai_message.tool_calls) if ai_message.tool_calls else 0} tool_calls, "
                f"{len(remaining_tasks)} remaining tasks, component_ids: {component_ids}"
            )

            return ai_message, remaining_tasks, saved_iteration, component_ids

        logger.info(f"No interrupted task found for workflow {workflow_task.input.target_name}")
        return None, None, None, None

    def _create_resume_task(self, event: Event, interrupted_task: Task) -> Task:
        """Create resume task

        - If message has InteractiveInput, use it directly
        - Otherwise create InteractiveInput from query
        - Update interrupted task input parameters
        - Update task status to Interrupted

        Args:
            event: Event
            interrupted_task: Interrupted task recovered from state

        Returns:
            Task: Resumed task object
        """
        # Check if message content already has InteractiveInput
        if event.content.interactive_input is not None:
            interactive_input = event.content.interactive_input
            logger.info(f"Using InteractiveInput from message for resuming workflow directly")
        else:
            # Create InteractiveInput from query
            query = event.content.get_query()
            logger.info(f"Creating InteractiveInput from query: {query}")
            interactive_input = InteractiveInput(raw_inputs=query)

        # Update interrupted task input to InteractiveInput
        interrupted_task.input.arguments = interactive_input

        # Keep task status as INTERRUPTED
        interrupted_task.status = TaskStatus.INTERRUPTED

        logger.info(
            f"Created resume task: task_id={interrupted_task.task_id}, "
            f"workflow={interrupted_task.input.target_name}, "
            f"status={interrupted_task.status}, "
            f"input_type={type(interrupted_task.input.arguments)}"
        )

        return interrupted_task

    async def _find_workflow_by_id(self, workflow_id: str, session: Session):
        """Find workflow object from session

        Args:
            workflow_id: workflow ID (format: {id}_{version})
            session: Session context

        Returns:
            Workflow object, None if not found
        """
        try:
            workflow = await Runner.resource_mgr.get_workflow(workflow_id=workflow_id, tag=self.config.id)
            return workflow
        except Exception as e:
            logger.error(f"Failed to find workflow {workflow_id}: {e}")
            return None

    def _prepare_workflow_stream_data(self, result) -> list:
        """Prepare workflow stream data"""
        try:
            if self._is_workflow_interrupted(result):
                return result.result
            else:
                payload = {"output": result, "result_type": "answer"}
                return [OutputSchema(type="workflow_final", index=0, payload=payload)]
        except Exception as e:
            logger.warning(f"Failed to prepare stream data: {e}")
            return []

    def _is_workflow_interrupted(self, result) -> bool:
        """Check if workflow is interrupted"""
        return hasattr(result, 'state') and result.state == WorkflowExecutionState.INPUT_REQUIRED

    def _clear_interrupted_state(self, task: Task, session: Session):
        """Clean up interruption state

        Remove interrupted task for specified workflow from session.state
        """
        workflow_id = task.input.target_id
        if not workflow_id:
            logger.warning("Cannot clear interrupted state: task has no target_id")
            return

        state = session.get_state("llm_controller") or {}
        interrupted_tasks = state.get("interrupted_tasks", {})

        # Normalize workflow_id for state key
        state_key = workflow_id.replace('.', '_')

        if state_key in interrupted_tasks:
            del interrupted_tasks[state_key]
            session.update_state({"llm_controller": None})  # clear state first
            session.update_state({"llm_controller": state})
            logger.info(
                f"Cleared interrupted state for workflow: {task.input.target_id}, "
                f"state_key: {state_key}"
            )

    async def interrupt_task(
            self,
            interruption_state: TaskInterruptionState
    ) -> Dict:
        """Save interruption state to session.state

        Args:
            interruption_state: Complete interruption state containing task, session,
                              ai_message, remaining_tasks, interaction_data, and current_iteration

        Returns:
            dict: Interruption information
        """
        # Ensure task has valid workflow_id (unified method)
        try:
            workflow_id = self._ensure_workflow_id(interruption_state.task)
        except ValueError as e:
            logger.error(f"interrupt_task: {e}")
            workflow_id = "unknown"

        # Step 1: Update task status
        interruption_state.task.status = TaskStatus.INTERRUPTED

        # Step 2: Save interruption state to session.state
        state = interruption_state.session.get_state("llm_controller") or {}
        if "interrupted_tasks" not in state:
            state["interrupted_tasks"] = {}

        # Extract component ID from interaction data
        component_ids = self._extract_component_ids_from_interaction_data(
            interruption_state.interaction_data
        )

        # Normalize workflow_id for state key
        state_key = workflow_id.replace('.', '_')

        state["interrupted_tasks"][state_key] = {
            "ai_message": interruption_state.ai_message.model_dump(),
            "remaining_tasks": [t.model_dump() for t in interruption_state.remaining_tasks],
            "component_ids": component_ids,
            "iteration": interruption_state.current_iteration
        }

        # Clear old state first, then update with new state, which ensures proper cleanup of nested dict keys
        interruption_state.session.update_state({"llm_controller": None})
        interruption_state.session.update_state({"llm_controller": state})

        logger.info(
            f"Task interrupted: workflow={workflow_id}, "
            f"state_key={state_key}, "
            f"saved remaining_tasks count: {len(interruption_state.remaining_tasks)}, "
            f"ai_message tool_calls: {len(interruption_state.ai_message.tool_calls) if interruption_state.ai_message.tool_calls else 0}, "
            f"task_id={interruption_state.task.task_id}, current_iteration={interruption_state.current_iteration}"
        )

        return {
            "status": "interrupted",
            "task_id": interruption_state.task.task_id,
            "workflow_id": workflow_id,
            "message": "Task interrupted, waiting for subsequent input"
        }

    def _extract_component_ids_from_interaction_data(self, interaction_data: Optional[list]) -> List[str]:
        """Extract component ID from interaction data

        Args:
            interaction_data: OutputSchema list containing interaction requests during interruption

        Returns:
            str: Component ID, defaults to "questioner"
        """
        if not interaction_data:
            logger.warning("No interaction_data provided, using default component_id")
            return ["questioner"]

        component_ids = []
        try:
            # Iterate through interaction_data to find outputs with INTERACTION type
            for output_schema in interaction_data:
                if (hasattr(output_schema, 'type') and
                        output_schema.type == const.INTERACTION):
                    # Extract InteractionOutput.id from payload
                    if (hasattr(output_schema, 'payload') and
                            hasattr(output_schema.payload, 'id')):
                        component_id = output_schema.payload.id
                        component_ids.append(component_id)
                        logger.info(
                            f"Extracted component_id from interaction_data: {component_id}"
                        )
        except Exception as e:
            logger.warning(
                f"Failed to extract component_id from interaction_data: {e}"
            )

        if not component_ids:
            logger.warning("No component_id found in interaction_data, using default")
            return ["questioner"]  # Default value

        return component_ids

    async def _write_message_stream_data(self, stream_data: List, session: Session):
        """Write stream data carried by message List[OutputSchema]"""
        try:
            for output_schema in stream_data:
                await session.write_stream(output_schema)
        except Exception as e:
            logger.warning(f"Failed to write message stream data: {e}")

    async def _send_final_stream(self, content: str, session: Session):
        """Send final stream data"""
        try:
            payload = {"output": content, "result_type": "answer"}
            final_stream = OutputSchema(
                type="answer",
                index=0,
                payload=payload
            )
            await session.write_stream(final_stream)
            return final_stream
        except Exception as e:
            logger.error(f"Failed to send final stream data: {e}")
            raise build_error(
                StatusCode.AGENT_CONTROLLER_EXECUTION_CALL_FAILED,
                error_msg=str(e),
                cause=e
            ) from e

    async def _send_error_stream(self, error_msg: str, session: Session):
        """Send error result stream and return OutputSchema"""
        try:
            error_stream = OutputSchema(
                type="final",
                index=0,
                payload={
                    "error": True,
                    "message": error_msg,
                    "status": "failed"
                }
            )
            await session.write_stream(error_stream)
            return error_stream
        except Exception as e:
            logger.error(f"Failed to send error stream: {e}")
            raise build_error(
                StatusCode.AGENT_CONTROLLER_EXECUTION_CALL_FAILED,
                error_msg=str(e),
                cause=e
            ) from e

    def _unwrap_result(self, result):
        """Unwrap result - unify return format"""
        if isinstance(result, list):
            if not result:
                return {"output": "", "result_type": "answer"}
            if isinstance(result[0], OutputSchema):
                # If it's interaction requests (multiple or single), return list
                if result[0].type == const.INTERACTION:
                    return result
                # If it's a single non-interaction OutputSchema, extract its payload
                if len(result) == 1:
                    payload = result[0].payload
                    if isinstance(payload, dict):
                        if 'output' in payload and isinstance(payload['output'], str):
                            payload['output'] = payload['output'].strip()
                        return payload
                    return {"output": payload, "result_type": "answer"}
                # If it's multiple non-interaction OutputSchemas, return list
                return result
            return {"output": result, "result_type": "answer"}

        if isinstance(result, OutputSchema):
            # If it's interaction, return wrapped in list for consistency
            if result.type == const.INTERACTION:
                return [result]
            payload = result.payload
            if isinstance(payload, dict):
                if 'output' in payload and isinstance(payload['output'], str):
                    payload['output'] = payload['output'].strip()
                return payload
            return {"output": payload, "result_type": "answer"}

        return {"output": result, "result_type": "answer"}

    def _get_first_interrupt(
            self,
            interaction_data: Optional[list]
    ) -> list:
        """从 interaction_data 中提取第一个中断用于流式返回

        当产生多个中断时，状态中保存所有中断，
        但流式输出只返回第一个中断给用户。

        Args:
            interaction_data: OutputSchema 列表，包含所有中断

        Returns:
            list: 只包含第一个 __interaction__ 的 OutputSchema 列表
                  保持其他类型的 chunk（tracer等）不变
        """
        if not interaction_data:
            return []

        first_interrupt_found = False
        result = []

        for chunk in interaction_data:
            if isinstance(chunk, OutputSchema) and chunk.type == const.INTERACTION:
                # 只保留第一个 __interaction__
                if not first_interrupt_found:
                    result.append(chunk)
                    first_interrupt_found = True
                    logger.info(
                        f"Found first interrupt: component_id="
                        f"{chunk.payload.id if hasattr(chunk.payload, 'id') else 'unknown'}"
                    )
                else:
                    # 跳过后续的 __interaction__
                    logger.info(
                        f"Skipping additional interrupt: component_id="
                        f"{chunk.payload.id if hasattr(chunk.payload, 'id') else 'unknown'}"
                    )
            else:
                # 保留非 __interaction__ 类型的 chunk（如 tracer）
                result.append(chunk)

        return result

    def _count_interactions(
            self,
            interaction_data: Optional[list]
    ) -> int:
        """统计 interaction_data 中的中断数量

        Args:
            interaction_data: OutputSchema 列表

        Returns:
            int: 中断数量
        """
        if not interaction_data:
            return 0

        count = 0
        for chunk in interaction_data:
            if isinstance(chunk, OutputSchema) and chunk.type == const.INTERACTION:
                count += 1

        return count

    def create_message(self, inputs: Dict) -> Event:
        """Create message object - override to support query field"""
        query = inputs.get("query", inputs.get("content", ""))
        conversation_id = inputs.get("conversation_id", "default_session")
        user_id = inputs.get("user_id")

        return Event.create_user_event(
            content=query,
            conversation_id=conversation_id,
            user_id=user_id
        )

    @staticmethod
    async def _write_workflow_stream_output(result, session):
        if isinstance(result, WorkflowOutput) and isinstance(result.result, list):
            for item in result.result:
                await session.write_stream(item)

    def set_llm_controller_prompt_template(self, prompt_template: List[Dict[str, str]]):
        """Set prompt template for LLMController"""
        self.config.prompt_template = prompt_template

    async def _get_system_prompt_keywords(self, inputs: Any, user_id: str):
        result = {}
        if self._enable_memory:
            memory_keywords = await self._get_keywords_from_memory(inputs, user_id)
            result.update(memory_keywords)
        return result

    async def _get_keywords_from_memory(self, inputs: Any, user_id: str):
        result = {}
        scope_id = f"{self.config.memory_scope_id}"
        if isinstance(inputs, str):
            query = inputs
        elif isinstance(inputs, dict):
            query = inputs.get("query", "")
        else:
            query = ""
        logger.info(f"scope_id: {scope_id} | user_id: {user_id} | inputs: {inputs}")
        if not self._long_term_memory_instance:
            return result
        if user_id and scope_id:
            if self._enable_mem_variables:
                memory_variables = await self._long_term_memory_instance.get_variables(
                    user_id=user_id,
                    scope_id=scope_id
                )
                if memory_variables:
                    cur_variables_config = [config.name for config in self.config.agent_memory_config.mem_variables]
                    filter_memory_variables = {k: v for k, v in memory_variables.items() if k in cur_variables_config}
                    result.update({"sys_memory_variables":
                                       JsonUtils.safe_json_dumps(filter_memory_variables, ensure_ascii=False)})
                logger.info(f"memory_variables: {memory_variables}")
            else:
                logger.info("[LongTermMemory] not enable memory variables")

            try:
                if not self._enable_long_term_mem:
                    logger.info("[LongTermMemory] not enable long_term_memory")
                    return result
                long_term_memory = await self._long_term_memory_instance.search_user_mem(
                    user_id=user_id,
                    scope_id=scope_id,
                    query=query,
                    num=10
                )
                if long_term_memory:
                    memory_contents = [mem.mem_info.content for mem in long_term_memory]
                    result.update(
                        {"sys_long_term_memory": JsonUtils.safe_json_dumps(memory_contents, ensure_ascii=False)})
                logger.info(f"long_term_memory: {long_term_memory}")
            except Exception as e:
                logger.error(f"[LongTermMemory] failed to search mem: {e}")
                result.update({"sys_long_term_memory": "[]"})
        return result

    def _convert_openai_tool_calls_to_tool_call_objects(self, tool_calls: List[Dict]) -> List[ToolCall]:
        """Convert OpenAI format tool_calls to ToolCall objects

        OpenAI format: {"id": "...", "type": "...", "function": {"name": "...", "arguments": "..."}}
        ToolCall format: ToolCall(id=..., type=..., name=..., arguments=...)
        """
        if not tool_calls:
            return []
        result = []
        for tool_call in tool_calls:
            # Handle both OpenAI format (with function nested) and ToolCall format (direct fields)
            if "function" in tool_call:
                # OpenAI format
                result.append(ToolCall(
                    id=tool_call.get("id"),
                    type=tool_call.get("type", "function"),
                    name=tool_call.get("function", {}).get("name", ""),
                    arguments=tool_call.get("function", {}).get("arguments", ""),
                    index=tool_call.get("index"),
                ))
            else:
                # Already in ToolCall format
                result.append(ToolCall(**tool_call))
        return result

    def _find_interrupted_task_by_node_id(
            self,
            interactive_input,
            session: Session
    ) -> Optional[tuple]:
        """Find interrupted workflow by node_id from InteractiveInput

        When user provides InteractiveInput with user_inputs (node_id -> value),
        we can directly find the interrupted workflow without LLM detection.

        This method searches in llm_controller state
        to find the interrupted task matching the node_id.

        Args:
            interactive_input: InteractiveInput with user_inputs
            session: Session context

        Returns:
            tuple(ai_message, remaining_tasks, saved_iteration) if found, None otherwise
        """
        state = session.get_state("llm_controller")
        if not state:
            return None

        interrupted_tasks = state.get("interrupted_tasks", {})
        if not interrupted_tasks:
            return None

        # Get node_id from InteractiveInput
        node_ids = list(interactive_input.user_inputs.keys())
        if not node_ids:
            return None

        logger.info(
            f"_find_interrupted_task_by_node_id: looking for node_id={node_ids}"
        )

        # Search through interrupted tasks to find matching component_id
        # Support multiple node_ids (parallel interruptions)
        for workflow_key, task_info in interrupted_tasks.items():
            component_ids = task_info.get("component_ids") or []
            if any(node_id in component_ids for node_id in node_ids):
                logger.info(
                    f"_find_interrupted_task_by_node_id: "
                    f"found match workflow_key={workflow_key}, "
                    f"given component_id={component_ids}"
                )

                # Restore complete information
                ai_message_data = task_info["ai_message"].copy()
                if "tool_calls" in ai_message_data and ai_message_data["tool_calls"]:
                    ai_message_data["tool_calls"] = [
                        tc.model_dump() if isinstance(tc, ToolCall) else tc
                        for tc in self._convert_openai_tool_calls_to_tool_call_objects(ai_message_data["tool_calls"])
                    ]

                # Restore complete information
                ai_message = AssistantMessage.model_validate(ai_message_data)
                remaining_tasks = [Task.model_validate(t) for t in task_info["remaining_tasks"]]
                saved_iteration = task_info["iteration"]

                logger.info(
                    f"Restored: ai_message with "
                    f"{len(ai_message.tool_calls) if ai_message.tool_calls else 0} tool_calls, "
                    f"{len(remaining_tasks)} remaining tasks"
                )

                return ai_message, remaining_tasks, saved_iteration

        logger.warning(
            f"_find_interrupted_task_by_node_id: "
            f"no match found for node_id={node_ids}"
        )
        return None

    def _find_plugin_id_by_name(self, tool_name: str) -> Optional[str]:
        config = self.config
        if hasattr(config, "plugins"):
            for plugin in config.plugins:
                if isinstance(plugin, PluginSchema) and tool_name == plugin.name:
                    return plugin.id
        return None
