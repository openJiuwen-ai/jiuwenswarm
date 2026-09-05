from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jiuwenclaw.agentserver.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenclaw.agentserver.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

logger = logging.getLogger(__name__)

_MAX_SEARCH_ROUNDS = 1
_MAX_BACKFILL_ROUNDS = 1
_MIN_SOURCES_PER_PAGE = 3
_MIN_KEY_FINDINGS = 3
_MIN_DATA_POINTS = 5
_MIN_DATA_TYPES = 2
_MIN_TIMEPOINTS = 3
_MIN_COMPARE_OBJECTS = 2
_MIN_COMPARE_DIMS = 2

_WORD_COUNT_MAP = {"L1": 1200, "L2": 2000, "L3": 3500}
_WORD_COUNT_NO_SEARCH_MAP = {"L1": 800, "L2": 1200, "L3": 2000}
_MIN_WORDS_PER_PAGE_FLOOR = 350

_PAGE_HEADER_RE = re.compile(r"^###\s*P(\d+)\s*[:：]", re.MULTILINE)
_TITLE_FIELD_RE = re.compile(r"\*\*标题\*\*[：:]\s*(.+)", re.IGNORECASE)
_DATA_NEED_RE = re.compile(r"\*\*数据需求\*\*[：:]\s*(.+)", re.IGNORECASE)
_PAGE_TYPE_RE = re.compile(r"\*\*类型\*\*[：:]\s*(\w+)", re.IGNORECASE)
_RESEARCH_QUERY_HEADER_RE = re.compile(r"\*\*研究查询\*\*[：:]\s*", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(.+)$", re.MULTILINE)
_NEXT_FIELD_RE = re.compile(r"\*\*[^*]+\*\*[：:]")
_SEARCHED_SOURCES_RE = re.compile(r"^##\s*已搜索来源", re.MULTILINE)
_URL_RE = re.compile(r"https?://[^\s\])>\"']+")
_NEXT_SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)


def _unique_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in urls:
        url = str(raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered


def _extract_fetch_result_items(result: Any) -> list[dict[str, Any]]:
    """Normalize a fetch_webpage tool result into a list of per-URL item dicts.

    fetch_webpage now returns a bare list of per-URL items; older callers may
    still receive ``{"results": [...]}`` or a plain string, so we keep tolerant
    fallbacks.
    """
    if isinstance(result, list):
        return [i for i in result if isinstance(i, dict)]
    if isinstance(result, dict):
        items = result.get("results")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
        if isinstance(items, dict):
            return [items]
    if isinstance(result, str) and result.strip():
        return [{"url": "", "content": result}]
    return []


def _extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return _unique_urls(_URL_RE.findall(text))


def _merge_seed_urls(*groups: list[str]) -> list[str]:
    """按组优先级去重合并 URL；先出现的组（用户链接、已搜索来源）优先。"""
    merged: list[str] = []
    for group in groups:
        merged.extend(group)
    return _unique_urls(merged)


def _seed_source_entries(urls: list[str]) -> list[dict[str, Any]]:
    return [{"url": url, "from_existing": True} for url in urls]


def _compute_min_words_per_page(
    research_depth: str, search_mode: str, page_count: int,
) -> int:
    total_min_words = _WORD_COUNT_MAP.get(research_depth, 2000)
    if search_mode == "no_search":
        total_min_words = _WORD_COUNT_NO_SEARCH_MAP.get(research_depth, 1200)
    return max(_MIN_WORDS_PER_PAGE_FLOOR, total_min_words // max(page_count, 1))


def _as_page_number(value: Any) -> int | None:
    try:
        page_num = int(value)
    except (TypeError, ValueError):
        return None
    return page_num if page_num > 0 else None


def _cli_researched_page_numbers(payload: dict[str, Any]) -> list[int]:
    raw = payload.get("researchedPages")
    if raw is None:
        raw = payload.get("researched_pages")
    nums: list[int] = []
    if isinstance(raw, list):
        for item in raw:
            page_num = _as_page_number(item)
            if page_num is not None:
                nums.append(page_num)
        if nums:
            return nums
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return []
    for item in pages:
        if not isinstance(item, dict) or item.get("isContent") is not True:
            continue
        page_num = _as_page_number(item.get("page") or item.get("page_number"))
        if page_num is not None:
            nums.append(page_num)
    return nums


def _is_placeholder_text(value: str) -> bool:
    return not value or value in {"-", "—", "无"}


def _normalize_query_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if not _is_placeholder_text(str(item).strip())]
    if not isinstance(value, str):
        return []
    text = value.strip()
    if _is_placeholder_text(text):
        return []
    return [text]


def _normalize_need_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if not _is_placeholder_text(str(item).strip())]
    if not isinstance(value, str):
        return []
    text = value.strip()
    if _is_placeholder_text(text):
        return []
    return [part.strip() for part in re.split(r"[、,，]", text) if not _is_placeholder_text(part.strip())]


@dataclass
class _ResearchConfig:
    """封装撰写所需配置参数，避免函数签名过长。"""
    search_mode: str
    research_depth: str
    topic: str
    no_data_fallback: bool = False


