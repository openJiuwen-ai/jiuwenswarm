# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Workflow Controller - Workflow-specific execution logic"""

import asyncio
from typing import Dict, List, Optional, Union

from openjiuwen.core.common.constants.enums import TaskType
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.legacy import AgentConfig, WorkflowSchema
from openjiuwen.core.common.utils.message_utils import MessageUtils
from openjiuwen.core.controller.legacy import (
    TaskStatus,
    IntentDetectionConfig,
    Intent,
    IntentDetectionController,
    IntentType,
    IntentDetector,
    Event,
    EventContent,
    Task,
    TaskInput
)
from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.common.logging import logger
from openjiuwen.core.session import InteractionOutput
from openjiuwen.core.single_agent import Session
from openjiuwen.core.session.stream import CustomSchema, OutputSchema
from openjiuwen.core.foundation.llm import AssistantMessage
from openjiuwen.core.workflow import WorkflowOutput, WorkflowExecutionState


class WorkflowController(IntentDetectionController):
    """WorkflowController - Implements workflow-specific execution logic
    
    Core responsibilities:
    1. Intent detection: Select workflow + Check interruption state
    2. Task execution: Execute workflow (new/resume)
    3. Interruption handling: Save interruption state to session.state
    """

    def __init__(
            self,
            config: AgentConfig = None,
            context_engine=None,
            session=None
    ):
        """Initialize WorkflowController
        
        Args:
            config: Agent configuration (optional, can be injected later)
            context_engine: Context engine (optional, can be injected later)
            session: Agent-level Session (optional, can be injected later)
            
        Note:
            If parameters are not provided, they will be injected by
            ControllerAgent via setup_from_agent()
        """
        super().__init__(config, context_engine, session)

        # Maintain backward compatible attribute name
        self.agent_config = config

        # Initialize intent detection module (only if config and context_engine are available)
        self._intent_detector = None
        if config is not None and context_engine is not None:
            self._init_intent_detection()

    def _init_intent_detection(self):
        """Initialize intent detection module - can be called after setup_from_agent"""
        # Intent detection will be lazily initialized when needed with session
        pass

    def setup_from_agent(self, agent):
        """Override to also initialize intent detection after setup"""
        super().setup_from_agent(agent)
        # Update backward compatible reference
        self.agent_config = self._config

    async def intent_detection(
            self,
            event: Event,
            session: Session
    ) -> Intent:
        """Intent detection: Select workflow + Check interruption state
        
        Process:
        1. Check if input is InteractiveInput with node_id - skip LLM detection
        2. Get available workflows
        3. Single workflow: Use directly; Multiple workflows: LLM recognition
        4. Check for interrupted tasks
        5. Return Intent (ExecNewTask or ResumeTask)
        
        Args:
            event: Event object
            session: Session context
            
        Returns:
            Intent: Intent object
        """
        workflows = self.agent_config.workflows or []

        if not workflows:
            raise ValueError("No workflows configured for single_agent")

        # 0. Fast path: InteractiveInput with node_id - directly resume workflow
        # When user provides InteractiveInput, always resume (don't return interruption again)
        interactive_input = getattr(event.content, 'interactive_input', None)
        if interactive_input is not None and interactive_input.user_inputs:
            resume_result = self._find_interrupted_task_by_node_id(
                interactive_input, session
            )
            if resume_result:
                workflow, task = resume_result
                logger.info(
                    f"InteractiveInput detected, directly resuming workflow: "
                    f"{workflow.name}"
                )
                return Intent(
                    intent_type=IntentType.ResumeTask,
                    task=task,
                    workflow=workflow
                )
            # If not found, fall through to normal detection

        # 1. Select workflow based on user's current query
        if len(workflows) == 1:
            # Single workflow: Use directly
            detected_workflow = workflows[0]
            logger.info(f"Single workflow mode: using {detected_workflow.name}")
        else:
            # Multiple workflows: LLM recognition
            detected_workflow = await self._detect_workflow_via_llm(
                event, session
            )
            # Check if default_response should be used
            if detected_workflow is None:
                default_text = self.agent_config.default_response.text
                logger.info(
                    f"Using default response: {default_text}"
                )
                return Intent(
                    intent_type=IntentType.DefaultResponse,
                    metadata={"default_response_text": default_text}
                )
            logger.info(
                f"Multi workflow mode: detected {detected_workflow.name}"
            )

        # 2. Check if detected workflow has an interrupted task
        interrupted_task = self._find_interrupted_task(
            detected_workflow, session
        )

        if interrupted_task:
            # Found interrupted task for this workflow
            # Check if we should resume or return the interruption again
            should_resume = self._should_resume_interrupted_task(
                interrupted_task, event, session
            )

            if should_resume:
                # Resume task execution
                logger.info(
                    f"Found interrupted task for workflow "
                    f"{detected_workflow.name}, resuming"
                )
                return Intent(
                    intent_type=IntentType.ResumeTask,
                    task=interrupted_task,
                    workflow=detected_workflow
                )
            else:
                # Return the interruption again (dict type interruption)
                logger.info(
                    f"Found interrupted task with dict-type interruption, "
                    f"returning interruption again for workflow {detected_workflow.name}"
                )
                # Use ResumeTask intent but with special metadata to indicate
                # we should return the interruption instead of executing
                return Intent(
                    intent_type=IntentType.ResumeTask,
                    task=interrupted_task,
                    workflow=detected_workflow,
                    metadata={"return_interruption": True}
                )
        else:
            # No interrupted task for this workflow: Create new task
            # Note: Other workflows' interrupted states are preserved
            logger.info(
                f"No interrupted task for workflow {detected_workflow.name}, "
                f"creating new task"
            )
            new_task = self._create_new_task(event, detected_workflow)
            return Intent(
                intent_type=IntentType.ExecNewTask,
                task=new_task,
                workflow=detected_workflow
            )

    async def exec_task(
            self,
            message_content: EventContent,
            task: Task,
            session: Session
    ) -> Dict:
        """Execute workflow task
        
        Execution method depends on task.status:
        - PENDING: New task, use Runner.run_workflow
        - INTERRUPTED: Resume task, use Runner.run_workflow with InteractiveInput
        
        Args:
            message_content: Message content
            task: Task object
            session: Session context
            
        Returns:
            dict: Execution result
        """
        workflow_id = task.input.target_id
        conversation_id = session.get_session_id()

        try:
            # 1. Check if there's a running task for this conversation
            if self.task_queue.has_running_task(conversation_id):
                # Cancel old task
                cancelled = await self.task_queue.cancel_running_task(
                    conversation_id
                )
                if cancelled:
                    logger.info(
                        f"Cancelled previous running task for "
                        f"conversation: {conversation_id}"
                    )
                    # Clear old task's interrupted state
                    old_info = self.task_queue.find_task(conversation_id)
                    if old_info:
                        # Create a temporary task object for cleanup
                        temp_task = Task(
                            task_id=old_info.task.task_id,
                            task_type=old_info.task.task_type,
                            status=TaskStatus.CANCELLED,
                            input=TaskInput(
                                target_id=old_info.target_id,
                                target_name="",
                                arguments={}
                            )
                        )
                        self._clear_interrupted_state(temp_task, session)

            # 2. Prepare execution (existing code)
            task.status = TaskStatus.RUNNING

            # Get workflow object (from controller's single_agent)
            workflow = await self._find_workflow_from_agent(workflow_id, session)
            if not workflow:
                raise ValueError(f"Workflow not found: {workflow_id}")

            # Create workflow session
            workflow_session = session.create_workflow_session()

            # Prepare input parameters
            inputs = task.input.arguments

            # If resuming task, parameters should already be InteractiveInput
            if task.status == TaskStatus.INTERRUPTED:
                logger.info(
                    f"Resuming workflow: {workflow_id}, "
                    f"inputs type={type(inputs)}"
                )
            else:
                logger.info(f"Starting workflow: {workflow_id}")

            # 3. Use streaming workflow call, so workflow layer can yield:
            #    - tracer_workflow (execution trace)
            #    - __interaction__ (interrupt request)
            #    - workflow_final (completion result)
            # Stream data written to session, single_agent layer's stream_iterator can read
            async def run_workflow_streaming():
                from openjiuwen.core.runner import Runner
                workflow_stream = Runner.run_workflow_streaming(
                    workflow,
                    inputs=inputs,
                    session=workflow_session,
                    context=await self._context_engine.create_context(
                        context_id=workflow_id, session=session
                    )
                )
                chunks = []
                has_interaction = False
                final_result = None
                async for chunk in workflow_stream:
                    # Check chunk type
                    if isinstance(chunk, OutputSchema):
                        if chunk.type == INTERACTION:
                            has_interaction = True
                            # Don't pass through __interaction__ here
                            # Let upper ControllerAgent.stream write after controller.invoke completes
                            # Ensure __interaction__ comes after all tracer events
                        elif chunk.type == "workflow_final":
                            # Direct pass-through of workflow_final frame from workflow
                            final_result = chunk.payload
                            await session.write_stream(chunk)
                        else:
                            # Pass through other stream data (tracer etc.)
                            await session.write_stream(chunk)
                    elif isinstance(chunk, CustomSchema):
                        await session.write_custom_stream(chunk)
                    else:
                        await session.write_stream(chunk)
                    chunks.append(chunk)

                # add messages to context
                if chunks:
                    content_parts = []
                    for chunk in chunks:
                        if isinstance(chunk, OutputSchema):
                            if isinstance(chunk.payload, dict):
                                response = chunk.payload.get("response", "")
                                if response is not None:
                                    content_parts.append(str(response))
                            elif isinstance(chunk.payload, InteractionOutput):
                                # Keep interaction interrupt output content
                                content_parts.append(str(chunk.payload.value) if chunk.payload.value else "")
                    workflow_content = "".join(content_parts)
                    await MessageUtils.add_ai_message(AssistantMessage(content=workflow_content),
                                                self._context_engine, session)

                # Construct WorkflowOutput
                if has_interaction:
                    return WorkflowOutput(
                        result=chunks,
                        state=WorkflowExecutionState.INPUT_REQUIRED
                    )
                else:
                    return WorkflowOutput(
                        result=final_result,
                        state=WorkflowExecutionState.COMPLETED
                    )

            workflow_task = asyncio.create_task(run_workflow_streaming())

            # 4. Register task to queue
            await self.task_queue.register_task(
                conversation_id, task, workflow_task, target_id=workflow_id
            )

            # 5. Wait for task completion (may be cancelled)
            try:
                result = await workflow_task
            except asyncio.CancelledError:
                logger.info(f"Workflow cancelled: {workflow_id}")
                task.status = TaskStatus.CANCELLED
                return {
                    "status": "cancelled",
                    "task_id": task.task_id,
                    "workflow_id": workflow_id
                }
            finally:
                # 6. Unregister task
                await self.task_queue.unregister_task(conversation_id)

            # 7. Process result (existing code)
            is_interrupted = self._is_workflow_interrupted(result)
            result_state = "NO STATE"
            if hasattr(result, 'state'):
                result_state = result.state
            logger.info(
                f"Workflow result state: {result_state}, "
                f"interrupted: {is_interrupted}"
            )

            if is_interrupted:
                # Workflow interrupted
                logger.info(f"Workflow interrupted: {workflow_id}")
                task.status = TaskStatus.INTERRUPTED

                # Extract interaction list from result
                interaction_data = (
                    result.result if hasattr(result, 'result') else None
                )
                
                # 状态保存：保存所有中断
                await self.interrupt_task(task, session, interaction_data)

                # 流式返回：只返回第一个中断
                first_interrupt = self._get_first_interrupt(interaction_data)
                logger.info(
                    f"Workflow has {self._count_interactions(interaction_data)} "
                    f"interrupts, returning only the first one for streaming"
                )
                return first_interrupt  # Return only first interrupt for streaming
            else:
                # Workflow completed
                logger.info(f"Workflow completed: {workflow_id}")
                task.status = TaskStatus.SUCCESS

                # Clean up interruption state (if any)
                self._clear_interrupted_state(task, session)

                # workflow_final already passed through directly in run_workflow_streaming
                # If workflow doesn't return workflow_final frame, don't write
                # Return completion response
                # Return value maintains compatibility with original format, includes output and result_type
                return {"output": result, "result_type": "answer"}

        except asyncio.CancelledError:
            # Task was cancelled
            logger.info(f"Task cancelled during execution: {workflow_id}")
            task.status = TaskStatus.CANCELLED
            await self.task_queue.unregister_task(conversation_id)
            return {
                "status": "cancelled",
                "task_id": task.task_id,
                "workflow_id": workflow_id
            }
        except Exception as e:
            # Execution failed
            logger.error(
                f"Workflow execution failed: {workflow_id}, error: {e}"
            )
            task.status = TaskStatus.FAILED
            await self.task_queue.unregister_task(conversation_id)
            raise

    async def _handle_resume(
            self,
            event: Event,
            intent: Intent,
            session: Session
    ) -> Dict:
        """Override parent's _handle_resume to support return_interruption logic

        If intent.metadata contains 'return_interruption': True,
        directly return the saved interruption instead of executing workflow.

        Args:
            event: Event object
            intent: Intent object
            session: Session context

        Returns:
            dict: Execution result or interruption data
        """
        # Check if we should return interruption directly
        if intent.metadata and intent.metadata.get("return_interruption"):
            logger.info("Returning saved interruption directly (dict-type interruption)")

            # Get saved interruption data from state
            task = intent.task
            workflow_id = task.input.target_id
            state = session.get_state("workflow_controller")

            if not state:
                logger.warning("No workflow_controller state found, falling back to normal resume")
                return await super()._handle_resume(event, intent, session)

            state_key = workflow_id.replace('.', '_')
            interrupted_info = state.get("interrupted_tasks", {}).get(state_key)

            if not interrupted_info:
                logger.warning("No interrupted task info found, falling back to normal resume")
                return await super()._handle_resume(event, intent, session)

            # Reconstruct the interruption OutputSchema
            component_id = interrupted_info.get("component_id", "questioner")
            last_interaction_value = interrupted_info.get("last_interaction_value")

            if last_interaction_value is None:
                logger.warning("No last_interaction_value found, falling back to normal resume")
                return await super()._handle_resume(event, intent, session)

            # Create InteractionOutput
            interaction_output = InteractionOutput(
                id=component_id,
                value=last_interaction_value
            )

            # Return as OutputSchema list (same format as workflow interruption)
            return [
                OutputSchema(
                    type="__interaction__",
                    index=0,
                    payload=interaction_output
                )
            ]

        # Normal resume flow
        return await super()._handle_resume(event, intent, session)

    async def interrupt_task(
            self,
            task: Task,
            session: Session,
            interaction_data: Optional[list] = None
    ) -> Dict:
        """Interrupt workflow task
        
        Save interruption state to session.state
        
        Args:
            task: Task object
            session: Session context
            interaction_data: Interaction data during interruption (OutputSchema list)
            
        Returns:
            dict: Interruption information
        """
        workflow_id = task.input.target_id

        # 1. Update task status
        task.status = TaskStatus.INTERRUPTED

        # 2. Save interruption state to session.state
        state = session.get_state("workflow_controller") or {}
        if "interrupted_tasks" not in state:
            state["interrupted_tasks"] = {}

        # Extract component ID and interaction value from interaction data
        component_id = self._extract_component_id_from_interaction_data(
            interaction_data
        )
        interaction_value = self._extract_interaction_value_from_interaction_data(
            interaction_data
        )
        state_key = workflow_id.replace('.', '_')

        state["interrupted_tasks"][state_key] = {
            "task": task.model_dump(),
            "component_id": component_id,
            "last_interaction_value": interaction_value
        }

        # Clear old state first, then update with new state
        # This ensures proper cleanup of nested dict keys
        session.update_state({"workflow_controller": None})
        session.update_state({"workflow_controller": state})

        logger.info(
            f"Task interrupted: workflow={workflow_id}, "
            f"state_key={state_key}, component_id={component_id}, "
            f"interaction_value_type={type(interaction_value).__name__}"
        )

        return {
            "status": "interrupted",
            "task_id": task.task_id,
            "workflow_id": workflow_id,
            "message": "Task interrupted, waiting for subsequent input"
        }

    async def _detect_workflow_via_llm(
            self,
            event: Event,
            session: Session
    ) -> Optional[WorkflowSchema]:
        """Use LLM to detect workflow
        
        If intent_detection exists and model is configured, call intent detection directly
        Otherwise return the first workflow
        
        Args:
            event: Event object
            session: Session context
            
        Returns:
            Optional[WorkflowSchema]: Detected workflow schema, or None if
                default_response should be used
        """
        try:
            # Initialize intent detection module (pass session)
            self._ensure_intent_detection_initialized(session)

            # If no intent detection, use first workflow
            if not self._intent_detector:
                logger.warning("No intent detection configured, using first workflow")
                return self.agent_config.workflows[0]

            # Call intent detection directly to detect intent
            detected_tasks = await self._intent_detector.process_message(event)

            if not detected_tasks:
                # Check if default_response.text is configured
                default_response = getattr(
                    self.agent_config, 'default_response', None
                )
                if (default_response and
                        default_response.text):
                    logger.info(
                        "Intent detection returned no tasks, "
                        "using configured default_response"
                    )
                    return None

                logger.warning(
                    "Intent detection returned no tasks, using first workflow"
                )
                return self.agent_config.workflows[0]

            # Extract workflow from detected_tasks
            # detected_tasks is a Task list
            # task.input.target_name contains workflow name
            workflow_name = detected_tasks[0].input.target_name

            # Match workflow
            for workflow in self.agent_config.workflows:
                if workflow.name == workflow_name:
                    return workflow

            # If not found, return first workflow
            logger.warning(
                f"Workflow '{workflow_name}' not found, "
                "using first workflow"
            )
            return self.agent_config.workflows[0]

        except Exception as e:
            logger.error(f"Intent detection failed: {e}, using first workflow")
            return self.agent_config.workflows[0]

    def _ensure_intent_detection_initialized(self, session: Session):
        """Initialize intent detection module
        
        If already has intent_detection, update session
        Otherwise create new IntentDetection instance
        
        Args:
            session: Session context
        """
        # If already initialized, update session
        if self._intent_detector:
            self._intent_detector.session = session
            logger.debug("Updated intent detection session")
            return

        # Prefer description for classification (richer semantics); fallback to name if not configured
        category_list = [
            workflow.description if workflow.description else workflow.name
            for workflow in self.agent_config.workflows
        ]
        intent_config = IntentDetectionConfig(
            category_list=category_list,
            category_info="\n".join(
                f"- {w.description if w.description else w.name}"
                for w in self.agent_config.workflows
            ),
            enable_history=True,
            enable_input=True,
        )

        self._intent_detector = IntentDetector(
            intent_config=intent_config,
            agent_config=self.agent_config,
            context_engine=self._context_engine,
            session=session  # Pass session
        )

        logger.info("Intent detection module initialized")

    def _should_resume_interrupted_task(
            self,
            task: Task,
            event: Event,
            session: Session
    ) -> bool:
        """Check if interrupted task should resume or return interruption again

        Logic:
        1. If user provides InteractiveInput -> always resume
        2. If last_interaction_value is dict (structured data from component) -> return interruption again
        3. If last_interaction_value is str (human interaction text) -> resume

        Args:
            task: Interrupted task
            message: Current message
            session: Session context

        Returns:
            bool: True if should resume, False if should return interruption again
        """
        # 1. Check if user provides InteractiveInput
        interactive_input = getattr(event.content, 'interactive_input', None)
        if interactive_input is not None and interactive_input.user_inputs:
            logger.info("User provided InteractiveInput, will resume task")
            return True

        # 2. Get last_interaction_value from state
        state = session.get_state("workflow_controller")
        if not state:
            logger.info("No workflow_controller state, will resume task")
            return True

        workflow_id = task.input.target_id
        state_key = workflow_id.replace('.', '_')
        interrupted_info = state.get("interrupted_tasks", {}).get(state_key)

        if not interrupted_info:
            logger.info("No interrupted task info in state, will resume task")
            return True

        last_interaction_value = interrupted_info.get("last_interaction_value")

        # 3. Check type of last_interaction_value
        if last_interaction_value is None:
            logger.info("No last_interaction_value, will resume task")
            return True

        if isinstance(last_interaction_value, dict) or isinstance(last_interaction_value, list):
            logger.info(
                f"last_interaction_value is dict (structured data), "
                f"will return interruption again"
            )
            return False
        elif isinstance(last_interaction_value, str):
            logger.info(
                f"last_interaction_value is str (human interaction), "
                f"will resume task"
            )
            return True
        else:
            # For other types, default to resume
            logger.info(
                f"last_interaction_value type is {type(last_interaction_value).__name__}, "
                f"will resume task"
            )
            return True

    def _find_interrupted_task_by_node_id(
            self,
            interactive_input,
            session: Session
    ) -> Optional[tuple]:
        """Find interrupted workflow by node_id from InteractiveInput
        
        When user provides InteractiveInput with user_inputs (node_id -> value),
        we can directly find the interrupted workflow without LLM detection.
        
        Args:
            interactive_input: InteractiveInput with user_inputs
            session: Session context
            
        Returns:
            tuple(WorkflowSchema, Task) if found, None otherwise
        """
        state = session.get_state("workflow_controller")
        if not state:
            return None

        interrupted_tasks = state.get("interrupted_tasks", {})
        if not interrupted_tasks:
            return None

        # Get node_ids from InteractiveInput
        node_ids = list(interactive_input.user_inputs.keys())
        if not node_ids:
            return None

        logger.info(
            f"_find_interrupted_task_by_node_id: looking for node_ids={node_ids}"
        )

        # Search through interrupted tasks to find matching component_id
        # Support multiple node_ids (parallel interruptions)
        for workflow_key, task_info in interrupted_tasks.items():
            component_id = task_info.get("component_id")
            # component_id can be a list (parallel interruptions) or a string (legacy)
            # Check if any node_id from user input matches any component_id in the interrupted task
            matched = False
            if isinstance(component_id, list):
                # New format: component_id is a list
                if any(node_id in component_id for node_id in node_ids):
                    matched = True
                    logger.info(
                        f"_find_interrupted_task_by_node_id: "
                        f"found match workflow_key={workflow_key}, "
                        f"component_ids={component_id}, node_ids={node_ids}"
                    )
            else:
                # Legacy format: component_id is a string
                if component_id in node_ids:
                    matched = True
                    logger.info(
                        f"_find_interrupted_task_by_node_id: "
                        f"found match workflow_key={workflow_key}, "
                        f"component_id={component_id}"
                    )

            if matched:
                task_data = task_info["task"]
                task = Task.model_validate(task_data)

                # Find corresponding WorkflowSchema
                for workflow in (self.agent_config.workflows or []):
                    base_id = f"{workflow.id}_{workflow.version.replace('.', '_')}"
                    if workflow_key == base_id or workflow_key == workflow.id:
                        return (workflow, task)

        logger.info(
            f"_find_interrupted_task_by_node_id: "
            f"no match found for node_ids={node_ids}"
        )
        return None

    def _find_interrupted_task(
            self,
            workflow: WorkflowSchema,
            session: Session
    ) -> Optional[Task]:
        """Find interrupted task for specified workflow
        
        Find interrupted task from session.state:
        state["workflow_controller"]["interrupted_tasks"][workflow_id]
        
        Note: Since update_dict uses '.' as nested path separator,
        '.' is replaced with '_' when saving, so we need to replace when finding too
        """
        state = session.get_state("workflow_controller")
        logger.info(f"_find_interrupted_task: workflow={workflow.name}, state={state}")
        if not state:
            logger.info("_find_interrupted_task: No workflow_controller state found")
            return None

        interrupted_tasks = state.get("interrupted_tasks", {})
        logger.info(f"_find_interrupted_task: interrupted_tasks keys={list(interrupted_tasks.keys())}")

        base_id_with_version = f"{workflow.id}_{workflow.version.replace('.', '_')}"
        possible_ids = [
            base_id_with_version,  # weather_flow_1_0
            workflow.id  # weather_flow
        ]
        logger.info(f"_find_interrupted_task: possible_ids={possible_ids}")

        for workflow_id in possible_ids:
            if workflow_id in interrupted_tasks:
                logger.info(f"_find_interrupted_task: Found interrupted task for {workflow_id}")
                task_data = interrupted_tasks[workflow_id]["task"]
                return Task.model_validate(task_data)

        logger.info(f"_find_interrupted_task: No interrupted task found for workflow {workflow.name}")
        return None

    def _create_new_task(
            self,
            event: Event,
            workflow: WorkflowSchema
    ) -> Task:
        """Create new workflow task
        
        Extract query from message and filter parameters based on workflow.input_params
        """
        # Get query
        query = event.content.get_query() if hasattr(event.content, 'get_query') else ""

        # Filter input parameters
        user_data = {"query": query}
        user_data.update(event.content.extensions or {})
        filtered_inputs = self._filter_workflow_inputs(
            workflow.input_params or {},
            user_data
        )

        logger.info(f"Creating task with inputs: {filtered_inputs}, query: {query}")

        # Create task
        task = Task(
            task_id=f"workflow_{event.event_id}",
            task_type=TaskType.WORKFLOW,
            status=TaskStatus.PENDING,
            input=TaskInput(
                target_name=workflow.name,
                target_id=f"{workflow.id}_{workflow.version}",
                arguments=filtered_inputs
            )
        )

        return task

    def _filter_workflow_inputs(
            self,
            schema: Dict,
            user_data: Dict
    ) -> Dict:
        """Filter input parameters based on schema
        
        Supports two formats:
        1. Standard JSON Schema: {"type": "object", "properties": {...}}
        2. Simplified format: {"query": {"type": "string"}}
        """
        filtered = {}

        # Try to get properties (standard format)
        properties = schema.get("properties", {})

        # If no properties field, but schema itself contains field definitions (simplified format)
        if not properties and schema:
            # Check if it's simplified format (directly contains field definitions)
            if any(isinstance(v, dict) and "type" in v for v in schema.values()):
                properties = schema

        for key, value in user_data.items():
            if key in properties or not properties:
                # If key is in properties, or properties is empty (allow all fields)
                filtered[key] = value

        return filtered

    def _get_interrupted_component_id(self, task: Task, session: Session) -> str:
        """Get component ID at interruption
        
        Find component_id of interrupted task from session.state
        """
        state = session.get_state("workflow_controller")
        if not state:
            return "questioner"  # Default value

        workflow_id = task.input.target_id
        # Replace '.' with '_', consistent with saving
        state_key = workflow_id.replace('.', '_')

        interrupted_info = state.get("interrupted_tasks", {}).get(state_key)
        if interrupted_info:
            return interrupted_info.get("component_id", "questioner")

        return "questioner"

    def _clear_interrupted_state(self, task: Task, session: Session):
        """Clean up interruption state
        
        Remove interrupted task for specified workflow from session.state
        """
        state = session.get_state("workflow_controller") or {}
        interrupted_tasks = state.get("interrupted_tasks", {})

        workflow_id = task.input.target_id
        # Replace '.' with '_', consistent with saving
        state_key = workflow_id.replace('.', '_')

        if state_key in interrupted_tasks:
            del interrupted_tasks[state_key]
            session.update_state({"workflow_controller": None})  # clear state first
            session.update_state({"workflow_controller": state})
            logger.info(f"Cleared interrupted state for workflow: {workflow_id}, state_key: {state_key}")

    def _extract_component_id_from_interaction_data(
            self,
            interaction_data: Optional[list]
    ) -> Union[str, List[str]]:
        """Extract component ID(s) from interaction data
        
        Reference old implementation: MessageHandler.extract_component_id_from_stream_data
        Find all OutputSchema with type '__interaction__' from interaction_data list,
        and extract component_ids from payload.id
        
        Support parallel interruptions by collecting all component IDs.
        
        Args:
            interaction_data: OutputSchema list, containing interaction requests during interruption
            
        Returns:
            Union[str, List[str]]: Single component ID string if only one interruption,
                                   List of component IDs if multiple interruptions,
                                   Default return "questioner" for legacy compatibility
        """
        if not interaction_data:
            logger.warning("No interaction_data provided, using default component_id")
            return "questioner"

        component_ids = []
        try:
            # Iterate through interaction_data, find all outputs with type '__interaction__'
            for output_schema in interaction_data:
                if (hasattr(output_schema, 'type') and
                        output_schema.type == '__interaction__'):
                    # Extract InteractionOutput.id from payload
                    if (hasattr(output_schema, 'payload') and
                            hasattr(output_schema.payload, 'id')):
                        component_id = output_schema.payload.id
                        component_ids.append(component_id)
                        logger.info(
                            f"Extracted component_id from interaction_data: "
                            f"{component_id}"
                        )
        except Exception as e:
            logger.warning(
                f"Failed to extract component_id from interaction_data: {e}"
            )

        if not component_ids:
            logger.warning("No component_id found in interaction_data, using default")
            return "questioner"  # Default value for legacy compatibility

        # Return single string if only one interruption, list if multiple
        if len(component_ids) == 1:
            logger.info(f"Extracted single component_id: {component_ids[0]}")
            return component_ids[0]
        else:
            logger.info(f"Extracted {len(component_ids)} component_ids: {component_ids}")
            return component_ids

    def _extract_interaction_value_from_interaction_data(
            self,
            interaction_data: Optional[list]
    ) -> Optional[any]:
        """Extract interaction value from interaction data

        Find OutputSchema with type '__interaction__' from interaction_data list,
        and extract value from payload.value

        Args:
            interaction_data: OutputSchema list, containing interaction requests during interruption

        Returns:
            Optional[any]: Interaction value (could be str, dict, or other types), None if not found
        """
        if not interaction_data:
            logger.warning("No interaction_data provided, cannot extract interaction_value")
            return None

        try:
            # Iterate through interaction_data, find output with type '__interaction__'
            for output_schema in interaction_data:
                if (hasattr(output_schema, 'type') and
                        output_schema.type == '__interaction__'):
                    # Extract InteractionOutput.value from payload
                    if (hasattr(output_schema, 'payload') and
                            hasattr(output_schema.payload, 'value')):
                        interaction_value = output_schema.payload.value
                        logger.info(
                            f"Extracted interaction_value from interaction_data: "
                            f"type={type(interaction_value).__name__}"
                        )
                        return interaction_value
        except Exception as e:
            logger.warning(
                f"Failed to extract interaction_value from interaction_data: {e}"
            )

        logger.warning("No interaction_value found in interaction_data")
        return None

    def _get_first_interrupt(
            self,
            interaction_data: Optional[list]
    ) -> list:
        """从 interaction_data 中提取第一个中断用于流式返回
        
        当 workflow 产生多个中断时，状态中保存所有中断，
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
            if isinstance(chunk, OutputSchema) and chunk.type == INTERACTION:
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
            if isinstance(chunk, OutputSchema) and chunk.type == INTERACTION:
                count += 1
        
        return count

    async def _find_workflow_from_agent(self, workflow_id: str, session: Session):
        """Find workflow object from session
        
        Args:
            workflow_id: workflow ID (format: {id}_{version})
            session: Task Session context
            
        Returns:
            Workflow object, None if not found
        """
        # First try to find from Runner's global resource_mgr
        try:
            from openjiuwen.core.runner import Runner
            logger.info(f"Trying to find workflow from resource_mgr: {workflow_id}")

            workflow = await Runner.resource_mgr.get_workflow(workflow_id=workflow_id, tag=self.agent_config.id,
                                                              session=session.base())
            logger.info(f"Found workflow from resource_mgr: {workflow is not None}")
            if workflow:
                return workflow
        except Exception as e:
            logger.warning(f"Failed to find workflow from resource_mgr {workflow_id}: {e}")

        # Then try to get from controller's _session
        try:
            from openjiuwen.core.runner import Runner
            logger.info(f"Trying to find workflow from controller._session: {workflow_id}")
            workflow = await Runner.resource_mgr.get_workflow(workflow_id=workflow_id, tag=self.agent_config.id)
            logger.info(f"Found workflow from controller._session: {workflow is not None}")
            return workflow
        except Exception as e:
            logger.error(f"Failed to find workflow from controller._session {workflow_id}: {e}")

        logger.error(f"Workflow not found: {workflow_id}")
        return None

    async def _find_workflow_by_id(self, workflow_id: str, session: Session):
        """Find workflow object from session
        
        Args:
            workflow_id: workflow ID (format: {id}_{version})
            session: Session context
            
        Returns:
            Workflow object, None if not found
        """
        try:
            workflow = await Runner.resource_mgr.get_workflow(workflow_id=workflow_id, tag=self.agent_config.id)
            return workflow
        except Exception as e:
            logger.error(f"Failed to find workflow {workflow_id}: {e}")
            return None

    def _is_workflow_interrupted(self, result) -> bool:
        """Check if workflow is interrupted
        
        Args:
            result: WorkflowOutput object
            
        Returns:
            True if interrupted, False otherwise
        """
        if not result:
            return False

        # Check state (compare enum value)
        if hasattr(result, 'state'):
            return result.state == WorkflowExecutionState.INPUT_REQUIRED

        return False
