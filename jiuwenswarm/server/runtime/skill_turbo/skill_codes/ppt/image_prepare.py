from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashExecError,
    cli_path,
    quote_path,
    run_bash,
)

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_INTERMEDIATE_FILES = (
    "temp_image_map.json",
    "local_image_info.json",
    "page_image_info.json",
    "ai_plan.json",
)

_VQA_QUESTION = (
    "请描述这张图片的内容，包括场景、物体、人物、颜色等关键信息。"
)

_STEP0_SYSTEM = "你是 PPT 图片需求分析助手。根据大纲和研究内容，逐页分析图片需求，输出 JSON。"

_A2_SYSTEM = "你是图片实体提取助手。从图片描述中提取专有名词（地名、建筑名、品牌名、产品名等），输出 JSON。"

_A3_SYSTEM = "你是图片语义匹配助手。根据页面需求和图片描述，智能匹配图片，输出 JSON。"


def _build_step0_prompt(outline: str, research: str) -> str:
    return (
        "根据以下 PPT 大纲和研究内容，逐页分析图片需求。\n\n"
        "## 大纲\n"
        f"{outline}\n\n"
        "## 研究内容\n"
        f"{research or '（无研究内容）'}\n\n"
        "## 规则\n"
        "1. 封面/章节页 → needCount=1（背景图）\n"
        "2. 数据/图表页 → needCount=0（不需要图片）\n"
        "3. 案例/产品页 → 从要点中提取具体实体，实体数量=图片数量\n"
        "4. 关键词必须是专有名词，禁止泛词（如'美食''景点''技术'）\n"
        "5. 跳过不需要图片的页\n"
        "6. imageSize 必须大于最低分辨率：cover ≥ 1920×1080，content ≥ 800×600\n"
        "   格式为 \"宽*高\"，如 \"1920*1080\" 或 \"1024*1024\"\n"
        "7. 每个需要图片的页必须给出与 needCount 等长的 imageSlots，\n"
        "   每槽至少含 slotId/prompt/keywords/targetAspectRatio/fit/subjectPosition\n"
        "8. 槽位比例先于生图确定并与计划版式一致（横条/竖栏/卡片/背景各用对应比例，\n"
        "   不得全部默认 16:9）；未指定时封面背景回退 16:9、普通内容图回退 4:3\n"
        "9. fit：场景/背景/氛围配图默认 cover；产品/人物/界面截图等必须完整展示用 contain\n"
        "10. 大纲或研究中出现的用户画面要求（如'不出现人像''包含风电场景'）\n"
        "    必须写进对应槽位的 prompt\n\n"
        '输出 JSON：{"pages":[{"page":1,"type":"cover","title":"...","keywords":["实体1"],'
        '"needCount":1,"visualStrategy":"大图背景","imageSize":"1920*1080",'
        '"imageSlots":[{"slotId":"image-1","prompt":"...","keywords":["实体1"],'
        '"targetAspectRatio":"16:9","fit":"cover","subjectPosition":"center"}]}],'
        '"totalNeed":N}'
    )


def _build_a2_prompt(images: list[dict], topic: str = "") -> str:
    lines = []
    for i, img in enumerate(images):
        lines.append(f"图片{i + 1}：路径={img['path']}，描述={img['description']}")
    topic_hint = f"\n\n## 用户主题上下文\n{topic}" if topic else ""
    return (
        "从以下图片描述中提取专有名词作为实体。\n\n"
        "## 图片列表\n"
        + "\n".join(lines)
        + topic_hint
        + "\n\n"
        "实体类型：人名、地名、机构名、品牌名、产品名、事件名、作品名等。\n"
        "排除泛词（类别词、属性词、抽象词、动词）。\n"
        "如果图片描述中的人物/场景与主题上下文相关，请结合主题推断实体。\n"
        '输出 JSON：{"images":[{"path":"...","description":"...","entities":["实体1"]]}'
    )


def _build_a3_prompt(page_info: str, local_info: str) -> str:
    return (
        "根据页面需求和图片描述，智能匹配图片。\n\n"
        "## 页面需求\n"
        f"{page_info}\n\n"
        "## 本地图片\n"
        f"{local_info}\n\n"
        "## 规则\n"
        "1. 匹配分数 ≥ 70 才接受\n"
        "2. 每页不超过 needCount 张图片\n"
        "3. 不能重复使用同一张图片\n"
        "4. 优先满足高优先级页面\n"
        '输出 JSON：{"1":[{"originalPath":"...","description":"...","entities":[],'
        '"score":85,"matchedKeywords":["关键词"],"type":"local"}]}'
        "（key 是页码字符串，value 是图片数组；无匹配的页不要包含）"
    )


