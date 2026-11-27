# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import ast
import re
from dataclasses import dataclass, field
from typing import Optional, Union, Callable, List

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.common.logging import workflow_logger, LogEventType
from openjiuwen.core.common.security.exception_utils import ExceptionUtils
from openjiuwen.core.workflow.components.base import ComponentConfig
from openjiuwen.core.workflow.components.component import ComponentComposable, ComponentExecutable
from openjiuwen.core.workflow.components.flow.branch_router import BranchRouter
from openjiuwen.core.workflow.components.condition.condition import Condition
from openjiuwen.core.context_engine import ModelContext
from openjiuwen.core.graph.base import Graph
from openjiuwen.core.graph.executable import Output, Input
from openjiuwen.core.session.node import Session
from openjiuwen.core.foundation.llm import (
    BaseMessage, UserMessage, SystemMessage, ModelRequestConfig, ModelClientConfig, Model
)
from openjiuwen.core.foundation.prompt import PromptTemplate
from openjiuwen.core.common.security.user_config import UserConfig


LUI = "llm"
NAME = "name"
MODEL = "model"
CLASS = "class"
REASON = "reason"
INPUT = "input"
USER_PROMPT = "user_prompt"
CATEGORY_INFO = "category_info"
CATEGORY_LIST = "category_list"
CATEGORY_NAME_LIST = "category_name_list"
DEFAULT_CLASS = "default_class"
CHAT_HISTORY = "chat_history"
EXAMPLE_CONTENT = "example_content"
ENABLE_HISTORY = "enable_history"
ENABLE_INPUT = "enable_input"
LLM_INPUTS = "llm_inputs"
LLM_OUTPUTS = "llm_outputs"
MODEL_SOURCE = "modelType"
MODEL_NAME = "modelName"
EXTENSION = "extension"
CHAT_HISTORY_MAX_TURN = "chat_history_max_turn"
INTENT_DETECTION_TEMPLATE = "intent_detection_template"
ROLE = "role"
CONTENT = "content"
ROLE_MAP = {"user": '用户', 'assistant': '助手', 'system': '系统', 'tool': '工具'}
JSON_PARSE_FAIL_REASON = "当前意图识别的输出:'{result}'格式不符合有效的JSON规范，导致解析失败，因此返回默认分类。"
CLASS_KEY_MISSING_REASON = "当前意图识别的输出 '{result}' 缺少必要的输出'class'分类信息，因此返回默认分类。"
VALIDATION_FAIL_REASON = "当前意图识别的输出类别 '{intent_class}' 不在预定义的分类列表: '{category_list}'中，因此系统返回默认分类。"
WORKFLOW_CHAT_HISTORY = "workflow_chat_history"

RESULT = "result"
CATEGORY_NAME_ITS1 = "category_name_list"
FEW_SHOT_NUM = 5
ENABLE_Q2L = 'enableKnowledge'
DEFAULT_QUERY_CATE = 'title'
DEFAULT_CLASS_CATE = 'content'
DEFAULT_INT = "不确定，其他的意图"
SEARCH_TYPE = "faq"
SEARCH_NUM = 5
CLASSIFICATION_ID = "classificationId"
CLASSIFICATION_DEFAULT_ID = 0
CLASSIFICATION_NAME = "name"
CLASSIFICATION_DEFAULT_NAME = "默认意图"
KG_FILTER_KEY = "filter_string"
KG_FILTER_PREFIX = "category:"
KG_SCOPE = "scope"

_PROVIDER_NAME_MAP = {
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "siliconflow": "SiliconFlow",
    "dashscope": "DashScope",
}

DEFAULT_SYSTEM_PROMPT = "你是一个识别用户输入意图的AI助手。"

DEFAULT_USER_PROMPT = """
{{user_prompt}}

当前可供选择的功能分类如下：
{{category_info}}

用户与助手的对话历史：
{{chat_history}}

当前输入：
{{input}}

请根据当前输入和对话历史分析并输出最适合的功能分类。输出格式为 JSON，包含以下两个字段：
class: 代表分类结果
reason: 说明为何选择该分类
例如: {"class": "分类xx", "reason": "当前输入xxx"}
请参考以下示例：
{{example_content}}
如果没有合适的分类，请输出 {{default_class}}。
"""


def get_default_template():
    return PromptTemplate(
                content=[
                    SystemMessage(content=DEFAULT_SYSTEM_PROMPT),
                    UserMessage(content=DEFAULT_USER_PROMPT),
                ]
            )


@dataclass
class IntentDetectionCompConfig(ComponentConfig):
    model_id: Optional[str] = None
    model_client_config: Optional[ModelClientConfig] = field(default=None)
    model_config: Optional[ModelRequestConfig] = field(default=None)
    category_name_list: list[str] = field(default_factory=list)
    user_prompt: str = ""
    example_content: list[str] = field(default_factory=list)
    enable_history: bool = False
    chat_history_max_turn: int = 3


@dataclass
class IntentDetectionDefaultConfig:
    category_list: list[str] = field(default_factory=list)
    intent_detection_template: PromptTemplate = field(default_factory=get_default_template)
    default_class: str = '分类0'
    enable_input: bool = True


