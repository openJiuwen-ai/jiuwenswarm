# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 定制的结构化 AskUserRail。

复用 DeepAgent 的 ``StructuredAskUserRail`` 中断/恢复骨架，但把用户作答的
``tool_result`` 改成 skill_code（如 ppt）期望的结构化格式：:

    {"status": "answered", "answers": [{question, selected_options, custom_input}]}

这样 skill_code 里 ``call_tool("ask_user", questions=[...])`` 返回后，
可直接用 ``_normalize_ask_result`` / ``_apply_answer_item`` 解析用户作答，
前端 ask_user_question 卡片提交的 answers 结构原样回填，无需二次映射。

中断侧（首次调用，``user_input is None``）：
- 非引导模式（``interactive_ask=False``）且 question 带 ``preview``（内容确认类，
  如大纲审阅）时，直接返回 ``{"status": "skipped"}``，不触发中断、不弹卡片，管线沿用当前内容继续。
- 其余情况照常 ``self.interrupt(...)`` 触发 harness 原生 HITL。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.agents.harness.common.rails.ask_user_rail import StructuredAskUserRail
from jiuwenswarm.common.schema.ask_user import decode_user_input

logger = logging.getLogger(__name__)


class SkillTurboAskUserRail(StructuredAskUserRail):
    """把用户作答按前端 answers 结构回填给 skill_code。"""

    def __init__(self, language: Optional[str] = None):
        super().__init__(language=language)

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[Any],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ) -> Any:
        # 首次调用：中断等待用户作答（questions 已随 tool_call 透传前端）
        if user_input is None:
            # 非引导模式：带 preview 的内容确认类 ask_user 直接跳过，不弹卡片
            if self._should_skip_for_non_interactive(tool_call):
                logger.info(
                    "[SkillTurboAskUserRail] interactive_ask=False, "
                    "skip preview ask_user (non-interactive mode)"
                )
                return self.reject(
                    tool_result={
                        "status": "skipped",
                        "message": "未开启引导式交互，跳过大纲确认，使用生成的大纲继续。",
                        "answers": [],
                    }
                )
            return self.interrupt(self._build_ask_request(tool_call))

        # 与父类 resolve_interrupt 对齐：JSON 字符串（传输层双重编码）/ pydantic
        # model 形态先归一为 dict/list，避免误判为不认识形态而 re-interrupt
        # （重放时确定性 tool_call_id 相同，重问帧会被 sidecar dedup 吞掉）。
        answers = self._parse_user_answers(decode_user_input(user_input))
        if answers is None:
            # 无法解析（不认识的 user_input 形态）→ 重新中断等待
            logger.warning(
                "[SkillTurboAskUserRail] unrecognized user_input type=%s, re-interrupt",
                type(user_input).__name__,
            )
            return self.interrupt(self._build_ask_request(tool_call))

        return self.reject(tool_result={"status": "answered", "answers": answers})

    @staticmethod
    def _first_outline_preview(questions: list[Any]) -> dict[str, Any] | None:
        """返回第一个带非空 text 的 preview。

        与原分支 ``ask_user_question_tool._first_outline_preview`` 同构，
        作为"内容确认类交互"的判定依据。
        """
        for q in questions:
            if not isinstance(q, dict):
                continue
            preview = q.get("preview")
            if isinstance(preview, dict) and str(preview.get("text") or "").strip():
                return preview
        return None

    @staticmethod
    def _should_skip_for_non_interactive(tool_call: Optional[Any]) -> bool:
        """非引导模式下，带 preview 的内容确认类 ask_user 跳过。

        对齐原分支 ``_ask_user_question_impl`` 的
        ``if not interactive: first_preview = _first_outline_preview(...)``：
        非引导模式（``interactive_ask=False``）时仅跳过带 preview 的确认类问答；
        不带 preview 的普通问答（如需求收集）在非引导模式仍照常中断等待用户。
        """
        try:
            from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
                get_interactive_ask,
            )
        except ImportError:
            return False

        if get_interactive_ask():
            return False

        try:
            args = SkillTurboAskUserRail._parse_tool_args(tool_call)
        except Exception:
            return False

        questions = args.get("questions")
        if not isinstance(questions, list):
            return False

        return SkillTurboAskUserRail._first_outline_preview(questions) is not None

    @classmethod
    def _parse_user_answers(cls, user_input: Any) -> list[dict[str, Any]] | None:
        """把 resume 的 user_input 解析为 skill_code 期望的 answers 列表。

        支持形态：
        1. list of dict —— 前端 ask_user_question 卡片提交的结构（主形态）
        2. dict["answers"] = list of dict —— 桥接层包装
        3. dict["answers"] = {question: answer} / dict = {question: answer} —— 键值映射
        """
        if isinstance(user_input, list):
            return cls._normalize_items(user_input)
        if isinstance(user_input, dict):
            if "answers" in user_input:
                raw = user_input["answers"]
                if isinstance(raw, list):
                    return cls._normalize_items(raw)
                if isinstance(raw, dict):
                    return cls._dict_answers_to_items(raw)
                return None
            return cls._dict_answers_to_items(user_input)
        return None

    @staticmethod
    def _normalize_items(items: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            selected = normalized.get("selected_options")
            if selected is not None and not isinstance(selected, list):
                normalized["selected_options"] = [selected]
            result.append(normalized)
        return result

    @staticmethod
    def _dict_answers_to_items(mapping: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for question, answer in mapping.items():
            if not isinstance(question, str) or not question.strip():
                continue
            if isinstance(answer, list):
                selected = [str(a) for a in answer if a is not None]
            elif isinstance(answer, str) and answer.strip():
                selected = [answer]
            else:
                selected = []
            if selected:
                items.append({"question": question, "selected_options": selected})
        return items