_FIT_PROMPT_SUFFIX = {
    "cover": "full-bleed, edge-to-edge, no border, no black bars, no letterboxing, crop-safe composition",
    "contain": "complete subject visible, clean natural background, no frame, no black bars",
}


@dataclass
class _AiPromptSlot:
    """单槽位生图 prompt 输入（G.FNM.03：相关参数具名封装）。

    keywords/usage/fit/slot_prompt 均来自同一 imageSlots 条目，
    与页级 topic/style_id 分离避免 6 参数长签名。
    """

    keywords: list
    usage: str
    fit: str = "cover"
    slot_prompt: str = ""


def _build_ai_prompt(slot: _AiPromptSlot, topic: str, style_id: str) -> str:
    kw = " ".join(slot.keywords) if slot.keywords else topic
    base = f"{slot.slot_prompt}，{kw}" if slot.slot_prompt else kw
    style_hint = f"，风格：{style_id}" if style_id else ""
    fit_suffix = _FIT_PROMPT_SUFFIX.get(slot.fit, _FIT_PROMPT_SUFFIX["cover"])
    scene = "background" if slot.usage == "cover" else "illustration"
    return f"{base}，{topic}，{scene} {fit_suffix}，no text{style_hint}"


def _is_positive_number(value: Any) -> bool:
    """数值且 > 0（bool 是 int 子类，显式排除）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _resolve_slot_size(slot: dict, page_size: str, usage: str) -> str:
    """槽位优先：targetWidth×targetHeight → 页级 imageSize → usage 默认值。"""
    w, h = slot.get("targetWidth"), slot.get("targetHeight")
    if _is_positive_number(w) and _is_positive_number(h):
        return f"{int(w)}*{int(h)}"
    if page_size:
        return page_size
    return "1920*1080" if usage == "cover" else "1024*1024"


def _to_absolute_win_path(path: str) -> str:
    """git-bash 风格 /c/... 转 Windows 绝对路径，其余原样返回。"""
    m = re.match(r"^/([a-zA-Z])/(.+)$", path)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"
    return path


def _extract_tool_text(result: Any, keys: tuple[str, ...]) -> str:
    """从 VQA/OCR 工具返回值中提取文本，兼容 dict/str/object 格式。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for k in keys:
            v = result.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        data = result.get("data")
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""
    if hasattr(result, "data"):
        data = result.data
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        if isinstance(data, str) and data.strip():
            return data.strip()
    return str(result).strip()


def _extract_vqa_answer(result: Any) -> str:
    return _extract_tool_text(result, ("answer", "text", "content"))


def _extract_ocr_text(result: Any) -> str:
    text = _extract_tool_text(result, ("ocr_text", "text", "content"))
    if text and text.lower() == "no text found.":
        return ""
    return text


def _parse_image_paths(text: str) -> list[str]:
    paths: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- ") and len(line) > 2:
            paths.append(line[2:].strip().strip('"'))
    if not paths:
        logger.warning("[P6.5] _parse_image_paths 未解析到图片路径，raw=%s", text[:200])
    return paths


