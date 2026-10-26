# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, List, Dict, Union, Tuple, Literal

from pydantic import BaseModel, Field, ConfigDict, ValidationError

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import workflow_logger, LogEventType
from openjiuwen.core.common.security.exception_utils import ExceptionUtils
from openjiuwen.core.common.security.user_config import UserConfig
from openjiuwen.core.workflow.components.base import ComponentConfig
from openjiuwen.core.workflow.components.component import ComponentComposable, ComponentExecutable
from openjiuwen.core.context_engine import ModelContext
from openjiuwen.core.graph.executable import Executable, Input, Output
from openjiuwen.core.session.node import Session
from openjiuwen.core.foundation.llm import (
    BaseMessage, UserMessage, SystemMessage, ModelRequestConfig, ModelClientConfig, Model
)
from openjiuwen.core.foundation.prompt import PromptTemplate

START_STR = "start"
END_STR = "end"
USER_INTERACT_STR = "user_interact"

SUB_PLACEHOLDER_PATTERN = r'\{\{([^}]*)\}\}'
CONTINUE_ASK_STATEMENT = "请您提供{non_extracted_key_fields_names}相关的信息"
WORKFLOW_CHAT_HISTORY = "workflow_chat_history"
TEMPLATE_NAME = "questioner"
QUESTIONER_STATE_KEY = "questioner_state"

QUESTIONER_SYSTEM_TEMPLATE = """\
你是一个信息收集助手，你需要根据指定的参数收集用户的信息，然后提交到系统。
请注意：不要使用任何工具、不用理会问题的具体含义，并保证你的输出仅有 JSON 格式的结果数据。
请严格遵循如下规则：
  1. 让我们一步一步思考。
  2. 用户输入中没有提及的参数提取为 null，并直接向询问用户没有明确提供的参数。
  3. 通过用户提供的对话历史以及当前输入中提取 {{required_name}}，不要追问任何其他信息。
  4. 参数收集完成后，将收集到的信息通过 JSON 的方式展示给用户。

## Specified Parameters
{{required_params_list}}

## Constraints
{{extra_info}}

## Examples
{{example}}
"""

QUESTIONER_USER_TEMPLATE = """\
对话历史
{{dialogue_history}}

请充分考虑以上对话历史及用户输入，正确提取最符合约束要求的 JSON 格式参数。
"""


def questioner_default_template():
    return [
        SystemMessage(content=QUESTIONER_SYSTEM_TEMPLATE),
        UserMessage(content=QUESTIONER_USER_TEMPLATE),
    ]


class ExecutionStatus(Enum):
    START = START_STR
    USER_INTERACT = USER_INTERACT_STR
    END = END_STR


class QuestionerEvent(Enum):
    START_EVENT = START_STR
    END_EVENT = END_STR
    USER_INTERACT_EVENT = USER_INTERACT_STR


class ResponseType(Enum):
    ReplyDirectly = "reply_directly"


class FieldInfo(BaseModel):
    field_name: str
    description: str
    type: Literal["string", "integer", "number", "boolean"] = Field(default="string")
    cn_field_name: str = Field(default="")
    required: bool = Field(default=False)
    default_value: Any = Field(default="")


@dataclass
class QuestionerConfig(ComponentConfig):
    model_id: Optional[str] = None
    model_client_config: Optional[ModelClientConfig] = field(default=None)
    model_config: Optional[ModelRequestConfig] = field(default=None)
    response_type: str = field(default=ResponseType.ReplyDirectly.value)
    question_content: str = field(default="")
    extract_fields_from_response: bool = field(default=True)
    field_names: List[FieldInfo] = field(default_factory=list)
    max_response: int = field(default=3)
    with_chat_history: bool = field(default=False)
    chat_history_max_rounds: int = field(default=5)
    extra_prompt_for_fields_extraction: str = field(default="")
    example_content: str = field(default="")