class IntentDetectionInput(BaseModel):
    query: str
    model_config = ConfigDict(extra='allow')   # Allow any extra fields


class IntentDetectionOutput(BaseModel):
    classification_id: int = Field(default=-1)
    reason: str = Field(default="")
    category_name: str = Field(default="")


@dataclass()
class IntentDetectionExecutable(ComponentExecutable):
    def __init__(self, component_config: IntentDetectionCompConfig):
        super().__init__()
        self._session: Union[Session, None] = None
        self._llm: Union[Model, None] = None
        self._initialized: bool = False
        self._config = component_config
        self._init_default_config_category_list(component_config)
        self._append_default_category()
        self._router: Union[BranchRouter, None] = None

    @staticmethod
    def _get_chat_history_from_context(context) -> List[BaseMessage]:
        chat_history = []
        if context:
            chat_history = context.get_messages()
        return chat_history

    @staticmethod
    def _refix_llm_output(input_str):
        json_path = r'\{.*\}'
        match = re.search(json_path, input_str, re.DOTALL)
        if match:
            res = match.group(0)
            res = res.replace("false", "False").replace("true", "True").replace("null", "None")
            return res
        else:
            return input_str

    async def invoke(self, inputs: Input, session: Session, context: ModelContext) -> Output:
        """Invoke IntentDetection node"""
        # Extract context data
        self._set_session(session)
        self._router.set_session(session)
        await self._initialize_if_needed()
        chat_history = self._get_chat_history_from_context(context)
        current_inputs = self._prepare_detection_inputs(inputs, chat_history)
        llm_output = await self._invoke_llm_and_get_result(current_inputs)
        workflow_logger.info(
            "Intent detection completed",
            event_type=LogEventType.WORKFLOW_COMPONENT_END,
            component_id=self._session.get_executable_id(),
            component_type_str="IntentDetectionComponent",
            session_id=self._session.get_session_id(),
            metadata={
                "has_output": bool(llm_output),
                "output_length": len(llm_output) if llm_output else 0,
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )
        intent_res = self._parse_detection_result(llm_output)
        return intent_res

    def set_router(self, router):
        self._router = router
        return self

    def post_commit(self) -> bool:
        return True

    def _get_category_info(self):
        return "\n".join(f"{cid}: {cname}" for cid, cname in
                         zip(self._default_config.category_list, self._config.category_name_list))

    def _set_session(self, session: Session):
        self._session = session

    async def _create_llm_instance(self) -> Model:
        if self._config.model_id is None:
            if self._config.model_client_config is None or self._config.model_config is None:
                raise build_error(
                    StatusCode.COMPONENT_INTENT_DETECTION_INVOKE_CALL_FAILED,
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
                    StatusCode.COMPONENT_INTENT_DETECTION_LLM_INIT_FAILED,
                    error_msg="failed to initialize llm if needed",
                    cause=e
                ) from e

    def _prepare_detection_inputs(self, inputs, chat_history):
        current_inputs = {}
        global_intent_map = []

        current_inputs.update({
            USER_PROMPT: self._config.user_prompt,
            CATEGORY_INFO: self._get_category_info(),
            DEFAULT_CLASS: self._default_config.default_class,
            ENABLE_HISTORY: self._config.enable_history,
            ENABLE_INPUT: self._default_config.enable_input,
            EXAMPLE_CONTENT: "\n\n".join(self._config.example_content),
            CHAT_HISTORY_MAX_TURN: self._config.chat_history_max_turn,
            CHAT_HISTORY: ""
        })

        # Process chat history
        if self._config.enable_history:
            chat_history_str = self._format_chat_history(chat_history)
            current_inputs.update({CHAT_HISTORY: chat_history_str})

        # Process current input
        if self._default_config.enable_input:
            try:
                intent_detection_input = IntentDetectionInput.model_validate(inputs)
                current_inputs.update({INPUT: intent_detection_input.query or ""})
            except ValidationError as e:
                raise build_error(
                    StatusCode.COMPONENT_INTENT_DETECTION_INPUT_PARAM_ERROR,
                    error_msg=ExceptionUtils.format_validation_error(e),
                    cause=e
                ) from e

        current_inputs['global_intent_map'] = global_intent_map

        return current_inputs

    def _format_chat_history(self, chat_history):
        chat_history_str = ""
        for history in chat_history[-self._config.chat_history_max_turn:]:
            if history.role in ROLE_MAP:
                chat_history_str += "{}: {}\n".format(
                    ROLE_MAP.get(history.role), history.content
                )
        return chat_history_str

    def _pre_process(self, inputs: dict):
        """Pre-process inputs for model"""
        final_prompts = self._default_config.intent_detection_template.format(inputs).to_messages()
        return final_prompts

    def _parse_detection_result(self, llm_output):
        intent_class, reason = self._post_process_intent_detection(llm_output)
        intent_id_and_name = self._get_intent_id_and_name(intent_class)
        return IntentDetectionOutput(classification_id=intent_id_and_name.get(CLASSIFICATION_ID, -1), reason=reason,
                                     category_name=intent_id_and_name.get(CLASSIFICATION_NAME, "")
                                     ).model_dump(exclude_defaults=True)

    async def _invoke_llm_and_get_result(self, current_inputs):
        """invoke llm and get result"""
        llm_inputs = self._default_config.intent_detection_template.format(current_inputs).to_messages()
        workflow_logger.info(
            "Intent detection LLM invoke started",
            event_type=LogEventType.WORKFLOW_COMPONENT_START,
            component_id=self._session.get_executable_id(),
            component_type_str="IntentDetectionComponent",
            session_id=self._session.get_session_id(),
            metadata={
                "has_inputs": bool(llm_inputs),
                "input_count": len(llm_inputs) if llm_inputs else 0,
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )
        llm_output_content = ""

        try:
            llm_output = await self._llm.invoke(messages=llm_inputs)
            llm_output_content = llm_output.content
        except Exception as e:
            raise build_error(
                StatusCode.COMPONENT_INTENT_DETECTION_INVOKE_CALL_FAILED,
                error_msg="failed to invoke llm and get result",
                cause=e
            ) from e
        workflow_logger.info(
            "Intent detection LLM invoke completed",
            event_type=LogEventType.WORKFLOW_COMPONENT_END,
            component_id=self._session.get_executable_id(),
            component_type_str="IntentDetectionComponent",
            session_id=self._session.get_session_id(),
            metadata={
                "has_output": bool(llm_output_content),
                "output_length": len(llm_output_content) if llm_output_content else 0,
                "sensitive_mode": UserConfig.is_sensitive()
            }
        )


        return llm_output_content

    def _post_process_intent_detection(self, result):
        """Post-process the result"""
        try:
            result = self._refix_llm_output(result)
            parsed_dict = ast.literal_eval(result)
        except Exception:
            return self._default_config.default_class, JSON_PARSE_FAIL_REASON.format(result=result)

        if not isinstance(parsed_dict, dict):
            return self._default_config.default_class, JSON_PARSE_FAIL_REASON.format(result=result)

        # post_process class information
        if not parsed_dict.get(CLASS):
            return self._default_config.default_class, CLASS_KEY_MISSING_REASON.format(result=parsed_dict)

        intent_class = parsed_dict.get(CLASS).replace('\n', '').replace(' ', '').replace('"', '').replace("'", '')
        match = re.search(r"分类\d+", intent_class)
        if match:
            parsed_dict.update({CLASS: match.group(0)})

        if not parsed_dict.get(CLASS) in self._default_config.category_list:
            reason = VALIDATION_FAIL_REASON.format(
                intent_class=parsed_dict.get(CLASS),
                category_list=self._default_config.category_list
            )
            parsed_dict.update({CLASS: self._default_config.default_class, REASON: reason})

        return parsed_dict.get(CLASS), parsed_dict.get(REASON, '')

    def _validate_llm_parsed_result(self, result):
        """Validation of LLM output"""
        return result in self._default_config.category_list

    def _append_default_category(self):
        self._default_config.category_list = [self._default_config.default_class] + self._default_config.category_list
        self._config.category_name_list = [CLASSIFICATION_DEFAULT_NAME] + self._config.category_name_list

    def _get_intent_id_and_name(self, intent_class):
        intent_res = {CLASSIFICATION_ID: CLASSIFICATION_DEFAULT_ID, CLASSIFICATION_NAME: CLASSIFICATION_DEFAULT_NAME}
        idx = next((i for i, category in enumerate(self._default_config.category_list) if category == intent_class), -1)
        if idx > -1:
            intent_res = {CLASSIFICATION_ID: idx, CLASSIFICATION_NAME: self._config.category_name_list[idx]}
        return intent_res

    def _init_default_config_category_list(self, component_config: IntentDetectionCompConfig):
        self._default_config = IntentDetectionDefaultConfig()
        for index, _ in enumerate(component_config.category_name_list, start=1):
            self._default_config.category_list.append(f"分类{index}")


class IntentDetectionComponent(ComponentComposable):
    def __init__(self, component_config: Optional[IntentDetectionCompConfig] = None):
        super().__init__()
        self._executable = None
        self._config = component_config
        self._router = BranchRouter()

    @property
    def executable(self) -> IntentDetectionExecutable:
        if self._executable is None:
            self._executable = self.to_executable()
        return self._executable

    def add_component(self, graph: Graph, node_id: str, wait_for_all: bool = False) -> None:
        graph.add_node(node_id, self.to_executable(), wait_for_all=wait_for_all)
        graph.add_conditional_edges(node_id, self._router)

    def to_executable(self) -> IntentDetectionExecutable:
        return IntentDetectionExecutable(self._config).set_router(self._router)

    def add_branch(self, condition: Union[str, Callable[[], bool], Condition], target: Union[str, list[str]],
                   branch_id: str = None):
        if isinstance(target, str):
            target = [target]
        self._router.add_branch(condition, target, branch_id=branch_id)

    def router(self) -> BranchRouter:
        return self._router
