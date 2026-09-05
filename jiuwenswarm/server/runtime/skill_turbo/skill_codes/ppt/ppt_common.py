from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError

_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_CAT_N_PREFIX_RE = re.compile(r"^[ \t]*\d+[ \t]", re.MULTILINE)
_OUTLINE_PAGE_HEADING_RE = re.compile(r"^### P(\d+):", re.MULTILINE)
_REQUIRED_SECTION_PAGE_TYPES = frozenset({"cover", "agenda", "content", "ending"})

logger = logging.getLogger(__name__)

# ──────────────────────── 节点显示名映射 ────────────────────────
# 将内部 plan_name（如 p0_pipeline_init）映射为界面上展示的中文名称。
# 排序遵循 ppt_gen_root 节点 sub_plans 的执行顺序（Stage 1 → Stage N）。
# 仅影响前端展示，不改变内部 plan_name 标识。
NODE_DISPLAY_NAMES: dict[str, str] = {
    "p0_pipeline_init": "Stage 1: 流水线初始化",
    "p1_intent_classify": "Stage 2: 意图分类",
    "p3_document_parse": "Stage 3: 文档解析",
    "p2_requirement_collect": "Stage 4: 需求收集",
    "p3_5_template_context": "Stage 5: 模板上下文预处理",
    "p4_content_plan": "Stage 6: 内容策划",
    "p5_outline_review": "Stage 7: 大纲审阅",
    "p6_deep_research": "Stage 8: 深度研究",
    "p7_style_prepare": "Stage 9: 风格准备",
    "p6_5_image_prepare": "Stage 10: 图片准备",
    "p8_ppt_page_gen": "Stage 11: 幻灯片生成",
    "p9_ppt_export": "Stage 12: PPTX导出",
    "p11_speaker_notes": "Stage 13: 演讲备注",
    "p10_delivery": "Stage 14: 交付",
    "ppt_gen_root": "PPT生成",
}


def _strip_line_numbers(text: str) -> str:
    return _CAT_N_PREFIX_RE.sub("", text)