@dataclass
class QuestionerDefaultConfig:
    prompt_template: List[BaseMessage] = field(default_factory=questioner_default_template)


class QuestionerInput(BaseModel):
    model_config = ConfigDict(extra='allow')   # Allow any extra fields
    query: Union[str, dict, None] = Field(default="")


class OutputCache(BaseModel):
    user_response: Union[str, dict] = Field(default="")
    question: str = Field(default="")
    key_fields: dict = Field(default_factory=dict)


class QuestionerOutput(BaseModel):
    user_response: Union[str, dict] = Field(default="")
    question: str = Field(default="")
    model_config = ConfigDict(extra='allow')  # Allow any extra fields


class QuestionerState(BaseModel):
    response_num: int = Field(default=0)
    user_response: Union[str, dict] = Field(default="")
    question: str = Field(default="")
    extracted_key_fields: Dict[str, Any] = Field(default_factory=dict)
    status: ExecutionStatus = Field(default=ExecutionStatus.START)

    @classmethod
    def deserialize(cls, raw_state: dict):
        state = cls.model_validate(raw_state)
        return state.handle_event(QuestionerEvent(state.status.value))

    def serialize(self) -> dict:
        return self.model_dump()

    def handle_event(self, event: QuestionerEvent):
        if event == QuestionerEvent.START_EVENT:
            return QuestionerStartState.from_state(self)
        if event == QuestionerEvent.USER_INTERACT_EVENT:
            return QuestionerInteractState.from_state(self)
        if event == QuestionerEvent.END_EVENT:
            return QuestionerEndState.from_state(self)
        return self

    def is_undergoing_interaction(self):
        return self.status in [ExecutionStatus.USER_INTERACT]

    def is_fresh_state(self):
        return self.status == ExecutionStatus.START and self.response_num == 0


class QuestionerStartState(QuestionerState):
    @classmethod
    def from_state(cls, questioner_state: QuestionerState):
        return cls(response_num=questioner_state.response_num,
                   user_response=questioner_state.user_response,
                   question=questioner_state.question,
                   extracted_key_fields=questioner_state.extracted_key_fields,
                   status=ExecutionStatus.START)

    def handle_event(self, event: QuestionerEvent):
        if event == QuestionerEvent.USER_INTERACT_EVENT:
            return QuestionerInteractState.from_state(self)
        if event == QuestionerEvent.END_EVENT:
            return QuestionerEndState.from_state(self)
        return self


class QuestionerInteractState(QuestionerState):
    status: ExecutionStatus = Field(default=ExecutionStatus.USER_INTERACT)

    @classmethod
    def from_state(cls, questioner_state: QuestionerState):
        return cls(response_num=questioner_state.response_num,
                   user_response=questioner_state.user_response,
                   question=questioner_state.question,
                   extracted_key_fields=questioner_state.extracted_key_fields,
                   status=ExecutionStatus.USER_INTERACT)

    def handle_event(self, event: QuestionerEvent):
        if event == QuestionerEvent.END_EVENT:
            return QuestionerEndState.from_state(self)
        return self


class QuestionerEndState(QuestionerState):
    status: ExecutionStatus = Field(default=ExecutionStatus.END)

    @classmethod
    def from_state(cls, questioner_state: QuestionerState):
        return cls(response_num=questioner_state.response_num,
                   user_response=questioner_state.user_response,
                   question=questioner_state.question,
                   extracted_key_fields=questioner_state.extracted_key_fields,
                   status=ExecutionStatus.END)

    def handle_event(self, event: QuestionerEvent):
        if event == QuestionerEvent.START_EVENT:
            return QuestionerState().handle_event(event)  # loop back to START state
        return self


