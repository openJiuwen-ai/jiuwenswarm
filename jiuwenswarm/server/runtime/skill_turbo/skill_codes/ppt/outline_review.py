from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.content_plan import (
    ContentPlanError,
    _validate_notes_leakage,
    _validate_outline_markdown_basic,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

logger = logging.getLogger(__name__)

_OUTLINE_CONFIRM_ID = "outline_confirm"
_OUTLINE_USE_EDITED_ID = "outline_use_edited"


class OutlineReviewNode(PlanNode):
    def __init__(self) -> None:
        super().__init__(
            plan_name="p5_outline_review",
            instruction=(
                "## P5 大纲审阅\n"
                "\n"
                "### 前置条件\n"
                "- `{output_dir}/outline.md` 存在且非空\n"
                "- `ask_user` 工具可用（不可用时直接跳过本节点）\n"
                "\n"
                "### 审阅流程\n"
                "1. 读取 `{output_dir}/outline.md` 全文\n"
                "2. 调用 `ask_user`，将大纲完整 Markdown 内容放入 preview 字段供用户预览：\n"
                "   - header: \"PPT 大纲审阅\"\n"
                "   - question: \"请审阅生成的 PPT 大纲，确认后将继续生成幻灯片\"\n"
                "   - preview: outline.md 的完整 Markdown 内容（禁止摘要、改写、省略）\n"
                "   - options: [确认大纲，继续生成 | 需要修改]\n"
                "3. 处理用户回复：\n"
                "   - 确认 → 保持 outline.md 不变，结束本节点\n"
                "   - 直接编辑 preview 文本 → 规则写回 outline.md，结束本节点\n"
                "   - NL 改稿 → 调用 LLM 修订 outline.md，结束本节点\n"
                "4. 改稿后复验 ✅ 页数门禁与备注泄漏；NL 失败可再修 1 次\n"
                "5. 失败兜底：工具调用失败或超时时，沿用当前 outline.md 继续\n"
                "\n"
                "### 关键约束\n"
                "- preview 字段必须传入 outline.md 的完整内容，禁止任何形式的摘要、改写、格式转换或省略\n"
                "- 引导模式未开启时 ask_user 返回 skipped，直接沿用当前大纲继续\n"
                "- 用户选择修改但未提供有效修改内容时，沿用当前大纲\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        if not self.has_tool("ask_user"):
            logger.info("[P5] ask_user 工具不可用，跳过大纲审阅")
            return {"p5_review_status": "skipped_no_tool", "p5_review_message": "ask_user 工具不可用，跳过大纲审阅"}

        output_dir = inputs.get("output_dir", "")
        outline_path = f"{output_dir}/outline.md" if output_dir else ""

        outline_text = await self._read_outline(outline_path)
        if not outline_text:
            logger.warning("[P5] outline.md 为空或不存在，跳过审阅")
            return {"p5_review_status": "skipped_no_outline", "p5_review_message": "outline.md 为空或不存在，跳过审阅"}

        ask_result = await self._ask_user_review(outline_text, outline_path)

        status = ask_result.get("status", "")
        if status == "skipped":
            logger.info("[P5] 引导模式未开启，跳过大纲审阅")
            return {"p5_review_status": "skipped_not_guided", "p5_review_message": "引导模式未开启，跳过大纲审阅，沿用当前大纲"}
        if status == "error":
            logger.warning("[P5] ask_user 调用失败，沿用当前大纲")
            return {"p5_review_status": "skipped_ask_error", "p5_review_message": "ask_user 调用失败，沿用当前大纲"}

        user_action = self._parse_user_action(ask_result)

        if user_action == _OUTLINE_CONFIRM_ID:
            logger.info("[P5] 用户确认大纲")
            return {"p5_review_status": "confirmed", "p5_review_message": "用户确认大纲，继续生成"}

        if user_action == _OUTLINE_USE_EDITED_ID:
            edited_text = self._extract_edited_text(ask_result, outline_text)
            if edited_text:
                gate_err = self._check_outline_gates(edited_text, inputs)
                if gate_err:
                    logger.warning("[P5] 用户直接编辑未过门禁（保留写回）: %s", gate_err)
                    await self._write_outline(outline_path, edited_text)
                    return {
                        "p5_review_status": "edited_with_gate_warning",
                        "p5_review_message": f"用户修改了大纲，但门禁告警：{gate_err}",
                        "p5_gate_warning": gate_err,
                    }
                await self._write_outline(outline_path, edited_text)
                logger.info("[P5] 用户修改了大纲（直接编辑preview），已写回")
                return {"p5_review_status": "edited", "p5_review_message": "用户修改了大纲（直接编辑preview），已写回"}

            user_nl_input = self._extract_user_nl_input(ask_result)
            if user_nl_input:
                revised = await self._llm_revise_outline(outline_text, user_nl_input)
                if revised:
                    gate_err = self._check_outline_gates(revised, inputs)
                    if gate_err:
                        logger.info("[P5] NL 改稿未过门禁，再修 1 次: %s", gate_err)
                        revised2 = await self._llm_revise_outline(
                            revised,
                            f"请修复以下大纲门禁问题后重新输出完整大纲：{gate_err}",
                        )
                        if revised2:
                            revised = revised2
                            gate_err = self._check_outline_gates(revised, inputs)
                    await self._write_outline(outline_path, revised)
                    if gate_err:
                        logger.warning("[P5] NL 改稿写回但仍有门禁告警: %s", gate_err)
                        return {
                            "p5_review_status": "nl_revised_with_gate_warning",
                            "p5_review_message": f"用户NL改稿已写回，但门禁告警：{gate_err}",
                            "p5_gate_warning": gate_err,
                        }
                    logger.info("[P5] 用户NL改稿，LLM修订大纲已写回")
                    return {"p5_review_status": "nl_revised", "p5_review_message": "用户NL改稿，LLM修订大纲已写回"}

            logger.warning("[P5] 用户选择修改但未提供有效修改内容，沿用当前大纲")
            return {"p5_review_status": "skipped_no_edit", "p5_review_message": "用户选择修改但未提供有效修改内容，沿用当前大纲"}

        logger.info("[P5] 未识别的用户操作，沿用当前大纲")
        return {"p5_review_status": "skipped_unknown_action", "p5_review_message": "未识别的用户操作，沿用当前大纲"}

    def _check_outline_gates(self, text: str, inputs: dict[str, Any]) -> str:
        """复验 ✅ 页数 + 备注泄漏；失败返回原因，通过返回空串。"""
        try:
            _validate_outline_markdown_basic(
                text,
                topic=str(inputs.get("topic") or "").strip(),
                page_count=inputs.get("page_count"),
                structural_page_request=str(inputs.get("structural_page_request") or "none"),
                structural_page_count=inputs.get("structural_page_count"),
                expand_page_mode=PptCommon.is_expand_page_mode(inputs),
            )
            _validate_notes_leakage(text)
            return ""
        except ContentPlanError as exc:
            return str(exc)
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            logger.warning("[P5] 门禁复验异常，跳过: %s", exc)
            return ""

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        review_status = result.get("p5_review_status", "")
        review_message = result.get("p5_review_message", "")

        _status_map = {
            "confirmed": "ok",
            "edited": "ok",
            "nl_revised": "ok",
            "edited_with_gate_warning": "warning",
            "nl_revised_with_gate_warning": "warning",
            "skipped_not_guided": "ok",
            "skipped_no_tool": "ok",
            "skipped_no_outline": "warning",
            "skipped_ask_error": "warning",
            "skipped_no_edit": "warning",
            "skipped_unknown_action": "warning",
        }

        yield {
            **result,
            "node": self.plan_name,
            "status": _status_map.get(review_status, "ok"),
            "message": review_message,
        }

    async def _read_outline(self, outline_path: str) -> str:
        if not outline_path:
            return ""
        if not self.has_tool("read_file"):
            logger.warning("[P5] read_file 工具不可用，无法读取 outline.md")
            return ""
        try:
            result = await self.call_tool("read_file", file_path=outline_path)
            return PptCommon.parse_tool_file_content(result)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P5] 读取 outline.md 失败: %s", e)
            return ""

    async def _write_outline(self, outline_path: str, content: str) -> None:
        if not self.has_tool("write_file"):
            logger.error("[P5] write_file 工具不可用，无法写入 outline.md")
            return
        try:
            await self.call_tool("write_file", path=outline_path, content=content)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P5] 写入 outline.md 失败: %s", e)

    async def _ask_user_review(
        self, outline_text: str, outline_path: str,
    ) -> dict[str, Any]:
        questions = [
            {
                "header": "PPT 大纲审阅",
                "question": "请审阅生成的 PPT 大纲，确认后将继续生成幻灯片",
                "multi_select": False,
                "preview": {
                    "title": "PPT 大纲审阅",
                    "text": outline_text,
                    "format": "markdown",
                    "editable": True,
                },
                "options": [
                    {"label": "确认大纲，继续生成", "description": "内容符合预期，继续执行", "id": _OUTLINE_CONFIRM_ID},
                    {"label": "需要修改", "description": "使用下方编辑后的正文", "id": _OUTLINE_USE_EDITED_ID},
                ],
                "outline_path": outline_path,
                "interaction_kind": "outline_review",
            }
        ]
        result = await self.call_tool("ask_user", questions=questions)
        if not isinstance(result, dict):
            return {"status": "error", "answers": []}
        return result

    def _parse_user_action(self, ask_result: dict[str, Any]) -> str:
        answers = ask_result.get("answers", [])
        if not answers:
            return ""
        first = answers[0]
        if not isinstance(first, dict):
            return ""
        selected = first.get("selected_options", [])
        if not selected:
            return ""
        return str(selected[0]).strip()

    def _extract_edited_text(self, ask_result: dict[str, Any], original: str) -> str:
        answers = ask_result.get("answers", [])
        if not answers:
            return ""
        first = answers[0]
        if not isinstance(first, dict):
            return ""
        edited = first.get("edited_text", "")
        if edited and edited.strip() and edited.strip() != original.strip():
            return edited.strip()
        return ""

    def _extract_user_nl_input(self, ask_result: dict[str, Any]) -> str:
        answers = ask_result.get("answers", [])
        if not answers:
            return ""
        first = answers[0]
        if not isinstance(first, dict):
            return ""
        text = first.get("text", "") or first.get("free_text", "") or first.get("other_text", "")
        return str(text).strip()

    async def _llm_revise_outline(self, original_outline: str, user_instruction: str) -> str:
        prompt = (
            "你是一个PPT大纲修订助手。请根据用户的修改意见修订以下PPT大纲。\n"
            "要求：\n"
            "1. 保持大纲的整体格式不变（# 大纲：、## 页面规划 等标题结构）\n"
            "2. 仅修改用户要求调整的部分\n"
            "3. 直接输出修订后的完整大纲，不要解释\n\n"
            f"原始大纲：\n{original_outline}\n\n"
            f"用户修改意见：\n{user_instruction}"
        )
        revised = await self.stream_llm_collect(
            prompt=prompt,
            system_prompt="你是PPT大纲修订专家，直接输出修订后的完整大纲Markdown",
        )
        return revised.strip() if revised else ""