class PptCommon:
    """PPT skill_codes 公共工具：流水线 inputs 解析与 LLM JSON 提取。"""

    TEXT_SOURCE_KEYS = ("task", "user_request", "user_message", "query")
    QUERY_PREFIXES = (
        "你收到一条消息：\n",
        "You receive a new message:\n",
    )
    JSON_FENCE_PATTERN = _JSON_FENCE_PATTERN

    @classmethod
    def extract_plain_user_text(cls, raw: str) -> str:
        """从 build_user_prompt 包装或裸文本中提取用户原文 content。"""
        text = raw.strip()
        if not text:
            return ""

        for prefix in cls.QUERY_PREFIXES:
            if not text.startswith(prefix):
                continue
            json_part = text[len(prefix):]
            try:
                payload = json.loads(json_part)
            except json.JSONDecodeError:
                break
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
            break

        brace_index = text.find("{")
        if brace_index >= 0:
            try:
                payload = json.loads(text[brace_index:])
            except json.JSONDecodeError:
                return text
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

        return text

    @classmethod
    def collect_user_text(cls, inputs: dict[str, Any]) -> str:
        """合并 task / user_request / user_message / query 中的用户可见原文。"""
        parts: list[str] = []
        seen: set[str] = set()
        for key in cls.TEXT_SOURCE_KEYS:
            value = inputs.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            normalized = cls.extract_plain_user_text(value)
            if not normalized:
                continue
            dedupe_key = normalized.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            parts.append(normalized)
        return "\n".join(parts)

    @staticmethod
    def normalize_required_sections(value: Any) -> list[dict[str, str]]:
        """规范化模型提取的用户指定页面清单，丢弃不完整或未知页型。"""
        if not isinstance(value, list):
            return []
        sections: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            page_type = str(item.get("page_type") or "").strip().lower()
            if not title or page_type not in _REQUIRED_SECTION_PAGE_TYPES:
                continue
            sections.append({"title": title, "page_type": page_type})
        return sections

    @classmethod
    def resolve_required_section_budget(cls, inputs: dict[str, Any]) -> None:
        """按用户明确指定的页面清单统一解析页数冲突。

        清单优先于普通总页数要求；只把 page_type=content 计入内部
        page_count，cover/ending 由固定首尾页承载，agenda 走结构页。
        """
        sections = cls.normalize_required_sections(inputs.get("required_sections"))
        if not sections:
            inputs.pop("required_sections", None)
            return
        inputs["required_sections"] = sections

        content_count = sum(
            1 for section in sections if section["page_type"] == "content"
        )
        has_agenda = any(
            section["page_type"] == "agenda" for section in sections
        )
        agenda_item_count = sum(
            1
            for section in sections
            if section["page_type"] in {"content", "ending"}
        )

        try:
            current_page_count = int(inputs.get("page_count") or 0)
        except (TypeError, ValueError):
            current_page_count = 0
        resolved_page_count = max(current_page_count, content_count)
        if resolved_page_count > 0:
            inputs["page_count"] = resolved_page_count

        if has_agenda:
            structural_request = str(
                inputs.get("structural_page_request") or "none"
            ).strip().lower()
            if structural_request in {"", "none"}:
                inputs["structural_page_request"] = "agenda"
                inputs["structural_page_count"] = 1

        structural_count = inputs.get("structural_page_count")
        if not isinstance(structural_count, int) or structural_count <= 0:
            structural_count = (
                1
                if str(inputs.get("structural_page_request") or "none").lower()
                != "none"
                else 0
            )
        resolved_total = resolved_page_count + 2 + structural_count
        requested_total = inputs.get("requested_total_pages")
        try:
            requested_total_int = (
                int(requested_total) if requested_total is not None else None
            )
        except (TypeError, ValueError):
            requested_total_int = None

        inputs["required_agenda_item_count"] = agenda_item_count
        inputs["resolved_total_pages"] = resolved_total
        inputs["page_count_resolution"] = (
            "required_sections_override"
            if requested_total_int is not None
            and resolved_total > requested_total_int
            else "required_sections_fit"
        )
        logger.info(
            "[PptCommon] required sections resolved requested_total=%s "
            "content_page_count=%d resolved_total=%d agenda_items=%d resolution=%s",
            requested_total_int,
            resolved_page_count,
            resolved_total,
            agenda_item_count,
            inputs["page_count_resolution"],
        )

    @classmethod
    def parse_json_payload(cls, raw: str) -> Any:
        """解析 LLM 返回的 JSON（支持 markdown fence 与正文中的 JSON 对象）。"""
        if not raw or not raw.strip():
            return None

        text = raw.strip()
        fence_match = cls.JSON_FENCE_PATTERN.search(text)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            object_match = re.search(r"\{[\s\S]*\}", text)
            if not object_match:
                return None
            try:
                return json.loads(object_match.group(0))
            except json.JSONDecodeError:
                return None

    @classmethod
    def parse_tool_file_content(cls, result: Any) -> str:
        """从 read_file / write_file 工具返回值中提取文本内容，并去掉 cat -n 行号前缀。"""
        if result is None:
            return ""
        # 增加兜底防止异常值(str(result)) 被当作文件内容返回。      
        if hasattr(result, "success") and result.success is False:
            return ""
        if isinstance(result, str):
            text = result.strip()
            if text.startswith("success=False") or text.startswith("success= False"):
                return ""
            return _strip_line_numbers(text)
        if isinstance(result, dict):
            content = result.get("content", "")
            if isinstance(content, str):
                return _strip_line_numbers(content.strip())
            return _strip_line_numbers(str(content or "").strip())
        if hasattr(result, "data"):
            data = result.data
            if isinstance(data, dict):
                content = data.get("content", "")
                if isinstance(content, str):
                    return _strip_line_numbers(content.strip())
                return _strip_line_numbers(str(content or "").strip())
            if isinstance(data, str):
                return _strip_line_numbers(data.strip())
        return _strip_line_numbers(str(result).strip())

    @classmethod
    async def read_file(
        cls,
        node: Any,
        file_path: str | Path | None,
        *,
        max_chars: int | None = None,
        required: bool = False,
        label: str = "file",
        error_type: type[Exception] = RuntimeError,
    ) -> str:
        if not file_path:
            if required:
                raise error_type(f"缺少 {label} 路径")
            return ""
        path = Path(str(file_path)).expanduser().resolve()
        if not node.has_tool("read_file"):
            if required:
                raise error_type(f"read_file 工具不可用，无法读取 {label}")
            return ""
        try:
            result = await node.call_tool("read_file", file_path=str(path))
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            if required:
                raise error_type(f"读取 {label} 失败: {path}: {exc}") from exc
            return ""
        text = cls.parse_tool_file_content(result)
        if not text:
            if required:
                raise error_type(f"{label} 为空或不存在: {path}")
            return ""
        if max_chars is not None and len(text) > max_chars:
            return text[:max_chars] + "\n\n...(内容已截断)"
        return text

    @classmethod
    async def write_file(
        cls,
        node: Any,
        file_path: str | Path,
        content: str,
        *,
        label: str = "file",
        error_type: type[Exception] = RuntimeError,
    ) -> Path:
        path = Path(str(file_path)).expanduser().resolve()
        normalized = content.strip() + "\n" if content.strip() else ""
        if not node.has_tool("write_file"):
            raise error_type(f"write_file 工具不可用，无法写入 {label}")
        try:
            await node.call_tool(
                "write_file",
                file_path=str(path),
                content=normalized,
            )
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            raise error_type(f"写入 {label} 失败: {path}: {exc}") from exc
        return path

    @staticmethod
    def resolve_total_pages(
        *,
        page_count: int = 0,
        total_pages: int | None = None,
        outline_text: str = "",
        outline_pages: dict[int, str] | None = None,
        default_structural_pages: int = 2,
    ) -> int:
        """从 outline 页码、上下文 total_pages 与 page_count 兜底推算总页数。

        含 agenda 等额外结构页时，``page_count + 2`` 会低估总页数；优先取 outline 最大页码。
        """
        candidates: list[int] = []
        if total_pages is not None:
            try:
                parsed = int(total_pages)
                if parsed > 0:
                    candidates.append(parsed)
            except (TypeError, ValueError):
                pass
        if outline_pages:
            candidates.append(max(outline_pages))
        if outline_text.strip():
            page_nums = [
                int(match.group(1))
                for match in _OUTLINE_PAGE_HEADING_RE.finditer(outline_text)
            ]
            if page_nums:
                candidates.append(max(page_nums))
        if page_count > 0:
            candidates.append(page_count + default_structural_pages)
        return max(candidates) if candidates else 0