class QuestionerUtils:
    @staticmethod
    def format_template(template: str, user_fields: dict):
        def replace(match):
            key = match.group(1)
            return str(user_fields.get(key))

        try:
            result = re.sub(SUB_PLACEHOLDER_PATTERN, replace, template)
            return result
        except (KeyError, TypeError, AttributeError):
            return ""

    @staticmethod
    def get_latest_k_rounds_chat(chat_messages, rounds):
        return chat_messages[-rounds * 2 - 1:]

    @staticmethod
    def format_continue_ask_question(non_extracted_key_fields: List[FieldInfo]):
        non_extracted_key_fields_names = list()
        for param in non_extracted_key_fields:
            non_extracted_key_fields_names.append(param.cn_field_name or param.description)
        result = ", ".join(non_extracted_key_fields_names)
        return CONTINUE_ASK_STATEMENT.format(non_extracted_key_fields_names=result)

    @staticmethod
    def format_questioner_output(output_cache: OutputCache) -> Dict:
        output = QuestionerOutput(**output_cache.key_fields)
        output.user_response = output_cache.user_response
        output.question = output_cache.question
        return output.model_dump(exclude_defaults=True)

    @staticmethod
    def validate_inputs(inputs):
        try:
            return QuestionerInput.model_validate(inputs)
        except ValidationError as e:
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_INPUT_PARAM_ERROR,
                error_msg=ExceptionUtils.format_validation_error(e),
                cause=e
            ) from e

    @staticmethod
    def is_valid_value(input_value):
        if input_value is None:
            return False
        if input_value in ("", {}, []):
            return False
        if isinstance(input_value, str):
            value = input_value.strip().lower()
            return value not in ("null", "none")
        return True

    @staticmethod
    def validate_and_convert_type(value: Any, expected_type: str) -> Tuple[Any, bool]:
        """
        Validate and convert a value to the expected type.
        
        Args:
            value: The value to validate and convert
            expected_type: One of "string", "integer", "number", "boolean"
            
        Returns:
            Tuple of (converted_value, is_valid)
            - If conversion succeeds: (converted_value, True)
            - If conversion fails: (None, False)
        """
        if value is None:
            return None, False

        try:
            if expected_type == "string":
                # String type: always convert to string
                return str(value), True

            elif expected_type == "integer":
                # Integer type: try to convert to int
                if isinstance(value, bool):
                    # Avoid bool being treated as int (True=1, False=0)
                    return None, False
                if isinstance(value, int):
                    return value, True
                if isinstance(value, float):
                    # Only accept if it's a whole number
                    if value == int(value):
                        return int(value), True
                    return None, False
                if isinstance(value, str):
                    # Try to parse string as integer
                    cleaned = value.strip()
                    return int(cleaned), True
                return None, False

            elif expected_type == "number":
                # Number type: try to convert to float
                if isinstance(value, bool):
                    return None, False
                if isinstance(value, (int, float)):
                    return float(value), True
                if isinstance(value, str):
                    cleaned = value.strip()
                    return float(cleaned), True
                return None, False

            elif expected_type == "boolean":
                # Boolean type: only accept bool or string "true"/"false" (case-insensitive)
                if isinstance(value, bool):
                    return value, True
                if isinstance(value, str):
                    cleaned = value.strip().lower()
                    if cleaned == "true":
                        return True, True
                    if cleaned == "false":
                        return False, True
                    return None, False
                return None, False

            else:
                # Unknown type: treat as string (backward compatible)
                return str(value), True

        except (ValueError, TypeError):
            return None, False


