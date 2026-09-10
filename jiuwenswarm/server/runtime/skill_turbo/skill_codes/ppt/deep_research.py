from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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


from jiuwenswarm.server.runtime.skill_turbo.plan_node import (
    AbortError,
    DisableThinkingMixin,
    PlanNode,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import (
    CONTENT_BRANCH_MATERIAL,
    CONTENT_BRANCH_RESEARCH,
    PptCommon,
    pipeline_role_boundary,
    research_evidence_limited_mentioned,
)

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

# 与 pptx-craft validate-research / research-output-template 对齐的单页落盘骨架
_RESEARCH_PAGE_SKELETON = """### P{N}: {页面标题}
> 页面类型：{type} | 研究级别：{L1|L2|L3}
**核心论点**：{≤60字一句结论，含1个关键数字} [{来源名称}]

#### PPT 内容建议
- **推荐主标题**：{headline}
- **主轴**（必填 | ≥4行×≥3列；证据受限时 ≥3×2 | 单元格≤12字）：
  | {轴名} | {对象1} | {对象2} | {对象3} | 来源 |
  | --- | --- | --- | --- | --- |
  （trend→时间轴；comparison/technology→对象轴；data→指标轴；case→实体轴。
  时序/对比数据必须写入本主轴表，禁止再用「时序数据」「对比数据」作顶层槽位名。）
- **关键数据**（5-8条，no_search/证据受限≥3条，≥2种数据类型）：
  | 数据项 | 数值 | 单位/口径 | 归属对象 | 来源 | 时间 | 数据类型 |
  | --- | --- | --- | --- | --- | --- | --- |
- **上屏要点**（4-6条 | 每条≤40字 | 必须自带数字或实体名 | 不写展开说明）：
  1. {短句} [{来源名称}]
- **案例**（2-3条 | 每条≤50字）：
  - {实体} — {含数字的结果} [{来源名称}]

#### 来源留痕
| 名称 | URL | 类别 | 评分 |
| --- | --- | --- | --- |
| {与内联[名称]完全同名} | {完整URL或用户文件名} | {权威机构/标准|企业官方一手|学术论文|行业研报|权威媒体|行业媒体} | {A+~B} |
"""

_RESEARCH_WRITE_HARD_RULES = (
    "### 写作硬规则\n"
    "1. 顶部一句**核心论点**即可；可上屏短句写入**上屏要点**（每条≤40字），禁止把「核心论点」写成 5-10 条长展开列表\n"
    "2. 必须包含槽位：**主轴** / **关键数据** / **上屏要点** / **案例**，以及 `#### 来源留痕`\n"
    "3. 禁止输出顶层槽位名「关键数据清单」「时序数据」「对比数据」「案例素材」——时序/对比信息并入**主轴**表\n"
    "4. 精准引用：事实陈述同句附来源名（如 [Gartner]），且名称须在来源留痕表可解析；禁止伪引用\n"
    "5. 反空泛：用精确数字替代模糊修饰；禁止 TODO/xxx 等占位文本\n"
    "6. 数据完整保留：所有数据点必须出现在**关键数据**表中\n"
    "7. 来源可识别：使用来源名称标注，禁止纯数字编号\n"
)

_RESEARCH_PREWRITE_CHECKLIST = (
    "### 落笔前自检（写入主轴表前必做，与 validate-research 一致）\n"
    "1. 数主轴表行列：标准 ≥4 行 × ≥3 列；页内标注数据有限/证据受限时 ≥3×2\n"
    "2. 禁止提交「行够列不够」（如 5×2）；不足则补对比对象/时间点列，或合并行\n"
    "3. 时序/对比数据必须写入主轴表，禁止 resurrect 旧顶层槽位名\n"
    "4. 单元格 ≤12 字；能量化用数字，不用「较强/支持」等空泛词\n"
)

_FORBIDDEN_RESEARCH_TOP_SLOTS = ("关键数据清单", "时序数据", "对比数据", "案例素材")
_AXIS_TABLE_QUOTA = (4, 3)
_AXIS_TABLE_QUOTA_LIMITED = (3, 2)
_AXIS_SECTION_RE = re.compile(
    r"[-*]\s*\*\*主轴\*\*[^\n]*\n(.*?)(?=\n[-*]\s*\*\*|\n####|\Z)",
    re.DOTALL,
)
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$", re.MULTILINE)

# 新 skill：三级字数统一目标约 1000 CJK；程序化检查改为 warn，不硬失败
_TARGET_WORDS_PER_PAGE = 1000
_WORD_COUNT_MAP = {"L1": 1000, "L2": 1000, "L3": 1000}
_WORD_COUNT_NO_SEARCH_MAP = {"L1": 800, "L2": 800, "L3": 800}

_PAGE_HEADER_RE = re.compile(r"^###\s*P(\d+)\s*[:：]", re.MULTILINE)
_TITLE_FIELD_RE = re.compile(r"\*\*标题\*\*[：:]\s*(.+)", re.IGNORECASE)
_DATA_NEED_RE = re.compile(r"\*\*数据需求\*\*[：:]\s*(.+)", re.IGNORECASE)
_PAGE_TYPE_RE = re.compile(r"\*\*类型\*\*[：:]\s*(\w+)", re.IGNORECASE)
_RESEARCH_QUERY_HEADER_RE = re.compile(r"\*\*研究查询\*\*[：:]\s*", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(.+)$", re.MULTILINE)
_NEXT_FIELD_RE = re.compile(r"\*\*[^*]+\*\*[：:]")
_SEARCHED_SOURCES_RE = re.compile(r"^##\s*已搜索来源", re.MULTILINE)
_URL_RE = re.compile(r"https?://[^\s\])>\"']+")
_MIN_NAMED_CITATIONS = 3
# 来源名称标注 [Name]，排除纯数字编号
_NAMED_CITATION_RE = re.compile(r"\[([^\[\]]{1,40})\]")
_HOST_FROM_URL_RE = re.compile(r"^https?://([^/\s?#:]+)", re.IGNORECASE)


def count_named_citations(section: str) -> int:
    """Count unique non-numeric ``[source]`` labels in a research section."""
    names: set[str] = set()
    for m in _NAMED_CITATION_RE.finditer(section or ""):
        name = m.group(1).strip()
        if not name or name.isdigit():
            continue
        names.add(name)
    return len(names)


def source_label_from_url(url: str) -> str:
    """Derive a short label from an existing URL host (no invention)."""
    m = _HOST_FROM_URL_RE.match((url or "").strip())
    if not m:
        return ""
    host = m.group(1).lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def enrich_section_citations(
    section: str,
    extractions: list[dict[str, Any]] | None,
    *,
    min_citations: int = _MIN_NAMED_CITATIONS,
) -> str:
    """Append real extraction hosts as ``[label]`` until min_citations if needed."""
    if not section or count_named_citations(section) >= min_citations:
        return section

    existing = {
        m.group(1).strip()
        for m in _NAMED_CITATION_RE.finditer(section)
        if m.group(1).strip() and not m.group(1).strip().isdigit()
    }
    need = min_citations - len(existing)
    labels: list[str] = []
    for item in extractions or []:
        if item.get("data_limited"):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        label = source_label_from_url(url)
        if not label or label in existing or label in labels:
            continue
        labels.append(label)
        if len(labels) >= need:
            break
    if not labels:
        return section
    tags = " ".join(f"[{lb}]" for lb in labels)
    return section.rstrip() + f"\n- **来源标注**：{tags}\n"


def _parse_markdown_table_rows(body: str) -> list[tuple[list[str], list[list[str]]]]:
    """Parse markdown pipe tables into (headers, data_rows) list."""
    lines = body.splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not _TABLE_ROW_RE.match(line) or _TABLE_SEP_RE.match(line):
            idx += 1
            continue
        block = [line]
        idx += 1
        while idx < len(lines) and _TABLE_ROW_RE.match(lines[idx]):
            block.append(lines[idx])
            idx += 1
        if len(block) < 2 or not _TABLE_SEP_RE.match(block[1]):
            continue
        headers = [c.strip() for c in block[0].strip().strip("|").split("|")]
        rows: list[list[str]] = []
        for row_line in block[2:]:
            if _TABLE_SEP_RE.match(row_line):
                continue
            rows.append([c.strip() for c in row_line.strip().strip("|").split("|")])
        if headers:
            tables.append((headers, rows))
    return tables


def _extract_axis_section_body(section: str) -> str:
    match = _AXIS_SECTION_RE.search(section)
    return match.group(1) if match else ""


def _measure_axis_table(section: str) -> tuple[int, int]:
    """Return best (rows, cols) in 主轴 section, aligned with validate-research measureAxisTable."""
    body = _extract_axis_section_body(section)
    best_rows, best_cols = 0, 0
    for headers, rows in _parse_markdown_table_rows(body):
        last_header = headers[-1] if headers else ""
        has_source_col = bool(re.search(r"来源|出处", last_header))
        cols = max(len(headers) - 1 - (1 if has_source_col else 0), 0)
        row_count = len(rows)
        if row_count * cols > best_rows * best_cols:
            best_rows, best_cols = row_count, cols
    return best_rows, best_cols


def _axis_table_quota(section: str) -> tuple[int, int]:
    if research_evidence_limited_mentioned(section):
        return _AXIS_TABLE_QUOTA_LIMITED
    return _AXIS_TABLE_QUOTA


def _precheck_research_section(section: str, config: "_ResearchConfig") -> list[str]:
    """Rule precheck before validate-research CLI (axis table + forbidden slots)."""
    issues: list[str] = []
    if not section.strip():
        return ["empty_section"]
    for slot in _FORBIDDEN_RESEARCH_TOP_SLOTS:
        if re.search(rf"^\s*[-*]\s*\*\*{re.escape(slot)}\*\*", section, re.MULTILINE):
            issues.append(f"forbidden_top_slot:{slot}")
    if "主轴" not in section:
        issues.append("missing:主轴")
        return issues
    need_rows, need_cols = _axis_table_quota(section)
    rows, cols = _measure_axis_table(section)
    if rows < need_rows or cols < need_cols:
        issues.append(f"axis_table_too_small:{rows}x{cols}<{need_rows}x{need_cols}")
    if config.search_mode == "no_search" and not research_evidence_limited_mentioned(section):
        issues.append("missing_evidence_limited_annotation")
    return issues


def _validate_reason_rewrite_hints(reasons: list[str]) -> str:
    """Map validate-research reason tags to skill-aligned rewrite instructions."""
    hints: list[str] = []
    for raw in reasons:
        reason = str(raw).strip()
        if not reason:
            continue
        if reason.startswith("axis_table_too_small:"):
            match = re.search(
                r"axis_table_too_small:(\d+)x(\d+)<(\d+)x(\d+)",
                reason,
            )
            if match:
                hints.append(
                    f"主轴表当前 {match.group(1)}×{match.group(2)}，"
                    f"需补至 ≥{match.group(3)} 行 × ≥{match.group(4)} 列"
                    "（优先补列：增加对比对象/时间点；禁止行够列不够）"
                )
            else:
                hints.append("主轴表规模不足：标准 ≥4×3，证据受限 ≥3×2")
        elif reason.startswith("missing:"):
            hints.append(f"补全槽位：{reason.split(':', 1)[1]}")
        elif reason.startswith("missing_section:"):
            hints.append(f"补全章节：{reason.split(':', 1)[1]}")
        elif reason.startswith("data_rows_below:"):
            hints.append(f"关键数据行数不足：{reason.split(':', 1)[1]}")
        elif reason == "missing_data_type_column":
            hints.append("关键数据表须含「数据类型」列")
        elif reason.startswith("forbidden_top_slot:"):
            hints.append(
                f"删除顶层槽位「{reason.split(':', 1)[1]}」，时序/对比并入主轴表"
            )
        elif reason.startswith("missing_evidence_limited_annotation"):
            hints.append("no_search 页须标注「数据有限，基于用户素材」")
        else:
            hints.append(reason)
    return "\n".join(f"- {h}" for h in hints)


@dataclass
class _ResearchConfig:
    """封装撰写所需配置参数，避免函数签名过长。"""
    search_mode: str
    research_depth: str
    topic: str
    no_data_fallback: bool = False
    writer_profile: str = "deep"  # material | deep
    min_citations: int = 3
    documents_excerpt: str = ""


class PrepareNode(DisableThinkingMixin, PlanNode):
    """P6.0 — 全局预处理：解析 outline、判定搜索策略、素材覆盖度评估、计算每页最低字数。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p6_0_prepare",
            instruction=(
                "## P6.0 全局预处理\n"
                "\n"
                "### 职责\n"
                "1. 读取 `{output_dir}/outline.md`\n"
                "2. LLM 解析需要研究的页面（page_number / title / page_type / research_queries / data_needs），失败时正则回退\n"
                "3. 提取 outline 中已搜索的 URL（`searched_urls`，跳过重复搜索）\n"
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
                "6. 计算每页最低字数 `min_words_per_page`（总最低字数 ÷ 页数，下限 200）\n"
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
                "- LLM 解析页面失败：正则回退\n"
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

        pages = await self._parse_outline_pages(outline_text)
        if not pages:
            logger.warning("[P6.0] 未从 outline.md 中解析到需要研究的页面")
            return {"prepare_status": "failed"}

        PptCommon.ensure_phase1_defaults(inputs)
        branch = PptCommon.ensure_content_branch(inputs)
        search_mode = str(inputs.get("search_mode") or "auto").strip()
        research_depth = str(inputs.get("research_depth") or "L2").strip()
        topic = inputs.get("topic", "")
        searched_urls = self._extract_searched_urls(outline_text)
        min_citations = PptCommon.min_citations_for_depth(research_depth)
        inputs["min_citations"] = min_citations

        source_material = ""
        if branch == CONTENT_BRANCH_MATERIAL:
            source_material = await PptCommon.load_source_material(
                self,
                inputs,
                max_chars=8000,
                error_type=RuntimeError,
            )
            writer_profile = "material"
        else:
            # research 分支不以用户文档为主数据源
            source_material = ""
            inputs["source_material"] = ""
            writer_profile = "deep"

        page_coverage: dict[str, dict[str, Any]] = {}
        pages_need_search: dict[str, bool] = {}

        if branch == CONTENT_BRANCH_RESEARCH:
            # 有 ✅ 就必须逐页研究；不得因「素材充实」跳过
            need_search = search_mode != "no_search"
            for page in pages:
                pages_need_search[str(page["page_number"])] = need_search
            no_data_fallback = search_mode == "no_search" and not source_material
        else:
            # material：按 search_mode + 覆盖度决定哪些页快搜
            if search_mode == "no_search":
                need_search = False
                for page in pages:
                    pages_need_search[str(page["page_number"])] = False
            elif search_mode == "force_search":
                need_search = True
                for page in pages:
                    pages_need_search[str(page["page_number"])] = True
            else:
                # auto：评估覆盖度，仅缺口页搜
                if source_material:
                    page_coverage = await self._evaluate_page_coverage(pages, source_material)
                need_search = False
                for page in pages:
                    key = str(page["page_number"])
                    cov = str((page_coverage.get(key) or {}).get("coverage") or "uncovered")
                    page_need = cov != "covered"
                    pages_need_search[key] = page_need
                    if page_need:
                        need_search = True
            no_data_fallback = (
                search_mode == "no_search"
                and (not source_material or len(source_material.strip()) < 200)
            )

        if no_data_fallback:
            logger.warning(
                "[P6.0] 跳过搜索且无用户素材，进入无研究数据降级撰写 (branch=%s search_mode=%s)",
                branch,
                search_mode,
            )

        # 新 skill：统一约 1000 CJK / 页；no_search 约 800；字数仅 warn
        if search_mode == "no_search":
            min_words_per_page = _WORD_COUNT_NO_SEARCH_MAP.get(research_depth, 800)
        else:
            min_words_per_page = _WORD_COUNT_MAP.get(research_depth, _TARGET_WORDS_PER_PAGE)

        documents_excerpt = ""
        if branch == CONTENT_BRANCH_MATERIAL and output_dir:
            documents_excerpt = await self._load_documents_excerpt(output_dir)

        logger.info(
            "[P6.0] 预处理完成 branch=%s writer=%s pages=%d need_search=%s "
            "no_data_fallback=%s min_words=%d min_citations=%d",
            branch,
            writer_profile,
            len(pages),
            need_search,
            no_data_fallback,
            min_words_per_page,
            min_citations,
        )

        return {
            "prepare_status": "ok",
            "pages": pages,
            "searched_urls": searched_urls,
            "need_search": need_search,
            "pages_need_search": pages_need_search,
            "no_data_fallback": no_data_fallback,
            "page_coverage": page_coverage,
            "min_words_per_page": min_words_per_page,
            "source_material": source_material,
            "search_mode": search_mode,
            "research_depth": research_depth,
            "topic": topic,
            "output_dir": output_dir,
            "content_branch": branch,
            "writer_profile": writer_profile,
            "min_citations": min_citations,
            "documents_excerpt": documents_excerpt,
            "outline_path": outline_path,
        }

    async def _load_documents_excerpt(self, output_dir: str, *, max_chars: int = 6000) -> str:
        """读取 output_dir/documents/*.md 供 material data_ref（截断）。"""
        from pathlib import Path

        docs_dir = Path(str(output_dir)) / "documents"
        if not docs_dir.is_dir():
            return ""
        parts: list[str] = []
        total = 0
        try:
            files = sorted(
                p for p in docs_dir.iterdir()
                if p.is_file() and p.suffix == ".md"
            )
        except OSError:
            return ""
        for path in files:
            text = await self._read_file(str(path))
            if not text.strip():
                continue
            chunk = f"## 文件：{path.name}\n{text.strip()}\n"
            if total + len(chunk) > max_chars:
                remain = max_chars - total
                if remain > 200:
                    parts.append(chunk[:remain] + "\n...(截断)\n")
                break
            parts.append(chunk)
            total += len(chunk)
        return "\n".join(parts)

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

    async def _parse_outline_pages(self, outline_text: str) -> list[dict[str, Any]]:
        base_prompt = (
            "你是一个大纲解析助手。请从以下 PPT 大纲中提取所有研究需求为 ✅ 的页面信息。\n"
            "对每个页面，提取：\n"
            "- page_number: 页码（整数）\n"
            "- title: 页面标题\n"
            "- page_type: 页面类型（如 trend/data/case/comparison/technology 等）\n"
            "- research_queries: 研究查询列表（字符串数组）\n"
            "- data_needs: 数据需求列表（字符串数组）\n\n"
            "以 JSON 数组格式输出，不要输出其他内容。如果没有需要研究的页面，输出空数组 []。\n"
            "重要：JSON 字符串值中如果包含双引号，必须用 \\\" 转义。\n\n"
            f"大纲内容：\n{outline_text}"
        )
        max_attempts = 3
        last_error: str | None = None
        for attempt in range(max_attempts):
            prompt = base_prompt
            if attempt > 0 and last_error:
                prompt = (
                    f"{base_prompt}\n\n"
                    f"上次输出的 JSON 存在格式错误：\n{last_error}\n"
                    "请修正格式后重新输出完整的 JSON 数组。"
                )
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="只输出 JSON 数组，不要输出其他内容",
            )
            try:
                pages = self.extract_json(result, expected_type=list)
                if isinstance(pages, list) and pages:
                    return pages
                if isinstance(pages, list) and not pages:
                    logger.warning("[P6.0] LLM 返回空列表（第%d次）", attempt + 1)
                    last_error = "返回了空数组，但大纲中存在 ✅ 页面"
            except (ValueError, TypeError) as e:
                last_error = str(e)
                logger.warning("[P6.0] LLM 解析大纲页面失败（第%d次）：%s", attempt + 1, last_error)
        logger.warning("[P6.0] LLM 解析大纲页面失败（%d次重试均失败），尝试正则回退", max_attempts)
        return self._parse_outline_pages_fallback(outline_text)

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
        return [u for u in _URL_RE.findall(section)]

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


class PageWorkerNode(DisableThinkingMixin, PlanNode):
    """P6.1 — per-page 并发闭环（Phase 2）。

    Dispatch 映射（见 ppt_common.DISPATCH_ACCEL_NOTES）：全页 asyncio.gather ≈ 整轮派发；
    页内校验失败重写 1 次是 accel 例外（非 skill retry_queue 按轮重试）。
    """

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
                "- `searched_urls`: outline 中已搜索的 URL（来自 P6.0）\n"
                "- `min_words_per_page`: 每页最低字数（来自 P6.0）\n"
                "- `source_material` / `search_mode` / `research_depth` / `topic` / `output_dir`（透传）\n"
                "\n"
                "### 输出\n"
                "- `research_paths`: {页码: research-P{N}.md 文件路径} 字典\n"
                "\n"
                "### 执行流程（per-page 闭环，N 页 asyncio.gather 并发）\n"
                "Dispatch：gather ≈ 整轮派发；页内失败重写 1 次 = accel 例外（非按轮 retry_queue）。\n"
                "对每一页独立执行：\n"
                "\n"
                "#### 阶段 1：搜索（need_search=True 时）\n"
                "a. 按覆盖度生成搜索查询：\n"
                "   - covered 页：仅 1 次验证性搜索\n"
                "   - partial 页：仅搜索未覆盖的数据需求 + 1 次验证性搜索\n"
                "   - uncovered 页：完整搜索（所有 research_queries + data_needs 综合查询）\n"
                "b. 并行搜索所有查询\n"
                "c. 来源评分筛选（A+/A/A-/B+/B/C，C 级丢弃）\n"
                "d. 已有 URL 合并（searched_urls 直接加入候选池）\n"
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
                "e. 回溯后取消严格二次充分性 LLM：直接标注 data_limited: true\n"
                "f. （保留）仍不通过场景统一走 data_limited 降级撰写\n"
                "\n"
                "#### 阶段 3：撰写\n"
                "a. LLM 撰写单页研究报告（以 `### P{N}:` 开头，不输出报告标题）\n"
                "b. 按页规则化校验（不调 LLM）：\n"
                "   1. 页面结构：`### P{N}:` + `#### PPT 内容建议` + `#### 来源留痕` + 必填槽位\n"
                "   2. 程序化预检：主轴表规模 / 禁止顶层槽位 / 证据受限标注\n"
                "   3. 字数：本页中文字数 ≥ min_words_per_page × 80%（仅 warn）\n"
                "   4. 规则补引用：按抓取 URL host 补足命名引用至最低数\n"
                "c. 仅结构/预检失败 → 重写1次（仅本页，覆盖当前版本，不再二次校验）\n"
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
                f"{_RESEARCH_PAGE_SKELETON}"
                "```\n"
                "\n"
                "### 写作硬规则\n"
                "1. 顶部一句核心论点；上屏短句写入上屏要点（≤40字）\n"
                "2. 精准引用：事实陈述首次出现时同句内附来源标注，并在来源留痕登记\n"
                "3. 反空泛：禁止无来源修饰与占位文本\n"
                "4. 数据完整保留：所有数据点必须出现在关键数据表中；时序/对比写入主轴\n"
                "5. 来源可识别：使用来源名称标注，禁止纯数字编号\n"
                "6. 关键数据每页 ≥5 条（no_search ≥3 条）、≥2 种数据类型；主轴 ≥4×3（证据受限 ≥3×2）\n"
                "7. 数据有限页面：显式标注'数据有限，基于用户素材'或'数据有限，基于 N 个来源'\n"
                "\n"
                "### no_search 模式调整\n"
                "| 项目 | 搜索模式 | no_search 模式 |\n"
                "|---|---|---|\n"
                "| 数据来源 | 外部研究为主 | 用户素材为主 |\n"
                "| 来源标注 | [机构名] | [资料名] |\n"
                "| 关键数据 | ≥5 条 | ≥3 条 |\n"
                "| 主轴 | ≥4×3 | 可降为 ≥3×2（须标注数据有限） |\n"
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
                "- 回溯后：跳过严格二次充分性 LLM，直接标注 data_limited: true，传递给撰写阶段降级处理\n"
                "- 撰写LLM失败：使用兜底骨架\n"
                "- 按页规则化校验失败：仅结构/预检失败触发重写；字数不足仅 warn\n"
                "- 重写LLM失败：保留当前版本\n"
                "- write_file 不可用/失败：记录错误日志，该页不写入 research_paths\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        pages = inputs.get("pages", [])
        need_search = inputs.get("need_search", False)
        pages_need_search = inputs.get("pages_need_search") or {}
        no_data_fallback = inputs.get("no_data_fallback", False)
        page_coverage = inputs.get("page_coverage", {})
        searched_urls = inputs.get("searched_urls", [])
        source_material = inputs.get("source_material", "")
        search_mode = inputs.get("search_mode", "auto")
        research_depth = inputs.get("research_depth", "L2")
        topic = inputs.get("topic", "")
        output_dir = inputs.get("output_dir", "")
        min_words_per_page = int(inputs.get("min_words_per_page") or 200)
        writer_profile = str(inputs.get("writer_profile") or "deep").strip() or "deep"
        min_citations = int(
            inputs.get("min_citations")
            or PptCommon.min_citations_for_depth(research_depth)
        )
        documents_excerpt = str(inputs.get("documents_excerpt") or "")

        config = _ResearchConfig(
            search_mode=search_mode,
            research_depth=research_depth,
            topic=topic,
            no_data_fallback=no_data_fallback,
            writer_profile=writer_profile,
            min_citations=min_citations,
            documents_excerpt=documents_excerpt,
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
                need_search=bool(
                    pages_need_search.get(str(page["page_number"]), need_search)
                ),
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

        # validate-research 全量门禁；失败则对 invalid 页重写 1 次后再校验
        pptx_root = str(inputs.get("pptx_root") or "").strip()
        outline_path = str(inputs.get("outline_path") or "").strip()
        validation_inputs_ready = all((research_paths, output_dir, pptx_root, outline_path))
        if validation_inputs_ready:
            validation_ok, invalid_pages, page_reasons = await self._run_validate_research(
                output_dir,
                pptx_root,
                outline_path,
                research_depth,
                min_citations=min_citations,
                require_min_citations=(writer_profile == "deep"),
            )
            if not validation_ok and invalid_pages:
                logger.warning(
                    "[P6.1] validate-research 未通过 invalid=%s，对失败页重写 1 次",
                    invalid_pages,
                )
                page_by_num = {int(p["page_number"]): p for p in pages}
                rewrite_tasks = []
                rewrite_pages: list[int] = []
                for page_num in invalid_pages:
                    page = page_by_num.get(int(page_num))
                    if not page:
                        continue
                    reasons = page_reasons.get(int(page_num), [])
                    rewrite_pages.append(int(page_num))
                    rewrite_tasks.append(
                        self._rewrite_page_for_validate(
                            page=page,
                            output_dir=output_dir,
                            config=config,
                            min_words_per_page=min_words_per_page,
                            reasons=reasons,
                        )
                    )
                if rewrite_tasks:
                    rewrite_results = await asyncio.gather(
                        *rewrite_tasks, return_exceptions=True
                    )
                    for page_num, result in zip(rewrite_pages, rewrite_results):
                        if isinstance(result, Exception):
                            logger.warning(
                                "[P6.1] P%d validate 重写异常: %s", page_num, result
                            )
                            continue
                        if result:
                            research_paths[page_num] = f"{output_dir}/research-P{page_num}.md"
                    validation_ok, invalid_pages, _ = await self._run_validate_research(
                        output_dir,
                        pptx_root,
                        outline_path,
                        research_depth,
                        min_citations=min_citations,
                        require_min_citations=(writer_profile == "deep"),
                    )
            if not validation_ok:
                logger.warning(
                    "[P6.1] validate-research 全量门禁仍未通过 invalid=%s，不阻塞 pipeline（降级继续）",
                    invalid_pages,
                )

        return {"research_paths": research_paths}

    async def _rewrite_page_for_validate(
        self,
        *,
        page: dict[str, Any],
        output_dir: str,
        config: _ResearchConfig,
        min_words_per_page: int,
        reasons: list[str],
    ) -> bool:
        """按 validate-research reasons 重写单页 research-P{N}.md。"""
        page_num = int(page["page_number"])
        path = f"{output_dir}/research-P{page_num}.md"
        existing = ""
        if self.has_tool("read_file"):
            try:
                raw = await self.call_tool("read_file", file_path=path)
                existing = PptCommon.parse_tool_file_content(raw)
            except Exception as exc:
                if isinstance(exc, AbortError):
                    raise
                existing = ""
        reason_text = "；".join(str(r) for r in reasons) if reasons else "结构不符合新槽位"
        hint_block = _validate_reason_rewrite_hints(reasons)
        prompt = (
            "请按新 skill 单页骨架重写本页 research Markdown。"
            "只输出以 `### P{N}:` 开头的完整页面内容，不要解释。\n\n"
            f"校验失败原因：{reason_text}\n"
        )
        if hint_block:
            prompt += f"\n### 修复指引（按 research-output-template）\n{hint_block}\n"
        prompt += (
            f"\n主题：{config.topic}\n"
            f"页面编号：P{page_num}\n"
            f"页面标题：{page.get('title', '')}\n"
            f"页面类型：{page.get('page_type', page.get('type', ''))}\n"
            f"研究深度：{config.research_depth}\n"
            f"搜索模式：{config.search_mode}\n"
            f"目标字数约 {min_words_per_page}\n\n"
            f"{_RESEARCH_PREWRITE_CHECKLIST}\n\n"
            "### 必须使用的骨架\n"
            "```\n"
            f"{_RESEARCH_PAGE_SKELETON}"
            "```\n\n"
            f"{_RESEARCH_WRITE_HARD_RULES}\n"
            "### 当前版本（可保留事实与数字，必须改槽位名与结构）\n"
            f"{existing or '（空）'}\n"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt=(
                    pipeline_role_boundary("P6")
                    + "你是研究报告修订助手：把旧槽位改成主轴/关键数据/上屏要点/案例/来源留痕；"
                    "直接输出 Markdown。"
                ),
                concurrent=True,
            )
            section = (result or "").strip()
            if not section:
                return False
            return await self._write_file(path, section)
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            logger.warning("[P6.1] P%d validate 重写失败: %s", page_num, exc)
            return False

    async def _run_validate_research(
        self,
        output_dir: str,
        pptx_root: str,
        outline_path: str,
        research_depth: str,
        *,
        min_citations: int = 3,
        require_min_citations: bool = True,
    ) -> tuple[bool, list[int], dict[int, list[str]]]:
        """调 cli validate-research；返回 (ok, invalid_pages, reasons_by_page)。"""
        try:
            from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
                cli_path, combined_output, quote_path, run_bash,
            )
            cmd = (
                f"{cli_path('validate-research', pptx_root)} "
                f"--dir {quote_path(output_dir)} "
                f"--outline {quote_path(outline_path)} "
                f"--level {research_depth}"
            )
            if require_min_citations:
                cmd += f" --min-citations {min_citations}"
            result = await run_bash(
                self, cmd,
                timeout_seconds=120, required=False, workdir=pptx_root,
            )
            detail = combined_output(result) or result.stderr or result.stdout or ""
            invalid_pages, page_reasons = self._parse_validate_research_output(detail)
            if result.exit_code != 0:
                logger.warning(
                    "[P6.1] validate-research 门禁返回 exit=%d invalid=%s: %s",
                    result.exit_code,
                    invalid_pages,
                    detail[:500],
                )
                return False, invalid_pages, page_reasons
            logger.info("[P6.1] validate-research 全量门禁通过")
            return True, [], {}
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] validate-research CLI 不可用，降级跳过: %s", e)
            return True, [], {}

    @staticmethod
    def _parse_validate_research_output(
        detail: str,
    ) -> tuple[list[int], dict[int, list[str]]]:
        """从 validate-research JSON 输出提取 invalid 页与 reasons。"""
        text = str(detail or "")
        # 去掉可能的 Exit code 前缀，取第一个 JSON 对象
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return [], {}
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return [], {}
        if not isinstance(payload, dict):
            return [], {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        invalid_raw = summary.get("invalid") or []
        invalid_pages: list[int] = []
        for item in invalid_raw:
            try:
                invalid_pages.append(int(item))
            except (TypeError, ValueError):
                continue
        page_reasons: dict[int, list[str]] = {}
        pages_obj = payload.get("pages") if isinstance(payload.get("pages"), dict) else {}
        for key, info in pages_obj.items():
            try:
                page_num = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(info, dict):
                continue
            reasons = info.get("reasons") or []
            if isinstance(reasons, list):
                page_reasons[page_num] = [str(r) for r in reasons]
        return invalid_pages, page_reasons

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
        """单页闭环。

        material：快搜 + 浅抓取（无评分矩阵/幽灵源/交叉验证）→ material writer
        deep：搜索→评分→补搜→抓取→ghost→校验→回溯→深度 writer
        """
        page_num = page["page_number"]
        light = config.writer_profile == "material"

        page_sources: list[dict[str, Any]] = []
        page_extractions: list[dict[str, Any]] = []

        if need_search:
            if light:
                page_sources = await self._search_for_page_light(
                    page, coverage_info, searched_urls,
                )
                if page_sources:
                    page_extractions = await self._fetch_for_page_light(
                        page, page_sources, config.research_depth,
                    )
            else:
                page_sources = await self._search_for_page(page, coverage_info, searched_urls)
                if page_sources:
                    page_extractions = await self._fetch_for_page(
                        page, page_sources, config.research_depth,
                    )

        section = await self._write_single_page(
            page=page,
            extractions=page_extractions,
            source_material=source_material,
            config=config,
            min_words_per_page=min_words_per_page,
        )

        if section:
            precheck_issues = _precheck_research_section(section, config)
            if precheck_issues:
                logger.info(
                    "[P6.1] 页面 P%d 主轴表预检未通过 issues=%s，尝试同页自修",
                    page_num,
                    precheck_issues,
                )
                repaired = await self._repair_research_section_precheck(
                    page=page,
                    section=section,
                    issues=precheck_issues,
                    config=config,
                )
                if repaired:
                    section = repaired
                    still_bad = _precheck_research_section(section, config)
                    if still_bad:
                        logger.warning(
                            "[P6.1] 页面 P%d 预检自修后仍 issues=%s",
                            page_num,
                            still_bad,
                        )

        # 按页规则化校验 + 失败重写 1 次（字数不足仅 warn，不触发重写）
        if section:
            section, passed = self._finalize_page_section(
                page, section, page_extractions, config, min_words_per_page,
            )
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
                    section, _ = self._finalize_page_section(
                        page, rewritten, page_extractions, config, min_words_per_page,
                    )

        return {"section": section}

    # ==================== 搜索阶段 ====================

    async def _search_for_page_light(
        self,
        page: dict[str, Any],
        coverage_info: dict[str, Any],
        searched_urls: list[str],
    ) -> list[dict[str, Any]]:
        """material 快搜：3–5 次查询，无评分矩阵 / 无补搜循环。"""
        if not self.has_tool("web_search"):
            logger.warning("[P6.1] web_search 工具不可用，跳过快搜")
            return []

        queries = self._build_page_queries(page, coverage_info)[:5]
        if not queries:
            logger.warning(
                "[P6.1] 页面 P%d 无搜索查询，跳过快搜", page["page_number"],
            )
            return []

        logger.info("[P6.1] 页面 P%d material 快搜 %d 个查询", page["page_number"], len(queries))
        search_tasks = [
            self.call_tool("web_search", query=q, search_mode="default")
            for q in queries
        ]
        results = await asyncio.gather(*search_tasks, return_exceptions=True)

        sources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for q, result in zip(queries, results):
            if isinstance(result, Exception):
                logger.warning("[P6.1] 快搜失败 query=%s: %s", q[:50], result)
                continue
            if isinstance(result, str) and result.startswith("[ERROR]"):
                continue
            for s in self._parse_search_results(result):
                url = s.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    sources.append(s)

        for url in searched_urls:
            if url and url not in seen:
                seen.add(url)
                sources.append({"url": url, "from_existing": True})

        return sources[:8]

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

        sources: list[dict[str, Any]] = []
        for q, result in zip(queries, results):
            if isinstance(result, Exception):
                logger.warning("[P6.1] 搜索失败 query=%s: %s", q[:50], result)
                continue
            if isinstance(result, str) and result.startswith("[ERROR]"):
                continue
            sources.extend(self._parse_search_results(result))

        # 合并已有 URL
        for url in searched_urls:
            sources.append({"url": url, "from_existing": True})

        # 评分筛选
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

            for s in existing:
                s["grade"] = "existing"
                kept.append(s)

            kept.sort(key=lambda x: grade_order.get(x.get("grade", ""), 99))

            logger.info(
                "[P6.1] 页面 P%d 来源评分完成，合格 %d / %d",
                page["page_number"], len(kept), len(unique_sources),
            )
            return kept
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

    async def _fetch_for_page_light(
        self,
        page: dict[str, Any],
        page_sources: list[dict[str, Any]],
        _research_depth: str,
    ) -> list[dict[str, Any]]:
        """material 浅抓取：仅批量 fetch，跳过 ghost / 充分性 / 回溯。"""
        if not self.has_tool("fetch_webpage"):
            logger.warning("[P6.1] fetch_webpage 工具不可用，跳过浅抓取")
            return []
        return await self._batch_fetch_single(page, page_sources, "L1")

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

        # 宽松失败 → 至多一轮补抓后直接 data_limited；取消严格二次充分性 LLM
        extractions.append({
            "url": "",
            "content": "[数据有限] 该页面研究素材不足，需降级撰写",
            "data_limited": True,
        })
        logger.info(
            "[P6.1] 页面 P%d 回溯后标注 data_limited（跳过严格二次充分性）",
            page["page_number"],
        )

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

    async def _repair_research_section_precheck(
        self,
        *,
        page: dict[str, Any],
        section: str,
        issues: list[str],
        config: _ResearchConfig,
    ) -> str:
        """Fix axis-table / slot issues in-place before validate-research CLI."""
        page_num = page["page_number"]
        hint_block = _validate_reason_rewrite_hints(issues)
        prompt = (
            "以下 research 页未通过落笔前预检，请只修复表格规模/槽位/标注问题，"
            f"保留已有事实与数字，直接输出完整 `### P{page_num}:` 页面 Markdown。\n\n"
            f"预检问题：{'; '.join(issues)}\n"
        )
        if hint_block:
            prompt += f"\n### 修复指引\n{hint_block}\n"
        prompt += (
            f"\n{_RESEARCH_PREWRITE_CHECKLIST}\n"
            f"{_RESEARCH_WRITE_HARD_RULES}\n\n"
            f"原文：\n{section}\n"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt=(
                    pipeline_role_boundary("P6")
                    + "你是 research 结构修订助手，只修表格与槽位，直接输出 Markdown。"
                ),
                concurrent=True,
            )
            return (result or "").strip()
        except Exception as exc:
            if isinstance(exc, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d 预检自修失败: %s", page_num, exc)
            return ""

    async def _write_single_page(
        self,
        page: dict[str, Any],
        extractions: list[dict[str, Any]],
        source_material: str,
        config: _ResearchConfig,
        min_words_per_page: int,
    ) -> str:
        """撰写单页研究报告，返回以 `### P{N}:` 开头的该页 Markdown 片段。"""
        if config.writer_profile == "material":
            return await self._write_material_page(
                page, extractions, source_material, config, min_words_per_page,
            )
        return await self._write_deep_page(
            page, extractions, source_material, config, min_words_per_page,
        )

    def _build_extraction_and_material_sections(
        self,
        extractions: list[dict[str, Any]],
        source_material: str,
        config: _ResearchConfig,
        *,
        material_limit_default: int = 2000,
    ) -> tuple[str, str]:
        extraction_summary = ""
        if extractions:
            for ext in extractions:
                extraction_summary += f"来源: {ext['url']}\n{ext['content']}\n\n"

        material_section = ""
        if source_material:
            material_limit = 8000 if config.search_mode == "no_search" else material_limit_default
            truncated = source_material[:material_limit]
            material_section = (
                f"\n\n用户文档摘要（前 {material_limit} 字，"
                f"以 uploaded_document_summary 语义使用）：\n{truncated}"
            )
        return extraction_summary, material_section

    async def _write_material_page(
        self,
        page: dict[str, Any],
        extractions: list[dict[str, Any]],
        source_material: str,
        config: _ResearchConfig,
        min_words_per_page: int,
    ) -> str:
        """素材分支撰写：用户数值优先 + data_ref；搜索值不得覆盖。"""
        page_num = page["page_number"]
        page_type = page.get("page_type", page.get("type", ""))
        title = page.get("title", "")
        data_needs = page.get("data_needs", []) or []
        extraction_summary, material_section = self._build_extraction_and_material_sections(
            extractions, source_material, config, material_limit_default=4000,
        )
        docs_section = ""
        if config.documents_excerpt:
            docs_section = (
                f"\n\n### documents/*.md 摘录（数值须逐字 data_ref，禁止改写）\n"
                f"{config.documents_excerpt}\n"
            )
        research_density_rule = (
            "no_search：标注「数据有限，基于用户素材」，关键数据 ≥3 行、主轴可降为 ≥3×2"
            if config.search_mode == "no_search"
            else "关键数据尽量 ≥5 行、≥2 种数据类型；主轴 ≥4×3"
        )

        prompt = (
            "你是素材分支内容研究员。主数据源是用户文档；外搜仅作缺口补充，"
            "搜到的数值不得覆盖用户数值。直接输出该页 Markdown（以 `### P{N}:` 开头）。\n\n"
            f"主题：{config.topic}\n"
            f"页面编号：P{page_num}\n"
            f"页面标题：{title}\n"
            f"页面类型：{page_type}\n"
            f"数据需求：{'; '.join(str(d) for d in data_needs)}\n"
            f"搜索模式：{config.search_mode}\n"
            f"研究深度：{config.research_depth}\n"
            f"本页目标字数：约 {min_words_per_page} 字（不足仅作质量提示，仍须输出完整结构）\n\n"
            "### 严格格式要求（只输出本页章节，以 `### P{N}:` 开头）\n"
            "```\n"
            f"{_RESEARCH_PAGE_SKELETON}"
            "#### 用户数据引用（data_ref，逐字使用，禁止改写）  ← 匹配到 documents 表/图时必填\n"
            "- data_ref: {id或工作表名} ｜ 来源: {文件} ｜ dataOrigin: {cache|cf|none}\n"
            "- 类别 / 系列数值：逐字复制，禁止取整换算编造\n"
            "```\n\n"
            f"{_RESEARCH_WRITE_HARD_RULES}"
            f"{_RESEARCH_PREWRITE_CHECKLIST}\n"
            "8. 用户素材数值逐字优先；外部搜索值仅作对比/背景并分别标注\n"
            "9. 匹配到 documents 表/图时必须写 data_ref 块；匹配不到不强行编造\n"
            f"10. {research_density_rule}\n\n"
            f"### 快搜补充内容（不得覆盖用户数值）\n{extraction_summary or '（无）'}"
            f"{material_section}"
            f"{docs_section}"
        )

        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt=(
                    pipeline_role_boundary("P6")
                    + "你是素材分支研究员：用户数值优先、data_ref 逐字；"
                    "直接输出该页 Markdown，不要解释。"
                ),
                concurrent=True,
            )
            return result.strip() if result else ""
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P6.1] 页面 P%d material 撰写LLM失败: %s", page_num, e)
            return ""

    async def _write_deep_page(
        self,
        page: dict[str, Any],
        extractions: list[dict[str, Any]],
        source_material: str,
        config: _ResearchConfig,
        min_words_per_page: int,
    ) -> str:
        """搜索分支深度撰写。"""
        page_num = page["page_number"]
        page_type = page.get("page_type", page.get("type", ""))
        title = page.get("title", "")
        data_needs = page.get("data_needs", []) or []
        extraction_summary, material_section = self._build_extraction_and_material_sections(
            extractions, source_material, config,
        )

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
            f"本页目标字数：约 {min_words_per_page} 字（不足仅作质量提示）\n"
            f"本页最低引用数：{config.min_citations}\n\n"
            "### 严格格式要求（只输出本页章节，以 `### P{N}:` 开头）\n"
            "```\n"
            f"{_RESEARCH_PAGE_SKELETON}"
            "```\n\n"
            f"{_RESEARCH_WRITE_HARD_RULES}"
            f"{_RESEARCH_PREWRITE_CHECKLIST}\n"
            f"8. 引用标注 ≥{config.min_citations} 个不同来源名，且均在来源留痕登记\n"
            "9. 关键数据每页 ≥5 条、≥2 种数据类型；主轴 ≥4 行×≥3 列"
            f"{'；no_search 或证据受限时标注「数据有限…」并允许关键数据 ≥3、主轴 ≥3×2' if config.search_mode == 'no_search' else ''}\n\n"
            f"### 抓取内容\n{extraction_summary}"
            f"{material_section}"
        )

        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt=(
                    pipeline_role_boundary("P6")
                    + "你是深度内容研究员，直接输出该页的 Markdown 内容，不要输出解释。"
                ),
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
            "- **主轴**：待补充\n"
            "- **关键数据**：待补充\n"
            "- **上屏要点**：待补充\n"
            "- **案例**：待补充\n"
            "\n"
            "#### 来源留痕\n"
            "| 名称 | URL | 类别 | 评分 |\n"
            "| --- | --- | --- | --- |\n"
            "| 待补充 | 待补充 | 待补充 | C |\n"
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
        lines.append(f"> 页面类型：{page_type} | 研究级别：{research_depth}")
        lines.append("")
        lines.append("**核心论点**：[数据有限，基于大纲规划]")
        lines.append("")
        lines.append("#### PPT 内容建议")
        lines.append(f"- **推荐主标题**：{title}")
        lines.append("- **主轴**（数据有限，基于用户素材 / 大纲）：")
        lines.append("  | 维度 | 对象A | 对象B | 来源 |")
        lines.append("  | --- | --- | --- | --- |")
        lines.append("  | 待补充 | 待补充 | 待补充 | 大纲 |")
        lines.append("  | 待补充 | 待补充 | 待补充 | 大纲 |")
        lines.append("  | 待补充 | 待补充 | 待补充 | 大纲 |")
        lines.append("- **关键数据**（无研究数据，待后续补充）：")
        lines.append("  | 数据项 | 数值 | 单位/口径 | 归属对象 | 来源 | 时间 | 数据类型 |")
        lines.append("  | --- | --- | --- | --- | --- | --- | --- |")
        if data_needs:
            for need in data_needs[:3]:
                lines.append(
                    f"  | {need} | 待补充 | 待补充 | 待补充 | 大纲 | 待补充 | 绝对值 |"
                )
        else:
            lines.append(
                "  | 待补充 | 待补充 | 待补充 | 待补充 | 大纲 | 待补充 | 绝对值 |"
            )
        lines.append("- **上屏要点**：")
        if queries:
            for i, q in enumerate(queries[:4], 1):
                lines.append(f"  {i}. {q}（待补充） [大纲]")
        else:
            lines.append("  1. 待补充 [大纲]")
        lines.append("- **案例**：")
        lines.append("  - 待补充 — 待补充 [大纲]")
        lines.append("")
        lines.append("#### 来源留痕")
        lines.append("| 名称 | URL | 类别 | 评分 |")
        lines.append("| --- | --- | --- | --- |")
        lines.append("| 大纲 | outline.md | 用户素材 | B |")
        lines.append("")
        lines.append("- **数据有限，基于用户素材**，本页未执行外部搜索。")
        lines.append("")
        return "\n".join(lines)

    def _finalize_page_section(
        self,
        page: dict[str, Any],
        section: str,
        extractions: list[dict[str, Any]] | None,
        config: _ResearchConfig,
        min_words_per_page: int,
    ) -> tuple[str, bool]:
        """结构 + 程序化预检 + 规则补引用。返回 (section, ok)；仅结构/预检失败才重写。"""
        if not section:
            return "", False

        page_num = page["page_number"]

        header_match = _PAGE_HEADER_RE.search(section)
        if not header_match or int(header_match.group(1)) != int(page_num):
            logger.warning(
                "[P6.1] 页面 P%d 校验失败：页码不对齐（section 未以 ### P%d: 开头）",
                page_num, page_num,
            )
            return section, False

        if "#### PPT 内容建议" not in section:
            logger.warning("[P6.1] 页面 P%d 校验失败：缺少 #### PPT 内容建议", page_num)
            return section, False
        if "#### 来源留痕" not in section:
            logger.warning("[P6.1] 页面 P%d 校验失败：缺少 #### 来源留痕", page_num)
            return section, False
        for slot in ("主轴", "关键数据", "上屏要点"):
            if slot not in section:
                logger.warning("[P6.1] 页面 P%d 校验失败：缺少槽位 %s", page_num, slot)
                return section, False

        precheck_issues = _precheck_research_section(section, config)
        blocking: list[str] = []
        blocking_prefixes = (
            "axis_table_too_small:",
            "missing:",
            "forbidden_top_slot:",
        )
        for issue in precheck_issues:
            if issue.startswith(blocking_prefixes) or issue == "missing_evidence_limited_annotation":
                blocking.append(issue)
        if blocking:
            logger.warning(
                "[P6.1] 页面 P%d 程序化预检未通过: %s",
                page_num,
                blocking,
            )
            return section, False

        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", section))
        min_words_80 = int(min_words_per_page * 0.8)
        if chinese_chars < min_words_80:
            logger.warning(
                "[P6.1] 页面 P%d 字数不足（warn）：%d < %d，不阻断",
                page_num, chinese_chars, min_words_80,
            )

        min_citations = max(1, int(config.min_citations or _MIN_NAMED_CITATIONS))
        if config.writer_profile == "material" and config.search_mode == "no_search":
            min_citations = max(1, min(min_citations, 2))
        section = enrich_section_citations(
            section, extractions, min_citations=min_citations,
        )
        return section, True

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
                "2. 调用 P6.1 PageWorkerNode → per-page 并发闭环（搜索→评分→补搜→抓取→ghost→校验→回溯→撰写→规则化按页校验→失败重写）\n"
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
