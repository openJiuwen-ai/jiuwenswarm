"""Phase 4.4 — 演讲备注生成与注入（仅 need_speaker_notes=True 时执行）。

prod 契约：
1. 取语调规则（优先 tone-style skill，降级为内置默认）
2. cli notes extract-text 抽取每页可见纯文本
3. 按页并发 LLM 生成备注分片 speaker-notes-page-{N}.txt
4. 分片校验（缺失/空页重跑一次；接近 dispatch.md「首轮后按文件重试」）
5. 单进程 cli notes inject 写回 .pptx

best-effort：任何失败都不阻塞 PPTX 交付。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    cli_path,
    quote_path,
    run_bash,
)

logger = logging.getLogger(__name__)

_DEFAULT_TONE = "简洁干练"


@dataclass(frozen=True, slots=True)
class _SpeakerNotesContext:
    """生成和校验逐页演讲备注所需的共享上下文。"""

    pages_dir: str
    page_texts: dict[int, str]
    total_pages: int
    topic: str
    audience: str
    presentation_purpose: str
    tone_rules: str
    notes_requirements: str


class SpeakerNotesNode(PlanNode):
    """Phase 4.4 — 演讲备注生成与注入。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p11_speaker_notes",
            instruction=(
                "## Phase 4.4 演讲备注生成与注入\n"
                "仅 need_speaker_notes=True 时执行，否则跳过。\n"
                "prompt 注入 notes_requirements（与 tone_rules 正交）。\n"
                "best-effort：任何失败都不阻塞 PPTX 交付。\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        need = bool(inputs.get("need_speaker_notes"))
        if not need:
            logger.info("[P11] need_speaker_notes=False，跳过演讲备注")
            return {"speaker_notes_status": "skipped", "speaker_notes_message": "未触发演讲备注"}

        pptx_path = str(inputs.get("pptx_path") or "").strip()
        pages_dir = str(inputs.get("pages_dir") or "").strip()
        output_dir = str(inputs.get("output_dir") or "").strip()
        pptx_root = str(inputs.get("pptx_root") or "").strip()
        total_pages = int(inputs.get("total_pages") or 0)
        topic = str(inputs.get("topic") or "").strip()
        audience = str(inputs.get("audience") or "").strip()
        presentation_purpose = str(inputs.get("presentation_purpose") or "").strip()
        notes_requirements = str(inputs.get("notes_requirements") or "").strip()

        if not pptx_path or not pages_dir or not pptx_root:
            logger.warning("[P11] 缺少必要路径，跳过演讲备注: pptx=%s pages=%s root=%s",
                           bool(pptx_path), bool(pages_dir), bool(pptx_root))
            return {"speaker_notes_status": "skipped", "speaker_notes_message": "缺少必要路径"}

        if total_pages <= 0:
            page_count = int(inputs.get("page_count") or 0)
            total_pages = page_count + 2

        # 1. 取语调规则
        tone_rules = await self._get_tone_rules(inputs)

        # 2. cli notes extract-text 抽取每页可见纯文本（0908 强制 --out）
        page_texts = await self._extract_page_texts(
            pptx_path, pptx_root, output_dir=output_dir,
        )
        extract_ok = bool(page_texts)
        notes_context = _SpeakerNotesContext(
            pages_dir=pages_dir,
            page_texts=page_texts,
            total_pages=total_pages,
            topic=topic,
            audience=audience,
            presentation_purpose=presentation_purpose,
            tone_rules=tone_rules,
            notes_requirements=notes_requirements,
        )

        # 3. 按页并发生成备注分片
        await self._generate_notes_per_page(notes_context)

        # 4. 分片校验 + 缺失重跑
        await self._validate_and_retry(notes_context)

        # 5. 单进程注入
        inject_ok = await self._inject_notes(pptx_path, pages_dir, pptx_root)

        # notes.md §6：best-effort 不阻塞交付，但状态须如实（禁止 extract 失败仍报干净成功）
        if inject_ok and extract_ok:
            status = "ok"
            msg = "演讲备注已注入"
        elif inject_ok and not extract_ok:
            status = "partial"
            msg = (
                "演讲备注已注入，但页面文本抽取失败或为空，"
                "备注可能与页内容无关（不阻塞交付）"
            )
        else:
            status = "partial"
            msg = "演讲备注注入失败（不阻塞交付）"
        logger.info(
            "[P11] 演讲备注完成 status=%s extract_ok=%s inject_ok=%s notes_requirements=%s",
            status,
            extract_ok,
            inject_ok,
            bool(notes_requirements),
        )
        return {
            "speaker_notes_status": status,
            "speaker_notes_message": msg,
        }

    def _build_notes_prompt(self, context: _SpeakerNotesContext, page_num: int, page_type: str) -> str:
        page_text = context.page_texts.get(page_num, "")
        notes_req_line = ""
        if context.notes_requirements:
            notes_req_line = (
                f"用户备注规格要求（必须满足，与语调规则正交）："
                f"{context.notes_requirements}\n"
            )
        return (
            f"你是演讲备注撰写者。请为第 {page_num}/{context.total_pages} 页幻灯片生成口播备注。\n"
            f"页类型：{page_type}\n"
            f"页可见文本：{page_text[:2000]}\n"
            f"主题：{context.topic}\n"
            f"受众：{context.audience}\n"
            f"演讲目的：{context.presentation_purpose}\n"
            f"语调规则：{context.tone_rules}\n"
            f"{notes_req_line}"
            f"要求：生成纯文本口播备注，50-200字（若用户规格另有字数/结构要求以用户为准），"
            f"直接输出备注正文，不要解释。\n"
        )

    async def _get_tone_rules(self, inputs: dict[str, Any]) -> str:
        """取语调规则：优先 tone-style skill，降级为默认。"""
        # 尝试调用 tone-style skill
        if self.has_tool("Skill"):
            try:
                tone_req = str(inputs.get("tone_requirement") or "").strip()
                result = await self.call_tool("Skill", skill="tone-style", args=tone_req)
                if isinstance(result, str) and result.strip():
                    logger.info("[P11] tone-style skill 返回语调规则")
                    return result.strip()
                if isinstance(result, dict):
                    content = result.get("content") or result.get("result") or ""
                    if isinstance(content, str) and content.strip():
                        logger.info("[P11] tone-style skill 返回语调规则")
                        return content.strip()
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P11] tone-style skill 不可用，降级: %s", e)

        # 降级：用内置默认语调指引
        audience = str(inputs.get("audience") or "").strip()
        tone_hint = str(inputs.get("tone_requirement") or "").strip()
        if not tone_hint:
            tone_hint = _DEFAULT_TONE
        rule = f"语调：{tone_hint}。受众：{audience or '一般受众'}。"
        logger.info("[P11] 使用降级语调规则: %s", rule)
        return rule

    async def _extract_page_texts(
        self,
        pptx_path: str,
        pptx_root: str,
        *,
        output_dir: str,
    ) -> dict[int, str]:
        """cli notes extract-text 抽取每页可见纯文本。

        0908 CLI 强制 ``--out <path>``，结果写入 JSON 后再读；不得依赖 stdout 当正文。
        """
        if not output_dir:
            logger.warning("[P11] notes extract-text 缺少 output_dir，跳过抽取")
            return {}
        out_json = str(Path(output_dir) / "notes-text.json")
        try:
            cmd = (
                f"{cli_path('notes', pptx_root)} extract-text "
                f"--pptx {quote_path(pptx_path)} --out {quote_path(out_json)}"
            )
            result = await run_bash(
                self, cmd, timeout_seconds=60, required=False, workdir=pptx_root,
            )
            if result.exit_code != 0:
                logger.warning(
                    "[P11] notes extract-text 失败 exit=%d: %s",
                    result.exit_code,
                    (result.stderr or result.stdout or "")[:300],
                )
                return {}
            raw = await PptCommon.read_file(
                self, out_json, label="notes-text.json",
            )
            if not raw or not str(raw).strip():
                logger.warning("[P11] notes-text.json 为空 path=%s", out_json)
                return {}
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[P11] notes-text.json 非 JSON path=%s", out_json)
                return {}
            if not isinstance(data, dict):
                logger.warning("[P11] notes-text.json 根类型非 object")
                return {}
            return {int(k): str(v) for k, v in data.items()}
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P11] notes extract-text 异常: %s", e)
            return {}

    async def _generate_notes_per_page(
        self,
        context: _SpeakerNotesContext,
    ) -> None:
        """按页并发生成备注分片。"""
        async def _gen_one(page_num: int) -> None:
            if page_num == 1:
                page_type = "cover"
            elif page_num >= context.total_pages:
                page_type = "ending"
            else:
                page_type = "content"

            prompt = self._build_notes_prompt(context, page_num, page_type)
            try:
                notes = await self.stream_llm_collect(
                    prompt=prompt,
                    system_prompt="你是演讲备注撰写专家，直接输出口播备注正文。",
                )
                if notes and notes.strip():
                    out_path = Path(context.pages_dir) / f"speaker-notes-page-{page_num}.txt"
                    await PptCommon.write_file(self, out_path, notes.strip())
                    logger.debug("[P11] 生成备注分片 page=%d", page_num)
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P11] 生成备注分片失败 page=%d: %s", page_num, e)

        tasks = [_gen_one(i) for i in range(1, context.total_pages + 1)]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _validate_and_retry(
        self,
        context: _SpeakerNotesContext,
    ) -> None:
        """分片校验：缺失/空页重跑一次。"""
        for page_num in range(1, context.total_pages + 1):
            out_path = Path(context.pages_dir) / f"speaker-notes-page-{page_num}.txt"
            content = await PptCommon.read_file(self, str(out_path), label=f"notes-page-{page_num}")
            if content and content.strip():
                continue
            logger.warning("[P11] 备注分片缺失 page=%d，重跑", page_num)
            if page_num == 1:
                page_type = "cover"
            elif page_num >= context.total_pages:
                page_type = "ending"
            else:
                page_type = "content"
            prompt = self._build_notes_prompt(context, page_num, page_type)
            try:
                notes = await self.stream_llm_collect(
                    prompt=prompt,
                    system_prompt="你是演讲备注撰写专家，直接输出口播备注正文。",
                )
                if notes and notes.strip():
                    await PptCommon.write_file(self, out_path, notes.strip())
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P11] 重跑备注分片仍失败 page=%d: %s", page_num, e)

    async def _inject_notes(self, pptx_path: str, pages_dir: str, pptx_root: str) -> bool:
        """单进程 cli notes inject 写回 .pptx。"""
        try:
            cmd = (
                f"{cli_path('notes', pptx_root)} inject "
                f"--pptx {quote_path(pptx_path)} "
                f"--notes-dir {quote_path(pages_dir)}"
            )
            result = await run_bash(self, cmd, timeout_seconds=60, required=False, workdir=pptx_root)
            if result.exit_code != 0:
                logger.warning("[P11] notes inject 失败 exit=%d: %s",
                               result.exit_code, (result.stderr or "")[:300])
                return False
            logger.info("[P11] notes inject 成功")
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P11] notes inject 异常: %s", e)
            return False

    async def _execute_stream(self, inputs: dict[str, Any]):
        result = await self._execute(inputs)
        status_map = {"ok": "ok", "partial": "warning", "skipped": "ok"}
        yield {
            **result,
            "node": self.plan_name,
            "status": status_map.get(result.get("speaker_notes_status", ""), "ok"),
            "message": result.get("speaker_notes_message", ""),
        }