class QuestionerDirectReplyHandler:
    def __init__(self):
        self._config = None
        self._model = None
        self._state = None
        self._prompt = None
        self._query = ""

    def config(self, config: QuestionerConfig):
        self._config = config
        return self

    def model(self, model: Model):
        self._model = model
        return self

    def state(self, state: QuestionerState):
        self._state = state
        return self

    def get_state(self):
        return self._state

    def prompt(self, prompt):
        self._prompt = prompt
        return self

    async def handle(self, inputs: Input, session: Session, context):
        if self._state.status == ExecutionStatus.START:
            return await self._handle_start_state(inputs, session, context)
        if self._state.status == ExecutionStatus.USER_INTERACT:
            return await self._handle_user_interact_state(inputs, session, context)
        if self._state.status == ExecutionStatus.END:
            return self._handle_end_state(inputs, session, context)
        return dict()

    async def _handle_start_state(self, inputs, session, context):
        questioner_input = QuestionerUtils.validate_inputs(inputs)
        output = OutputCache()
        self._query = questioner_input.query or ""
        chat_history = self._get_latest_chat_history(context)
        if self._is_set_question_content():
            user_fields = questioner_input.model_dump(exclude={'query'})
            output.question = QuestionerUtils.format_template(self._config.question_content, user_fields)
            self._update_questioner_states_question(output.question)
            self._state = self._state.handle_event(QuestionerEvent.USER_INTERACT_EVENT)
            return QuestionerUtils.format_questioner_output(output)

        if self._need_extract_fields():
            is_continue_ask = await self._initial_extract_from_chat_history(chat_history, output)
            event = QuestionerEvent.USER_INTERACT_EVENT if is_continue_ask else QuestionerEvent.END_EVENT
            if is_continue_ask:
                self._update_questioner_states_question(output.question)
            self._state = self._state.handle_event(event)
        else:
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_INPUT_INVALID,
                error_msg="question_content is empty and no extractable fields are configured"
            )
        return QuestionerUtils.format_questioner_output(output)

    async def _handle_user_interact_state(self, inputs, session: Session, context):
        await self._get_latest_human_feedback(session)
        output = OutputCache(question=self._state.question, user_response=self._query)

        chat_history = self._get_latest_chat_history(context)
        user_response = chat_history[-1].content if chat_history else ""

        if self._is_set_question_content() and not self._need_extract_fields():
            output.user_response = user_response
            self._state = self._state.handle_event(QuestionerEvent.END_EVENT)
            return QuestionerUtils.format_questioner_output(output)

        if self._need_extract_fields():
            is_continue_ask = await self._repeat_extract_from_chat_history(chat_history, output)
            event = QuestionerEvent.USER_INTERACT_EVENT if is_continue_ask else QuestionerEvent.END_EVENT
            if is_continue_ask:
                self._update_questioner_states_question(output.question)
            self._state = self._state.handle_event(event)
        else:
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_INPUT_INVALID,
                error_msg="question_content is empty and no extractable fields are configured"
            )
        return QuestionerUtils.format_questioner_output(output)

    def _handle_end_state(self, inputs, session, context):
        output = QuestionerOutput(**self._state.extracted_key_fields)
        output.user_response = self._state.user_response
        output.question = self._state.question
        return output.model_dump(exclude_defaults=True)

    def _is_set_question_content(self):
        return isinstance(self._config.question_content, str) and len(self._config.question_content) > 0

    def _need_extract_fields(self):
        return (self._config.extract_fields_from_response and
                len(self._config.field_names) > len(self._state.extracted_key_fields))

    async def _initial_extract_from_chat_history(self, chat_history, output: OutputCache) -> bool:
        await self._invoke_llm_and_parse_result(chat_history, output)

        self._update_param_default_value(output)
        self._update_state_of_key_fields(output.key_fields)

        return self._check_if_continue_ask(output)

    async def _repeat_extract_from_chat_history(self, chat_history, output: OutputCache) -> bool:
        await self._invoke_llm_and_parse_result(chat_history, output)

        self._update_param_default_value(output)
        self._update_state_of_key_fields(output.key_fields)

        return self._check_if_continue_ask(output)

    def _get_latest_chat_history(self, context) -> List:
        result = list()
        if self._config.with_chat_history and context:
            raw_chat_history = context.get_messages()
            if raw_chat_history:
                result = QuestionerUtils.get_latest_k_rounds_chat(raw_chat_history,
                                                                  self._config.chat_history_max_rounds)
        if not result or result[-1].role in ["assistant"]:
            # make sure content is Union[str, List[Union[str, Dict]]]
            content = self._query
            if isinstance(content, dict):
                content = [content]  # wrap dict in list
            result.append(UserMessage(role="user", content=content))
        return result

    def _build_llm_inputs(self, chat_history: list = None) -> List[BaseMessage]:
        prompt_template_input = self._create_prompt_template_keywords(chat_history)
        formatted_template: PromptTemplate = self._prompt.format(prompt_template_input)
        return formatted_template.to_messages()

    def _create_prompt_template_keywords(self, chat_history: List[BaseMessage]):
        params_list, required_name_list = list(), list()
        for param in self._config.field_names:
            params_list.append(f"{param.field_name}: {param.description}")
            if param.required:
                required_name_list.append(param.cn_field_name or param.description)
        required_name_str = "、".join(required_name_list) + f"{len(required_name_list)}个必要信息"
        all_param_str = "\n".join(params_list)
        dialogue_history_str = "\n".join([f"{_.role}：{_.content}" for _ in chat_history])

        return dict(required_name=required_name_str, required_params_list=all_param_str,
                    extra_info=self._config.extra_prompt_for_fields_extraction, example=self._config.example_content,
                    dialogue_history=dialogue_history_str)

    async def _invoke_llm_for_extraction(self, llm_inputs: List[BaseMessage]):
        response = ""

        workflow_logger.info(
            "Questioner LLM extraction started",
            event_type=LogEventType.WORKFLOW_COMPONENT_START,
            component_type_str="QuestionerComponent",
            metadata={
                "has_inputs": bool(llm_inputs),
                "input_count": len(llm_inputs) if llm_inputs else 0,
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )

        try:
            response = (await self._model.invoke(messages=llm_inputs)).content
        except Exception as e:
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_INVOKE_CALL_FAILED,
                error_msg="failed to invoke llm for extraction",
                cause=e
            ) from e

        workflow_logger.info(
            "Questioner LLM extraction completed",
            event_type=LogEventType.WORKFLOW_COMPONENT_END,
            component_type_str="QuestionerComponent",
            metadata={
                "has_response": bool(response),
                "response_length": len(response) if response else 0,
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )

        result = dict()
        try:
            cleaned = re.sub(r'^\s*```json\s*|\s*```\s*$', '', response.strip(), flags=re.IGNORECASE)
            cleaned = re.sub(r"^\s*'''json\s*|\s*'''\s*$", '', cleaned, flags=re.IGNORECASE)
            result = json.loads(cleaned, strict=False)
        except json.JSONDecodeError as _:
            workflow_logger.error(
                "Questioner JSON parse failed",
                event_type=LogEventType.WORKFLOW_COMPONENT_ERROR,
                component_type_str="QuestionerComponent",
                metadata={"error": "Failed to parse JSON from LLM response"}
            )
            return result

        if not isinstance(result, dict):
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_EXECUTION_PROCESS_ERROR,
                error_msg="failed to parse json from llm response"
            )
        result = {k: v for k, v in result.items() if QuestionerUtils.is_valid_value(v)}
        
        # Validate and convert field types based on FieldInfo.type
        result = self._validate_and_convert_fields(result)
        return result

    def _validate_and_convert_fields(self, extracted_result: dict) -> dict:
        """
        Validate and convert extracted field values based on FieldInfo.type definitions.
        
        If validation fails, attempts type conversion. If conversion also fails,
        the field is treated as not extracted (removed from result).
        
        Args:
            extracted_result: Dict of field_name -> extracted_value from LLM
            
        Returns:
            Dict with validated and type-converted values. Invalid fields are removed.
        """
        # Build field_name -> expected_type mapping
        field_type_map = {f.field_name: f.type for f in self._config.field_names}
        
        validated_result = {}
        for field_name, value in extracted_result.items():
            # Get expected type, default to "string" for backward compatibility
            expected_type = field_type_map.get(field_name, "string")
            
            converted_value, is_valid = QuestionerUtils.validate_and_convert_type(value, expected_type)
            
            if is_valid:
                validated_result[field_name] = converted_value
            else:
                # Log warning and skip this field (treated as not extracted)
                workflow_logger.warning(
                    "Questioner field validation failed",
                    event_type=LogEventType.WORKFLOW_COMPONENT_ERROR,
                    component_type_str="QuestionerComponent",
                    metadata={
                        "field_name": field_name,
                        "value": str(value) if not UserConfig.is_sensitive() else None,
                        "expected_type": expected_type
                    }
                )
        
        return validated_result

    def _filter_non_extracted_key_fields(self) -> List[FieldInfo]:
        result = []
        for item in self._config.field_names:
            if item.required and item.field_name not in self._state.extracted_key_fields:
                result.append(item)
        return result

    def _update_state_of_key_fields(self, key_fields):
        for k, v in key_fields.items():
            if v:
                self._state.extracted_key_fields.update({k: v})

    def _update_param_default_value(self, output: OutputCache):
        result = dict()
        extracted_key_fields = self._state.extracted_key_fields
        for param in self._config.field_names:
            param_name = param.field_name
            default_value = param.default_value
            if default_value and param_name not in extracted_key_fields:
                result.update({param_name: default_value})
        output.key_fields.update(result)

    def _increment_state_of_response_num(self):
        self._state.response_num += 1

    def _exceed_max_response(self):
        return self._state.response_num >= self._config.max_response

    def _check_if_continue_ask(self, output: OutputCache):
        is_continue_ask = False
        non_extracted_key_fields: List[FieldInfo] = self._filter_non_extracted_key_fields()
        if non_extracted_key_fields:
            if not self._exceed_max_response():
                output.question = QuestionerUtils.format_continue_ask_question(non_extracted_key_fields)
                is_continue_ask = True
            else:
                raise build_error(
                    StatusCode.COMPONENT_QUESTIONER_RUNTIME_ERROR,
                    error_msg=" max_response reached before all required fields were extracted"
                )
        if is_continue_ask:
            output.key_fields.clear()
        else:
            output.key_fields.update(self._state.extracted_key_fields)
        return is_continue_ask

    async def _invoke_llm_and_parse_result(self, chat_history, output):
        llm_inputs = self._build_llm_inputs(chat_history=chat_history)
        extracted_key_fields = await self._invoke_llm_for_extraction(llm_inputs)
        for k, v in extracted_key_fields.items():
            if v:
                output.key_fields.update({k: v})

        self._update_state_of_key_fields(extracted_key_fields)

    async def _get_latest_human_feedback(self, session):
        for _ in range(self._state.response_num + 1):
            self._query = await session.interact(self._state.question)  # keep the last question, in case of no feedback
        self._increment_state_of_response_num()

    def _update_questioner_states_question(self, question):
        self._state.question = question


