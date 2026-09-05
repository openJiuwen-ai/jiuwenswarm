from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

logger = logging.getLogger(__name__)

_MATERIAL_RICHNESS = frozenset({"rich", "thin", "empty"})
_VALID_SEARCH_MODES = frozenset({"auto", "no_search", "force_search"})
_VALID_SOURCE_TYPES = frozenset({"topic", "outline", "description"})
_SOURCE_MATERIAL_MAX_CHARS = 4000
_SEARCH_RESULT_MAX_CHARS = 3500
_OUTLINE_NAME = "outline.md"
_SEARCH_RESULTS_FOR_P43_MAX_CHARS = 8000
_PAGE_HEADING_PATTERN = re.compile(r"^###\s+P(\d+)\s*:", re.MULTILINE)
_OUTLINE_FIELD_PATTERN = re.compile(
    r"^-?\s*\*\*(?P<field>[^*]+)\*\*[：:]\s*(?P<value>.+?)\s*$",
    re.MULTILINE,
)
_P4_MAX_ATTEMPTS = 2
_INSUFFICIENT_INFO_MARKER = "[INSUFFICIENT_INFO]"
_QUERY_BOUNDS_NO_MATERIAL = (5, 8)
_QUERY_BOUNDS_WITH_MATERIAL = (3, 5)
_RESEARCH_DIMENSIONS = (
    "领域现状",
    "关键维度",
    "最新动态",
    "核心玩家",
    "争议热点",
)

_P41_SYSTEM_PROMPT = """你是 PPT 内容策划助手。根据主题、目标页数与用户素材，评估素材充裕度并给出研究重点。

素材充裕度 material_richness 判定：
- rich（充实）：有清晰章节结构，且具体数据/案例/论述足以支撑目标页数
- thin（单薄）：仅有提纲或概要，信息量不足以填充多页 PPT
- empty（空）：无素材或内容极少

规则：
1. 只根据给定素材与需求判断，不要编造素材中不存在的信息。
2. 无素材时必须返回 material_richness=empty。
3. focus_areas 为 1~3 句研究重点方向；无素材时可基于 topic 推断。

必须只输出 JSON：
{"material_richness":"empty","focus_areas":"..."}"""

_P42A_SYSTEM_PROMPT = """你是 PPT 快速调研助手。根据主题与研究重点生成网页搜索 query。

规则：
1. 覆盖不同维度（可合并进单条 intent）：领域现状、关键维度、最新动态、核心玩家、争议热点。
2. 中文主题搭配中英 query；可加当前年份或 latest/report。
3. 有用户素材时只补未覆盖维度；不编造事实。
4. 只产出搜索 query：禁止分析、结论、结果摘要、逐步推理正文。

输出（唯一允许的全文）：一个 JSON 对象；无前言、无 Markdown 围栏、无结尾说明。
{"entity":"主题核心实体名或 null","queries":[{"dimension":"领域现状","query":"..."}]}"""

# 健康短 JSON（约 8 条 query）远小于此；基线级非 JSON 长散文（数万 token）必触发中途熔断。
_P42A_RESPONSE_MAX_CHARS = 4096
_P42_MAX_RETRIES = 2
# 设计：R0 初始搜索用 _P42A_SYSTEM_PROMPT（广覆盖、中英双语、加年份/report 等可信词）；
# 重搜用下方 _P42_RELEVANCE_SYSTEM_PROMPT 定向收窄/扩搜。重搜刻意不复用 _P42A_SYSTEM_PROMPT——
# 其“加年份/report/statistics、覆盖5维度”规则正是 R0 把稀有实体名稀释的元凶，带入重搜会再次稀释。
_P42_RELEVANCE_SYSTEM_PROMPT = """你是 PPT 快速调研的相关性判定与重搜助手。

判定视角：假设你需要用这些搜索结果为「主题实体」撰写一份 PPT 大纲，结果中的信息是否足够支撑你写出关于该实体的具体内容？

相关性判定标准：
- sufficient：搜索结果中至少有一条直接提及主题实体名，且包含该实体的具体信息（如功能、数据、案例、产品细节），足以支撑撰写关于该实体的 PPT 大纲
- insufficient：搜索结果中均未提及主题实体名，或仅有同行业/同领域的泛泛内容但缺乏实体具体信息。即使结果属于同一行业领域，只要未直接提及实体名并包含实体具体信息，均判 insufficient

重搜 query 生成规则（仅在相关性 insufficient 或无可用结果时生成，2-4 条）：
- failure_mode=empty（无可用搜索结果）：扩大查询范围，尝试同义词或英文变体
- failure_mode=irrelevant（有结果但均不相关）：聚焦主题实体名，用纯实体名或实体名+官网/产品介绍/是什么等限定词收窄

只输出 JSON：
{"relevance":"sufficient|insufficient","reason":"...","retry_queries":[{"dimension":"...","query":"..."}]}
相关性 sufficient 时 retry_queries 为空数组。不要编造 query。"""

_P43_COMMON_RULES = """大纲格式要求（必须严格遵守）：

1. 文件以 `# 大纲：{topic}` 开头，随后元信息行：
   **受众**、**总页数**、**叙事主线**、**输入类型**、**搜索模式**
2. 若需写入已搜索来源，在 `## 已搜索来源` 下用表格列出 URL 与覆盖维度（不写正式评分，评分留给 P6）。
   无需搜索时删除整个 `## 已搜索来源` 章节。
3. `## 页面规划` 下每页一个 `### P{N}:` 块，字段齐全：
   - **类型**：cover/ending/agenda/section/chapter/trend/data/case/comparison/technology 等
   - **研究需求**：cover/ending/agenda/section/chapter 标 ❌，其余标 ✅
   - **标题**：结论性完整句（Action + Result）；结构性页面（cover/ending/agenda/section/chapter）可使用描述性标题
   - **内容概要**：具体有信息量
   - **研究查询**：✅ 页 2-4 个精准查询；❌ 页填 `-`
   - **数据需求**：✅ 页写具体数据类型和维度，数据需求必须具体化；❌ 页填 `-`
4. 内容页数（研究需求：✅）必须等于 page_count。封面（cover）、结束页（ending）及用户明确要求的结构页（section/agenda/chapter 等）标 ❌，其余页必须标 ✅。
   中间结构页的添加规则见下方「中间结构页」指令（由系统根据用户需求动态注入）。
   **页面顺序**：cover 必须是 P1（首页），ending 必须是末页（P{总页数}）。
   **ending 页约束**：标题优先「感谢聆听」或 ≤16 字简短收束语；全文总结、数据回响、趋势展望必须放在最后一个内容页（✅），不得把长总结句写入 ending 页标题；ending 页内容概要只描述结束页展示（感谢语、可选一句总结语、汇报人/日期），不得复制正文页大纲。
   **agenda 页内容**：内容概要只列内容页（✅）章节标题与导航，不得列入 cover/ending/agenda 等结构页本身。
5. 基于给定素材与搜索结果，不编造不存在的趋势或数据。
6. 只输出 Markdown 正文，不要 JSON，不要代码围栏。"""

_P43_TOPIC_SYSTEM_PROMPT = f"""你是 PPT 大纲策划师（source_type=topic）。基于搜索结果与用户素材，生成结构化 outline.md。

{_P43_COMMON_RULES}

## 信息充分性自检（强制，优先于大纲生成）
生成大纲前，先自检搜索结果中关于主题实体的信息是否充分：
- 充分：搜索结果中有至少1条直接提及主题实体名，且包含该实体的具体功能/产品/服务/案例信息，足以支撑撰写关于该实体的 PPT 大纲。
- 不足：搜索结果中均未提及主题实体名，或虽有名称但仅为不同语境的同名实体，或无具体功能描述（仅有行业报告、竞品信息、通用趋势等）。

若判定不足，必须只输出以下内容（禁止生成大纲、禁止编造功能）：
{_INSUFFICIENT_INFO_MARKER} <一句话说明搜索结果中缺少关于主题实体的什么信息>

示例：
- 主题"产品X的核心功能"，搜索结果全是行业报告和竞品信息 →
  {_INSUFFICIENT_INFO_MARKER} 搜索结果未包含关于产品X的具体功能描述，仅有行业趋势和竞品信息。
"""

_P43_OUTLINE_SYSTEM_PROMPT = f"""你是 PPT 大纲策划师（source_type=outline）。用户已提供结构化大纲，**必须保留原文**。

核心规则：
1. **禁止修改**用户原文的任何内容
2. **禁止添加**原文中没有的新内容
3. **禁止删除**原文中的任何内容
4. 只做结构化重组，映射为 `### P{{N}}:` 页面块
5. 保留用户原文标题与要点，整合到内容概要中，不重新措辞
6. 为每页推断类型与研究需求标记；研究查询基于各页主题自动生成

{_P43_COMMON_RULES}"""

