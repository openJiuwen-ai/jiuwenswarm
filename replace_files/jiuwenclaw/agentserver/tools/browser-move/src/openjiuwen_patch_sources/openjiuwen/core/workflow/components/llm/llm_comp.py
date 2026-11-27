# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Any, Dict, Optional, AsyncIterator, Union

from pydantic import ValidationError, Field, BaseModel

from openjiuwen.core.common.exception.errors import build_error, BaseError
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.logging import workflow_logger, LogEventType
from openjiuwen.core.common.utils.schema_utils import SchemaUtils
from openjiuwen.core.workflow.components.base import ComponentConfig
from openjiuwen.core.workflow.components.component import ComponentComposable, ComponentExecutable
from openjiuwen.core.context_engine import ModelContext
from openjiuwen.core.graph.executable import Input, Output
from openjiuwen.core.session.node import Session
from openjiuwen.core.common.security.user_config import UserConfig
from openjiuwen.core.foundation.llm import (
    BaseMessage, SystemMessage, UserMessage, ModelRequestConfig, ModelClientConfig, Model
)
from openjiuwen.core.foundation.prompt import PromptTemplate

WORKFLOW_CHAT_HISTORY = "workflow_chat_history"
_ROLE = "role"
_CONTENT = "content"
ROLE_MAP = {"user": "用户", "assistant": "助手", "system": "系统"}
_SPAN = "span"
_WORKFLOW_DATA = "workflow_data"
_ID = "id"
_TYPE = "type"
_INSTRUCTION_NAME = "instruction_name"
_TEMPLATE_NAME = "template_name"

# Provider name mapping: lowercase -> correct case for _CLIENT_TYPE_REGISTRY
_PROVIDER_NAME_MAP = {
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "siliconflow": "SiliconFlow",
    "dashscope": "DashScope",
}


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    FUNCTION = "function"


class WorkflowLLMResponseType(Enum):
    JSON = "json"
    MARKDOWN = "markdown"
    TEXT = "text"


RESPONSE_FORMAT_TO_PROMPT_MAP = {
    WorkflowLLMResponseType.JSON.value: {
        _INSTRUCTION_NAME: "jsonInstruction",
        _TEMPLATE_NAME: "llm_json_formatting"
    },
    WorkflowLLMResponseType.MARKDOWN.value: {
        _INSTRUCTION_NAME: "markdownInstruction",
        _TEMPLATE_NAME: "llm_markdown_formatting"
    }
}


class WorkflowLLMUtils:

    @staticmethod
    def extract_content(response) -> str:
        return response.content if hasattr(response, "content") else str(response)


class ValidationUtils:

    @staticmethod
    def raise_invalid_params_error(error_msg: str = "") -> None:
        raise build_error(
            StatusCode.COMPONENT_LLM_CONFIG_INVALID,
            error_msg=error_msg
        )

    @staticmethod
    def validate_type(instance: Any, expected_type: str) -> None:
        type_validators = {
            "object": lambda x: isinstance(x, dict),
            "array": lambda value: isinstance(value, list),
            "string": lambda value: isinstance(value, str),
            "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
            "boolean": lambda value: isinstance(value, bool),
            "number": lambda value: isinstance(value, (float, int)) and not isinstance(value, bool),
        }

        validator = type_validators.get(expected_type)
        if not validator:
            ValidationUtils.raise_invalid_params_error(error_msg=f"{expected_type} is not a valid type")

        if not validator(instance):
            ValidationUtils.raise_invalid_params_error(
                error_msg=f"expected type {expected_type} but got {type(instance)}")

    @staticmethod
    def validate_json_schema(instance: Any, schema: Dict[str, Any]) -> None:
        if "type" not in schema:
            ValidationUtils.raise_invalid_params_error("schema must have 'type' key")
        ValidationUtils.validate_type(instance=instance, expected_type=schema["type"])

        if schema["type"] == "object":
            ValidationUtils._validate_object_properties(instance, schema)

        elif schema["type"] == "array":
            ValidationUtils._validate_array_items(instance, schema)

    @staticmethod
    def _validate_object_properties(instance: Any, schema: Dict[str, Any]) -> None:
        if "properties" not in schema:
            return
        required_fields = schema.get("required", [])
        missing_fields = [field for field in required_fields if field not in instance]
        if missing_fields:
            ValidationUtils.raise_invalid_params_error(f"missing required properties {missing_fields}")
        for prop_name, prop_schema in schema["properties"].items():
            if prop_name in instance:
                ValidationUtils.validate_json_schema(instance=instance[prop_name], schema=prop_schema)

    @staticmethod
    def _validate_array_items(instance: Any, schema: Dict[str, Any]) -> None:
        if "items" not in schema:
            return

        for i, item in enumerate(instance):
            try:
                ValidationUtils.validate_json_schema(instance=item, schema=schema["items"])
            except BaseError as e:
                ValidationUtils.raise_invalid_params_error(f"invalid array item {i}: {type(e).__name__}")

    @staticmethod
    def validate_outputs_config(outputs_config: Any) -> None:
        """Validate output config parameters"""
        if not outputs_config:
            ValidationUtils.raise_invalid_params_error("outputs config must not be empty")
        if not isinstance(outputs_config, dict):
            ValidationUtils.raise_invalid_params_error("outputs config must be a dict")


