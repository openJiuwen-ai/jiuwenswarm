# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.


"""Intent recognition module.

This module implements event handling based on intent recognition, including:

- IntentRecognizer: recognizes user intent from input events.
- EventHandlerWithIntentRecognition: event handler that routes logic by
  recognized intent.

Workflow:
    1. Receive an input event.
    2. Use ``IntentRecognizer`` to recognize intent.
    3. Call the corresponding handler method based on intent type.

Supported intent types (see ``IntentType`` for details):
- CREATE_TASK
- PAUSE_TASK
- RESUME_TASK
- CONTINUE_TASK
- SUPPLEMENT_TASK
- CANCEL_TASK
- MODIFY_TASK
- SWITCH_TASK
- UNKNOWN_TASK
"""
import asyncio
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.context_engine import ContextEngine, ModelContext
from openjiuwen.core.controller import DataFrame, TextDataFrame, FileDataFrame, JsonDataFrame, IntentType, TaskStatus, \
    Task
from openjiuwen.core.controller.base import ControllerConfig
from openjiuwen.core.controller.modules.event_handler import EventHandler, EventHandlerInput
from openjiuwen.core.controller.modules.intent_toolkits import IntentToolkits
from openjiuwen.core.controller.modules.task_manager import TaskManager, TaskFilter
from openjiuwen.core.controller.schema import Intent
from openjiuwen.core.controller.schema.event import Event, InputEvent, TaskFailedEvent, TaskCompletionEvent, \
    TaskInteractionEvent
from openjiuwen.core.foundation.llm import SystemMessage, UserMessage, ToolMessage
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.ability_manager import AbilityManager


class IntentRecognizer:
    """意图识别器

    负责识别用户输入中的意图，将事件转换为Intent对象。
    """
    def __init__(
            self,
            config: ControllerConfig,
            task_manager: TaskManager,
            ability_manager: AbilityManager,
            context_engine: ContextEngine
    ):
        """初始化意图识别器

        Args:
            config: 控制器配置
            task_manager: 任务管理器
            ability_manager: 能力包
            context_engine: 上下文引擎
        """
        self._config = config
        self._task_manager = task_manager
        self._context_engine = context_engine
        self._ability_manager = ability_manager

        self._system_message = SystemMessage(content="""# 角色
你是一个任务管理助手，专门使用工具创建和管理任务。你的核心理念是：**任何用户请求都可以转化为一个任务**，并由任务管理器处理。

# 核心原则
1. **任务化一切**：对于任何用户请求（包括信息查询、事务处理、提醒等），你的第一反应不是直接执行或拒绝，而是思考如何将它创建为一个任务。
2. **透明管理**：如果任务需要外部能力（如天气API），你仍然创建它，并明确告知用户任务的状态。

# 工作流程
1. **解析请求**：理解用户想做什么。
2. **任务操作**：使用工具创建一个对应的任务或修改已有任务。
3. **永远不拒绝**：不声称“超出能力范围”，而是告知用户任务会由其他执行器处理。

# 任务目标
- 根据用户输入，**总是优先创建对应的任务**。
- 使用工具进行任务操作（创建、更新、列表、删除）。
- 只有纯粹闲聊或问候时不调用工具。
""")

        self._user_prompt_template = """你当前拥有的任务有：
{task_descriptions}

当前用户的输入为：
{query}

请根据你当前的任务和用户输入，进行合适的任务操作。
"""

    async def _prepare_user_message(self, query):
        tasks = await self._task_manager.get_task()
        task_prompt = []
        if tasks:
            for task in tasks:
                task_prompt.append(
                    f"## Task id: {task.task_id}\n### Task description: {task.description}\nStatus: {task.status}\n")
        else:
            task_prompt.append("无")
        task_prompt = "\n".join(task_prompt)

        prompt = self._user_prompt_template.format(
            task_descriptions=task_prompt,
            query=query
        )
        return UserMessage(content=prompt)

    async def recognize(self, event: Event, session: Session) -> List[Intent]:
        """识别意图

        Args:
            event: 输入事件
            session: 会话对象

        Returns:
            Intent: 识别出的意图对象
        """

        context = self._context_engine.get_context(session_id=session.get_session_id())
        if not context:
            context = await self._context_engine.create_context(session=session)

        if not isinstance(event, InputEvent):
            raise ValueError

        inputs: List[DataFrame] = event.input_data
        texts = [df for df in inputs if isinstance(df, TextDataFrame)]
        files = [df for df in inputs if isinstance(df, FileDataFrame)]
        jsons = [df for df in inputs if isinstance(df, JsonDataFrame)]

        if files or jsons:
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg="Inputs with files or jsons are not supported for intent recognition."
            )

        if len(texts) > 1:
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg="Multiple inputs are not supported for intent recognition."
            )

        from openjiuwen.core.runner import Runner
        model = await Runner.resource_mgr.get_model(model_id=self._config.intent_llm_id)
        user_message = await self._prepare_user_message(query=texts[0].text)
        await context.add_messages(user_message)
        toolkits = IntentToolkits(event, self._config.intent_confidence_threshold)
        max_message_len = 50
        response = await model.invoke(
            messages=[self._system_message] + context.get_messages(size=max_message_len),
            tools=toolkits.get_openai_tool_schemas(self._config.intent_type_list)
        )
        await context.add_messages(response)

        intents = []
        while True:
            if not response.tool_calls:
                break
            else:
                for tool_call in response.tool_calls:
                    instance = getattr(toolkits, tool_call.name)
                    intent, result = await instance(**json.loads(tool_call.arguments))
                    intents.append(intent)
                    await context.add_messages(ToolMessage(
                        tool_call_id=tool_call.id,
                        content=result
                    ))
                response = await model.invoke(
                    messages=[self._system_message] + context.get_messages(size=max_message_len),
                    tools=toolkits.get_openai_tool_schemas()
                )
                await context.add_messages(response)

        return intents