_P43_DESCRIPTION_SYSTEM_PROMPT = f"""你是 PPT 大纲策划师（source_type=description）。从用户详细描述文本中提取页面结构。

核心规则：
1. 识别描述中的页面/章节结构，提取每页标题与关键要点
2. 保留描述中的逻辑结构与组织方式
3. 要点为描述内容的简明摘要
4. 补充页面类型、研究需求、研究查询、数据需求等元信息

{_P43_COMMON_RULES}"""


class ContentPlanError(RuntimeError):
    """P4 内容策划失败。"""


def _require_p4_prerequisites(inputs: dict[str, Any]) -> None:
    topic = inputs.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise ContentPlanError("缺少演示主题 topic，无法进入 P4")

    page_count = inputs.get("page_count")
    if page_count is None:
        raise ContentPlanError("缺少 page_count，无法进入 P4")

    audience = inputs.get("audience")
    if not isinstance(audience, str) or not audience.strip():
        raise ContentPlanError("缺少 audience，无法进入 P4")

    search_mode = str(inputs.get("search_mode") or "").strip()
    if search_mode not in _VALID_SEARCH_MODES:
        raise ContentPlanError(f"缺少或无效的 search_mode: {search_mode!r}")

    source_type = str(inputs.get("source_type") or "").strip()
    if source_type not in _VALID_SOURCE_TYPES:
        raise ContentPlanError(f"缺少或无效的 source_type: {source_type!r}")

    output_dir = inputs.get("output_dir")
    if not output_dir or not str(output_dir).strip():
        raise ContentPlanError("缺少 output_dir，无法进入 P4")


def _decide_p4_should_search(search_mode: str, material_richness: str) -> bool:
    """按 outline-planner 素材充裕度 × search_mode 决策表计算是否执行 P4.2。"""
    if search_mode == "no_search":
        return False
    if search_mode == "force_search":
        return True
    if material_richness == "rich":
        return False
    if material_richness in ("thin", "empty"):
        return True
    raise ContentPlanError(f"无效的 material_richness: {material_richness!r}")


def _p4_search_reason(search_mode: str, material_richness: str, should_search: bool) -> str:
    if not should_search:
        if search_mode == "no_search":
            return "search_mode=no_search，跳过快速调研"
        if search_mode == "auto" and material_richness == "rich":
            return "auto 模式且素材充实，跳过快速调研"
        return "无需快速调研"
    if search_mode == "force_search":
        return "search_mode=force_search，执行快速调研"
    return f"auto 模式且素材为 {material_richness}，执行快速调研"


def _parse_p41_response(raw: str) -> dict[str, str]:
    payload = PptCommon.parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise ContentPlanError("P4.1 解析失败：LLM 未返回有效 JSON")

    material_richness = str(payload.get("material_richness") or "").strip().lower()
    if material_richness not in _MATERIAL_RICHNESS:
        raise ContentPlanError(f"P4.1 无效的 material_richness: {material_richness!r}")

    focus_areas = str(payload.get("focus_areas") or "").strip()
    if not focus_areas:
        raise ContentPlanError("P4.1 缺少 focus_areas")

    return {
        "material_richness": material_richness,
        "focus_areas": focus_areas,
    }


def _build_p41_prompt(inputs: dict[str, Any], source_material: str) -> str:
    parts = [
        "请评估以下 PPT 需求的素材充裕度。\n",
        f"- topic: {inputs.get('topic', '')}\n",
        f"- page_count: {inputs.get('page_count')}\n",
        f"- audience: {inputs.get('audience', '')}\n",
        f"- presentation_purpose: {inputs.get('presentation_purpose', '')}\n",
        f"- source_type: {inputs.get('source_type', '')}\n",
        f"- search_mode: {inputs.get('search_mode', '')}\n",
        f"- has_documents: {bool(inputs.get('has_documents'))}\n",
        f"- doc_parse_ok: {bool(inputs.get('doc_parse_ok'))}\n",
    ]
    failure_reason = inputs.get("failure_reason")
    if isinstance(failure_reason, str) and failure_reason.strip():
        parts.append(f"上次失败原因：\n{failure_reason.strip()}\n")
    if source_material:
        parts.append(f"用户素材（doc_raw 摘要）：\n{source_material}\n")
    else:
        parts.append("用户素材：无\n")
    parts.append("按 JSON 返回 material_richness、focus_areas。")
    return "\n".join(parts)


def _apply_p41_result(inputs: dict[str, Any], parsed: dict[str, str], source_material: str) -> None:
    search_mode = str(inputs.get("search_mode") or "").strip()
    material_richness = parsed["material_richness"]
    should_search = _decide_p4_should_search(search_mode, material_richness)

    inputs["has_source_material"] = bool(source_material)
    inputs["source_material_chars"] = len(source_material)
    inputs["material_richness"] = material_richness
    inputs["focus_areas"] = parsed["focus_areas"]
    inputs["p4_should_search"] = should_search
    inputs["p4_search_reason"] = _p4_search_reason(search_mode, material_richness, should_search)
    inputs["content_plan_status"] = "normalized"


async def _run_p41_normalize(node: PlanNode, inputs: dict[str, Any]) -> None:
    _require_p4_prerequisites(inputs)

    source_material = await PptCommon.read_file(
        node,
        inputs.get("doc_raw_path"),
        max_chars=_SOURCE_MATERIAL_MAX_CHARS,
        error_type=ContentPlanError,
    )
    response = await node.stream_llm_collect(
        _build_p41_prompt(inputs, source_material),
        system_prompt=_P41_SYSTEM_PROMPT,
    )
    if not isinstance(response, str) or not response.strip():
        raise ContentPlanError("P4.1 失败：LLM 返回为空")

    parsed = _parse_p41_response(response)
    _apply_p41_result(inputs, parsed, source_material)


def _query_count_bounds(has_source_material: bool) -> tuple[int, int]:
    return _QUERY_BOUNDS_WITH_MATERIAL if has_source_material else _QUERY_BOUNDS_NO_MATERIAL