class QuestionerExecutable(ComponentExecutable):
    def __init__(self, config: QuestionerConfig):
        super().__init__()
        self._validate_config(config)
        self._config = config
        self._default_config = QuestionerDefaultConfig()
        self._llm: Union[Model, None] = None
        self._initialized: bool = False
        self._prompt: PromptTemplate = self._init_prompt()
        self._state = None

    @staticmethod
    def _load_state_from_session(session: Session) -> QuestionerState:
        questioner_state = session.get_state()
        state_dict = questioner_state.get(QUESTIONER_STATE_KEY) if isinstance(questioner_state, dict) else None
        if state_dict:
            return QuestionerState.deserialize(state_dict)
        return QuestionerState()

    @staticmethod
    def _store_state_to_session(state: QuestionerState, session: Session):
        state_dict = state.serialize()
        session.update_state({QUESTIONER_STATE_KEY: state_dict})

    @staticmethod
    def _validate_max_response_num_config(max_response_num: int):
        if max_response_num <= 0:
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_CONFIG_ERROR,
                error_msg="max response must be greater than 0"
            )

    @staticmethod
    def _validate_extract_key_fields_config(if_extract: bool, extract_key_fields: List[FieldInfo]):
        if if_extract and not extract_key_fields:
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_CONFIG_ERROR,
                error_msg="extracted key fields cannot be empty"
            )
        for item in extract_key_fields:
            if not item.field_name:
                raise build_error(
                    StatusCode.COMPONENT_QUESTIONER_CONFIG_ERROR,
                    error_msg="extracted key field name cannot be empty"
                )

    @staticmethod
    def _validate_response_type_config(response_type: str):
        response_type_values = [member.value for member in ResponseType]
        if response_type not in response_type_values:
            raise build_error(
                StatusCode.COMPONENT_QUESTIONER_CONFIG_ERROR,
                error_msg=f"response type {response_type} is invalid"
            )

    def state(self, state: QuestionerState):
        self._state = state
        return self

    async def invoke(self, inputs: Input, session: Session, context: ModelContext) -> Output:
        state_from_session = self._load_state_from_session(session)
        if state_from_session.is_undergoing_interaction():
            current_state = state_from_session  # recover state from session
        else:
            current_state = QuestionerState()  # create new state

        current_state = current_state.handle_event(QuestionerEvent.START_EVENT)
        await self._initialize_if_needed()
        invoke_result = dict()
        if self._config.response_type == ResponseType.ReplyDirectly.value:
            invoke_result = await self._handle_questioner_direct_reply_safe(
                inputs, session, context, current_state
            )
            # handler might update state
            current_state = invoke_result.pop('_state', current_state)

        self._store_state_to_session(current_state, session)

        if current_state.is_undergoing_interaction():
            await session.interact(invoke_result.get("question", ""))

        return invoke_result

    async def _create_llm_instance(self) -> Model:
        if self._config.model_id is None:
            if self._config.model_client_config is None or self._config.model_config is None:
                raise build_error(
                    StatusCode.COMPONENT_QUESTIONER_INVOKE_CALL_FAILED,
                    error_msg="failed to create llm instance"
                )
            return Model(self._config.model_client_config, self._config.model_config)
        else:
            from openjiuwen.core.runner import Runner
            return await Runner.resource_mgr.get_model(id=self._config.model_id)

    async def _initialize_if_needed(self):
        if not self._initialized:
            try:
                self._llm = await self._create_llm_instance()
                self._initialized = True
            except Exception as e:
                raise build_error(
                    StatusCode.COMPONENT_QUESTIONER_INVOKE_CALL_FAILED,
                    error_msg="failed to initialize llm if needed",
                    cause=e
                ) from e

    def _init_prompt(self) -> PromptTemplate:
        return PromptTemplate(content=self._default_config.prompt_template)

    async def _handle_questioner_direct_reply(self, inputs: Input, session: Session, context):
        handler = (QuestionerDirectReplyHandler()
                   .config(self._config).model(self._llm).state(self._state).prompt(self._prompt))
        result = await handler.handle(inputs, session, context)
        self._state = handler.get_state()
        return result

    async def _handle_questioner_direct_reply_safe(
            self, inputs: Input, session: Session, context, current_state: QuestionerState
    ):
        handler = (QuestionerDirectReplyHandler()
                   .config(self._config).model(self._llm).state(current_state).prompt(self._prompt))
        result = await handler.handle(inputs, session, context)
        # return updated state, let caller manage
        result['_state'] = handler.get_state()
        return result

    def _validate_config(self, config: QuestionerConfig):
        self._validate_response_type_config(config.response_type)
        self._validate_extract_key_fields_config(config.extract_fields_from_response, config.field_names)
        self._validate_max_response_num_config(config.max_response)


class QuestionerComponent(ComponentComposable):
    def __init__(self, questioner_comp_config: QuestionerConfig = None):
        super().__init__()
        self._questioner_config = questioner_comp_config
        self._executable = None

    def to_executable(self) -> Executable:
        return QuestionerExecutable(self._questioner_config).state(QuestionerState())