class PrepareNode(PlanNode):
    """P6.0 — 全局预处理：解析 outline、判定搜索策略、素材覆盖度评估、计算每页最低字数。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p6_0_prepare",
            instruction=(
                "## P6.0 全局预处理\n"
                "\n"
                "### 职责\n"
                "1. 读取 `{output_dir}/outline.md`\n"
                "2. 调用官方 `parse-outline` CLI 解析大纲，消费 "
                "`researched_pages` / `structural_pages`；结构页不派研究。"
                "CLI 未给出 research_queries / data_needs 时从 outline.md 正则补齐；"
                "CLI 失败再正则回退（不走 LLM）\n"
                "3. 提取用户原文 URL 与 outline `## 已搜索来源`（`searched_urls`）："
                "去重后优先进深抓池并计入 fetch 上限，不再为这些 URL 重复搜索\n"
                "4. 判断是否执行搜索（`_should_search`）：\n"
                "\n"
                "| search_mode | 素材状态 | 路径 |\n"
                "|---|---|---|\n"
                "| no_search | — | 跳过搜索 → 直接撰写 |\n"
                "| force_search | — | 完整流程 搜索 → 抓取 → 撰写 |\n"
                "| auto | 素材充实（LLM 评估） | 跳过搜索 → 直接撰写 |\n"
                "| auto | 素材不足或为空 | 完整流程 搜索 → 抓取 → 撰写 |\n"
                "\n"
                "5. 素材覆盖度评估（有素材且 need_search 时）：用 LLM 逐页评估素材对各页数据需求的覆盖程度（covered/partial/uncovered），并输出未覆盖的数据需求列表\n"
                "6. 计算每页最低字数 `min_words_per_page`（总最低字数 ÷ 页数，下限 350）\n"
                "\n"
                "### 输出\n"
                "- `prepare_status`: ok / failed\n"
                "- `pages`: 需要研究的页面列表\n"
                "- `searched_urls`: outline 中已搜索的 URL\n"
                "- `need_search`: 是否执行搜索\n"
                "- `no_data_fallback`: 无研究数据降级标志\n"
                "- `page_coverage`: 每页的素材覆盖度信息\n"
                "- `min_words_per_page`: 每页最低字数\n"
                "- `source_material` / `search_mode` / `research_depth` / `topic` / `output_dir`（透传）\n"
                "\n"
                "### 失败兜底\n"
                "- outline.md 为空/不存在：返回 prepare_status=failed\n"
                "- 解析不到 ✅ 页面：返回 prepare_status=failed\n"
                "- parse-outline CLI 失败或未得到 researched_pages：正则回退（不走 LLM）\n"
                "- 素材充足性评估失败：默认需要搜索\n"
                "- 素材覆盖度评估失败：按无素材处理（所有页面 uncovered）\n"
                "- need_search=False 且 source_material 为空/<200字：设置 no_data_fallback=True\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        output_dir = inputs.get("output_dir", "")
        outline_path = f"{output_dir}/outline.md" if output_dir else ""

        outline_text = await self._read_file(outline_path)
        if not outline_text:
            logger.warning("[P6.0] outline.md 为空或不存在")
            return {"prepare_status": "failed"}

        pptx_root = str(inputs.get("pptx_root") or "").strip()
        pages = await self._parse_outline_pages(
            outline_text, outline_path=outline_path, pptx_root=pptx_root,
        )
        if not pages:
            logger.warning("[P6.0] 未从 outline.md 中解析到需要研究的页面")
            return {"prepare_status": "failed"}

        search_mode = inputs.get("search_mode", "auto")
        research_depth = inputs.get("research_depth", "L2")
        source_material = inputs.get("source_material", "")
        topic = inputs.get("topic", "")
        searched_urls = _merge_seed_urls(
            _extract_urls(PptCommon.collect_user_text(inputs)),
            self._extract_searched_urls(outline_text),
        )
        need_search = await self._should_search(search_mode, source_material, pages)

        no_data_fallback = (
            not need_search
            and (not source_material or len(source_material.strip()) < 200)
        )
        if no_data_fallback:
            logger.warning(
                "[P6.0] 跳过搜索且无用户素材，进入无研究数据降级撰写 (search_mode=%s)",
                search_mode,
            )

        page_coverage: dict[str, dict[str, Any]] = {}
        if need_search and source_material:
            page_coverage = await self._evaluate_page_coverage(pages, source_material)

        min_words_per_page = _compute_min_words_per_page(
            research_depth, search_mode, len(pages),
        )

        logger.info(
            "[P6.0] 预处理完成 pages=%d need_search=%s no_data_fallback=%s "
            "min_words_per_page=%d seed_urls=%d",
            len(pages), need_search, no_data_fallback, min_words_per_page, len(searched_urls),
        )

        return {
            "prepare_status": "ok",
            "pages": pages,
            "searched_urls": searched_urls,
            "need_search": need_search,
            "no_data_fallback": no_data_fallback,
            "page_coverage": page_coverage,
            "min_words_per_page": min_words_per_page,
            "source_material": source_material,
            "search_mode": search_mode,
            "research_depth": research_depth,
            "topic": topic,
            "output_dir": output_dir,
        }

    async def _read_file(self, path: str) -> str:
        if not path:
            return ""
        if not self.has_tool("read_file"):
            logger.warning("[P6.0] read_file 工具不可用，无法读取文件 %s", path)
            return ""
        try:
            result = await self.call_tool("read_file", file_path=path)
            return PptCommon.parse_tool_file_content(result)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.0] 读取文件失败 %s: %s", path, e)
            return ""

    async def _parse_outline_pages(
        self,
        outline_text: str,
        *,
        outline_path: str = "",
        pptx_root: str = "",
    ) -> list[dict[str, Any]]:
        payload = await self._run_parse_outline_cli(outline_path, pptx_root)
        if payload:
            pages = self._pages_from_parse_outline_payload(payload, outline_text)
            if pages:
                logger.info("[P6.0] parse-outline CLI 解析到 researched_pages=%d", len(pages))
                return pages
            logger.warning("[P6.0] parse-outline CLI 未得到 researched_pages，正则回退")
        else:
            logger.warning("[P6.0] parse-outline CLI 不可用或失败，正则回退")
        return self._parse_outline_pages_fallback(outline_text)

    async def _run_parse_outline_cli(
        self, outline_path: str, pptx_root: str,
    ) -> dict[str, Any] | None:
        """调用官方 parse-outline CLI；失败返回 None，由调用方正则回退。"""
        if not outline_path or not pptx_root:
            logger.warning("[P6.0] 缺少 outline_path/pptx_root，跳过 parse-outline CLI")
            return None
        try:
            from jiuwenclaw.agentserver.skill_turbo.skill_codes.ppt.utils.bash_utils import (
                cli_path, quote_path, run_bash,
            )
            cmd = f"{cli_path('parse-outline', pptx_root)} {quote_path(outline_path)}"
            result = await run_bash(
                self, cmd,
                timeout_seconds=60, required=False, workdir=pptx_root,
            )
            if result.exit_code != 0:
                detail = result.stderr or result.stdout or ""
                logger.warning("[P6.0] parse-outline 返回 exit=%d: %s", result.exit_code, detail[:500])
                return None
            payload = PptCommon.parse_json_payload(result.stdout or result.raw)
            if not isinstance(payload, dict):
                logger.warning("[P6.0] parse-outline 输出不是 JSON 对象")
                return None
            return payload
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.0] parse-outline CLI 不可用: %s", e)
            return None

    def _pages_from_parse_outline_payload(
        self, payload: dict[str, Any], outline_text: str,
    ) -> list[dict[str, Any]]:
        """把官方 parse-outline JSON 转成下游 pages；缺 queries/needs 时用正则补齐。"""
        researched_nums = _cli_researched_page_numbers(payload)
        if not researched_nums:
            return []

        cli_by_num: dict[int, dict[str, Any]] = {}
        raw_pages = payload.get("pages")
        if isinstance(raw_pages, list):
            for item in raw_pages:
                if not isinstance(item, dict):
                    continue
                page_num = _as_page_number(item.get("page") or item.get("page_number"))
                if page_num is not None:
                    cli_by_num[page_num] = item

        fallback_by_num = {
            p["page_number"]: p for p in self._parse_outline_pages_fallback(outline_text)
        }

        pages: list[dict[str, Any]] = []
        for page_num in researched_nums:
            cli_page = cli_by_num.get(page_num, {})
            fallback = fallback_by_num.get(page_num, {})
            queries = _normalize_query_list(
                cli_page.get("researchQueries") or cli_page.get("research_queries"),
            )
            if not queries:
                queries = list(fallback.get("research_queries") or [])
            data_needs = _normalize_need_list(
                cli_page.get("dataNeeds") or cli_page.get("data_needs"),
            )
            if not data_needs:
                data_needs = list(fallback.get("data_needs") or [])
            pages.append({
                "page_number": page_num,
                "title": str(cli_page.get("title") or fallback.get("title") or "").strip(),
                "page_type": str(
                    cli_page.get("type") or cli_page.get("page_type") or fallback.get("page_type") or "data"
                ).strip() or "data",
                "research_queries": queries,
                "data_needs": data_needs,
            })
        return pages

    def _parse_outline_pages_fallback(self, outline_text: str) -> list[dict[str, Any]]:
        pages = []
        for m in _PAGE_HEADER_RE.finditer(outline_text):
            page_num = int(m.group(1))
            start = m.end()
            next_m = _PAGE_HEADER_RE.search(outline_text, start)
            section = outline_text[start:next_m.start() if next_m else len(outline_text)]

            if "✅" not in section:
                continue

            title_m = _TITLE_FIELD_RE.search(section)
            title = title_m.group(1).strip() if title_m else ""
            page_type_m = _PAGE_TYPE_RE.search(section)
            page_type = page_type_m.group(1) if page_type_m else "data"

            queries = self._extract_multi_line_list(section, "研究查询")
            dn_m = _DATA_NEED_RE.search(section)
            dn_str = dn_m.group(1).strip() if dn_m else ""
            data_needs = [s.strip() for s in re.split(r"[、,，]", dn_str) if s.strip()] if dn_str else []

            pages.append({
                "page_number": page_num,
                "title": title,
                "page_type": page_type,
                "research_queries": queries,
                "data_needs": data_needs,
            })
        return pages

    @staticmethod
    def _extract_multi_line_list(section: str, field_name: str) -> list[str]:
        """提取 **字段名**： 后的多行 - 列表项或单行值。"""
        header = re.compile(
            rf"\*\*{re.escape(field_name)}\*\*[：:]\s*",
            re.IGNORECASE,
        )
        m = header.search(section)
        if not m:
            return []
        after = section[m.end():]
        next_field = _NEXT_FIELD_RE.search(after)
        block = after[:next_field.start()] if next_field else after
        items = [mi.group(1).strip() for mi in _LIST_ITEM_RE.finditer(block)]
        if items:
            return items
        first_line = block.strip().split("\n")[0].strip() if block.strip() else ""
        if first_line and first_line not in ("-", "—", "无", "N/A"):
            return [s.strip() for s in re.split(r"[、,，]", first_line) if s.strip()]
        return []

    def _extract_searched_urls(self, outline_text: str) -> list[str]:
        m = _SEARCHED_SOURCES_RE.search(outline_text)
        if not m:
            return []
        section = outline_text[m.end():]
        next_heading = _NEXT_SECTION_RE.search(section)
        if next_heading:
            section = section[:next_heading.start()]
        return _extract_urls(section)

    async def _should_search(
        self,
        search_mode: str,
        source_material: str,
        pages: list[dict[str, Any]],
    ) -> bool:
        if search_mode == "no_search":
            return False
        if search_mode == "force_search":
            return True
        if not source_material or len(source_material.strip()) < 200:
            return True
        if search_mode == "auto":
            return await self._evaluate_material_sufficiency(source_material, pages)
        return True

    async def _evaluate_material_sufficiency(
        self,
        source_material: str,
        pages: list[dict[str, Any]],
    ) -> bool:
        research_needs = []
        for page in pages:
            queries = page.get("research_queries", [])
            data_needs = page.get("data_needs", [])
            if queries or data_needs:
                research_needs.append(
                    f"P{page['page_number']}({page.get('page_type', '')}): "
                    f"研究查询={queries}, 数据需求={data_needs}"
                )
        if not research_needs:
            return False

        needs_text = "\n".join(research_needs)
        material_preview = source_material[:3000]

        prompt = (
            "请判断用户素材是否足以支撑以下研究需求，无需额外搜索。\n\n"
            f"研究需求：\n{needs_text}\n\n"
            f"用户素材（前3000字）：\n{material_preview}\n\n"
            "判断标准：\n"
            "- 素材覆盖了大部分页面的研究查询和数据需求 → 回答 sufficient\n"
            "- 素材仅覆盖少部分页面，或数据需求明显缺失 → 回答 insufficient\n\n"
            "只输出 sufficient 或 insufficient，不要输出其他内容。"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是一个素材充足性评估助手，只输出 sufficient 或 insufficient。",
            )
            decision = result.strip().lower()
            logger.info("[P6.0] 素材充足性评估结果: %s", decision)
            return decision != "sufficient"
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.0] 素材充足性评估失败，默认需要搜索: %s", e)
            return True

    async def _evaluate_page_coverage(
        self,
        pages: list[dict[str, Any]],
        source_material: str,
    ) -> dict[str, dict[str, Any]]:
        page_descriptions = []
        for page in pages:
            page_descriptions.append(
                f"P{page['page_number']}({page.get('page_type', '')}): "
                f"研究查询={page.get('research_queries', [])}, "
                f"数据需求={page.get('data_needs', [])}"
            )
        pages_text = "\n".join(page_descriptions)
        material_preview = source_material[:3000]

        prompt = (
            "请逐页评估用户素材对各页面研究需求的覆盖程度。\n\n"
            f"页面研究需求：\n{pages_text}\n\n"
            f"用户素材（前3000字）：\n{material_preview}\n\n"
            "对每个页面输出：\n"
            "- coverage: covered（素材已覆盖大部分数据需求）/ partial（部分覆盖）/ uncovered（完全未覆盖）\n"
            "- uncovered_needs: 素材未覆盖的数据需求列表（covered 为空数组，partial 列出未覆盖项，uncovered 列出全部）\n\n"
            '以 JSON 对象格式输出，key 为页码字符串。\n'
            '例如：{"1": {"coverage": "covered", "uncovered_needs": []}, '
            '"2": {"coverage": "partial", "uncovered_needs": ["2024年AI市场规模", "CAGR数据"]}, '
            '"3": {"coverage": "uncovered", "uncovered_needs": ["全部数据需求"]}}\n'
            "只输出 JSON，不要输出其他内容。"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是一个素材覆盖度评估助手，只输出 JSON 对象。",
            )
            raw = self.extract_json(result, expected_type=dict)
            if isinstance(raw, dict):
                coverage_map: dict[str, dict[str, Any]] = {}
                for k, v in raw.items():
                    if isinstance(v, dict):
                        coverage_map[str(k)] = {
                            "coverage": str(v.get("coverage", "uncovered")),
                            "uncovered_needs": v.get("uncovered_needs", []),
                        }
                    else:
                        coverage_map[str(k)] = {
                            "coverage": str(v),
                            "uncovered_needs": [],
                        }
                logger.info("[P6.0] 素材覆盖度评估: %s", coverage_map)
                return coverage_map
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.0] 素材覆盖度评估失败，按无素材处理: %s", e)
        return {}

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        ok = result.get("prepare_status") == "ok"
        yield {
            **result,
            "node": self.plan_name,
            "status": "ok" if ok else "error",
            "message": "预处理完成" if ok else "预处理失败",
        }


class PageWorkerNode(PlanNode):
    """P6.1 — per-page 并发闭环：搜索→评分→补搜→抓取→ghost→校验→回溯→撰写→按页校验→失败重写，N 页并发。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p6_1_page_worker",
            instruction=(
                "## P6.1 per-page 闭环（合并原 P6.2 搜索 + P6.3 抓取校验 + P6.4 撰写校验）\n"
                "\n"
                "### 前置条件\n"
                "- P6.0 已完成预处理（pages / need_search / page_coverage / min_words_per_page 等）\n"
                "- `read_file` / `write_file` 工具可用\n"
                "- `web_search` 工具可用（搜索模式，不可用时降级为纯素材撰写）\n"
                "- `fetch_webpage` 工具可用（搜索模式，不可用时降级为纯素材撰写）\n"
                "\n"
                "### 输入\n"
                "- `pages`: 需要研究的页面列表（来自 P6.0）\n"
                "- `need_search`: 是否执行搜索（来自 P6.0）\n"
                "- `no_data_fallback`: 无研究数据降级标志（来自 P6.0）\n"
                "- `page_coverage`: 每页的素材覆盖度信息（来自 P6.0）\n"
                "- `searched_urls`: 用户链接 + outline 已搜索来源（来自 P6.0，优先进深抓池）\n"
                "- `min_words_per_page`: 每页最低字数（来自 P6.0）\n"
                "- `source_material` / `search_mode` / `research_depth` / `topic` / `output_dir`（透传）\n"
                "\n"
                "### 输出\n"
                "- `research_paths`: {页码: research-P{N}.md 文件路径} 字典\n"
                "\n"
                "### 执行流程（per-page 闭环，N 页 asyncio.gather 并发）\n"
                "对每一页独立执行：\n"
                "\n"
                "#### 阶段 1：搜索（need_search=True 时）\n"
                "a. 按覆盖度生成搜索查询：\n"
                "   - covered 页：仅 1 次验证性搜索\n"
                "   - partial 页：仅搜索未覆盖的数据需求 + 1 次验证性搜索\n"
                "   - uncovered 页：完整搜索（所有 research_queries + data_needs 综合查询）\n"
                "b. 并行搜索所有查询\n"
                "c. 用户链接与已搜索来源置于候选池前端并计入 fetch 上限；搜索结果去重后追加\n"
                "d. 来源评分筛选（仅对新搜索结果：A+/A/A-/B+/B/C，C 级丢弃）\n"
                "e. 缺口检查（合格来源 <3 个 → 标记缺口页）\n"
                "f. 定向补搜（最多1轮，加 report/白皮书/官方 限定词）\n"
                "\n"
                "#### 阶段 2：抓取校验（need_search=True 时）\n"
                "a. 来源筛选 + 并行抓取：每页取 top_sources（L1=2/L2=3/L3=4），调 fetch_webpage（带 prompt，max_chars=8000，timeout=8s）\n"
                "b. 幽灵来源识别（LLM 判断 6 类幽灵来源，返回应排除的序号）\n"
                "c. 数据充分性校验（4 项，宽松标准，LLM 判断）：\n"
                "   1. 证据密度：≥2 条 key_findings 且 ≥3 条关键数据点（宽松）\n"
                "   2. 数据类型覆盖：≥1 种数据类型（宽松）\n"
                "   3. 时序/对比：trend/data 页须有≥3时间点；comparison/technology 页须有≥2行×≥2列表格\n"
                "   4. 交叉验证：L3≥3源 / L2≥2源 / L1标注单源\n"
                "d. 定向回溯（最多1轮）：\n"
                "   - 优先从 page_sources[backfill_start:backfill_end] 补抓\n"
                "   - 候选池不足时按 missing 类别生成定向查询调 web_search\n"
                "   - 新 URL 走同样的抓取流程\n"
                "e. 回溯后严格二次校验：\n"
                "   1. 证据密度：≥3 条 key_findings 且 ≥5 条关键数据点（严格）\n"
                "   2. 数据类型覆盖：≥2 种数据类型（严格）\n"
                "   3. 时序/对比：同上\n"
                "   4. 交叉验证：同上\n"
                "f. 仍不通过 → 标注 data_limited: true\n"
                "\n"
                "#### 阶段 3：撰写\n"
                "a. LLM 撰写单页研究报告（以 `### P{N}:` 开头，不输出报告标题）\n"
                "b. 按页校验（LLM 判断 7 项）：\n"
                "   1. 页面结构：`### P{N}:` 标题 + `#### PPT 内容建议`\n"
                "   2. PPT 内容建议：推荐主标题 + 核心论点(5-10条) + 关键数据清单表格(≥5/≥3行) + 时序数据表(必要时) + 对比数据表(必要时) + 案例素材\n"
                "   3. 数据表格格式：关键数据清单含'数据类型'列；时序/对比为专用表格非散文\n"
                "   4. 引用规范：每页 ≥3 个来源标注（来源名称，非数字编号）\n"
                "   5. 反空泛：无模糊修饰、无占位文本\n"
                "   6. 字数达标：本页中文字数 ≥ min_words_per_page × 80%\n"
                "   7. 素材充实度：核心论点有展开说明和来源标注；案例含具体实体名称\n"
                "c. 校验不通过 → 重写1次（仅本页，覆盖当前版本，不再二次校验）\n"
                "\n"
                "### 来源可信度评分标准\n"
                "| 等级 | 分数 | 来源类型 |\n"
                "|---|---|---|\n"
                "| A+ | 90-100 | 权威机构（政府、国际组织） |\n"
                "| A | 80-89 | 企业官方（年报、财报） |\n"
                "| A- | 70-79 | 学术论文 |\n"
                "| B+ | 65-69 | 权威媒体 |\n"
                "| B | 60-64 | 行业媒体 |\n"
                "| C | <60 | 自媒体/内容农场（排除） |\n"
                "\n"
                "### 排除条件\n"
                "纯观点无数据、来源不明的二手转述、商业推广、可信度 <60\n"
                "\n"
                "### 缺口补搜策略\n"
                "| 缺口类型 | 判定条件 | 补搜策略 |\n"
                "|---|---|---|\n"
                "| 数据需求缺口 | 某条数据需求无来源覆盖 | 针对该需求生成精准查询 |\n"
                "| 来源类型偏斜 | 某页全部为媒体来源 | 加 report/白皮书/官方 限定词 |\n"
                "| 页面来源不足 | 某页合格来源 <3 个 | 换同义词、加英文查询 |\n"
                "\n"
                "### 幽灵来源特征（6 类）\n"
                "1. 无URL或URL明显无效\n"
                "2. DOI不匹配（DOI链接指向的内容与标题/预期不符）\n"
                "3. 标题/年份与内容矛盾\n"
                "4. 无法回溯的二手转述\n"
                "5. 引用来源与页面数据需求领域不符\n"
                "6. 发布时间异常（>2年旧信息，非经典案例除外）\n"
                "\n"
                "### 定向回溯查询模板\n"
                "| missing 类别 | 查询模板 |\n"
                "|---|---|\n"
                "| 缺时序数据 | {topic} 历年数据 趋势 |\n"
                "| 缺对比数据 | {topic} 对比 排名 |\n"
                "| 数据类型单一 | {topic} 统计数据 报告 |\n"
                "| 证据密度不足 | {topic} 白皮书 研究报告 |\n"
                "| 来源不足 | {topic} 官方报告 权威数据 |\n"
                "\n"
                "### WebFetch prompt 构造规则\n"
                "```\n"
                "从本文提取关于「{该页 data_needs 拼接}」的信息，仅输出以下结构化内容，禁止输出全文或无关内容：\n"
                "1. 关键事实（具体数据点、统计数字，带年份）\n"
                "2. 核心观点（1-2句结论性陈述）\n"
                "3. 案例信息（具体公司/产品/实施情况，含名称）\n"
                "4. 时序数据（如有：格式为\"指标：2023年=X，2024年=Y，2025年=Z\"）\n"
                "5. 对比数据（如有：格式为\"对象A=X，对象B=Y\"）\n"
                "6. 原始来源（数据出处和发布时间）\n"
                "如文中无相关数据，输出\"本文无相关数据\"即可。\n"
                "```\n"
                "\n"
                "### research-P{N}.md 结构骨架（每页独立文件，不含全局 header）\n"
                "```\n"
                "### P{N}: {页面标题}\n"
                "> 页面类型：{type}\n"
                "**核心论点**：{一句结论性陈述}\n"
                "#### PPT 内容建议\n"
                "- **推荐主标题**：{headline}\n"
                "- **核心论点**（5-10条，每条附展开说明和来源引用）\n"
                "- **关键数据清单**（Markdown表格，搜索模式≥5行 / no_search≥3行，含数据类型列）：\n"
                "  | 数据项 | 数值/结果 | 来源 | 时间 | 数据类型 |\n"
                "- **时序数据**（trend/data/comparison/technology页必填，≥3时间点）：\n"
                "  | 指标 | {t1} | {t2} | {t3} | 来源 |\n"
                "- **对比数据**（comparison/data/technology页必填，≥2对象×≥2维度）：\n"
                "  | 对比维度 | {A} | {B} | 来源 |\n"
                "- **案例素材**：{entity} — {description} [来源名称]\n"
                "```\n"
                "\n"
                "### 写作硬规则\n"
                "1. 要点优先，核心论点附展开说明和来源引用\n"
                "2. 精准引用：事实陈述首次出现时同句内附来源标注（如 [Gartner]、[年度报告]），禁止伪引用\n"
                "3. 反空泛：禁止'市场前景广阔''发展迅速'等无来源修饰，用精确数字替代；禁止 TODO/xxx 等占位文本\n"
                "4. 数据完整保留：所有数据点必须出现在关键数据清单表格中\n"
                "5. 来源可识别：使用来源名称标注，禁止纯数字编号\n"
                "6. 关键数据清单每页 ≥5 条（no_search ≥3 条）、≥2 种数据类型\n"
                "7. trend/data/comparison/technology 页必须有时序数据（≥3时间点）和对比数据（≥2对象×≥2维度）\n"
                "8. 数据有限页面：page_extractions 中含 `data_limited: true` 的页面，在该页 PPT 内容建议下显式标注'数据有限，基于用户素材'或'数据有限'\n"
                "\n"
                "### no_search 模式调整\n"
                "| 项目 | 搜索模式 | no_search 模式 |\n"
                "|---|---|---|\n"
                "| 数据来源 | 外部研究为主 | 用户素材为主 |\n"
                "| 来源标注 | [机构名] | [资料名] |\n"
                "| 全文字数 | L1≥1.2k/L2≥2k/L3≥3.5k | L1≥800/L2≥1.2k/L3≥2k |\n"
                "| 关键数据清单 | ≥5 条 | ≥3 条 |\n"
                "| 数据有限标注 | 仅在搜索不足时 | 每个仅凭素材的页面均标注'数据有限，基于用户素材' |\n"
                "\n"
                "### 失败兜底\n"
                "- no_data_fallback=True：跳过搜索/抓取/校验，直接代码模板生成大纲骨架，跳过 LLM 撰写和校验\n"
                "- web_search 不可用：返回空 page_sources，直接进入撰写\n"
                "- fetch_webpage 不可用：返回空 page_extractions，直接进入撰写\n"
                "- 搜索失败：保留已有来源，不重试\n"
                "- 来源评分失败：保留全部来源\n"
                "- 补搜失败：保留已有来源，不重试\n"
                "- 单个URL抓取异常：日志记录，跳过该URL\n"
                "- 幽灵来源LLM识别失败：保留全部来源，不过滤\n"
                "- 数据充分性校验LLM失败：保守视为缺口，进入回溯\n"
                "- 回溯后仍不通过：标注 data_limited: true，传递给撰写阶段降级处理\n"
                "- 撰写LLM失败：使用兜底骨架\n"
                "- 按页校验LLM失败：保守视为通过（避免假阳性触发无意义重写）\n"
                "- 重写LLM失败：保留当前版本\n"
                "- write_file 不可用/失败：记录错误日志，该页不写入 research_paths\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        pages = inputs.get("pages", [])
        need_search = inputs.get("need_search", False)
        no_data_fallback = inputs.get("no_data_fallback", False)
        page_coverage = inputs.get("page_coverage", {})
        searched_urls = inputs.get("searched_urls", [])
        source_material = inputs.get("source_material", "")
        search_mode = inputs.get("search_mode", "auto")
        research_depth = inputs.get("research_depth", "L2")
        topic = inputs.get("topic", "")
        output_dir = inputs.get("output_dir", "")
        min_words_per_page = int(inputs.get("min_words_per_page") or 200)

        config = _ResearchConfig(
            search_mode=search_mode,
            research_depth=research_depth,
            topic=topic,
            no_data_fallback=no_data_fallback,
        )

        # 降级路径：无研究数据 — 逐页写 stub
        if no_data_fallback:
            logger.info("[P6.1] 无研究数据降级模式，跳过搜索/抓取/校验")
            research_paths: dict[int, str] = {}
            for page in pages:
                page_num = int(page["page_number"])
                path = f"{output_dir}/research-P{page_num}.md"
                stub = self._build_no_data_page_section(page, topic, search_mode, research_depth)
                if await self._write_file(path, stub):
                    research_paths[page_num] = path
            return {"research_paths": research_paths}

        if not pages:
            logger.warning("[P6.1] pages 为空，无法撰写")
            return {"research_paths": {}}

        # per-page 并发闭环
        tasks = [
            self._run_page_pipeline(
                page=page,
                coverage_info=page_coverage.get(str(page["page_number"]), {}),
                searched_urls=searched_urls,
                source_material=source_material,
                config=config,
                min_words_per_page=min_words_per_page,
                need_search=need_search,
            )
            for page in pages
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 逐页写 research-P{N}.md（不再合并为单文件）
        research_paths: dict[int, str] = {}
        for page, result in zip(pages, results):
            page_num = int(page["page_number"])
            path = f"{output_dir}/research-P{page_num}.md"
            if isinstance(result, Exception):
                logger.warning("[P6.1] 页面 P%d 闭环异常: %s", page_num, result)
                section = self._build_fallback_page_section(page)
            else:
                section = result.get("section", "")
                if not section:
                    section = self._build_fallback_page_section(page)
            if await self._write_file(path, section):
                research_paths[page_num] = path

        logger.info("[P6.1] per-page 闭环完成，已落盘 %d 个 research-P{N}.md", len(research_paths))

        # validate-research 全量门禁（prod 契约：所有页面写完后统一校验）
        pptx_root = str(inputs.get("pptx_root") or "").strip()
        outline_path = str(inputs.get("outline_path") or "").strip()
        validation_inputs_ready = all((research_paths, output_dir, pptx_root, outline_path))
        if validation_inputs_ready:
            validation_ok = await self._run_validate_research(output_dir, pptx_root, outline_path, research_depth)
            if not validation_ok:
                logger.warning("[P6.1] validate-research 全量门禁未通过，但不阻塞 pipeline（降级继续）")

        return {"research_paths": research_paths}

    async def _run_validate_research(
        self,
        output_dir: str,
        pptx_root: str,
        outline_path: str,
        research_depth: str,
    ) -> bool:
        """调 cli validate-research 全量门禁，校验所有页面研究质量。

        prod 契约：cli validate-research --dir <dir> --outline <outline.md> --level <L>。
        CLI 不可用时降级为通过。
        """
        try:
            from jiuwenclaw.agentserver.skill_turbo.skill_codes.ppt.utils.bash_utils import (
                cli_path, quote_path, run_bash,
            )
            cmd = (
                f"{cli_path('validate-research', pptx_root)} "
                f"--dir {quote_path(output_dir)} "
                f"--outline {quote_path(outline_path)} "
                f"--level {research_depth}"
            )
            result = await run_bash(
                self, cmd,
                timeout_seconds=120, required=False, workdir=pptx_root,
            )
            if result.exit_code != 0:
                detail = result.stderr or result.stdout or ""
                logger.warning("[P6.1] validate-research 门禁返回 exit=%d: %s", result.exit_code, detail[:500])
                return False
            logger.info("[P6.1] validate-research 全量门禁通过")
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] validate-research CLI 不可用，降级跳过: %s", e)
            return True

    async def _run_page_pipeline(
        self,
        *,
        page: dict[str, Any],
        coverage_info: dict[str, Any],
        searched_urls: list[str],
        source_material: str,
        config: _ResearchConfig,
        min_words_per_page: int,
        need_search: bool,
    ) -> dict[str, Any]:
        """单页闭环：搜索→评分→补搜→抓取→ghost→校验→回溯→撰写→按页校验→失败重写。"""
        page_num = page["page_number"]

        page_sources: list[dict[str, Any]] = []
        page_extractions: list[dict[str, Any]] = []

        if need_search:
            # 阶段1：搜索
            page_sources = await self._search_for_page(page, coverage_info, searched_urls)

            # 阶段2：抓取校验
            if page_sources:
                page_extractions = await self._fetch_for_page(page, page_sources, config.research_depth)

        # 阶段3：撰写
        section = await self._write_single_page(
            page=page,
            extractions=page_extractions,
            source_material=source_material,
            config=config,
            min_words_per_page=min_words_per_page,
        )

        # 按页校验 + 失败重写
        if section:
            passed = await self._validate_single_page(page, section, config, min_words_per_page)
            if not passed:
                logger.info("[P6.1] 页面 P%d 校验未通过，尝试重写1次", page_num)
                rewritten = await self._write_single_page(
                    page=page,
                    extractions=page_extractions,
                    source_material=source_material,
                    config=config,
                    min_words_per_page=min_words_per_page,
                )
                if rewritten:
                    section = rewritten

        return {"section": section}

    # ==================== 搜索阶段 ====================

    async def _search_for_page(
        self,
        page: dict[str, Any],
        coverage_info: dict[str, Any],
        searched_urls: list[str],
    ) -> list[dict[str, Any]]:
        """单页搜索：生成查询→并行搜索→评分筛选→缺口补搜。"""
        if not self.has_tool("web_search"):
            logger.warning("[P6.1] web_search 工具不可用，跳过搜索")
            return []

        queries = self._build_page_queries(page, coverage_info)
        if not queries:
            logger.warning("[P6.1] 页面 P%d 无搜索查询（research_queries 和 data_needs 均为空），跳过搜索", page["page_number"])
            return []

        logger.info("[P6.1] 页面 P%d 搜索 %d 个查询", page["page_number"], len(queries))

        search_tasks = [
            self.call_tool("web_search", query=q, search_mode="default")
            for q in queries
        ]
        results = await asyncio.gather(*search_tasks, return_exceptions=True)

        seed_urls = _unique_urls(searched_urls)
        sources = _seed_source_entries(seed_urls)
        seen_urls = set(seed_urls)
        for q, result in zip(queries, results):
            if isinstance(result, Exception):
                logger.warning("[P6.1] 搜索失败 query=%s: %s", q[:50], result)
                continue
            if isinstance(result, str) and result.startswith("[ERROR]"):
                continue
            for item in self._parse_search_results(result):
                url = str(item.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                sources.append(item)

        # 评分筛选（已有 seed 不参与评分、保持前端优先）
        sources = await self._score_sources_for_page(page, sources)

        # 缺口检查 + 补搜
        if len(sources) < _MIN_SOURCES_PER_PAGE:
            logger.info(
                "[P6.1] 页面 P%d 合格来源 %d < %d，触发补搜",
                page["page_number"], len(sources), _MIN_SOURCES_PER_PAGE,
            )
            sources = await self._backfill_search_for_page(page, sources)

        return sources

    def _build_page_queries(
        self,
        page: dict[str, Any],
        coverage_info: dict[str, Any],
    ) -> list[str]:
        """按覆盖度生成搜索查询。"""
        cov = coverage_info.get("coverage", "uncovered") if isinstance(coverage_info, dict) else "uncovered"
        uncovered_needs = coverage_info.get("uncovered_needs", []) if isinstance(coverage_info, dict) else []
        research_queries = page.get("research_queries", [])
        data_needs = page.get("data_needs", [])

        queries: list[str] = []
        if cov == "covered":
            for q in research_queries[:1]:
                queries.append(q)
        elif cov == "partial":
            for need in uncovered_needs:
                queries.append(str(need))
            for q in research_queries[:1]:
                queries.append(q)
        else:
            page_type = str(page.get("page_type", page.get("type", ""))).lower()
            need_comparison = any(
                kw in page_type for kw in ("comparison", "technology", "data", "trend")
            )
            if research_queries:
                queries.append(research_queries[0])
            if data_needs:
                combined_needs = " ".join(str(d) for d in data_needs[:3])
                if need_comparison:
                    combined_needs = f"{combined_needs} 对比 排名 参数"
                queries.append(combined_needs)

        return queries

    def _parse_search_results(self, raw: str) -> list[dict[str, Any]]:
        sources = []
        for m in _URL_RE.finditer(raw):
            url = m.group(0)
            start = max(0, m.start() - 100)
            context = raw[start:m.start()]
            title = context.split("\n")[-1].strip().lstrip("-•* ").strip()
            sources.append({"url": url, "title": title})
        return sources

    async def _score_sources_for_page(
        self,
        page: dict[str, Any],
        sources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """单页来源评分筛选：A+/A/A-/B+/B 保留，C 级丢弃。"""
        to_score = [s for s in sources if not s.get("from_existing") and s.get("url")]
        existing = [s for s in sources if s.get("from_existing")]

        if not to_score:
            return sources

        # URL 去重
        seen_urls: set[str] = set()
        unique_sources: list[dict[str, Any]] = []
        for s in to_score:
            url = s.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_sources.append(s)

        if not unique_sources:
            return existing

        source_list_text = "\n".join(
            f"{i+1}. {s.get('title', '')} — {s.get('url', '')}"
            for i, s in enumerate(unique_sources)
        )

        prompt = (
            "请对以下搜索结果来源进行可信度评分。\n\n"
            f"来源列表：\n{source_list_text}\n\n"
            "评分标准：\n"
            "- A+ (90-100)：权威机构（政府、国际组织）\n"
            "- A (80-89)：企业官方（年报、财报）\n"
            "- A- (70-79)：学术论文\n"
            "- B+ (65-69)：权威媒体\n"
            "- B (60-64)：行业媒体\n"
            "- C (<60)：自媒体/内容农场（排除）\n\n"
            "排除条件：纯观点无数据、来源不明的二手转述、商业推广、可信度 <60。\n\n"
            "### 快速直判规则（按域名后缀直接定级，禁止推理 URL 归属机构）\n"
            "- `.gov.cn` / `.org.cn`(政府/国际组织) → A+\n"
            "- `*.icbc.com.cn` / 企业官网 / 官方 PDF → A\n"
            "- `edu.cn` / `edu.com.cn` → A-\n"
            "- 权威媒体（sina/163/sohu/fx678/cs.com.cn/financialnews 等主流门户/财经媒体） → B+\n"
            "- 行业媒体（knowcat/pinggu/gold678/uwnews 等垂直/地方媒体） → B\n"
            "- 不明 / 自媒体 / 内容农场 / 1234567.com.cn 等域名 → C\n"
            "- 域名识别不清时按 URL 特征直判，**禁止逐个列举机构比对**\n\n"
            '以 JSON 对象输出，key 为来源序号（数字字符串，从1开始），value 为评分等级（A+/A/A-/B+/B/C）。\n'
            '例如：{"1": "A", "2": "C", "3": "B+"}\n'
            "只输出 JSON 对象，不要输出其他内容，不要输出原始URL。"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是来源可信度评估助手，只输出 JSON 对象。",
                concurrent=True,
            )
            raw_scores = self.extract_json(result, expected_type=dict)
            if not isinstance(raw_scores, dict):
                return sources

            url_scores: dict[str, str] = {}
            for key, grade in raw_scores.items():
                try:
                    idx = int(str(key).strip()) - 1
                except (ValueError, TypeError):
                    continue
                if 0 <= idx < len(unique_sources):
                    url_scores[unique_sources[idx]["url"]] = str(grade).strip().upper()

            grade_order = {"A+": 0, "A": 1, "A-": 2, "B+": 3, "B": 4}

            kept: list[dict[str, Any]] = []
            for s in unique_sources:
                url = s.get("url", "")
                grade = url_scores.get(url, "")
                if grade in grade_order:
                    s["grade"] = grade
                    kept.append(s)

            kept.sort(key=lambda x: grade_order.get(x.get("grade", ""), 99))

            logger.info(
                "[P6.1] 页面 P%d 来源评分完成，合格 %d / %d",
                page["page_number"],
                len(existing) + len(kept),
                len(unique_sources) + len(existing),
            )
            return existing + kept
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d 来源评分失败，保留全部来源: %s", page["page_number"], e)
            return sources

    async def _backfill_search_for_page(
        self,
        page: dict[str, Any],
        existing_sources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """单页缺口补搜（最多1轮）。"""
        if not self.has_tool("web_search"):
            return existing_sources

        research_queries = page.get("research_queries", [])
        data_needs = page.get("data_needs", [])

        if research_queries:
            query = f"{research_queries[0]} report 白皮书"
        elif data_needs:
            query = f"{data_needs[0]} 官方数据"
        else:
            return existing_sources

        try:
            search_result = await self.call_tool(
                "web_search", query=query, search_mode="default",
            )
            new_sources = self._parse_search_results(search_result)
            existing_sources.extend(new_sources)
            logger.info(
                "[P6.1] 页面 P%d 补搜新增 %d 个来源",
                page["page_number"], len(new_sources),
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d 补搜失败: %s", page["page_number"], e)

        return existing_sources

    # ==================== 抓取校验阶段 ====================

    async def _fetch_for_page(
        self,
        page: dict[str, Any],
        page_sources: list[dict[str, Any]],
        research_depth: str,
    ) -> list[dict[str, Any]]:
        """单页抓取校验：批量抓取→ghost识别→数据校验→定向回溯。"""
        if not self.has_tool("fetch_webpage"):
            logger.warning("[P6.1] fetch_webpage 工具不可用，跳过抓取")
            return []

        # 阶段1：批量抓取
        extractions = await self._batch_fetch_single(page, page_sources, research_depth)

        # 阶段2：幽灵来源识别
        extractions = await self._identify_ghost_single(page, extractions)

        # 阶段3：数据充分性校验（宽松）
        gap, missing = await self._validate_page_sufficiency(
            page, extractions, research_depth, strict=False,
        )

        # 阶段4：定向回溯
        if gap:
            extractions = await self._backfill_fetch_single(
                page, page_sources, extractions, missing, research_depth,
            )

        return extractions

    async def _batch_fetch_single(
        self,
        page: dict[str, Any],
        page_sources: list[dict[str, Any]],
        research_depth: str,
        extra_urls: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """单页批量抓取。"""
        top_n = {"L1": 2, "L2": 3, "L3": 4}.get(research_depth, 3)
        top_sources = list(page_sources[:top_n])

        if extra_urls:
            existing_top_urls = {s.get("url") for s in top_sources}
            for url in extra_urls:
                if url not in existing_top_urls:
                    top_sources.append({"url": url})

        fetch_urls: list[str] = []
        for source in top_sources:
            url = source.get("url", "")
            if url and url not in fetch_urls:
                fetch_urls.append(url)

        if not fetch_urls:
            return []

        try:
            result = await self.call_tool(
                "fetch_webpage",
                url=fetch_urls,
                max_chars=8000,
                timeout_seconds=8,
            )
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            logger.warning("[P6.1] WebFetch 批量抓取失败 urls=%s: %s", fetch_urls, exc)
            return []

        items = _extract_fetch_result_items(result)

        extractions: list[dict[str, Any]] = []
        for item in items:
            url = str(item.get("url", "")).strip()
            error = item.get("error")
            if error:
                logger.warning("[P6.1] WebFetch 失败 url=%s: %s", url[:80], error)
                continue
            content = str(item.get("content", "") or "")
            if not content:
                continue
            extractions.append({"url": url, "content": content})

        return extractions

    async def _identify_ghost_single(
        self,
        page: dict[str, Any],
        extractions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """单页幽灵来源识别。"""
        if not extractions:
            return extractions

        needs = "；".join(page.get("data_needs", [])) or page.get("title", "")
        source_list = "\n".join(
            f"{i+1}. URL: {e.get('url', '')}\n   内容摘要: {e.get('content', '')[:200]}"
            for i, e in enumerate(extractions)
        )
        prompt = (
            "请识别以下来源中的幽灵来源（不可靠/虚假来源），返回应排除的序号列表。\n\n"
            f"当前日期：{datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}\n\n"
            f"页面数据需求：{needs}\n\n"
            f"来源列表：\n{source_list}\n\n"
            "幽灵来源特征：\n"
            "1. 无URL或URL明显无效\n"
            "2. DOI不匹配（DOI链接指向的内容与标题/预期不符）\n"
            "3. 标题/年份与内容矛盾（如标题说2024但内容是2021数据）\n"
            "4. 无法回溯的二手转述（如\"据XX报道\"但无原始链接）\n"
            "5. 引用来源与页面数据需求领域不符\n"
            "6. 发布时间异常（>2年旧信息，非经典案例）\n\n"
            "### 输出纪律（硬约束，必须遵守）\n"
            "- 默认保留所有来源，排除是例外。仅当来源存在明显且无争议的特征匹配时才排除；有任何不确定性即保留\n"
            "- 每个来源只判定一次，给出「排除/保留」结论后立即进入下一个来源，禁止回溯和反复论证\n"
            "- 必须先输出最终 JSON 数组，补充说明合计 ≤2 句，禁止逐条来源展开论证\n\n"
            '以 JSON 数组输出应排除的序号（从1开始），无需排除则输出 []。\n'
            "只输出 JSON 数组，不要输出其他内容。"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是来源可靠性验证助手，只输出 JSON 数组。",
                concurrent=True,
            )
            exclude_indices = self.extract_json(result, expected_type=list)
            if isinstance(exclude_indices, list):
                exclude_set = set(int(i) - 1 for i in exclude_indices if isinstance(i, (int, float)))
                verified = [e for i, e in enumerate(extractions) if i not in exclude_set]
                logger.info(
                    "[P6.1] 页面 P%d ghost 识别排除 %d 个来源",
                    page["page_number"], len(extractions) - len(verified),
                )
                return verified
            return extractions
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d ghost 识别LLM失败，保留全部: %s", page["page_number"], e)
            return extractions

    async def _validate_page_sufficiency(
        self,
        page: dict[str, Any],
        extractions: list[dict[str, Any]],
        research_depth: str,
        strict: bool = False,
    ) -> tuple[bool, list[str]]:
        """单页数据充分性校验，返回 (is_gap, missing_items)。"""
        if not extractions:
            return True, ["无任何抓取内容"]

        min_sources = {"L3": 3, "L2": 2, "L1": 1}.get(research_depth, 2)

        if strict:
            density_rule = "每页 ≥3 条 key_findings 且关键数据点 ≥5 条"
            type_rule = "≥2 种数据类型（绝对值/百分比/排名/增长率）"
            density_missing = '"证据密度不足"：key_findings <3 或 关键数据点 <5'
            type_missing = '"数据类型单一"：仅有 1 种数据类型（绝对值/百分比/排名/增长率）'
            strict_label = "严格（回填后二次校验）"
        else:
            density_rule = "每页 ≥2 条 key_findings 且关键数据点 ≥3 条"
            type_rule = "≥1 种数据类型（绝对值/百分比/排名/增长率）"
            density_missing = '"证据密度不足"：key_findings <2 或 关键数据点 <3'
            type_missing = '"数据类型单一"：未提取到任何数据类型（绝对值/百分比/排名/增长率）'
            strict_label = "宽松（首次校验）"

        combined = self._compose_validation_content(extractions)
        page_type = page.get("page_type", page.get("type", ""))
        data_needs = "；".join(page.get("data_needs", []))
        source_count = len({e.get("url", "") for e in extractions if e.get("url")})

        prompt = (
            "请校验以下抓取内容的数据充分性，仅输出 JSON。\n\n"
            "【判断纪律（硬约束，必须遵守）】\n"
            "- 逐项快速判断，每项给出结论后不再回溯，禁止反复质疑已下结论的项\n"
            "- 推理总步数 ≤5 步；存在歧义时一律按\"通过\"处理，避免过度思考\n"
            "- 校验项4 交叉验证：直接用上方独立来源数与阈值比对，不得展开论证\n"
            "- 必须先输出最终 JSON，禁止在结论后继续推理或自我推翻\n\n"
            f"页面类型：{page_type}\n"
            f"数据需求：{data_needs}\n"
            f"研究深度：{research_depth}\n"
            f"独立来源数：{source_count}（当前已抓取的不同URL数量，直接用于校验项4判断）\n"
            f"校验严格度：{strict_label}\n\n"
            f"抓取内容：\n{combined}\n\n"
            "校验项（4 项，均为二元判断，是→通过）：\n"
            f"1. 证据密度：{density_rule}\n"
            f"2. 数据类型覆盖：{type_rule}\n"
            "3. 时序/对比数据：trend/data 页需有≥3时间点的时序数据；"
            "comparison/technology 页需有任意结构化表格（≥2行×≥2列即可，"
            "不要求表格对象与数据需求匹配）\n"
            f"4. 交叉验证：独立来源数≥{min_sources}（直接按上方独立来源数判断）\n\n"
            '输出 JSON：{"pass": true/false, "missing": ["缺失类别1", "缺失类别2"]}\n'
            "missing 字段必须从以下受控词汇表中选取（可多选）：\n"
            f"- {density_missing}\n"
            f"- {type_missing}\n"
            "- \"缺时序数据\"：trend/data 页未提取到 ≥3 时间点\n"
            "- \"缺对比数据\"：comparison/technology 页未提取到任意结构化表格（≥2行×≥2列）\n"
            "- \"来源不足\"：独立来源数 < " + str(min_sources) + "\n"
            "通过则输出 missing: []。\n"
            "只输出 JSON，不要输出其他内容。"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是数据充分性校验助手，只输出 JSON。",
                concurrent=True,
            )
            check = self.extract_json(result, expected_type=dict)
            if isinstance(check, dict) and not check.get("pass", False):
                raw_missing = check.get("missing", [])
                if isinstance(raw_missing, list):
                    missing = [str(m) for m in raw_missing]
                elif isinstance(raw_missing, str) and raw_missing:
                    missing = [raw_missing]
                else:
                    missing = ["未说明"]
                logger.info(
                    "[P6.1] 页面 P%d 数据不充分(strict=%s): %s",
                    page["page_number"], strict, missing,
                )
                return True, missing
            return False, []
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d 校验失败，视为缺口: %s", page["page_number"], e)
            return True, ["校验失败"]

    def _compose_validation_content(
        self,
        extractions: list[dict[str, Any]],
        max_chars: int = 2500,
    ) -> str:
        """合并抓取内容，优先保留结构化表格段落，按字符估算截断到约 2500 字（≈3000 token）。

        截断策略：
        1. 先从所有抓取内容中分离 markdown 表格段落（| ... |）和普通段落
        2. 优先拼接表格段落（结构化数据对校验更关键）
        3. 剩余预算拼接普通段落
        4. 超出 max_chars 时截断
        """
        table_parts: list[str] = []
        prose_parts: list[str] = []
        for e in extractions:
            content = e.get("content", "")
            if not content:
                continue
            lines = content.split("\n")
            current_table: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("|") and stripped.endswith("|") and "|" in stripped[1:-1]:
                    current_table.append(line)
                else:
                    if current_table:
                        table_parts.append("\n".join(current_table))
                        current_table = []
                    if stripped:
                        prose_parts.append(line)
            if current_table:
                table_parts.append("\n".join(current_table))

        result_parts: list[str] = []
        used = 0

        for part in table_parts:
            if used >= max_chars:
                break
            remain = max_chars - used
            if len(part) > remain:
                if remain > 100:
                    result_parts.append(part[:remain])
                    used = max_chars
                break
            result_parts.append(part)
            used += len(part)

        for part in prose_parts:
            if used >= max_chars:
                break
            remain = max_chars - used
            if len(part) > remain:
                if remain > 100:
                    result_parts.append(part[:remain])
                    used = max_chars
                break
            result_parts.append(part)
            used += len(part)

        return "\n---\n".join(result_parts) if result_parts else ""

    async def _backfill_fetch_single(
        self,
        page: dict[str, Any],
        page_sources: list[dict[str, Any]],
        extractions: list[dict[str, Any]],
        missing: list[str],
        research_depth: str,
    ) -> list[dict[str, Any]]:
        """单页定向回溯：补抓候选池剩余URL + 定向搜索。"""
        if not self.has_tool("fetch_webpage"):
            return extractions

        page_key = str(page["page_number"])
        existing_urls = {e.get("url") for e in extractions}

        # 从候选池取 2 个新 URL
        backfill_start = {"L1": 2, "L2": 3, "L3": 4}.get(research_depth, 3)
        backfill_end = backfill_start + 2

        extra_urls: list[str] = []
        for source in page_sources[backfill_start:backfill_end]:
            url = source.get("url", "")
            if url and url not in existing_urls:
                extra_urls.append(url)

        # 候选池不足时定向搜索
        if not extra_urls and self.has_tool("web_search"):
            targeted_queries = self._build_targeted_queries(page, missing)
            for q in targeted_queries[:1]:
                try:
                    search_result = await self.call_tool(
                        "web_search",
                        query=q,
                        search_mode="default",
                    )
                    new_urls = _URL_RE.findall(str(search_result))
                    for u in new_urls[:2]:
                        if u not in existing_urls:
                            extra_urls.append(u)
                except Exception as e:
                    if isinstance(e, AbortError):
                        raise
                    logger.warning("[P6.1] 页面 P%s 补搜失败: %s", page_key, e)

        # 抓取新 URL
        if extra_urls:
            backfill = await self._batch_fetch_single(
                page, page_sources, research_depth, extra_urls=extra_urls,
            )
            extractions.extend(backfill)

        # 回填后用严格标准二次校验
        still_gap, _ = await self._validate_page_sufficiency(
            page, extractions, research_depth, strict=True,
        )
        if still_gap:
            extractions.append({
                "url": "",
                "content": "[数据有限] 该页面研究素材不足，需降级撰写",
                "data_limited": True,
            })
            logger.info("[P6.1] 页面 P%d 回溯后仍不通过，标注 data_limited", page["page_number"])

        return extractions

    def _build_targeted_queries(self, page: dict[str, Any], missing: list[str]) -> list[str]:
        title = page.get("title", "")
        topic = title or "；".join(page.get("data_needs", [])[:1]) or page.get("type", "")
        queries: list[str] = []

        missing_set = set(missing)
        if "缺时序数据" in missing_set:
            queries.append(f"{topic} 历年数据 趋势")
        if "缺对比数据" in missing_set:
            queries.append(f"{topic} 对比 排名")
        if "数据类型单一" in missing_set:
            queries.append(f"{topic} 统计数据 报告")
        if "证据密度不足" in missing_set:
            queries.append(f"{topic} 白皮书 研究报告")
        if "来源不足" in missing_set:
            queries.append(f"{topic} 官方报告 权威数据")

        if not queries:
            base = page.get("research_queries", [])[:1]
            for q in base:
                queries.append(f"{q} 报告 白皮书 官方")
            if not queries:
                queries.append(f"{topic} 报告 白皮书 官方")

        return queries

    # ==================== 撰写阶段 ====================

    async def _write_single_page(
        self,
        page: dict[str, Any],
        extractions: list[dict[str, Any]],
        source_material: str,
        config: _ResearchConfig,
        min_words_per_page: int,
    ) -> str:
        """撰写单页研究报告，返回以 `### P{N}:` 开头的该页 Markdown 片段。"""
        page_num = page["page_number"]
        page_type = page.get("page_type", page.get("type", ""))
        title = page.get("title", "")
        data_needs = page.get("data_needs", []) or []

        extraction_summary = ""
        if extractions:
            for ext in extractions:
                extraction_summary += f"来源: {ext['url']}\n{ext['content']}\n\n"

        material_section = ""
        if source_material:
            material_limit = 8000 if config.search_mode == "no_search" else 2000
            truncated = source_material[:material_limit]
            material_section = f"\n\n用户素材（前 {material_limit} 字）：\n{truncated}"

        prompt = (
            "你是一位深度内容研究员。请撰写以下单页的研究报告段落，"
            "直接输出该页 Markdown 内容（以 `### P{N}:` 开头），"
            "不要输出报告标题（# 开头）或其他页面内容。\n\n"
            f"主题：{config.topic}\n"
            f"页面编号：P{page_num}\n"
            f"页面标题：{title}\n"
            f"页面类型：{page_type}\n"
            f"数据需求：{'; '.join(str(d) for d in data_needs)}\n"
            f"搜索模式：{config.search_mode}\n"
            f"研究深度：{config.research_depth}\n"
            f"本页最低字数：{min_words_per_page} 字\n\n"
            "### 严格格式要求（只输出本页章节，以 `### P{N}:` 开头）\n"
            "```\n"
            "### P{N}: {页面标题}\n"
            "> 页面类型：{type}\n"
            "**核心论点**：{一句结论性陈述}\n"
            "#### PPT 内容建议\n"
            "- **推荐主标题**：{headline}\n"
            "- **核心论点**（5-10条，每条附展开说明和来源引用）\n"
            "- **关键数据清单**（Markdown表格，≥5行，含数据类型列）：\n"
            "  | 数据项 | 数值/结果 | 来源 | 时间 | 数据类型 |\n"
            "- **时序数据**（trend/data/comparison/technology页必填，≥3时间点）：\n"
            "  | 指标 | {t1} | {t2} | {t3} | 来源 |\n"
            "- **对比数据**（comparison/data/technology页必填，≥2对象×≥2维度）：\n"
            "  | 对比维度 | {A} | {B} | 来源 |\n"
            "- **案例素材**：{entity} — {description} [来源名称]\n"
            "```\n\n"
            "### 写作硬规则\n"
            "1. 要点优先，核心论点附展开说明和来源引用\n"
            "2. 精准引用：事实陈述首次出现时同句内附来源标注（如 [Gartner]、[年度报告]），禁止伪引用\n"
            "3. 反空泛：禁止'市场前景广阔''发展迅速'等无来源修饰，用精确数字替代\n"
            "4. 数据完整保留：所有数据点必须出现在关键数据清单表格中\n"
            "5. 来源可识别：使用来源名称标注，禁止纯数字编号\n"
            "6. 关键数据清单每页 ≥5 条、≥2 种数据类型\n"
            "7. trend/data/comparison/technology 页必须有时序数据（≥3时间点）和对比数据（≥2对象×≥2维度）\n"
            f"8. 本页 ≥{min_words_per_page} 字\n"
            f"{'9. no_search 模式：数据有限页面标注「数据有限，基于用户素材」，关键数据清单 ≥3 行即可' if config.search_mode == 'no_search' else ''}\n\n"
            "### 思考预算（强制约束）\n"
            "- 内容规划**上限 500 字思考**：用要点列表描述核心论点和数据需求即可，禁止逐句推演\n"
            "- **禁止做「读取→验证→发现不匹配→重新解释→再验证」循环**；"
            "若来源数据与页面数据需求存在轻微不匹配，直接采用来源原文，不得反复解释\n"
            "- 数据整合一次过：来源不足时直接标注 data_limited，禁止反复补搜验证\n"
            "- 思考阶段产出 ≤800 tokens 即可开始写 Markdown；超过此量说明陷入过度研究，"
            "应立即停止思考并输出内容\n\n"
            f"### 抓取内容\n{extraction_summary}"
            f"{material_section}"
        )

        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是深度内容研究员，直接输出该页的 Markdown 内容，不要输出解释。",
                concurrent=True,
            )
            return result.strip() if result else ""
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d 撰写LLM失败: %s", page_num, e)
            return ""

    def _build_fallback_page_section(self, page: dict[str, Any]) -> str:
        """单页撰写失败时的兜底骨架。"""
        page_num = page.get("page_number", "")
        title = page.get("title", "")
        page_type = page.get("page_type", page.get("type", ""))
        return (
            f"### P{page_num}: {title}\n"
            f"> 页面类型：{page_type}\n"
            "**核心论点**：[撰写失败，待补充]\n"
            "#### PPT 内容建议\n"
            f"- **推荐主标题**：{title}\n"
            "- **核心论点**：待补充\n"
            "- **关键数据清单**：待补充\n"
        )

    def _build_no_data_page_section(
        self,
        page: dict[str, Any],
        topic: str,
        search_mode: str,
        research_depth: str,
    ) -> str:
        """无研究数据降级模式：生成单页 stub（不含全局 header）。"""
        page_num = page.get("page_number", "")
        title = page.get("title", "")
        page_type = page.get("page_type", page.get("type", ""))
        data_needs = page.get("data_needs", []) or []
        queries = page.get("research_queries", []) or []

        lines: list[str] = []
        lines.append(f"### P{page_num}: {title}")
        lines.append(f"> 页面类型：{page_type}")
        lines.append("")
        lines.append("**核心论点**：[数据有限，基于大纲规划]")
        lines.append("")
        lines.append("#### PPT 内容建议")
        lines.append(f"- **推荐主标题**：{title}")
        lines.append("- **核心论点**：")
        if queries:
            for q in queries[:5]:
                lines.append(f"  - {q}（待补充数据）")
        else:
            lines.append("  - 待补充")
        lines.append("- **关键数据清单**（无研究数据，待后续补充）：")
        lines.append("  | 数据项 | 数值/结果 | 来源 | 时间 | 数据类型 |")
        lines.append("  | --- | --- | --- | --- | --- |")
        if data_needs:
            for need in data_needs[:3]:
                lines.append(f"  | {need} | 待补充 | 待补充 | 待补充 | 待补充 |")
        else:
            lines.append("  | 待补充 | 待补充 | 待补充 | 待补充 | 待补充 |")
        lines.append("- **数据有限**，本页未执行外部搜索，亦无用户素材。")
        lines.append("")
        return "\n".join(lines)

    async def _validate_single_page(
        self,
        page: dict[str, Any],
        section: str,
        config: _ResearchConfig,
        min_words_per_page: int,
    ) -> bool:
        """单页校验（7 项，LLM 判断）。LLM 异常保守判通过。"""
        if not section:
            return False

        page_num = page["page_number"]
        page_type = page.get("page_type", page.get("type", ""))

        # 程序化前置检查：页码对齐
        header_match = _PAGE_HEADER_RE.search(section)
        if not header_match or int(header_match.group(1)) != int(page_num):
            logger.warning(
                "[P6.1] 页面 P%d 校验失败：页码不对齐（section 未以 ### P%d: 开头）",
                page_num, page_num,
            )
            return False

        # 程序化前置检查：必须包含 #### PPT 内容建议
        if "#### PPT 内容建议" not in section:
            logger.warning("[P6.1] 页面 P%d 校验失败：缺少 #### PPT 内容建议", page_num)
            return False

        # 程序化前置检查：字数
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", section))
        min_words_80 = int(min_words_per_page * 0.8)
        if chinese_chars < min_words_80:
            logger.warning(
                "[P6.1] 页面 P%d 校验失败：字数不足 %d < %d",
                page_num, chinese_chars, min_words_80,
            )
            return False

        # LLM 校验剩余项
        data_table_min = 5 if config.search_mode != "no_search" else 3

        prompt = (
            "请对以下单页研究报告内容做 7 项产物验证，仅输出 JSON。\n\n"
            f"页面编号：P{page_num}\n"
            f"页面类型：{page_type}\n"
            f"搜索模式：{config.search_mode}\n"
            f"研究深度：{config.research_depth}\n"
            f"本页最低字数：{min_words_per_page}（已通过程序化字数检查）\n"
            f"关键数据清单最少行数：{data_table_min}\n\n"
            "【判断纪律】\n"
            "- 逐项快速判断，每项给出结论后不再回溯，禁止反复质疑\n"
            "- 存在歧义时一律按\"通过\"处理，避免过度思考\n"
            "- 简洁推理，直接给结论\n\n"
            "校验项（7 项，均为二元判断，是→通过）：\n"
            "1. 页面结构：`### P{N}:` 标题 + `#### PPT 内容建议`（已程序化检查通过）\n"
            f"2. PPT 内容建议：推荐主标题 + 核心论点(5-10条) + 关键数据清单表格(≥{data_table_min}行) + 时序数据表(必要时) + 对比数据表(必要时) + 案例素材\n"
            "3. 数据表格格式：关键数据清单含'数据类型'列；时序/对比为专用表格非散文\n"
            "4. 引用规范：每页 ≥3 个来源标注（来源名称，非数字编号）\n"
            "5. 反空泛：无模糊修饰、无占位文本\n"
            "6. 字数达标：本页中文字数 ≥ min_words_per_page × 80%（已程序化检查通过）\n"
            "7. 素材充实度：核心论点有展开说明和来源标注；案例含具体实体名称\n\n"
            '输出 JSON：{"pass": true/false, "reason": "不通过原因简述，通过则为空"}\n'
            "只输出 JSON，不要输出其他内容。\n\n"
            f"待校验内容：\n{section}"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是研究报告校验助手，只输出 JSON。",
                concurrent=True,
            )
            check = self.extract_json(result, expected_type=dict)
            if isinstance(check, dict):
                passed = bool(check.get("pass", False))
                if not passed:
                    logger.info(
                        "[P6.1] 页面 P%d 校验未通过: %s",
                        page_num, check.get("reason", ""),
                    )
                return passed
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d 校验LLM失败，保守视为通过: %s", page_num, e)
            return True

    async def _write_file(self, path: str, content: str) -> bool:
        if not path:
            return False
        if not self.has_tool("write_file"):
            logger.warning("[P6.1] write_file 工具不可用，无法写入文件 %s", path)
            return False
        try:
            await self.call_tool("write_file", file_path=path, content=content)
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 写入文件失败 %s: %s", path, e)
            return False

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        research_paths = result.get("research_paths", {})
        ok = bool(research_paths)
        yield {
            **result,
            "node": self.plan_name,
            "status": "ok" if ok else "error",
            "message": f"研究完成，已落盘 {len(research_paths)} 个 research-P{{N}}.md" if ok else "研究失败",
        }


class DeepResearchNode(PlanNode):
    """P6 — 深度研究根节点：编排预处理 + per-page 并发闭环。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p6_deep_research",
            instruction=(
                "## P6 深度研究（根节点）\n"
                "\n"
                "### 前置条件\n"
                "- `{output_dir}/outline.md` 存在且非空\n"
                "- `read_file` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `output_dir`: 工作目录（读 outline.md，写 research-P{N}.md）\n"
                "- `search_mode`: no_search / auto / force_search\n"
                "- `research_depth`: L1 / L2 / L3\n"
                "- `source_material`: 用户素材（可空）\n"
                "- `topic`: PPT 主题\n"
                "\n"
                "### 输出\n"
                "```json\n"
                '{"research_paths": {"1": "{output_dir}/research-P1.md", "2": "{output_dir}/research-P2.md"}}\n'
                "```\n"
                "\n"
                "### 执行流程（两阶段串行）\n"
                "1. 调用 P6.0 PrepareNode → 全局预处理（解析 outline、判定搜索策略、素材覆盖度评估、计算每页最低字数）\n"
                "2. 调用 P6.1 PageWorkerNode → per-page 并发闭环（搜索→评分→补搜→抓取→ghost→校验→回溯→撰写→按页校验→失败重写）\n"
                "   - N 页 asyncio.gather 并发，单页内各阶段串行\n"
                "   - LLM 并发度由框架 semaphore 控制\n"
                "\n"
                "### 子节点调用与数据流\n"
                "```\n"
                "P6（根节点）\n"
                "  inputs: output_dir, search_mode, research_depth, source_material, topic\n"
                "    │\n"
                "    ▼\n"
                "  PrepareNode (P6.0)\n"
                "    ├─ 输入: output_dir, search_mode, research_depth, source_material, topic\n"
                "    └─ 输出: prepare_status, pages, searched_urls, need_search,\n"
                "            no_data_fallback, page_coverage, min_words_per_page\n"
                "    │\n"
                "    ▼\n"
                "  PageWorkerNode (P6.1)\n"
                "    ├─ 输入: pages, searched_urls, need_search, no_data_fallback,\n"
                "    │       page_coverage, min_words_per_page, source_material,\n"
                "    │       search_mode, research_depth, topic, output_dir\n"
                "    └─ 输出: research_paths（{页码: research-P{N}.md 路径}）\n"
                "```\n"
                "\n"
                "### 失败兜底\n"
                "- outline.md 为空/不存在：返回空 research_paths\n"
                "- P6.0 prepare_status=failed：返回空 research_paths，不进入 P6.1\n"
                "- P6.1 内部 per-page 异常：单页降级为兜底骨架，不阻塞其他页\n"
                "- write_file 不可用/失败：该页不写入 research_paths\n"
            ),
            sub_plans=[
                PrepareNode(),
                PageWorkerNode(),
            ],
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        # P6.0 预处理
        prep_result = await self.execute_subplan(self.sub_plans[0], inputs)
        if not isinstance(prep_result, dict) or prep_result.get("prepare_status") != "ok":
            logger.error("[P6] P6.0 预处理失败，终止深度研究")
            return {"research_paths": {}}

        # 合并预处理结果到 inputs
        worker_inputs = {**inputs, **prep_result}

        # P6.1 per-page 闭环
        worker_result = await self.execute_subplan(self.sub_plans[1], worker_inputs)
        if isinstance(worker_result, dict):
            research_paths = worker_result.get("research_paths", {})
        else:
            logger.error("[P6] P6.1 执行异常")
            research_paths = {}

        logger.info("[P6] 深度研究完成，已落盘 %d 个 research-P{N}.md", len(research_paths))
        return {
            "research_paths": research_paths,
            "__artifact__": {
                "files": [{"path": p, "desc": "深度研究报告"} for p in research_paths.values()] if research_paths else [],
            },
        }

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        research_paths = result.get("research_paths", {})
        ok = bool(research_paths)
        yield {
            **result,
            "node": self.plan_name,
            "status": "ok" if ok else "error",
            "message": f"深度研究完成，已落盘 {len(research_paths)} 个 research-P{{N}}.md" if ok else "深度研究失败",
        }