def _normalize_tool_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for key in ("content", "output", "result", "stdout", "text", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if result.get("success") is False:
            error = result.get("error") or result.get("message")
            if isinstance(error, str):
                return f"[ERROR]: {error}"
    return str(result).strip()


def _is_search_result_usable(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("[ERROR]"):
        return False
    return True


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n...(内容已截断)"


def _parse_p42a_queries(raw: str, *, has_source_material: bool) -> list[dict[str, str]]:
    payload = PptCommon.parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise ContentPlanError("P4.2a 解析失败：LLM 未返回有效 JSON")

    queries_raw = payload.get("queries")
    if not isinstance(queries_raw, list) or not queries_raw:
        raise ContentPlanError("P4.2a 缺少 queries 数组")

    min_count, max_count = _query_count_bounds(has_source_material)
    if not min_count <= len(queries_raw) <= max_count:
        raise ContentPlanError(
            f"P4.2a 查询数量应为 {min_count}~{max_count}，实际 {len(queries_raw)}"
        )

    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in queries_raw:
        if not isinstance(item, dict):
            raise ContentPlanError("P4.2a queries 项必须为对象")
        dimension = str(item.get("dimension") or "").strip() or "综合"
        query = str(item.get("query") or "").strip()
        if not query:
            raise ContentPlanError("P4.2a 存在空 query")
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        parsed.append({"dimension": dimension, "query": query})

    if not min_count <= len(parsed) <= max_count:
        raise ContentPlanError(
            f"P4.2a 去重后查询数量应为 {min_count}~{max_count}，实际 {len(parsed)}"
        )
    return parsed


def _build_p42a_prompt(inputs: dict[str, Any], source_material: str) -> str:
    now = datetime.now(tz=timezone.utc)
    now_str = now.strftime("%Y-%m-%d")
    current_year = now.strftime("%Y")
    min_count, max_count = _query_count_bounds(bool(source_material))
    parts = [
        f"当前日期：{now_str}（年份 {current_year}）。涉及时效的 query 优先带当前年份。\n",
        f"生成 {min_count}~{max_count} 条并行搜索 query。\n",
        f"- topic: {inputs.get('topic', '')}\n",
        f"- page_count: {inputs.get('page_count')}\n",
        f"- audience: {inputs.get('audience', '')}\n",
        f"- focus_areas: {inputs.get('focus_areas', '')}\n",
        f"- has_source_material: {bool(source_material)}\n",
        f"- 建议覆盖维度: {', '.join(_RESEARCH_DIMENSIONS)}\n",
    ]
    if source_material:
        parts.append(f"用户素材摘要：\n{source_material}\n")
    parts.append(
        "只输出一个 JSON："
        '{"entity":"...或 null","queries":[{"dimension":"...","query":"..."}]}'
        "——无其它文字。"
    )
    return "\n".join(parts)


async def _stream_llm_collect_bounded(
    node: PlanNode,
    prompt: str,
    *,
    system_prompt: str,
    max_chars: int,
    error_prefix: str = "P4.2a",
) -> str:
    """流式收集 LLM 可见 content；超限立即失败以中止非 JSON 长正文空转。"""
    chunks: list[str] = []
    total = 0
    async for chunk in node.stream_llm(prompt, system_prompt=system_prompt):
        piece = chunk if isinstance(chunk, str) else str(chunk or "")
        if not piece:
            continue
        total += len(piece)
        if total > max_chars:
            raise ContentPlanError(
                f"{error_prefix} 响应过长（>{max_chars} 字符），疑似非 JSON 空转，已中止"
            )
        chunks.append(piece)
    return "".join(chunks)


def _format_search_results_for_p43(search_results: list[dict[str, str]]) -> str:
    """将搜索结果格式化为 P4.3 prompt 中的文本。"""
    parts: list[str] = ["### 网页搜索结果\n"]
    total_chars = 0
    for batch in search_results:
        block = (
            f"#### query: {batch['query']}（维度: {batch.get('dimension', '')}）\n"
            f"{batch['result']}\n\n"
        )
        if total_chars + len(block) > _SEARCH_RESULTS_FOR_P43_MAX_CHARS:
            parts.append("...(更多搜索结果已截断)\n")
            break
        parts.append(block)
        total_chars += len(block)
    return "\n".join(parts)


async def _run_parallel_web_searches(
    node: PlanNode,
    queries: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not node.has_tool("web_search"):
        raise ContentPlanError("缺少 web_search 工具，无法执行 P4.2 快速调研")

    async def _search_one(item: dict[str, str]) -> dict[str, str]:
        raw = await node.call_tool("web_search", query=item["query"])
        result = _truncate_text(_normalize_tool_text(raw), _SEARCH_RESULT_MAX_CHARS)
        return {
            "dimension": item["dimension"],
            "query": item["query"],
            "result": result,
        }

    return list(await asyncio.gather(*[_search_one(item) for item in queries]))


def _extract_entity(raw: str) -> str:
    """从 P4.2a 的 LLM 输出中提取主题核心实体名（无明确实体时返回空串）。"""
    payload = PptCommon.parse_json_payload(raw)
    if not isinstance(payload, dict):
        return ""
    entity = payload.get("entity")
    if entity is None:
        return ""
    entity_str = str(entity).strip()
    if entity_str.lower() in ("null", "none", ""):
        return ""
    return entity_str


def _entity_in_result_body(entity: str, result: str) -> bool:
    """检查实体名是否出现在搜索结果正文（排除 Query 回显行）中。

    与 _entity_in_results 的逐条判定逻辑一致，供排序使用。
    不用 target in result 是因为 result 含 "Query: {entity}" 回显行，
    会导致所有结果都判定为 True，排序失效。
    """
    if not entity:
        return False
    target = entity.lower()
    raw = (result or "").lower()
    body = "\n".join(
        line for line in raw.splitlines()
        if not line.lstrip().startswith("query:")
    )
    return target in body


def _entity_in_results(entity: str, batches: list[dict[str, str]]) -> bool:
    """规则预检：搜索结果中是否直接提及实体名（不区分大小写）。

    用于在 LLM 相关性判定前做兜底预检，避免 LLM 因截断或理解偏差
    错误判定 sufficient/insufficient。

    排除 web_search 结果开头的 Query 回显行，避免查询词中的实体名导致假阳性。
    """
    if not entity:
        return False
    target = entity.lower()
    for b in batches:
        raw = (b.get("result") or "").lower()
        # 排除 "Query: ..." 回显行，该行是搜索查询的回显而非结果正文
        body = "\n".join(
            line for line in raw.splitlines()
            if not line.lstrip().startswith("query:")
        )
        found = target in body
        logger.debug(
            "[P4.2][DEBUG] _entity_in_results: entity=%r target=%r found=%s "
            "raw_first200=%r body_full=%r",
            entity, target, found,
            raw[:200], body[:1000],
        )
        if found:
            return True
    return False


async def _assess_and_suggest_retry(
    node: PlanNode,
    topic: str,
    entity: str,
    usable_batches: list[dict[str, str]],
    failure_mode: str,
) -> tuple[str, str, list[dict[str, str]]]:
    """判定搜索结果与主题的相关性，相关性不足时生成重搜 query。

    failure_mode:
      - "empty": 无可用结果，直接生成扩搜 query（换同义词/英文）
      - "irrelevant": 有结果，由 LLM 判定相关性；insufficient 时生成收窄 query

    返回 (relevance, reason, retry_queries)。empty 模式恒为 insufficient；
    irrelevant 模式由 LLM 判定 sufficient/insufficient。

    双保险机制：
    1. 规则预检：搜索结果中直接提及实体名 → 判定 sufficient，跳过 LLM
    2. LLM 判定：规则预检未命中时，由 LLM 判定相关性
    """
    # 方案2：规则预检 — 搜索结果中直接提及实体名则直接判定 sufficient
    if failure_mode == "irrelevant" and _entity_in_results(entity, usable_batches):
        logger.info("[P4.2] 规则预检命中：搜索结果中直接提及实体 '%s'，判定 sufficient", entity)
        return "sufficient", f"规则预检：搜索结果中直接提及实体 '{entity}'", []

    if failure_mode == "empty":
        results_block = "（无可用搜索结果）"
    else:
        # 按实体名出现优先排序：包含实体名的结果排前面，确保截断时丢弃的是不相关结果
        if entity:
            ordered = sorted(
                usable_batches,
                key=lambda b: not _entity_in_result_body(entity, b.get("result") or ""),
            )
        else:
            ordered = usable_batches
        results_block = "\n---\n".join(
            f"query: {b['query']}\n{b['result'][:1200]}"
            for b in ordered
        )[:6000]
    prompt = (
        f"# 主题\n{topic}\n\n"
        f"# 主题实体\n{entity or '（无明显实体，为主题类）'}\n\n"
        f"# 失败模式\n{failure_mode}\n\n"
        f"# 搜索结果\n{results_block}\n\n"
        "按系统提示判定相关性并在不足时生成重搜 query，只输出 JSON。"
    )
    try:
        # 仅用 _P42_RELEVANCE_SYSTEM_PROMPT，不引入 _P42A_SYSTEM_PROMPT 的广覆盖/加年份规则，
        # 避免重搜 query 再次稀释实体名
        resp = await node.stream_llm_collect(prompt, system_prompt=_P42_RELEVANCE_SYSTEM_PROMPT)
    except Exception as exc:
        if isinstance(exc, AbortError):
            raise
        logger.warning("[P4.2] 相关性评估调用失败，保守判 insufficient: %s", exc)
        return "insufficient", f"评估调用失败: {exc}", []

    payload = PptCommon.parse_json_payload(resp)
    if not isinstance(payload, dict):
        return "insufficient", "相关性评估解析失败，保守判 insufficient", []

    relevance = str(payload.get("relevance", "")).strip().lower()
    if relevance not in ("sufficient", "insufficient"):
        relevance = "insufficient"
    reason = str(payload.get("reason", "")).strip()

    retry_queries: list[dict[str, str]] = []
    retry_raw = payload.get("retry_queries") or []
    if isinstance(retry_raw, list):
        seen: set[str] = set()
        for item in retry_raw:
            if not isinstance(item, dict):
                continue
            q = str(item.get("query") or "").strip()
            if not q or q.casefold() in seen:
                continue
            seen.add(q.casefold())
            dimension = str(item.get("dimension") or "").strip() or "重搜"
            retry_queries.append({"dimension": dimension, "query": q})
    return relevance, reason, retry_queries


async def _run_p42_quick_research(node: PlanNode, inputs: dict[str, Any]) -> None:
    source_material = await PptCommon.read_file(
        node,
        inputs.get("doc_raw_path"),
        max_chars=_SOURCE_MATERIAL_MAX_CHARS,
        error_type=ContentPlanError,
    )
    has_source_material = bool(source_material)

    response_a = await _stream_llm_collect_bounded(
        node,
        _build_p42a_prompt(inputs, source_material),
        system_prompt=_P42A_SYSTEM_PROMPT,
        max_chars=_P42A_RESPONSE_MAX_CHARS,
        error_prefix="P4.2a",
    )
    if not isinstance(response_a, str) or not response_a.strip():
        raise ContentPlanError("P4.2a 失败：LLM 返回为空")

    query_items = _parse_p42a_queries(response_a, has_source_material=has_source_material)
    entity = _extract_entity(response_a)
    topic = str(inputs.get("topic", ""))
    all_used_queries: list[str] = [item["query"] for item in query_items]

    # R0：初始并行搜索
    search_batches = await _run_parallel_web_searches(node, query_items)
    usable = [b for b in search_batches if _is_search_result_usable(b["result"])]

    # 相关性闸门 + 最多 _P42_MAX_RETRIES 轮重搜：
    #   无可用结果(empty) → 扩搜（换同义词/英文）
    #   有结果但不相关(irrelevant) → 收窄（聚焦实体名）
    # 任一轮判定 sufficient 即完成；耗尽仍 insufficient 则 raise，交由 P4 整体重试 / fallback 兜底。
    last_relevance = "insufficient"
    last_reason = ""
    retry_round = 0
    while True:
        failure_mode = "empty" if not usable else "irrelevant"
        relevance, reason, retry_queries = await _assess_and_suggest_retry(
            node, topic, entity, usable, failure_mode=failure_mode,
        )
        last_relevance, last_reason = relevance, reason
        if failure_mode == "irrelevant" and relevance == "sufficient":
            break  # 有结果且相关，完成
        if retry_round >= _P42_MAX_RETRIES or not retry_queries:
            break  # 已达重试上限或 LLM 未给出可重搜 query，无法继续
        all_used_queries.extend(q["query"] for q in retry_queries)
        retry_batches = await _run_parallel_web_searches(node, retry_queries)
        usable.extend(b for b in retry_batches if _is_search_result_usable(b["result"]))
        retry_round += 1

    if not usable:
        raise ContentPlanError("P4.2b 快速调研失败：所有 web_search 均无有效结果")

    if last_relevance == "insufficient":
        raise ContentPlanError(
            f"P4.2 快速调研相关性不足：经 {retry_round} 轮重搜仍未获得关于主题「{topic}」的有效信息，"
            f"建议用纯实体名或实体名+官网重搜定位权威来源。原因：{last_reason}"
        )

    # 按实体名出现优先排序：包含实体名的结果排前面，
    # 确保 P4.3 截断时丢弃的是不相关结果（与 _assess_and_suggest_retry 中排序逻辑一致）
    if entity:
        usable.sort(key=lambda b: not _entity_in_result_body(entity, b.get("result") or ""))

    inputs["search_results"] = usable
    inputs["p4_search_queries"] = all_used_queries
    inputs["p4_search_entity"] = entity
    inputs["p4_search_hit_count"] = len(usable)
    inputs["p4_quick_research_status"] = "completed"
    inputs["content_plan_status"] = "quick_research_done"


def _p43_system_prompt(source_type: str) -> str:
    if source_type == "outline":
        return _P43_OUTLINE_SYSTEM_PROMPT
    if source_type == "description":
        return _P43_DESCRIPTION_SYSTEM_PROMPT
    return _P43_TOPIC_SYSTEM_PROMPT


def _should_include_searched_sources(inputs: dict[str, Any]) -> bool:
    return str(inputs.get("p4_quick_research_status") or "").strip() == "completed"


def _is_no_search_degraded(inputs: dict[str, Any]) -> bool:
    search_mode = str(inputs.get("search_mode") or "").strip()
    if search_mode != "no_search":
        return False
    richness = str(inputs.get("material_richness") or "").strip()
    return richness in ("thin", "empty")


def _has_no_image_source(inputs: dict[str, Any]) -> bool:
    """本流程不会有 image_map：无用户本地图，且未启用 AI 生图。

    供 _build_p43_prompt 判定是否需要在大纲规划阶段就抑制"放图"意图。
    """
    image_paths = inputs.get("image_paths") or []
    if image_paths:
        return False
    need_imagegen = bool(inputs.get("need_imagegen", False))
    return not need_imagegen


def _build_structural_page_directive(inputs: dict[str, Any]) -> str:
    """根据 structural_page_request 构建中间结构页指令，注入 P4.3 prompt。

    与 pptx-craft outline-planner 的「中间结构页触发与默认数量规则」对齐：
    - none: 禁止自行添加任何中间结构页
    - agenda/section/chapter: 按指定类型生成，数量由 structural_page_count 或默认规则决定
    - auto: 用户要求章节页但未指定类型，由 LLM 根据语境选择 section 或 chapter
    """
    spr = str(inputs.get("structural_page_request") or "none").strip().lower()
    spc = inputs.get("structural_page_count")
    page_count = inputs.get("page_count")

    if spr == "none":
        return (
            "- 中间结构页：用户未要求任何中间结构页（目录页/章节页/分隔页）。"
            "禁止自行添加 section/chapter/transition/agenda/conclusion 等结构页。"
            "即使内容有章节结构，也通过内容页标题、页内分组和视觉层级承接，不单独占用一页过渡。"
            "总页数 = page_count + 2。\n"
        )

    # 用户要求了中间结构页
    type_hint = {
        "agenda": "agenda（目录页）",
        "section": "section（章节页/章节分隔页）",
        "chapter": "chapter（PART 页/章首页）",
        "auto": "section 或 chapter（根据用户语境自动选择：用户说'PART/章首页'用 chapter，其余用 section）",
    }.get(spr, "section")

    # 数量计算
    if isinstance(spc, int) and spc > 0:
        count_str = f"{spc} 页（用户指定数量）"
        total_structural = spc
    elif isinstance(page_count, int) and page_count > 0:
        if page_count <= 5:
            default_count = 1
        else:
            default_count = -(-page_count // 4)  # ceil(page_count / 4)
        count_str = f"{default_count} 页（用户未指定数量，按默认规则：page_count={page_count} -> {default_count} 页）"
        total_structural = default_count
    else:
        count_str = "1 页（无法确定 page_count，默认 1 页）"
        total_structural = 1

    return (
        f"- 中间结构页：用户已明确要求中间结构页，类型={type_hint}，数量={count_str}。\n"
        f"  规则：\n"
        f"  1. 中间结构页的「研究需求」标 ❌，不计入 page_count 内容页配额\n"
        f"  2. 总页数 = page_count + 2 + {total_structural}（内容页 + 封面/结束页 + 结构页）\n"
        f"  3. 结构页放在每个内容组之前：cover ->（可选 agenda）-> 结构页 1 -> 内容组 1 -> 结构页 2 -> 内容组 2 -> ... -> ending\n"
        f"  4. 每组至少含 2 个内容页，最后一组不得仅含 1 页\n"
        f"  5. 结构页必须有明确的章节编号、章节标题或转场目的；不得为了填页数生成空泛页面\n"
        f"  6. conclusion/transition 不再支持；如需总结页，并入最后的 ending 页\n"
    )


def _build_p43_prompt(
    inputs: dict[str, Any],
    source_material: str,
    search_results_text: str,
) -> str:
    topic = str(inputs.get("topic") or "").strip()
    page_count = inputs.get("page_count")
    audience = str(inputs.get("audience") or "").strip()
    source_type = str(inputs.get("source_type") or "topic").strip()
    search_mode = str(inputs.get("search_mode") or "").strip()
    focus_areas = str(inputs.get("focus_areas") or "").strip()
    presentation_purpose = str(inputs.get("presentation_purpose") or "").strip()
    include_sources = _should_include_searched_sources(inputs)
    degraded = _is_no_search_degraded(inputs)
    user_text = PptCommon.collect_user_text(inputs).strip()

    entity = str(inputs.get("p4_search_entity") or "").strip()

    parts = [
        f"请生成 outline.md 正文，主题：「{topic}」\n",
        f"- page_count: {page_count}（内容页数，不含封面/结束页；默认总页数为 page_count + 2）\n",
        f"- audience: {audience}\n",
        f"- source_type: {source_type}\n",
        f"- search_mode: {search_mode}\n",
        f"- focus_areas: {focus_areas}\n",
    ]
    if entity:
        parts.append(
            f"- 主题实体: {entity}（大纲内容必须基于搜索结果中关于此实体的具体信息，"
            f"禁止用行业通用趋势或竞品功能替代）\n"
        )
    if presentation_purpose:
        parts.append(f"- presentation_purpose: {presentation_purpose}\n")
    if user_text:
        parts.append(f"- 用户原文：{user_text}\n")
    required_sections = PptCommon.normalize_required_sections(
        inputs.get("required_sections")
    )
    if required_sections:
        parts.append(
            "- 用户指定页面清单（优先于原始总页数，必须逐项落实，不得删除或合并）：\n"
            f"{json.dumps(required_sections, ensure_ascii=False)}\n"
            "- page_type=cover 必须由首页承载；agenda 必须生成目录页；"
            "content 每项必须对应一个独立的研究需求✅内容页；"
            "ending 必须由末页承载。目录应列出全部 content 与 ending 业务章节。\n"
        )
    # 模板叙事框架注入（template_canvas 模式下由 P3.5 读取 template-spec.json 获得）
    narrative_framework = str(inputs.get("narrative_framework") or "").strip()
    if narrative_framework:
        parts.append(
            f"- narrative_framework（模板叙事框架，作为软约束注入大纲）：\n{narrative_framework}\n"
        )
    # 无图片来源：topic 模式下从源头抑制"放图"意图，避免下游页面生成时自行产图。
    # outline/description 模式保留用户原文，不注入此约束。
    if source_type == "topic" and _has_no_image_source(inputs):
        parts.append(
            "- 图片素材状态：本流程无图片来源（无用户本地图、未启用 AI 生图）。"
            "「内容概要」字段禁止出现“展示图片/配图/插图/带图”等任何放图承诺；"
            "视觉表达用数据卡片、ECharts 图表、纯色/CSS 图形描述，"
            "不得要求页面出现具体图片。\n"
        )
    parts.append(f"- include_searched_sources_section: {include_sources}\n")
    if degraded:
        parts.append(
            "- 注意：no_search 模式且素材不足，请在大纲中标注「素材有限」相关页面，尽力基于现有素材生成。\n"
        )
    if str(inputs.get("search_mode") or "").strip() == "no_search":
        parts.append(
            '- no_search 模式：研究查询与数据需求仍需填写（描述"如有搜索会查询什么"），但标注为「仅参考」。\n'
        )

    # 中间结构页需求注入
    parts.append(_build_structural_page_directive(inputs))

    failure_reason = inputs.get("failure_reason")
    if isinstance(failure_reason, str) and failure_reason.strip():
        parts.append(f"上次失败原因：\n{failure_reason.strip()}\n")

    if source_material:
        parts.append(f"用户素材（doc_raw）：\n{source_material}\n")
    else:
        parts.append("用户素材：无\n")

    if search_results_text:
        parts.append(f"调研结果（网页搜索）：\n{search_results_text}\n")
    elif include_sources:
        parts.append("调研结果：无（请基于素材与主题生成，已搜索来源章节可留空表格或简要说明）\n")
    else:
        parts.append("调研结果：无（跳过搜索，不要写 ## 已搜索来源 章节）\n")

    parts.append("输出完整 outline.md Markdown 正文。")
    return "\n".join(parts)


def _strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    fence_match = PptCommon.JSON_FENCE_PATTERN.search(stripped)
    if fence_match and stripped.startswith("```"):
        return fence_match.group(1).strip()
    return stripped


def _validate_outline_markdown_basic(
    text: str,
    *,
    topic: str,
    page_count: Any,
    structural_page_request: str = "none",
    structural_page_count: Any = None,
    required_sections: Any = None,
) -> None:
    stripped = text.strip()
    if not stripped:
        raise ContentPlanError("P4.3 生成的 outline 为空")

    if "# 大纲：" not in stripped:
        raise ContentPlanError("P4.3 outline 缺少 `# 大纲：` 标题")

    if topic.strip():
        topic_marker = f"# 大纲：{topic.strip()}"
        if topic_marker not in stripped:
            raise ContentPlanError("P4.3 outline 标题与 topic 不匹配")

    if "## 页面规划" not in stripped:
        raise ContentPlanError("P4.3 outline 缺少 `## 页面规划` 章节")

    page_numbers = [int(match.group(1)) for match in _PAGE_HEADING_PATTERN.finditer(stripped)]
    if not page_numbers:
        raise ContentPlanError("P4.3 outline 缺少 `### P{N}:` 页面块")

    expected_content_pages = int(page_count) if page_count is not None else None
    if expected_content_pages is not None:
        pages = _split_outline_pages(stripped)
        # 仅统计 ✅ 页（研究需求为 ✅ 的内容页），结构页（❌）不计入内容页配额。
        # 使用 < 比较容忍 LLM 多生成内容页，但不容忍内容页不足。
        content_count = sum(1 for _, blk in pages if _is_research_required_page(blk))
        if content_count < expected_content_pages:
            raise ContentPlanError(
                f"P4.3 outline 内容页数（✅）应为 {expected_content_pages}，"
                f"实际 {content_count}"
            )

    # 总页数校验：max(page_numbers) 应等于 page_count + 2(封面/结束) + 结构页数
    # 防止 intent 阶段 page_count 算错导致总页数与用户要求不一致
    if expected_content_pages is not None:
        spr = str(structural_page_request or "none").strip().lower()
        if isinstance(structural_page_count, int) and structural_page_count > 0:
            structural_num = structural_page_count
        elif spr != "none":
            structural_num = 1
        else:
            structural_num = 0
        expected_total = expected_content_pages + 2 + structural_num
        actual_total = max(page_numbers) if page_numbers else 0
        if actual_total != expected_total:
            raise ContentPlanError(
                f"P4.3 outline 总页数应为 {expected_total}"
                f"（内容页{expected_content_pages} + 封面/结束2 + 结构页{structural_num}），"
                f"实际最大页码为 {actual_total}"
            )

    # 遵从 pptx-craft outline-planner Stage 3 产物验证：
    # 首页类型为 cover，末页类型为 ending（conclusion/transition 为别名）
    _struct_pages = _split_outline_pages(stripped)
    if _struct_pages:
        _first_type = _extract_outline_field(_struct_pages[0][1], "类型").strip().lower()
        if _first_type and _first_type not in ("cover", "intro"):
            raise ContentPlanError(
                f"P4.3 outline 首页类型应为 cover，实际为 {_first_type}"
            )
        _last_type = _extract_outline_field(_struct_pages[-1][1], "类型").strip().lower()
        if _last_type and _last_type not in ("ending", "conclusion", "transition"):
            raise ContentPlanError(
                f"P4.3 outline 末页类型应为 ending，实际为 {_last_type}"
            )

    # 中间结构页合法性校验
    _validate_structural_pages(
        _struct_pages,
        structural_page_request=structural_page_request,
        structural_page_count=structural_page_count,
    )

    required_fields = ("**类型**", "**研究需求**", "**标题**", "**内容概要**", "**研究查询**", "**数据需求**")
    for field in required_fields:
        if field not in stripped:
            raise ContentPlanError(f"P4.3 outline 缺少字段 {field}")

    normalized_sections = PptCommon.normalize_required_sections(required_sections)
    if normalized_sections:
        pages = _split_outline_pages(stripped)
        type_aliases = {
            "cover": {"cover", "intro"},
            "agenda": {"agenda"},
            "ending": {"ending", "conclusion"},
        }
        matched_page_indices: set[int] = set()
        for section in normalized_sections:
            expected_title = section["title"].strip()
            expected_type = section["page_type"]
            matched = False
            for page_index, (_, block) in enumerate(pages):
                if page_index in matched_page_indices:
                    continue
                actual_title = _extract_outline_field(block, "标题").strip()
                actual_type = _extract_outline_field(block, "类型").strip().lower()
                if expected_title != actual_title:
                    continue
                if expected_type == "content":
                    matched = _is_research_required_page(block)
                else:
                    matched = actual_type in type_aliases.get(expected_type, set())
                if matched:
                    matched_page_indices.add(page_index)
                    break
            if not matched:
                raise ContentPlanError(
                    f"P4.3 outline 未按指定页型落实章节："
                    f"{expected_title} ({expected_type})"
                )


# 结构页类型集合（❌ 页，不含 cover/ending）
_STRUCTURAL_PAGE_TYPES = frozenset({"agenda", "section", "chapter", "transition", "conclusion"})


def _validate_structural_pages(
    pages: list[tuple[int, str]],
    *,
    structural_page_request: str = "none",
    structural_page_count: Any = None,
) -> None:
    """校验中间结构页合法性，与 pptx-craft outline-planner Stage 3 对齐。

    - structural_page_request="none": 不允许任何中间结构页（仅 cover/ending）
    - structural_page_request="agenda"/"section"/"chapter"/"auto": 允许对应类型的结构页，
      数量校验见下方逻辑
    """
    spr = str(structural_page_request or "none").strip().lower()

    # 收集所有中间结构页（排除首尾的 cover/ending）
    if not pages:
        return
    middle_pages = pages[1:-1]  # 去掉首尾
    found_structural: list[tuple[int, str]] = []  # (page_num, page_type)
    for page_num, blk in middle_pages:
        ptype = _extract_outline_field(blk, "类型").strip().lower()
        if ptype in _STRUCTURAL_PAGE_TYPES:
            found_structural.append((page_num, ptype))

    if spr == "none":
        if found_structural:
            page_list = ", ".join(f"P{n}({t})" for n, t in found_structural)
            raise ContentPlanError(
                f"P4.3 outline 用户未要求中间结构页，但出现了结构页：{page_list}"
            )
        return

    # 用户要求了结构页，校验类型和数量
    allowed_types: set[str]
    if spr == "agenda":
        allowed_types = {"agenda"}
    elif spr == "section":
        allowed_types = {"section"}
    elif spr == "chapter":
        allowed_types = {"chapter"}
    elif spr == "auto":
        allowed_types = {"section", "chapter"}
    else:
        allowed_types = _STRUCTURAL_PAGE_TYPES

    # 检查是否有不允许的结构页类型
    for page_num, ptype in found_structural:
        if ptype not in allowed_types:
            raise ContentPlanError(
                f"P4.3 outline 结构页类型不匹配：P{page_num} 类型为 {ptype}，"
                f"用户要求 structural_page_request={spr}，允许类型为 {allowed_types}"
            )

    # 数量校验（仅当用户明确指定数量时校验，默认规则生成的数量不严格校验）
    if isinstance(structural_page_count, int) and structural_page_count > 0:
        expected = structural_page_count
        actual = len(found_structural)
        if actual != expected:
            raise ContentPlanError(
                f"P4.3 outline 中间结构页数量应为 {expected}，实际 {actual}"
            )


def _split_outline_pages(text: str) -> list[tuple[int, str]]:
    matches = list(_PAGE_HEADING_PATTERN.finditer(text))
    if not matches:
        return []
    pages: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append((int(match.group(1)), text[start:end]))
    return pages


def _extract_outline_field(block: str, field: str) -> str:
    for line_match in _OUTLINE_FIELD_PATTERN.finditer(block):
        if line_match.group("field").strip() == field:
            return line_match.group("value").strip()
    return ""


def _is_research_required_page(block: str) -> bool:
    return "✅" in _extract_outline_field(block, "研究需求")


def _is_placeholder_field_value(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    normalized = stripped
    for suffix in ("（仅参考）", "(仅参考)"):
        normalized = normalized.replace(suffix, "")
    normalized = normalized.strip()
    return normalized in {"-", "—", "–", "无", "N/A", "n/a"}


def _validate_outline_markdown_full(
    text: str,
    *,
    topic: str,
    page_count: Any,
    include_searched_sources: bool,
    structural_page_request: str = "none",
    structural_page_count: Any = None,
    required_sections: Any = None,
) -> None:
    _validate_outline_markdown_basic(
        text,
        topic=topic,
        page_count=page_count,
        structural_page_request=structural_page_request,
        structural_page_count=structural_page_count,
        required_sections=required_sections,
    )

    if include_searched_sources and "## 已搜索来源" not in text:
        raise ContentPlanError("P4.4 outline 缺少 `## 已搜索来源` 章节（搜索模式下必填）")

    if not include_searched_sources and "## 已搜索来源" in text:
        pass  # 允许 LLM 误写，不因此失败

    for page_number, block in _split_outline_pages(text):
        if not _is_research_required_page(block):
            continue
        research_queries = _extract_outline_field(block, "研究查询")
        data_needs = _extract_outline_field(block, "数据需求")
        if _is_placeholder_field_value(research_queries):
            raise ContentPlanError(
                f"P4.4 P{page_number} 研究需求为 ✅，但缺少有效 **研究查询**"
            )
        if _is_placeholder_field_value(data_needs):
            raise ContentPlanError(
                f"P4.4 P{page_number} 研究需求为 ✅，但缺少有效 **数据需求**"
            )


def _outline_validate_kwargs(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic": str(inputs.get("topic") or "").strip(),
        "page_count": inputs.get("page_count"),
        "include_searched_sources": _should_include_searched_sources(inputs),
        "structural_page_request": str(inputs.get("structural_page_request") or "none"),
        "structural_page_count": inputs.get("structural_page_count"),
        "required_sections": inputs.get("required_sections"),
    }


def _outline_full_error(text: str, inputs: dict[str, Any]) -> str | None:
    try:
        _validate_outline_markdown_full(text, **_outline_validate_kwargs(inputs))
        return None
    except ContentPlanError as exc:
        return str(exc)


_URL_IN_TEXT_PATTERN = re.compile(r"https?://[^\s\]\)\"'<>]+")
_OUTLINE_TITLE_LINE_PATTERN = re.compile(r"(?m)^#\s*大纲\s*[：:].*$")


def _fix_outline_title_line(text: str, topic: str) -> str:
    topic = topic.strip()
    if not topic or not _OUTLINE_TITLE_LINE_PATTERN.search(text):
        return text
    return _OUTLINE_TITLE_LINE_PATTERN.sub(f"# 大纲：{topic}", text, count=1)


def _build_searched_sources_section(inputs: dict[str, Any]) -> str | None:
    rows: list[str] = []
    seen: set[str] = set()
    for batch in inputs.get("search_results") or []:
        if not isinstance(batch, dict):
            continue
        dim = str(batch.get("dimension") or "").strip() or "-"
        result_text = str(batch.get("result") or "")
        urls = _URL_IN_TEXT_PATTERN.findall(result_text)
        if urls:
            for url in urls:
                key = url.rstrip(".,;）)")
                if key in seen:
                    continue
                seen.add(key)
                rows.append(f"| {key} | {dim} |")
            continue
        query = str(batch.get("query") or "").strip()
        if query and query not in seen:
            seen.add(query)
            rows.append(f"| {query} | {dim} |")
    if not rows:
        return None
    return (
        "## 已搜索来源\n\n"
        "| URL | 覆盖维度 |\n"
        "| --- | --- |\n"
        + "\n".join(rows)
        + "\n"
    )


def _replace_outline_field_value(block: str, field: str, new_value: str) -> str:
    pattern = re.compile(
        rf"(^[ \t]*-?\s*\*\*{re.escape(field)}\*\*[：:][ \t]*)(.+)$",
        re.MULTILINE,
    )
    return pattern.sub(lambda m: m.group(1) + new_value, block, count=1)


def _fallback_queries_text(inputs: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in inputs.get("p4_search_queries") or []:
        if isinstance(item, dict):
            q = str(item.get("query") or "").strip()
        else:
            q = str(item).strip()
        if q:
            parts.append(q)
        if len(parts) >= 3:
            break
    return "；".join(parts)


def _fix_placeholder_research_fields(text: str, inputs: dict[str, Any]) -> str:
    queries_fallback = _fallback_queries_text(inputs)
    pages = _split_outline_pages(text)
    if not pages:
        return text
    pieces: list[str] = []
    cursor = 0
    for _page_num, block in pages:
        start = text.find(block, cursor)
        if start < 0:
            continue
        pieces.append(text[cursor:start])
        new_block = block
        if _is_research_required_page(block):
            if _is_placeholder_field_value(_extract_outline_field(block, "研究查询")):
                fill = queries_fallback or _extract_outline_field(block, "内容概要").strip()
                if fill and not _is_placeholder_field_value(fill):
                    new_block = _replace_outline_field_value(new_block, "研究查询", fill)
            if _is_placeholder_field_value(_extract_outline_field(new_block, "数据需求")):
                fill = _extract_outline_field(new_block, "内容概要").strip()
                if fill and not _is_placeholder_field_value(fill):
                    new_block = _replace_outline_field_value(new_block, "数据需求", fill)
        pieces.append(new_block)
        cursor = start + len(block)
    pieces.append(text[cursor:])
    return "".join(pieces)


def _normalize_outline_contract(text: str, inputs: dict[str, Any]) -> str:
    """写盘/校验前确定性规范化：标题对齐 topic、补已搜索来源、回填占位字段。不编造事实。"""
    out = text
    topic = str(inputs.get("topic") or "").strip()
    if topic:
        out = _fix_outline_title_line(out, topic)
    if _should_include_searched_sources(inputs) and "## 已搜索来源" not in out:
        section = _build_searched_sources_section(inputs)
        if section and "## 页面规划" in out:
            out = out.replace("## 页面规划", f"{section}\n## 页面规划", 1)
    return _fix_placeholder_research_fields(out, inputs)


def _resolve_outline_path(inputs: dict[str, Any]) -> Path:
    outline_path = inputs.get("outline_path")
    if outline_path:
        return Path(str(outline_path)).expanduser().resolve()

    output_dir = inputs.get("output_dir")
    if not output_dir:
        raise ContentPlanError("P4.4 缺少 outline_path 与 output_dir")
    return (Path(str(output_dir)).expanduser() / _OUTLINE_NAME).resolve()


async def _run_p44_validate(node: PlanNode, inputs: dict[str, Any]) -> None:
    outline_path = _resolve_outline_path(inputs)
    outline_text = await PptCommon.read_file(
        node,
        outline_path,
        required=True,
        label=_OUTLINE_NAME,
        error_type=ContentPlanError,
    )

    normalized = _normalize_outline_contract(outline_text, inputs)
    err = _outline_full_error(normalized, inputs)
    if err:
        raise ContentPlanError(err)
    if normalized != outline_text:
        await _write_outline(node, outline_path.parent, normalized)
        logger.info("[P4.4] outline normalized before pass")

    inputs["outline_path"] = str(outline_path)
    inputs["p4_validate_status"] = "passed"
    inputs["content_plan_status"] = "completed"


async def _write_outline(
    node: PlanNode,
    output_dir: str | Path,
    content: str,
) -> Path:
    path = Path(str(output_dir)).expanduser() / _OUTLINE_NAME
    written = await PptCommon.write_file(
        node,
        path,
        content,
        label=_OUTLINE_NAME,
        error_type=ContentPlanError,
    )
    logger.info("[P4] %s 已落盘：%s", _OUTLINE_NAME, written)
    return written


def _check_insufficient_info(outline_text: str, inputs: dict[str, Any]) -> None:
    """检查 LLM 是否标记了信息不足，若是则 raise ContentPlanError 触发重试/fallback。"""
    if _INSUFFICIENT_INFO_MARKER not in outline_text:
        return

    marker_pos = outline_text.find(_INSUFFICIENT_INFO_MARKER)
    detail = outline_text[marker_pos + len(_INSUFFICIENT_INFO_MARKER):].strip()
    topic = str(inputs.get("topic") or "").strip()
    entity = str(inputs.get("p4_search_entity") or "").strip()
    entity_hint = f"（主题实体：{entity}）" if entity else ""

    search_target = entity or topic
    raise ContentPlanError(
        f"P4.3 信息不足自检触发：搜索结果中关于主题「{topic}」{entity_hint}的"
        f"实体特定信息不充分。{detail}"
        f"\n\n补充搜索指令：现有搜索结果不足以支撑大纲生成。"
        f"你必须先使用 web_search 工具搜索更多关于「{search_target}」的信息"
        f"（建议查询：'{search_target} 官网'、'{search_target} 是什么'、'{search_target} 产品功能'），"
        f"再基于补充后的搜索结果生成大纲。"
        f"禁止仅凭现有搜索结果推断或编造功能。"
    )


async def _run_p43_outline_gen(node: PlanNode, inputs: dict[str, Any]) -> None:
    _require_p4_prerequisites(inputs)

    source_type = str(inputs.get("source_type") or "topic").strip()
    if source_type not in _VALID_SOURCE_TYPES:
        raise ContentPlanError(f"P4.3 无效的 source_type: {source_type!r}")

    source_material = await PptCommon.read_file(
        node,
        inputs.get("doc_raw_path"),
        max_chars=_SOURCE_MATERIAL_MAX_CHARS,
        error_type=ContentPlanError,
    )
    search_results_text = ""
    search_results = inputs.get("search_results")
    if search_results:
        search_results_text = _format_search_results_for_p43(search_results)

    async def _generate_once() -> str:
        response = await node.stream_llm_collect(
            _build_p43_prompt(inputs, source_material, search_results_text),
            system_prompt=_p43_system_prompt(source_type),
        )
        if not isinstance(response, str) or not response.strip():
            raise ContentPlanError("P4.3 失败：LLM 返回为空")
        text = _strip_markdown_fence(response)
        _check_insufficient_info(text, inputs)
        return text

    raw_outline = await _generate_once()
    outline_text = _normalize_outline_contract(raw_outline, inputs)
    err = _outline_full_error(outline_text, inputs)
    if err:
        # normalize 未改动 → 问题不在契约表面，再生成易白烧一轮；直接交 DeepAgent
        if outline_text == raw_outline:
            logger.info("[P4.3] give_up_to_fallback (normalize noop): %s", err[:120])
            raise ContentPlanError(err)
        logger.info("[P4.3] local_regen after normalize: %s", err[:120])
        inputs["failure_reason"] = err
        outline_text = _normalize_outline_contract(await _generate_once(), inputs)
        err = _outline_full_error(outline_text, inputs)
        if err:
            logger.info("[P4.3] give_up_to_fallback: %s", err[:120])
            raise ContentPlanError(err)

    _all_page_nums = [int(m.group(1)) for m in _PAGE_HEADING_PATTERN.finditer(outline_text)]
    inputs["total_pages"] = max(_all_page_nums) if _all_page_nums else 0

    outline_path = await _write_outline(
        node,
        str(inputs["output_dir"]),
        outline_text,
    )
    inputs["outline_path"] = str(outline_path)
    inputs["p4_outline_gen_status"] = "completed"
    inputs["content_plan_status"] = "outline_generated"


class P41NormalizeNode(PlanNode):
    """P4.1 — 需求标准化与素材充裕度评估。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p4_1_normalize",
            instruction=(
                "## P4.1 需求标准化与素材评估\n"
                "\n"
                "### 节点职责\n"
                "读取素材 → LLM 评估充裕度 → 计算 p4_should_search。\n"
                "\n"
                "### 前置条件\n"
                "- `read_file` / `stream_llm` 工具可用\n"
                "- `output_dir` 已由 P0 创建\n"
                "\n"
                "### 输入\n"
                "- `doc_raw_path`（可选）: 文档素材路径（有则读取，无则 source_material 为空）\n"
                "- `search_mode`（必填）: 搜索策略\n"
                "- `topic`（必填）: PPT 主题\n"
                "\n"
                "### 输出\n"
                "- `has_source_material`: bool — 是否有素材\n"
                "- `source_material_chars`: int — 素材字符数\n"
                "- `material_richness`: str — 充裕度评估（rich / moderate / poor / empty）\n"
                "- `focus_areas`: list[str] — 重点领域\n"
                "- `p4_should_search`: bool — 是否需要 P4.2 搜索\n"
                "- `p4_search_reason`: str — 搜索或不搜索的原因\n"
                "- `content_plan_status`: str = 'normalizing'\n"
                "\n"
                "### 执行流程\n"
                "1. 读取 doc_raw_path 作为 source_material\n"
                "2. call_llm 评估 material_richness\n"
                "3. 按 search_mode × 素材充裕度规则表计算 p4_should_search\n"
                "\n"
                "### 失败兜底\n"
                "- doc_raw_path 不存在或为空: has_source_material=False, material_richness='empty'\n"
                "- LLM 评估失败: raise ContentPlanError\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        await _run_p41_normalize(self, inputs)
        return inputs


class P42QuickResearchNode(PlanNode):
    """P4.2 — 条件化快速调研：生成 query → 并行 web_search，搜索结果直接传递给 P4.3。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p4_2_quick_research",
            instruction=(
                "## P4.2 条件快速调研\n"
                "\n"
                "### 节点职责\n"
                "p4_should_search=True 时：LLM 生成搜索 query → 并行 web_search → 搜索结果供 P4.3 使用。\n"
                "p4_should_search=False 时：跳过。\n"
                "\n"
                "### 前置条件\n"
                "- P4.1 已完成，`p4_should_search` 已确定\n"
                "- `stream_llm` / `web_search` 工具可用（仅 p4_should_search=True 时需要）\n"
                "\n"
                "### 输入\n"
                "- `p4_should_search`（必填）: 是否需要搜索\n"
                "- `topic`（必填）: PPT 主题（生成搜索 query 的依据）\n"
                "- `focus_areas`（可选）: 重点领域（辅助 query 生成）\n"
                "\n"
                "### 输出\n"
                "p4_should_search=True 时：\n"
                "- `search_results`: list[dict[str, str]] — 搜索结果批次列表（每项含 query/dimension/result）\n"
                "- `p4_search_queries`: list[str] — 本次搜索使用的 query 列表\n"
                "- `p4_search_hit_count`: int — 搜索命中数量\n"
                "- `p4_quick_research_status`: str = 'completed'\n"
                "\n"
                "p4_should_search=False 时：\n"
                "- `p4_quick_research_status`: str = 'skipped'\n"
                "- `search_results` 为空 / 不存在\n"
                "\n"
                "### 执行流程\n"
                "1. p4_should_search=False → 直接跳过，写入 skipped\n"
                "2. p4_should_search=True → LLM 生成固定批次 query（含 entity 实体名）\n"
                "3. 并行 web_search，汇总搜索结果\n"
                "4. 相关性闸门：无可用结果→扩搜（换同义词/英文）；有结果但不相关→收窄（聚焦实体名）\n"
                "5. 最多 2 轮重搜；任一轮判定 sufficient 即完成\n"
                "\n"
                "### 失败兜底\n"
                "- web_search 全部失败: raise ContentPlanError\n"
                "- 2 轮重搜后仍无可用结果或仍不相关: raise ContentPlanError（交由 P4 整体重试 / fallback 兜底，不向下游传错误信息）\n"
                "- LLM 生成 query 失败: 使用 topic 直接作为单条 query 搜索\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        if not inputs.get("p4_should_search"):
            inputs["p4_quick_research_status"] = "skipped"
            return inputs

        await _run_p42_quick_research(self, inputs)
        return inputs


class P43OutlineGenNode(PlanNode):
    """P4.3 — 按 source_type 生成 outline.md。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p4_3_outline_gen",
            instruction=(
                "## P4.3 大纲生成\n"
                "\n"
                "### 节点职责\n"
                "按 source_type 策略 LLM 生成大纲，write_file 落盘 outline.md。\n"
                "\n"
                "### 前置条件\n"
                "- P4.1 已完成，素材评估已产出\n"
                "- P4.2 已完成（或已跳过），search_results 已确定\n"
                "- `stream_llm` / `write_file` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `output_dir`（必填）: 工作目录\n"
                "- `source_type`（必填）: 素材来源策略\n"
                "- `topic`（必填）: PPT 主题\n"
                "- `page_count`（必填）: 页数\n"
                "- `search_results`（可选）: P4.2 搜索结果（有则辅助大纲生成）\n"
                "- `source_material` / `focus_areas`（可选）: P4.1 产出的素材与重点领域\n"
                "\n"
                "### 输出\n"
                "- `outline_path`: str — `{output_dir}/outline.md` 绝对路径（文件已写入）\n"
                "- `p4_outline_gen_status`: str = 'completed'\n"
                "- `content_plan_status`: str = 'outline_generated'\n"
                "\n"
                "### 执行流程\n"
                "1. 读取 search_results（如有）与素材\n"
                "2. 按 source_type 策略构造 LLM prompt\n"
                "3. call_llm 生成大纲 Markdown\n"
                "4. write_file 落盘 `{output_dir}/outline.md`\n"
                "\n"
                "### outline.md 格式规范（必须严格遵守）\n"
                f"{_P43_COMMON_RULES}\n"
                "\n"
                "### 失败兜底\n"
                "- LLM 生成空内容: raise ContentPlanError\n"
                "- write_file 失败: raise ContentPlanError\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        await _run_p43_outline_gen(self, inputs)
        return inputs


class P44ValidateNode(PlanNode):
    """P4.4 — 产物校验：outline.md 结构完整；搜索模式下校验已搜索来源章节。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p4_4_validate",
            instruction=(
                "## P4.4 产物校验\n"
                "\n"
                "### 节点职责\n"
                "读取 outline.md → 规则校验结构与内容完整性。\n"
                "\n"
                "### 前置条件\n"
                "- P4.3 已完成，`outline_path` 指向已落盘的 outline.md\n"
                "- `read_file` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `outline_path`（必填）: outline.md 文件路径\n"
                "- `search_mode`（可选）: 搜索模式下需校验已搜索来源章节\n"
                "\n"
                "### 输出\n"
                "校验通过时：\n"
                "- `p4_validate_status`: str = 'passed'\n"
                "- `content_plan_status`: str = 'completed'\n"
                "\n"
                "校验失败时：\n"
                "- raise ContentPlanError，触发 P4 整体重试\n"
                "\n"
                "### 执行流程\n"
                "1. read_file 读取 outline.md\n"
                "2. 校验结构标记：`# 大纲：`、`## 页面规划`\n"
                "3. 校验 ✅ 页研究查询 / 数据需求\n"
                "4. 搜索模式下校验 `## 已搜索来源` 章节\n"
                "\n"
                "### outline.md 合规格式（校验不通过时需按此格式修复后重写文件）\n"
                f"{_P43_COMMON_RULES}\n"
                "\n"
                "### 失败兜底\n"
                "- outline.md 不存在或为空: raise ContentPlanError\n"
                "- 结构不完整: raise ContentPlanError，触发 P4 从 P4.1 重跑（最多 1 次重试）\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        await _run_p44_validate(self, inputs)
        return inputs


class ContentPlanNode(PlanNode):
    """P4 — 内容策划（P4.1 → P4.2 → P4.3 → P4.4）。

    预期输入（ctx / inputs，应由 P0/P2/P3 就绪）:
        必填: topic, page_count, audience, search_mode, source_type, output_dir
        可选: presentation_purpose, doc_raw_path, has_documents, doc_parse_ok
        可选: failure_reason — P4 整体重试时附带

    预期输出（P4.1 完成后写入）:
        has_source_material, source_material_chars, material_richness, focus_areas
        p4_should_search, p4_search_reason, content_plan_status

    预期输出（P4.2 完成后追加）:
        search_results, p4_search_queries, p4_search_hit_count
        p4_quick_research_status（completed | skipped）

    预期输出（P4.3 完成后追加）:
        outline_path, p4_outline_gen_status, content_plan_status=outline_generated

    预期输出（P4.4 校验通过后）:
        p4_validate_status=passed, content_plan_status=completed

    重试：P4 整体最多 2 次（初始 + 1 次重试，从 P4.1 重跑），失败时写入 failure_reason。
    """

    def __init__(self) -> None:
        super().__init__(
            plan_name="p4_content_plan",
            instruction=(
                "## P4 内容策划\n"
                "\n"
                "### 节点职责\n"
                "1. 需求标准化与素材充裕度评估（P4.1）\n"
                "2. 条件化快速调研（P4.2）\n"
                "3. 生成大纲 outline.md（P4.3）\n"
                "4. 产物校验（P4.4）\n"
                "P4 整体最多 2 次尝试（初始 + 1 次重试，从 P4.1 重跑）。\n"
                "\n"
                "### 前置条件\n"
                "- P0/P2/P3 已完成，`topic`, `page_count`, `audience`, `search_mode`, `source_type`, `output_dir` 已就绪\n"
                "- `stream_llm` / `web_search` / `read_file` / `write_file` 工具可用\n"
                "\n"
                "### 输入\n"
                "- `topic`（必填）: PPT 主题\n"
                "- `page_count`（必填）: 页数\n"
                "- `audience`（必填）: 受众\n"
                "- `search_mode`（必填）: 搜索策略\n"
                "- `source_type`（必填）: 素材来源\n"
                "- `output_dir`（必填）: 工作目录\n"
                "- `presentation_purpose`（可选）: 演示目的\n"
                "- `doc_raw_path` / `has_documents` / `doc_parse_ok`（可选）: 文档相关\n"
                "- `failure_reason`（可选）: P4 整体重试时附带\n"
                "\n"
                "### 输出\n"
                "P4.1 完成后：\n"
                "- `has_source_material`, `source_material_chars`, `material_richness`, `focus_areas`\n"
                "- `p4_should_search`, `p4_search_reason`, `content_plan_status`\n"
                "\n"
                "P4.2 完成后追加：\n"
                "- `search_results`, `p4_search_queries`, `p4_search_hit_count`\n"
                "- `p4_quick_research_status`: completed | skipped\n"
                "\n"
                "P4.3 完成后追加：\n"
                "- `outline_path`: `{output_dir}/outline.md` 绝对路径\n"
                "- `p4_outline_gen_status`: 'completed'\n"
                "- `content_plan_status`: 'outline_generated'\n"
                "\n"
                "P4.4 校验通过后：\n"
                "- `p4_validate_status`: 'passed'\n"
                "- `content_plan_status`: 'completed'\n"
                "\n"
                "### 执行流程\n"
                "1. P4.1: 读取素材 → LLM 评估充裕度 → 计算 p4_should_search\n"
                "2. P4.2: p4_should_search=True 时并行 web_search，否则跳过\n"
                "3. P4.3: 按 source_type 策略 LLM 生成大纲 → write_file 落盘 outline.md\n"
                "4. P4.4: read_file 读取 outline.md → 规则校验结构与内容\n"
                "\n"
                "### 失败兜底\n"
                "- P4.4 校验失败: raise ContentPlanError，触发 P4 整体重试（最多 1 次重试）\n"
                "- 2 次均失败: 写入 failure_reason，由根节点决定后续处理\n"
            ),
            sub_plans=[
                P41NormalizeNode(),
                P42QuickResearchNode(),
                P43OutlineGenNode(),
                P44ValidateNode(),
            ],
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        ctx = inputs
        last_error: str | None = None

        for attempt in range(_P4_MAX_ATTEMPTS):
            if attempt:
                ctx["failure_reason"] = last_error or "P4 校验失败"
                ctx["p4_retry_count"] = attempt

            try:
                for subplan in self.sub_plans:
                    await self.execute_subplan(subplan, ctx)
                ctx["__artifact__"] = {
                    "files": [
                        {"path": str(ctx.get("outline_path", "")), "desc": "PPT大纲"}
                    ] if ctx.get("outline_path") else [],
                }
                return ctx
            except ContentPlanError as exc:
                last_error = str(exc)
                ctx["p4_validate_status"] = "failed"
                if attempt + 1 >= _P4_MAX_ATTEMPTS:
                    ctx["failure_reason"] = last_error
                    raise

        raise ContentPlanError(last_error or "P4 失败")