class ImagePrepareNode(PlanNode):
    """P6.5 图片准备节点（Diana）：按 image_sources 级联分配图片，产出 image_map.json。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p6_5_image_prepare",
            instruction=(
                "## P6.5 图片准备（Diana）\n"
                "\n"
                "### 职责\n"
                "按 `{image_sources}` 有序列表驱动的级联分配图片，产出 `image_map.json`。\n"
                "\n"
                "### 前置条件\n"
                "- `output_dir` / `pptx_root` / `outline_path` / `research_paths` 已由上游写入\n"
                "- `image_paths` / `image_sources` 由 P1/P2 写入\n"
                "\n"
                "### 输入\n"
                "- `image_paths`: 本地图片路径列表（local 源）\n"
                "- `image_sources`: 来源有序列表，默认 [\"local\"]\n"
                "- `outline_path` / `research_paths`: PPT 大纲与研究产物\n"
                "- `total_pages` / `topic` / `style_id`: 辅助参数\n"
                "\n"
                "### 输出\n"
                "```json\n"
                '{"image_map_path": "{output_dir}/image_map.json"}\n'
                "}\n"
                "失败/跳过时 `image_map_path` 为空字符串，下游 P8 走无图布局。\n"
                "\n"
                "### 门控\n"
                "- local 可用 ← image_paths 非空\n"
                "- network 恒不可用（禁用）\n"
                "- ai 可用 ← has_tool(generate_image)\n"
                "- 全不可用 → 跳过\n"
                "\n"
                "### 失败兜底\n"
                "- 整流程最多重试 2 次，仍失败不阻塞 pipeline\n"
                "- 任何工具缺失自动降级（文件名降级 / 跳过 ai 源）\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        output_dir = str(inputs.get("output_dir", "")).strip()
        pptx_root = str(inputs.get("pptx_root", "")).strip()
        outline_path = str(inputs.get("outline_path", "")).strip()
        research_paths = inputs.get("research_paths", {})
        image_paths = inputs.get("image_paths", [])
        image_sources = inputs.get("image_sources", ["local"])
        total_pages = inputs.get("total_pages") or inputs.get("page_count") or 0
        topic = str(inputs.get("topic", "")).strip()
        style_id = str(inputs.get("style_id", "")).strip()

        if not output_dir:
            logger.error("[P6.5] output_dir 为空，跳过图片准备")
            return {"image_map_path": ""}

        # 生图能力检测：读 P2 写的 imagegen_status.json
        ai_supported = await self._read_imagegen_status(self, output_dir)
        if ai_supported and "ai" not in image_sources:
            image_sources.append("ai")
            logger.info("[P6.5] imagegen_status.supported=true，已添加 ai 源")

        # 门控
        local_ok = bool(image_paths)
        ai_ok = "ai" in image_sources and ai_supported
        if not local_ok and not ai_ok:
            logger.info("[P6.5] 无可用图片来源（local=%s, ai=%s），跳过", local_ok, ai_ok)
            return {"image_map_path": ""}

        image_map_path = f"{output_dir}/image_map.json"
        for attempt in range(_MAX_RETRIES):
            try:
                ok = await self._step0_page_needs(output_dir, outline_path, research_paths)
                if not ok:
                    await self._cleanup(output_dir)
                    continue

                await self._step_a_local(output_dir, image_paths, topic)

                if ai_ok:
                    await self._ai_source(output_dir, pptx_root, image_sources, topic, style_id)

                ok = await self._step_d_finalize(output_dir, pptx_root, total_pages)
                if not ok:
                    await self._cleanup(output_dir)
                    continue

                if self._validate(output_dir):
                    logger.info("[P6.5] 图片准备成功: %s", image_map_path)
                    return {"image_map_path": image_map_path}

                logger.warning("[P6.5] image_map.json 校验失败 (attempt %d)", attempt + 1)
                await self._cleanup(output_dir)
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P6.5] 图片准备异常 (attempt %d): %s", attempt + 1, e)
                await self._cleanup(output_dir)

        logger.warning("[P6.5] 图片准备最终失败，降级为无图布局")
        return {"image_map_path": ""}

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"node": self.plan_name, "status": "progress", "message": "正在准备图片..."}
        result = await self._execute(inputs)
        path = result.get("image_map_path", "")
        yield {
            **result,
            "node": self.plan_name,
            "status": "ok",
            "message": f"图片准备完成：{path}" if path else "图片准备跳过（无可用来源）",
        }

    # ── Step 0: 页面需求分析 ──────────────────────────────

    async def _step0_page_needs(
        self, output_dir: str, outline_path: str, research_paths: Any,
    ) -> bool:
        outline = await PptCommon.read_file(
            self, outline_path, label="outline", max_chars=8000,
        )
        research = await self._collect_research(research_paths)

        if not outline and not research:
            logger.warning("[P6.5] outline 和 research 均为空")
            await self._write_json(
                f"{output_dir}/page_image_info.json",
                {"pages": [], "totalNeed": 0},
            )
            return True

        prompt = _build_step0_prompt(outline, research)
        try:
            resp = await self.stream_llm_collect(prompt=prompt, system_prompt=_STEP0_SYSTEM)
            data = PptCommon.parse_json_payload(resp)
            if not isinstance(data, dict) or "pages" not in data:
                logger.warning("[P6.5] Step 0 LLM 返回格式错误")
                return False
            await self._write_json(f"{output_dir}/page_image_info.json", data)
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.5] Step 0 LLM 失败: %s", e)
            return False

    async def _collect_research(self, research_paths: Any) -> str:
        if not research_paths:
            return ""
        parts: list[str] = []
        if isinstance(research_paths, dict):
            keys = sorted(research_paths.keys())
        else:
            keys = list(range(len(research_paths)))
        for k in keys:
            path = research_paths[k]
            text = await PptCommon.read_file(
                self, path, label=f"research-P{k}", max_chars=2000,
            )
            if text:
                parts.append(text)
        return ("\n\n---\n\n".join(parts))[:12000]

    # ── Step A: 本地图片处理 ──────────────────────────────

    async def _step_a_local(self, output_dir: str, image_paths: list[str], topic: str = "") -> None:
        if not image_paths:
            return

        images = await self._describe_images(image_paths)

        has_real_desc = any(
            img["description"] and img["description"] != Path(img["path"]).stem
            for img in images
        )
        if has_real_desc:
            images = await self._extract_entities(images, topic)

        local_info = {"images": images, "total": len(images)}
        await self._write_json(f"{output_dir}/local_image_info.json", local_info)

        page_info_raw = await PptCommon.read_file(
            self, f"{output_dir}/page_image_info.json", label="page_image_info",
        )
        if not page_info_raw:
            return

        await self._match_images(output_dir, page_info_raw, local_info)

    async def _describe_images(self, image_paths: list[str]) -> list[dict]:
        images: list[dict] = []
        has_vqa = self.has_tool("visual_question_answering")
        has_ocr = self.has_tool("image_ocr")

        for path in image_paths:
            desc = ""
            if has_vqa:
                try:
                    raw = await self.call_tool(
                        "visual_question_answering",
                        image_path_or_url=path,
                        question=_VQA_QUESTION,
                    )
                    desc = _extract_vqa_answer(raw)
                except Exception as e:
                    if isinstance(e, AbortError):
                        raise
                    logger.warning("[P6.5] VQA 失败 %s: %s", path, e)
            if not desc and has_ocr:
                try:
                    raw = await self.call_tool("image_ocr", image_path_or_url=path)
                    desc = _extract_ocr_text(raw)
                except Exception as e:
                    if isinstance(e, AbortError):
                        raise
                    logger.warning("[P6.5] OCR 失败 %s: %s", path, e)
            if not desc:
                desc = Path(path).stem
            images.append({"path": path, "description": desc, "entities": []})
        return images

    async def _extract_entities(self, images: list[dict], topic: str = "") -> list[dict]:
        prompt = _build_a2_prompt(images, topic)
        try:
            resp = await self.stream_llm_collect(prompt=prompt, system_prompt=_A2_SYSTEM)
            data = PptCommon.parse_json_payload(resp)
            if isinstance(data, dict) and isinstance(data.get("images"), list):
                result = data["images"]
                for i, img in enumerate(images):
                    if i < len(result) and isinstance(result[i], dict):
                        entities = result[i].get("entities", [])
                        if isinstance(entities, list):
                            img["entities"] = entities
                        desc = result[i].get("description")
                        if isinstance(desc, str) and desc.strip():
                            img["description"] = desc
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.5] 实体提取 LLM 失败: %s", e)
        return images

    async def _match_images(
        self, output_dir: str, page_info: str, local_info: dict,
    ) -> None:
        prompt = _build_a3_prompt(page_info, json.dumps(local_info, ensure_ascii=False))
        try:
            resp = await self.stream_llm_collect(prompt=prompt, system_prompt=_A3_SYSTEM)
            data = PptCommon.parse_json_payload(resp)
            if isinstance(data, dict):
                for key, imgs in data.items():
                    if key == "metadata" or not isinstance(imgs, list):
                        continue
                    for img in imgs:
                        if isinstance(img, dict):
                            img.setdefault("type", "local")
                            img.setdefault("score", 0.5)
                await self._write_json(f"{output_dir}/temp_image_map.json", data)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.5] 本地匹配 LLM 失败: %s", e)

    # ── AI 源 ──────────────────────────────────────────────

    async def _ai_source(
        self, output_dir: str, pptx_root: str,
        image_sources: list, topic: str, style_id: str,
    ) -> None:
        # 最终确认：generate_image 工具是否可用
        if not self.has_tool("generate_image"):
            logger.warning("[P6.5] generate_image 工具不可用，跳过 ai 源")
            return

        # 与 skill Stage 5 对齐：用 ai-plan.js 生成生图计划
        # （能力门控读 imagegen_status.json + 成本上限 + 槽位缺口计算）
        plan_script = Path(pptx_root) / "image-insert" / "scripts" / "ai-plan.js"
        if not plan_script.is_file():
            logger.warning("[P6.5] ai-plan.js 不存在: %s，跳过 ai 源", plan_script)
            return
        sources_csv = ",".join(image_sources)
        plan_cmd = (
            f"node {quote_path(str(plan_script))} "
            f"{quote_path(output_dir)} {sources_csv}"
        )
        try:
            plan_result = await run_bash(self, plan_cmd, workdir=pptx_root, required=False)
            if plan_result.exit_code != 0:
                logger.warning(
                    "[P6.5] ai-plan.js exit=%d: %s",
                    plan_result.exit_code, plan_result.stderr or plan_result.stdout or "",
                )
                return
            logger.info("[P6.5] ai-plan.js: %s", (plan_result.stdout or "")[:300])
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.5] ai-plan.js 失败，降级跳过 ai 源: %s", e)
            return

        ai_plan_raw = await PptCommon.read_file(
            self, f"{output_dir}/ai_plan.json", label="ai_plan",
        )
        if not ai_plan_raw:
            return
        ai_plan = PptCommon.parse_json_payload(ai_plan_raw)
        if not isinstance(ai_plan, list) or not ai_plan:
            return

        try:
            stage_cmd_prefix = cli_path("stage-ai-image", pptx_root)
        except BashExecError as e:
            logger.warning("[P6.5] 定位 cli.js 失败，跳过 ai 源: %s", e)
            return

        temp_map = await self._read_temp_map(output_dir)

        # 从 page_image_info.json 读取 LLM 动态返回的图片分辨率
        page_size_map: dict[str, str] = {}
        page_info_raw = await PptCommon.read_file(
            self, f"{output_dir}/page_image_info.json", label="page_image_info",
        )
        if page_info_raw:
            page_info = PptCommon.parse_json_payload(page_info_raw)
            if isinstance(page_info, dict):
                for p in page_info.get("pages", []):
                    if isinstance(p, dict) and p.get("page"):
                        page_size_map[str(p["page"])] = p.get("imageSize", "")

        for item in ai_plan:
            if not isinstance(item, dict):
                continue
            page = item.get("page")
            keywords = item.get("keywords", [])
            usage = item.get("usage", "content")
            if not page:
                continue
            # 逐槽生成（skill 契约：一槽一图）；无 slots 的旧计划按 count 合成
            raw_slots = item.get("slots")
            slots = raw_slots if isinstance(raw_slots, list) else []
            if not slots:
                count = int(item.get("count", 0) or 0)
                slots = [{"slotId": f"image-{i + 1}"} for i in range(count)]
            for i, slot in enumerate(slots):
                if not isinstance(slot, dict):
                    continue
                slot_id = str(slot.get("slotId") or f"image-{i + 1}")
                slot_keywords = slot.get("keywords") or keywords
                prompt_slot = _AiPromptSlot(
                    keywords=slot_keywords,
                    usage=usage,
                    fit=str(slot.get("fit") or "cover"),
                    slot_prompt=str(slot.get("prompt") or "").strip(),
                )
                prompt = _build_ai_prompt(prompt_slot, topic, style_id)
                size = _resolve_slot_size(slot, page_size_map.get(str(page), ""), usage)
                try:
                    raw = await self.call_tool(
                        "generate_image", inputs={"prompt": prompt, "size": size, "n": 1},
                    )
                    paths = _parse_image_paths(str(raw))
                    if not paths:
                        continue
                    src = _to_absolute_win_path(paths[0])
                    if not Path(src).is_absolute():
                        logger.warning(
                            "[P6.5] 生图返回非绝对路径，跳过 page=%s slot=%s: %s",
                            page, slot_id, src,
                        )
                        continue
                    # 与 skill 对齐：stage-ai-image 精确复制（字节数 + SHA-256 校验），禁止裸 cp
                    copy_cmd = (
                        f"{stage_cmd_prefix} "
                        f"--source {quote_path(src)} "
                        f"--output-dir {quote_path(output_dir)} "
                        f"--page {page} --index {i + 1}"
                    )
                    copy_result = await run_bash(
                        self, copy_cmd, workdir=pptx_root, required=False,
                    )
                    if copy_result.exit_code != 0:
                        logger.warning(
                            "[P6.5] stage-ai-image 失败 page=%s slot=%s exit=%d: %s",
                            page, slot_id, copy_result.exit_code,
                            copy_result.stderr or copy_result.stdout or "",
                        )
                        continue
                    payload = PptCommon.parse_json_payload(copy_result.stdout or "")
                    # 兜底：bash 工具返回纯 JSON 文本时 parse_bash_payload 会把
                    # stage-ai-image 的输出对象当命令 payload 解析（stdout 为空），
                    # 此时从 raw 原始文本恢复
                    if not isinstance(payload, dict):
                        payload = PptCommon.parse_json_payload(copy_result.raw or "")
                    if not isinstance(payload, dict) or payload.get("verified") is not True:
                        logger.warning(
                            "[P6.5] stage-ai-image 校验未通过 page=%s slot=%s", page, slot_id,
                        )
                        continue
                    dest = str(payload.get("path") or "")
                    if not dest:
                        continue
                    page_key = str(page)
                    if page_key not in temp_map:
                        temp_map[page_key] = []
                    temp_map[page_key].append({
                        "path": dest,
                        "type": "ai",
                        "description": " ".join(slot_keywords) or topic,
                        "entities": slot_keywords,
                        "matchedKeywords": slot_keywords,
                        "score": 0.9,
                        "usage": usage,
                        "slotId": slot_id,
                        "targetAspectRatio": slot.get("targetAspectRatio")
                        or ("16:9" if usage == "cover" else "4:3"),
                        "targetWidth": slot.get("targetWidth"),
                        "targetHeight": slot.get("targetHeight"),
                        "fit": prompt_slot.fit,
                        "subjectPosition": slot.get("subjectPosition") or "center",
                    })
                except Exception as e:
                    if isinstance(e, AbortError):
                        raise
                    logger.warning("[P6.5] AI 生图失败 page=%s slot=%s: %s", page, slot_id, e)

        await self._write_json(f"{output_dir}/temp_image_map.json", temp_map)

    async def _read_temp_map(self, output_dir: str) -> dict:
        raw = await PptCommon.read_file(
            self, f"{output_dir}/temp_image_map.json", label="temp_image_map",
        )
        if not raw:
            return {}
        data = PptCommon.parse_json_payload(raw)
        if isinstance(data, dict):
            return data.get("pageImageMap", data)
        return {}

    # ── Step D: 汇总 ───────────────────────────────────────

    async def _step_d_finalize(
        self, output_dir: str, pptx_root: str, total_pages: int,
    ) -> bool:
        script = Path(pptx_root) / "image-insert" / "scripts" / "stepD-finalize.js"
        if not script.is_file():
            logger.warning("[P6.5] stepD-finalize.js 不存在: %s", script)
            return False

        cmd = f"node {quote_path(str(script))} {quote_path(output_dir)} {total_pages}"
        try:
            result = await run_bash(self, cmd, workdir=pptx_root, required=False)
            if result.exit_code != 0:
                logger.warning(
                    "[P6.5] stepD-finalize.js exit=%d: %s",
                    result.exit_code, result.stderr or result.stdout or "",
                )
                return False
            logger.info("[P6.5] stepD-finalize.js: %s", (result.stdout or "")[:300])
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.5] stepD-finalize.js 失败: %s", e)
            return False

    # ── 校验与清理 ─────────────────────────────────────────

    def _validate(self, output_dir: str) -> bool:
        path = Path(output_dir) / "image_map.json"
        return path.is_file()

    @staticmethod
    async def _read_imagegen_status(node: "ImagePrepareNode", output_dir: str) -> bool:
        """读 P2 写的 imagegen_status.json，返回 supported 字段。"""
        status_path = f"{output_dir}/imagegen_status.json"
        raw = await PptCommon.read_file(node, status_path, label="imagegen_status")
        if not raw:
            return False
        try:
            data = json.loads(raw)
            return bool(data.get("supported", False))
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.5] 解析 imagegen_status.json 失败: %s", e)
            return False

    async def _cleanup(self, output_dir: str) -> None:
        targets = [Path(output_dir) / f for f in _INTERMEDIATE_FILES]
        paths_str = " ".join(quote_path(str(t)) for t in targets if t.is_file())
        if not paths_str:
            return
        try:
            await run_bash(self, f"rm -f {paths_str}", required=False, workdir=output_dir)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            pass

    async def _write_json(self, file_path: str, data: Any) -> None:
        try:
            await PptCommon.write_file(self, file_path, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.5] 写 %s 失败: %s", file_path, e)