class SchemaGenerator:
    @staticmethod
    def generate_json_schema(outputs_config: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        properties = {}
        required = []

        for field_name, field_config in outputs_config.items():
            properties[field_name] = {
                "type": field_config.get("type", "string"),
                "description": field_config.get("description", "")
            }

            if field_config.get("type") == "array" and "items" in field_config:
                properties[field_name]["items"] = field_config["items"]

            if field_config.get("type") == "object" and "properties" in field_config:
                properties[field_name]["properties"] = field_config["properties"]

            if field_config.get("required", True):
                required.append(field_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required
        }


class JsonParser:
    @staticmethod
    def parse_json_content(response_content: str) -> Dict[str, Any]:
        content = JsonParser._clean_markdown_blocks(response_content)

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            if UserConfig.is_sensitive():
                ValidationUtils.raise_invalid_params_error("Json parse error")
            else:
                ValidationUtils.raise_invalid_params_error(f"Json parse error: {response_content}")

    @staticmethod
    def _clean_markdown_blocks(content: str):
        content = content.strip()

        if not (content.startswith("```") and content.endswith("```")):
            return content

        lines = content.split("\n")

        if lines[0].startswith("```"):
            lines = lines[1:]

        if lines and lines[-1] == "```":
            lines = lines[:-1]

        return '\n'.join(lines).strip()


class OutputFormatter:
    @staticmethod
    def format_response(response_content: str, response_format: dict, outputs_config: dict) -> dict:
        response_type = response_format.get("type")
        ValidationUtils.validate_outputs_config(outputs_config)

        formatters = {
            "text": OutputFormatter._format_text_response,
            "markdown": OutputFormatter._format_text_response,
            "json": OutputFormatter._format_json_response
        }

        formatter = formatters.get(response_type)
        if not formatter:
            ValidationUtils.raise_invalid_params_error(f"no supported response type: '{response_type}'")

        return formatter(response_content, outputs_config)

    @staticmethod
    def _format_text_response(response_content: str, outputs_config: dict) -> dict:
        if len(outputs_config) != 1:
            ValidationUtils.raise_invalid_params_error(
                f"text/markdown response type, outputs_config must contain only one field")
        field_name = next(iter(outputs_config))
        return {field_name: response_content}

    @staticmethod
    def _format_json_response(response_content: str, outputs_config: dict) -> dict:
        if not outputs_config:
            ValidationUtils.raise_invalid_params_error(
                f"json response format, output config should contain at least one field")

        parsed_json = JsonParser.parse_json_content(response_content)

        config_type = outputs_config.get("type")
        if isinstance(config_type, str) and config_type == "object":
            SchemaUtils.validate_with_schema(parsed_json, outputs_config)
            return SchemaUtils.format_with_schema(parsed_json, outputs_config)
        else:
            json_schema = SchemaGenerator.generate_json_schema(outputs_config)
            OutputFormatter._validate_json_schema(parsed_json, json_schema, response_content)

            return OutputFormatter._extract_configured_fields(parsed_json, outputs_config)

    @staticmethod
    def _validate_json_schema(parsed_json: dict, json_schema: dict, original_content: str) -> None:
        try:
            ValidationUtils.validate_json_schema(parsed_json, json_schema)
        except BaseError as e:
            raise e
        except Exception as e:
            if UserConfig.is_sensitive():
                ValidationUtils.raise_invalid_params_error("json schema validation failed.")
            else:
                ValidationUtils.raise_invalid_params_error(f"json schema validation failed: {original_content}")

    @staticmethod
    def _extract_configured_fields(parsed_json: dict, outputs_config: dict) -> dict:
        output = {}
        missing_keys = []

        for field_name, field_config in outputs_config.items():
            if field_name not in parsed_json:
                if field_config.get("required", True):
                    missing_keys.append(field_name)
            else:
                iterable_data = list(v) if (v := parsed_json[field_name]) and isinstance(v, dict) else []
                for key in iterable_data:
                    if isinstance(key, str) and key not in field_config.get("properties", {}):
                        parsed_json[field_name].pop(key)
                output[field_name] = parsed_json[field_name]

        if missing_keys:
            if UserConfig.is_sensitive():
                ValidationUtils.raise_invalid_params_error("missing required fields.")
            else:
                ValidationUtils.raise_invalid_params_error(f"missing required fields: {', '.join(missing_keys)}")

        return output


class LLMPromptFormatter:

    _DEFAULT_MARKDOWN_INSTRUCTION = (
        "Please return the answer in markdown format.\n"
        "- For headings, use number signs (#).\n"
        "- For list items, start with dashes (-).\n"
        "- To emphasize text, wrap it with asterisks (*).\n"
        "- For code or commands, surround them with backticks (`).\n"
        "- For quoted text, use greater than signs (>).\n"
        "- For links, wrap the text in square brackets [], followed by the URL in parentheses ().\n"
        "- For images, use square brackets [] for the alt text, followed by the image URL in parentheses ().\n"
        "The question is: ${query}."
    )

    _DEFAULT_JSON_INSTRUCTION = (
        "Carefully consider the user's question to ensure your answer is logical and makes sense.\n"
        "- Make sure your explanation is concise and easy to understand, not verbose.\n"
        "- Strictly return the answer in valid JSON format only, and "
        "\"DO NOT ADD ANY COMMENTS BEFORE OR AFTER IT\" to ensure it could be formatted "
        "as a JSON instance that conforms to the JSON schema below.\n"
        "Here is the JSON schema: ${json_schema}.\n"
        "The question is: ${query}."
    )

    @staticmethod
    def _get_role(msg) -> str:
        """Get role from message (dict or BaseMessage)."""
        if isinstance(msg, dict):
            return msg.get("role", "")
        return getattr(msg, "role", "")

    @staticmethod
    def _get_content(msg) -> str:
        """Get content from message (dict or BaseMessage)."""
        if isinstance(msg, dict):
            return msg.get("content", "")
        return getattr(msg, "content", "")

    @staticmethod
    def _set_content(msg, content: str):
        """Set content on message (dict or BaseMessage)."""
        if isinstance(msg, dict):
            msg["content"] = content
        else:
            msg.content = content

    @staticmethod
    def _find_last_user_index(history: List[BaseMessage]) -> int | None:
        for idx in range(len(history) - 1, -1, -1):
            if history[idx].role == "user":
                return idx
        return None

    @staticmethod
    def format_prompt(
            history: List[BaseMessage],
            response_format: Dict[str, Any],
            output_config: dict,
    ) -> List[BaseMessage]:
        res_type = response_format.get("type")
        if res_type == "text":
            return history

        last_user_idx = LLMPromptFormatter._find_last_user_index(history)
        if last_user_idx is None:
            return history
        query = history[last_user_idx].content
        prompt = query

        if res_type == "markdown":
            instruction = (
                    response_format.get("markdownInstruction")
                    or LLMPromptFormatter._DEFAULT_MARKDOWN_INSTRUCTION
            )
            prompt = instruction.replace("${query}", query)

        elif res_type == "json":
            config_type = output_config.get("type")
            if isinstance(config_type, str) and config_type == "object":
                json_schema = output_config
            else:
                json_schema = SchemaGenerator.generate_json_schema(output_config)
            instruction = (
                    response_format.get("jsonInstruction")
                    or LLMPromptFormatter._DEFAULT_JSON_INSTRUCTION
            )
            prompt = (
                instruction
                .replace("${json_schema}", json.dumps(json_schema, ensure_ascii=False))
                .replace("${query}", query)
            )

        history[last_user_idx].content = prompt
        return history


@dataclass
class LLMCompConfig(ComponentConfig):
    model_id: Optional[str] = None
    model_client_config: Optional[ModelClientConfig] = field(default=None)
    model_config: Optional[ModelRequestConfig] = field(default=None)
    template_content: List[Any] = field(default_factory=list)
    system_prompt_template: Optional[SystemMessage] = None
    user_prompt_template: Optional[UserMessage] = None
    response_format: Dict[str, Any] = field(default_factory=dict)
    output_config: Dict[str, Any] = field(default_factory=dict)
    enable_history: bool = False
    cache_stream: bool = False


class ResponseFormatConfig(BaseModel):
    response_type: str = Field(pattern=r'^(text|markdown|json)$', alias="type")


class OutputParamConfig(BaseModel):
    param_type: str = Field(default="", alias="type")
    param_description: str = Field(default="", alias="description")
    param_required: bool = Field(default=False, alias="required")


class LLMExecutableState:
    """State maintained by LLMExecutable for caching stream results"""
    
    def __init__(self):
        self.final_result: Dict[str, Any] = {}
        self._accumulated_content: str = ""
    
    def accumulate_content(self, content: str):
        """Accumulate stream content chunks"""
        self._accumulated_content += content
    
    def build_final_result(self, response_format: Dict[str, Any], output_config: Dict[str, Any]) -> Dict[str, Any]:
        """Build final result from accumulated content"""
        if not self._accumulated_content:
            return {}
        return OutputFormatter.format_response(
            self._accumulated_content,
            response_format,
            output_config
        )
    
    def clear(self):
        """Clear state"""
        self.final_result = {}
        self._accumulated_content = ""


class LLMExecutable(ComponentExecutable):
    def __init__(self, component_config: LLMCompConfig):
        super().__init__()
        self._validate_config(component_config)
        self._config: LLMCompConfig = component_config
        self._llm: Union[Model, None] = None
        self._initialized: bool = False
        self._session = None
        self._context = None
        self._state = LLMExecutableState()

    @property
    def config(self) -> LLMCompConfig:
        return self._config

    @staticmethod
    def _validate_template(template_content, system_prompt_template, user_prompt_template):
        if system_prompt_template or user_prompt_template or not template_content:
            return
        try:
            for element in template_content:
                if element.get(_ROLE, "") == "system":
                    SystemMessage.model_validate(element)
        except ValidationError as e:
            raise build_error(
                StatusCode.COMPONENT_LLM_TEMPLATE_CONFIG_ERROR,
                error_msg="system message is invalid",
                cause=e
            ) from e

        if_contain_user_message = False
        for element in template_content:
            if element.get(_ROLE, "") == "user":
                UserMessage.model_validate(element)
                if_contain_user_message = True
            if if_contain_user_message and element.get(_ROLE, "") == "system":
                SystemMessage.model_validate(element)
                raise build_error(
                    StatusCode.COMPONENT_LLM_TEMPLATE_CONFIG_ERROR,
                    error_msg="system message must be before user message"
                )
        if not if_contain_user_message:
            raise build_error(
                StatusCode.COMPONENT_LLM_TEMPLATE_CONFIG_ERROR,
                error_msg="user message is required"
            )

    @staticmethod
    def _validate_output_config(output_config):
        if not output_config:
            raise build_error(
                StatusCode.COMPONENT_LLM_CONFIG_ERROR,
                error_msg="output config is empty"
            )
        config_type = output_config.get("type")
        if isinstance(config_type, str) and config_type == "object":
            return
        for param, value in output_config.items():
            if not param:
                raise build_error(
                    StatusCode.COMPONENT_LLM_CONFIG_ERROR,
                    error_msg=f"output config parameter {param} is empty"
                )
            try:
                OutputParamConfig.model_validate(value)
            except ValidationError as e:
                if UserConfig.is_sensitive():
                    raise build_error(
                        StatusCode.COMPONENT_LLM_CONFIG_ERROR,
                        error_msg=f"output config parameter's config value is invalid",
                        cause=e
                    ) from e
                else:
                    raise build_error(
                        StatusCode.COMPONENT_LLM_CONFIG_ERROR,
                        error_msg=f"output config parameter's config {value} is invalid",
                        cause=e
                    ) from e

    @staticmethod
    def _validate_response_format(response_format, output_config):
        response_type = ""
        try:
            response_type = ResponseFormatConfig.model_validate(response_format).response_type
        except ValidationError as e:
            raise build_error(
                StatusCode.COMPONENT_LLM_RESPONSE_CONFIG_INVALID,
                error_msg=f"response format {response_format} is invalid",
                cause=e
            ) from e

        if response_type in ["text", "markdown"] and len(output_config) != 1:
            raise build_error(
                StatusCode.COMPONENT_LLM_RESPONSE_CONFIG_INVALID,
                error_msg="output config must contain exactly one parameter for text or markdown response type"
            )

    async def invoke(self, inputs: Input, session: Session, context: ModelContext) -> Output:
        self._set_session(session)
        self._set_context(context)
        model_inputs = await self._prepare_model_inputs(inputs)
        workflow_logger.info(
            "LLM component invoke started",
            event_type=LogEventType.WORKFLOW_COMPONENT_START,
            component_id=self._session.get_executable_id(),
            component_type_str="LLMComponent",
            session_id=self._session.get_session_id(),
            metadata={
                "model_name": self._config.model_config.model_name if self._config.model_config else None,
                "has_inputs": bool(model_inputs),
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )
        response = ""
        try:
            llm_response = await self._llm.invoke(messages=model_inputs)
            response = llm_response.content
        except Exception as e:
            if UserConfig.is_sensitive():
                raise build_error(
                    StatusCode.COMPONENT_LLM_INVOKE_CALL_FAILED,
                    error_msg=f"invoke llm failed",
                    cause=e
                ) from e
            else:
                raise build_error(
                    StatusCode.COMPONENT_LLM_INVOKE_CALL_FAILED,
                    error_msg=str(e),
                    cause=e
                ) from e

        workflow_logger.info(
            "LLM component invoke completed",
            event_type=LogEventType.WORKFLOW_COMPONENT_END,
            component_id=self._session.get_executable_id(),
            component_type_str="LLMComponent",
            session_id=self._session.get_session_id(),
            metadata={
                "has_response": bool(response),
                "response_length": len(response) if response else 0,
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )
        return self._create_output(response)

    async def stream(self, inputs: Input, session: Session, context: ModelContext) -> AsyncIterator[Output]:
        self._set_session(session)
        self._set_context(context)
        # Reset state for new stream
        if self._config.cache_stream:
            self._state.clear()
        response_format_type = self._config.response_format.get(_TYPE, "")
        try:
            if response_format_type == WorkflowLLMResponseType.JSON.value:
                async for out in self._invoke_for_json_format(inputs):
                    yield out
            else:
                async for out in self._stream_with_chunks(inputs):
                    yield out
        except Exception as e:
            if UserConfig.is_sensitive():
                raise build_error(
                    StatusCode.COMPONENT_LLM_INVOKE_CALL_FAILED,
                    error_msg="failed to stream",
                    cause=e
                ) from e
            else:
                raise build_error(
                    StatusCode.COMPONENT_LLM_INVOKE_CALL_FAILED,
                    error_msg=str(e),
                    cause=e
                ) from e

    def get_stream_output(self) -> Optional[dict]:
        output = None
        if self._config.cache_stream and self._session:
            final_result = self._state.build_final_result(
                self._config.response_format,
                self._config.output_config
            )
            output = final_result if final_result else output
        return output

    async def _initialize_if_needed(self):
        if not self._initialized:
            try:
                self._llm = await self._create_llm_instance()
                self._initialized = True
            except Exception as e:
                raise build_error(
                    StatusCode.COMPONENT_LLM_INIT_FAILED,
                    error_msg="failed to initialize llm if needed",
                    cause=e
                ) from e

    async def _create_llm_instance(self):
        if self._config.model_id is None:
            if self._config.model_client_config is None or self._config.model_config is None:
                raise build_error(
                    StatusCode.COMPONENT_LLM_INVOKE_CALL_FAILED,
                    error_msg="failed to create llm instance"
                )
            return Model(self._config.model_client_config, self._config.model_config)
        else:
            from openjiuwen.core.runner import Runner
            return await Runner.resource_mgr.get_model(id=self._config.model_id)

    def _build_user_prompt_content(self, inputs: dict) -> list[BaseMessage]:
        system_prompt_template = getattr(self._config, "system_prompt_template", None)
        user_prompt_template = getattr(self._config, "user_prompt_template", None)

        if system_prompt_template is not None or user_prompt_template is not None:
            if user_prompt_template is None:
                return [UserMessage(content="")]
            return PromptTemplate(content=[user_prompt_template]).format(inputs).to_messages()

        template_content = self._config.template_content
        if not template_content:
            return [UserMessage(content="")]

        user_prompt = [
            element for element in template_content
            if element.get(_ROLE, "") == MessageRole.USER.value
        ]

        return PromptTemplate(content=[user_prompt[0]]).format(inputs).to_messages()

    def _get_model_input(self, inputs: dict):
        system_prompt = self._build_system_prompt(inputs)
        user_prompt = self._build_user_prompt_content(inputs)
        all_prompts = self._insert_history_to_system_and_user_prompt(system_prompt, user_prompt)
        return LLMPromptFormatter.format_prompt(history=all_prompts,
                                                response_format=self._config.response_format,
                                                output_config=self._config.output_config)

    def _insert_history_to_system_and_user_prompt(self, system_prompt: list, user_prompt: list):
        original_history = system_prompt if isinstance(system_prompt, list) else []
        if self._config.enable_history and self._context:
            chat_history = self._context.get_messages()
            original_history.extend(chat_history)
        original_history.extend(user_prompt)
        return original_history

    def _build_template_filters(self) -> dict:
        filters = {}

        model_name = self._config.model_config.model_name
        if model_name:
            filters["model_name"] = model_name

        return filters

    def _create_output(self, llm_output) -> Output:
        try:
            formatted_res = OutputFormatter.format_response(llm_output,
                                                            self._config.response_format,
                                                            self._config.output_config)
            return formatted_res
        except BaseError as e:
            if e.code == StatusCode.COMPONENT_LLM_CONFIG_INVALID.code:
                raise build_error(
                    StatusCode.COMPONENT_LLM_EXECUTION_PROCESS_ERROR,
                    error_msg=e.message,
                    cause=e
                ) from e
            else:
                raise e

    def _set_session(self, session: Session):
        self._session = session

    def _set_context(self, context):
        self._context = context

    async def _prepare_model_inputs(self, inputs):
        await self._initialize_if_needed()
        return self._get_model_input(inputs)

    async def _invoke_for_json_format(self, inputs: Input) -> AsyncIterator[Output]:
        model_inputs = await self._prepare_model_inputs(inputs)
        workflow_logger.info(
            "LLM component JSON format invoke started",
            event_type=LogEventType.WORKFLOW_COMPONENT_START,
            component_id=self._session.get_executable_id(),
            component_type_str="LLMComponent",
            session_id=self._session.get_session_id(),
            metadata={
                "response_format": "json",
                "has_inputs": bool(model_inputs),
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )
        llm_output = await self._llm.invoke(messages=model_inputs) # Add await if invoke is async
        llm_output_content = llm_output.content

        if self._config.cache_stream:
            self._state.accumulate_content(llm_output_content)

        output = self._create_output(llm_output_content)
        
        yield output

    async def _stream_with_chunks(self, inputs: Input) -> AsyncIterator[Output]:
        model_inputs = await self._prepare_model_inputs(inputs)
        try:
            async for chunk in self._llm.stream(messages=model_inputs):
                content = WorkflowLLMUtils.extract_content(chunk)
                if content:
                    # Accumulate content if cache_stream is enabled
                    if self._config.cache_stream:
                        self._state.accumulate_content(content)
                    
                    formatted_res = OutputFormatter.format_response(content,
                                                                    self._config.response_format,
                                                                    self._config.output_config)
                    stream_out = formatted_res
                    yield stream_out
        except Exception:
            # Clear state on error
            if self._config.cache_stream:
                self._state.clear()
            raise

    def _build_system_prompt(self, inputs: dict):
        system_prompt_template = getattr(self._config, "system_prompt_template", None)
        user_prompt_template = getattr(self._config, "user_prompt_template", None)

        if system_prompt_template is not None or user_prompt_template is not None:
            if system_prompt_template is None:
                return []
            return PromptTemplate(content=[system_prompt_template]).format(inputs).to_messages()

        system_prompt = []
        for element in self._config.template_content:
            if element.get(_ROLE, "") == "system":
                system_prompt.append(element)
            else:
                break
        return PromptTemplate(content=system_prompt).format(inputs).to_messages()

    def _validate_config(self, config: LLMCompConfig):
        self._validate_template(config.template_content, config.system_prompt_template, config.user_prompt_template)
        self._validate_response_format(config.response_format, config.output_config)
        self._validate_output_config(config.output_config)


class LLMComponent(ComponentComposable):
    def __init__(self, component_config: Optional[LLMCompConfig] = None):
        super().__init__()
        self._executable = None
        self._config = component_config

    @property
    def executable(self) -> LLMExecutable:
        if self._executable is None:
            self._executable = self.to_executable()
        return self._executable

    def to_executable(self) -> LLMExecutable:
        return LLMExecutable(self._config)