class EventHandlerWithIntentRecognition(EventHandler):
    """基于意图识别的事件处理器

    在EventHandler的基础上增加意图识别功能，根据识别出的意图调用相应的处理方法。
    """

    def __init__(self):
        super().__init__()
        self.recognizer = IntentRecognizer(
            self._config,
            self.task_manager,
            self.ability_manager,
            self.context_engine
        )

    async def handle_input(self, inputs: EventHandlerInput):
        """处理输入事件

        识别输入意图，并调用相应方法处理意图，可重写。

        Args:
            inputs: 事件处理器输入
        """
        intents = await self.recognizer.recognize(inputs.event, inputs.session)
        tasks = []
        for intent in intents:
            if intent.intent_type == IntentType.CREATE_TASK:
                tasks.append(asyncio.create_task(self._process_create_task_intent(intent, inputs.session)))
            elif intent.intent_type == IntentType.PAUSE_TASK:
                tasks.append(asyncio.create_task(self._process_pause_task_intent(intent, inputs.session)))
            elif intent.intent_type == IntentType.RESUME_TASK:
                tasks.append(asyncio.create_task(self._process_resume_task_intent(intent, inputs.session)))
            elif intent.intent_type == IntentType.CONTINUE_TASK:
                tasks.append(asyncio.create_task(self._process_continue_task_intent(intent, inputs.session)))
            elif intent.intent_type == IntentType.SUPPLEMENT_TASK:
                tasks.append(asyncio.create_task(self._process_supplement_task_intent(intent, inputs.session)))
            elif intent.intent_type == IntentType.CANCEL_TASK:
                tasks.append(asyncio.create_task(self._process_cancel_task_intent(intent, inputs.session)))
            elif intent.intent_type == IntentType.MODIFY_TASK:
                tasks.append(asyncio.create_task(self._process_modify_task_intent(intent, inputs.session)))
            else:
                tasks.append(asyncio.create_task(self._process_unknown_task_intent(intent, inputs.session)))
        return await asyncio.gather(*tasks)

    async def handle_task_interaction(self, inputs: EventHandlerInput):
        """处理任务交互事件

        将interaction直接抛出给用户，可重写。

        Args:
            inputs: 事件处理器输入
        """
        if not isinstance(inputs.event, TaskInteractionEvent):
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg=f"Input Event has to be type of TaskInteractionEvent, not {type(inputs.event)}"
            )
        await inputs.session.write_stream({
                "interaction": inputs.event.interaction
            })

    async def handle_task_completion(self, inputs: EventHandlerInput):
        """处理任务完成事件

        将任务完成信息抛出给用户，可重写。

        Args:
            inputs: 事件处理器输入
        """
        if not isinstance(inputs.event, TaskCompletionEvent):
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg=f"Input Event has to be type of TaskCompletionEvent, not {type(inputs.event)}"
            )
        await inputs.session.write_stream({
                "result": inputs.event.task_result
            })

    async def handle_task_failed(self, inputs: EventHandlerInput):
        """处理任务失败事件

        将错误信息抛出给用户，可重写。

        Args:
            inputs: 事件处理器输入
        """
        if not isinstance(inputs.event, TaskFailedEvent):
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg=f"Input Event has to be type of TaskFailedEvent, not {type(inputs.event)}"
            )
        await inputs.session.write_stream({
                "error_message": inputs.event.error_message
            })

    async def _process_create_task_intent(self, intent: Intent, session: Session):
        """处理创建任务意图

        用户自定义执行新任务逻辑。

        Args:
            intent: 意图
            session: Session
        """
        task = Task(
            session_id=session.get_session_id(),
            task_id=intent.target_task_id,
            task_type="default_task_type",
            description=intent.target_task_description,
            priority=1,
            context_id=f"{session.get_session_id()}_{intent.target_task_id}",
            inputs=[intent.event] if isinstance(intent.event, InputEvent) else None,
            status=TaskStatus.SUBMITTED,
            error_message=None,
            metadata=intent.metadata,
        )
        await self.task_manager.add_task(task)

    async def _process_pause_task_intent(self, intent: Intent, session: Session):
        """处理暂停任务意图

        调用 task_scheduler 的 pause_task 方法打断目标任务。

        Args:
            intent: 意图
            session: Session
        """
        await self.task_scheduler.pause_task(intent.target_task_id)

    async def _process_resume_task_intent(self, intent: Intent, session: Session):
        """处理恢复任务意图

        将要恢复的任务的状态置为 submitted。

        Args:
            intent: 意图
            session: Session
        """
        task = await self.task_manager.get_task(TaskFilter(task_id=intent.target_task_id))
        task = task[0]
        if task.status == TaskStatus.PAUSED:
            task.status = TaskStatus.SUBMITTED
            await self.task_manager.update_task(task)

    async def _process_continue_task_intent(self, intent: Intent, session: Session):
        """处理接续任务意图

        Args:
            intent: 意图
            session: Session
        """
        if not isinstance(intent.event, InputEvent):
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg=f"Input Event has to be type of InputEvent, not {type(intent.event)}"
            )
        previous_events = []
        context_ids = []
        for task_id in intent.depend_task_id:
            old_tasks = await self.task_manager.get_task(TaskFilter(task_id=task_id))
            if old_tasks:
                previous_events.extend(old_tasks[0].inputs)
                context_id = old_tasks[0].context_id
                context_ids.append(context_id)
        event: InputEvent = intent.event
        event.input_data.append(
            JsonDataFrame(data={
                context_id: (await self._context_engine.get_context(context_id)).get_messages()
                for context_id in context_ids
            })
        )
        previous_events.append(event)
        task = Task(
            session_id=session.get_session_id(),
            task_id=intent.target_task_id,
            task_type="default_task_type",
            description=intent.target_task_description,
            priority=1,
            context_id=f"{session.get_session_id()}_{intent.target_task_id}",
            inputs=previous_events,
            status=TaskStatus.SUBMITTED,
            error_message=None,
            metadata=intent.metadata,
        )
        await self.task_manager.add_task(task)

    async def _process_supplement_task_intent(self, intent: Intent, session: Session):
        """处理补充任务意图

        Args:
            intent: 意图
            session: Session
        """
        if intent.intent_type != IntentType.SUPPLEMENT_TASK:
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg=f"Input Event has to be type of SUPPLEMENT_TASK, not {type(intent.event)}"
            )

        tasks = await self.task_manager.get_task(TaskFilter(task_id=intent.target_task_id))
        task = tasks[0]
        await self.task_scheduler.pause_task(intent.target_task_id)
        task.description += "\n\n任务补充信息:\n{}".format(intent.supplementary_info)
        task.status = TaskStatus.SUBMITTED
        await self.task_manager.update_task(task)

    async def _process_cancel_task_intent(self, intent: Intent, session: Session):
        """处理取消任务意图

        调用 task_scheduler 的 cancel_task 方法取消目标任务。

        Args:
            intent: 意图
            session: Session
        """
        if intent.intent_type != IntentType.CANCEL_TASK:
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg=f"Input event has to be type of CANCEL_TASK, not {type(intent.event)}"
            )

        await self.task_scheduler.cancel_task(intent.target_task_id)

    async def _process_modify_task_intent(self, intent: Intent, session: Session):
        """处理修改任务意图

        修改目标任务后，将其状态置为 submitted。

        Args:
            intent: 意图
            session: Session
        """
        if intent.intent_type != IntentType.MODIFY_TASK:
            raise build_error(
                status=StatusCode.AGENT_CONTROLLER_RUNTIME_ERROR,
                error_msg=f"Input Event has to be type of InputEvent, not {type(intent.event)}"
            )
        await self.task_scheduler.cancel_task(intent.target_task_id)
        task = await self.task_manager.get_task(TaskFilter(task_id=intent.target_task_id))
        task[0].description = intent.target_task_description
        if not isinstance(task[0].inputs, list):
            task[0].inputs = [intent.event]
        else:
            task[0].inputs.append(intent.event)
        task[0].status = TaskStatus.SUBMITTED
        await self.task_manager.update_task(task[0])

    async def _process_unknown_task_intent(self, intent: Intent, session: Session):
        """处理未知任务意图

        返回 Intent 的 clarification_prompt 字段给用户。

        Args:
            intent: 意图
            session: Session
        """
        if intent.intent_type != IntentType.UNKNOWN_TASK:
            raise ValueError
        await session.write_stream({
                "clarification_prompt": intent.clarification_prompt
            })
