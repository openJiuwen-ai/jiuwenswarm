"""SOP document ingestion and structured extraction.

Loads plain text from files (see ``_extract_raw_text``), then extracts a full
``SOPStructure`` via the LLM prompts in this module and ``sop_chunk_merge``
(single-shot or chunked, budget-aware).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .models import SOPStructure
from .sop_chunk_merge import extract_structure_chunked, extract_structure_single_shot

logger = logging.getLogger(__name__)

DEFAULT_SINGLE_SHOT_BUDGET = 12000
DEFAULT_MAX_CHUNK_CHARS = 8000
DEFAULT_CHUNK_OVERLAP = 1200
DEFAULT_MAX_CONTEXT_CHARS = 120_000
DEFAULT_SAFETY_MARGIN = 2000

_SOP_EXTRACTION_PROMPT = """\
你是一名企业 SOP 结构化分析专家，擅长从各种格式的 SOP / 政策 / 流程文档中提取结构化信息。

**体裁说明**：下文中的行业场景、数字与流程**仅用于示范抽取 JSON 的格式**（提示工程常见做法），为虚构或合成示意，**不代表**产品与任何特定客户、业务线或内部制度绑定。对真实输入文档应忠实于原文，勿套用下列范例中的具体数值或域名。

## 任务

给你一份 SOP 原始文本，请完成以下两步：

### 第一步：判断 SOP 类型

先通读全文，判断该文档属于以下哪种类型：

- **procedural**（流程型）：文档以**按时间顺序执行的操作步骤**为主。大部分内容是"第一步做什么、第二步做什么"的指令序列。例：员工入职流程、软件发布审批流程。
- **knowledge**（知识/政策型）：文档以**规则、标准、条件、限额、场景分支**为主。可能有一个简短的流程骨架，但大部分篇幅是各种场景下的具体规定和参考信息。例：变更窗口与发布管控规定、客服工单 SLA、合规抽检比例、**事项登记/办结超期与阶梯扣减**类规定、数据安全管理规定。
- **hybrid**（混合型）：既有清晰的流程步骤，又有大量嵌入式规则和条件分支。例：采购审批流程（流程清晰，但不同金额/类别有不同审批规则）。

### 第二步：按类型提取结构化信息

#### `steps`（操作步骤）—— 仅提取真正的顺序操作

steps 只放**按时间先后执行的具体操作动作**。判断标准：这件事是某个人/系统在某个时间点**做**的一个动作。

✅ 应放入 steps：
- "运维工程师在变更系统中提交生产发布申请并附带回滚方案" —— 这是一个动作
- "客服坐席在工单系统中受理客户请求并创建工单" —— 这是一个动作
- "经办人在系统完成流程办结登记并提交时间戳确认" —— 这是一个动作（仅为跨领域格式示意）

❌ 不应放入 steps：
- "生产环境周五 18:00 至周一 10:00 禁止未经审批的变更" —— 这是一条规则，放 knowledge_items
- "P2 工单须在 4 小时内首次响应" —— 这是一个 SLA 参数，放 knowledge_items
- "审计发现的高风险缺陷须在 30 日内闭环整改" —— 这是一条时限罚则，放 knowledge_items

对于 knowledge 型文档，steps 可能很少（3-5 个骨架步骤），这是正常的。不要为了填充 steps 而把规则伪装成步骤。

#### `knowledge_items`（知识条目）—— 提取规则、标准、阈值、条件、罚则

knowledge_items 放**非流程性的参考知识**：规则、政策、标准、限额、条件判断、处罚规定、场景特例、速查信息。

每条 knowledge_item 必须是**自包含的完整语句**，包含该规则的条件和结论。读者不需要看上下文就能理解这条规则的含义。

✅ 好的 knowledge_item（具体、自包含、保留原文数字）：
- "生产环境变更须通过 CAB 评审；紧急变更须在事后 24 小时内补录评审记录"
- "P1 级故障须在识别后 15 分钟内通知值班负责人并建立应急沟通渠道"
- "正式登记日晚于基准事件日 90 天至 365 天的，系统按制度实施阶梯扣减；超过 365 天关闭补录或不予受理（仅为跨领域体裁示意）"
- "合规抽检：高风险流程每年全覆盖，中风险流程每年随机抽检不少于 30% 笔数"
- "客户投诉类工单须在创建后 24 小时内由客服组长复核处理结论并回复客户"

❌ 差的 knowledge_item（模糊、缺少数字、需要上下文才能理解）：
- "变更有窗口限制" —— 缺少具体时段或例外条件
- "工单响应要快" —— 没有保留具体时限
- "超期会有扣减" —— 没有保留具体比例和时间

**提取原则：宁多勿少。** 宁可多提取几条规则也不要遗漏重要的标准和阈值。原文中出现的具体数字、金额、比例、时间限制、重量限制等必须原样保留在 knowledge_item 中。

#### `sections`（章节结构）—— 保留文档的层级组织

提取文档中的章节/小节层级结构。每个 section 包含：
- `id`：原文编号（如 "1.1", "2.3.1", "Part 2"），如无编号则用 "s1", "s2" 等
- `title`：章节标题
- `content_summary`：该章节主要内容的一句话概述

#### 其他字段

- `decision_points`：流程中的条件分支（"如果A则B，否则C"）
- `exceptions`：异常/边界场景的处理方式
- `references`：引用的外部文档、系统、URL

如果某字段在原文中未明确提及，留空字符串或空列表，不要编造。

**JSON 硬约束（必须遵守）**：`steps[].step_number` 与 `sections[].id` 必须是**带双引号的字符串**（例如 `"1"`、`"1.1"`、`"2.3.1"`）。**禁止**输出未加引号的层级编号如 `1.1.1`——在 JSON 中这是非法数字，会导致解析失败。

## 输出 JSON 格式

```json
{{
  "title": "SOP 标题",
  "purpose": "SOP 目的/目标",
  "scope": "适用范围",
  "sop_type": "procedural | knowledge | hybrid",
  "roles": ["角色1", "角色2"],
  "steps": [
    {{
      "step_number": "1",
      "actor": "执行者/角色",
      "action": "具体操作描述",
      "system": "使用的工具或系统（如有）",
      "output": "预期输出/交付物",
      "notes": "条件、注意事项"
    }}
  ],
  "knowledge_items": [
    "自包含的规则/标准/阈值/条件语句，保留原文数字"
  ],
  "sections": [
    {{
      "id": "2.1",
      "title": "章节标题",
      "content_summary": "该章节主要内容一句话概述"
    }}
  ],
  "decision_points": [
    "条件分支描述"
  ],
  "exceptions": [
    "异常/边界场景处理方式"
  ],
  "references": [
    "引用的外部文档或系统"
  ]
}}
```

## 示例 A：流程型 SOP

**输入片段**：
> # 员工入职流程
> 1. HR 在系统中创建新员工账号
> 2. IT 部门配置工位和电脑
> 3. 部门经理安排入职培训
> 4. 员工签署保密协议

**预期输出**：
```json
{{
  "sop_type": "procedural",
  "steps": [
    {{"step_number": "1", "actor": "HR", "action": "在系统中创建新员工账号", "system": "HR系统", "output": "员工账号", "notes": ""}},
    {{"step_number": "2", "actor": "IT部门", "action": "配置工位和电脑", "system": "", "output": "工位和设备就绪", "notes": ""}},
    {{"step_number": "3", "actor": "部门经理", "action": "安排入职培训", "system": "", "output": "", "notes": ""}},
    {{"step_number": "4", "actor": "员工", "action": "签署保密协议", "system": "", "output": "已签署的保密协议", "notes": ""}}
  ],
  "knowledge_items": [],
  "sections": []
}}
```

## 示例 B：知识/政策型 SOP（客服工单 SLA 与升级 — 虚构示意）

**输入片段**：
> # 客服工单处理与升级规范
> 1.1 一线坐席在系统中创建工单并标注优先级（P1–P4）
> 1.2 升级规则：P2 故障若 4 小时内无二线接手，自动升级到值班主管；投诉类工单须在 24 小时内由组长复核结论
> 2.1 P1：核心业务不可用，须在 15 分钟内电话通知值班负责人并拉通应急群
> 2.2 P3–P4：工作日按队列顺序处理，单工单无故搁置不得超过 48 小时
> 3.1 知识库已覆盖的咨询类问题，首次响应须在 30 分钟内给出标准答复或文档链接

**预期输出**：
```json
{{
  "sop_type": "hybrid",
  "steps": [
    {{"step_number": "1", "actor": "一线坐席", "action": "在系统中创建工单并标注优先级", "system": "工单系统", "output": "已分级工单", "notes": ""}},
    {{"step_number": "2", "actor": "二线工程师", "action": "接手故障工单并排查", "system": "", "output": "排查记录或解决方案", "notes": "按优先级 SLA"}}
  ],
  "knowledge_items": [
    "P2 级故障工单若 4 小时内无二线工程师接手，系统自动升级并通知值班主管",
    "投诉类工单须在创建后 24 小时内由客服组长复核处理结论并回复客户",
    "P1 级（核心业务不可用）须在识别后 15 分钟内电话通知值班负责人并建立应急沟通群",
    "P3、P4 级工单在工作日须按队列顺序处理，单条工单无故搁置不得超过 48 小时",
    "知识库已覆盖的咨询类工单，首次响应须在 30 分钟内给出标准答复或有效文档链接"
  ],
  "sections": [
    {{"id": "1.1", "title": "工单创建与分级", "content_summary": "坐席创建工单并标注 P1–P4"}},
    {{"id": "1.2", "title": "升级规则", "content_summary": "超时自动升级与投诉复核时限"}},
    {{"id": "2.1", "title": "P1 应急响应", "content_summary": "15 分钟内通知负责人并建群"}},
    {{"id": "2.2", "title": "P3–P4 处理", "content_summary": "队列处理与搁置上限"}},
    {{"id": "3.1", "title": "咨询类 SLA", "content_summary": "30 分钟内标准答复或文档"}}
  ],
  "decision_points": [
    "工单为 P1 时是否立即启动应急通知流程",
    "投诉类是否必须在 24 小时内经组长复核"
  ],
  "exceptions": [],
  "references": []
}}
```

## 示例 C：登记时效与阶梯扣减（节选，仅示范「时限 + 数字」体裁，虚构）

**输入片段**：
> # 通用流程办结与登记时效（节选，虚构示意）
> 4.1 正式登记日晚于基准事件日 90 天提交的，系统按制度实施阶梯扣减；超过 365 天的不予受理或关闭补录
> 4.2 缺失必填佐证材料时须在系统选择「缺件声明」，并按制度计提扣减（具体比例以原文为准）

**预期输出**：
```json
{{
  "sop_type": "knowledge",
  "steps": [],
  "knowledge_items": [
    "正式登记日晚于基准事件日 90 天的，系统按制度实施阶梯扣减；超过 365 天提交的不予受理",
    "缺失必填佐证材料时须在系统选择「缺件声明」，并按制度计提扣减，扣减比例以制度原文记载为准"
  ],
  "sections": [
    {{"id": "4.1", "title": "登记时效", "content_summary": "90 天与 365 天两条时限及阶梯扣减或不予受理"}},
    {{"id": "4.2", "title": "缺件与扣减", "content_summary": "系统声明选项与扣减依原文"}}
  ],
  "decision_points": [],
  "exceptions": [],
  "references": []
}}
```

## SOP 原始文本

{raw_text}

请只输出 JSON，不要输出其他内容。"""


def _single_shot_prompt_overhead() -> int:
    """Length of single-shot template with empty raw_text placeholder."""
    return len(_SOP_EXTRACTION_PROMPT) - len("{raw_text}")


def _room_for_sop_single(max_context_chars: int, safety_margin: int) -> int:
    """Max SOP chars that fit in a single-shot prompt."""
    return max(1, max_context_chars - _single_shot_prompt_overhead() - safety_margin)


def _default_parse_options(options: dict[str, Any] | None) -> dict[str, Any]:
    opts = dict(options or {})
    opts.setdefault("sop_parse_mode", "auto")
    opts.setdefault("single_shot_budget", DEFAULT_SINGLE_SHOT_BUDGET)
    opts.setdefault("max_chunk_chars", DEFAULT_MAX_CHUNK_CHARS)
    opts.setdefault("chunk_overlap", DEFAULT_CHUNK_OVERLAP)
    opts.setdefault("max_context_chars", DEFAULT_MAX_CONTEXT_CHARS)
    opts.setdefault("safety_margin", DEFAULT_SAFETY_MARGIN)
    return opts


async def parse_sop_raw_text(
    raw_text: str,
    *,
    invoke_llm_json: Callable[..., Any],
    parse_options: dict[str, Any] | None = None,
    source_label: str = "",
) -> tuple[SOPStructure, dict[str, Any]]:
    """Run SOP structure extraction on already-loaded plain text (file, URL body, etc.).

    ``invoke_llm_json`` is required: full ``SOPStructure`` extraction is LLM-only
    (prompts in this module and ``sop_chunk_merge``).

    ``source_label`` is stored in ``extraction_meta`` (e.g. file path or URL).
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise ValueError("SOP 文本为空")

    opts = _default_parse_options(parse_options)
    mode = str(opts["sop_parse_mode"]).strip().lower()
    max_context = int(opts["max_context_chars"])
    safety_margin = int(opts["safety_margin"])
    room_single = _room_for_sop_single(max_context, safety_margin)
    single_shot_cap = int(opts["single_shot_budget"])
    if single_shot_cap > 0 and single_shot_cap < room_single:
        room_single = single_shot_cap
    max_chunk = int(opts["max_chunk_chars"])
    overlap = int(opts["chunk_overlap"])

    use_chunked: bool
    if mode == "chunked":
        use_chunked = True
    elif mode == "single":
        use_chunked = False
    else:
        # auto: chunk only when full prompt (template + SOP) would exceed context
        use_chunked = len(raw_text) > room_single

    extraction_meta: dict[str, Any]

    if use_chunked:
        sop, extraction_meta = await extract_structure_chunked(
            raw_text,
            invoke_llm_json,
            max_context_chars=max_context,
            safety_margin=safety_margin,
            max_chunk_chars=max_chunk if max_chunk > 0 else None,
            chunk_overlap=overlap,
            run_reconcile=True,
        )
        extraction_meta["single_shot_budget"] = room_single
        extraction_meta["source_path"] = source_label

        empty_chunk_warnings = [
            w
            for w in extraction_meta.get("merge_warnings", [])
            if isinstance(w, str) and w.startswith("chunk_") and "empty" in w
        ]
        if len(empty_chunk_warnings) >= 2:
            logger.warning(
                "[SOPParser] Chunked extraction lost %s partials; running single-shot fallback",
                len(empty_chunk_warnings),
            )
            prompt_truncated_ss = len(raw_text) > room_single
            text_for_prompt_ss = raw_text[:room_single] if prompt_truncated_ss else raw_text
            sop_ss, meta_ss = await extract_structure_single_shot(
                text_for_prompt_ss,
                invoke_llm_json,
                full_prompt_template=_SOP_EXTRACTION_PROMPT,
                single_shot_budget=room_single,
                prompt_truncated=prompt_truncated_ss,
            )
            score_c = len(sop.steps) + len(sop.knowledge_items) + len(sop.sections)
            score_s = len(sop_ss.steps) + len(sop_ss.knowledge_items) + len(sop_ss.sections)
            if score_s >= score_c:
                sop = sop_ss
                extraction_meta["replaced_by_single_shot_after_chunk_loss"] = True
                extraction_meta["single_shot_fallback_meta"] = {
                    "mode": meta_ss.get("mode"),
                    "merge_warnings": list(meta_ss.get("merge_warnings", [])),
                }
                if prompt_truncated_ss:
                    extraction_meta.setdefault("merge_warnings", []).append(
                        f"single_shot_fallback_truncated_to_{room_single}_chars"
                    )

        if not sop.steps and not sop.title and not sop.knowledge_items:
            extraction_meta["merge_warnings"].append("extraction_empty_after_chunked")
            raise ValueError(
                "SOP LLM extraction produced no usable structure after chunked pass "
                f"(no steps, title, or knowledge_items). merge_warnings={extraction_meta.get('merge_warnings')}"
            )
    else:
        prompt_truncated = len(raw_text) > room_single
        text_for_prompt = raw_text[:room_single] if prompt_truncated else raw_text
        sop, extraction_meta = await extract_structure_single_shot(
            text_for_prompt,
            invoke_llm_json,
            full_prompt_template=_SOP_EXTRACTION_PROMPT,
            single_shot_budget=room_single,
            prompt_truncated=prompt_truncated,
        )
        extraction_meta["source_path"] = source_label
        if prompt_truncated:
            extraction_meta.setdefault("merge_warnings", []).append(
                f"single_shot_prompt_truncated_to_{room_single}_chars"
            )
        if not sop.steps and not sop.title and not sop.knowledge_items:
            extraction_meta.setdefault("merge_warnings", []).append("extraction_empty_single_shot")
            raise ValueError(
                "SOP LLM extraction produced no usable structure "
                f"(no steps, title, or knowledge_items). merge_warnings={extraction_meta.get('merge_warnings')}"
            )

    weak_reasons: list[str] = []
    if use_chunked:
        if extraction_meta.get("chunk_count", 0) > 1 and not sop.title.strip():
            weak_reasons.append("missing_title_after_chunk_merge")
        if extraction_meta.get("chunk_count", 0) > 1 and not sop.scope.strip() and len(sop.sections) >= 3:
            weak_reasons.append("missing_scope_with_many_sections")
        if extraction_meta.get("chunk_count", 0) > 1 and len(sop.knowledge_items) < 3 and len(raw_text) > room_single:
            weak_reasons.append("too_few_knowledge_items_for_long_sop")
        if extraction_meta.get("chunk_count", 0) > 1 and not extraction_meta.get("reconcile_applied", False):
            weak_reasons.append("reconcile_not_applied")
    extraction_meta["output_counts"] = {
        "steps": len(sop.steps),
        "knowledge_items": len(sop.knowledge_items),
        "sections": len(sop.sections),
        "decision_points": len(sop.decision_points),
        "exceptions": len(sop.exceptions),
    }
    extraction_meta["weak_reasons"] = weak_reasons
    extraction_meta["use_raw_excerpt_draft_fallback"] = bool(weak_reasons)
    sop.raw_text = raw_text
    return sop, extraction_meta


async def parse_sop_file(
    file_path: str | Path,
    *,
    invoke_llm_json: Callable[..., Any],
    parse_options: dict[str, Any] | None = None,
) -> tuple[SOPStructure, dict[str, Any]]:
    """Parse an SOP document and extract structured content.

    Returns ``(SOPStructure, extraction_meta)``. Full document text is always
    stored in ``SOPStructure.raw_text``.

    parse_options keys:
        - sop_parse_mode: ``auto`` | ``single`` | ``chunked``
        - max_context_chars: model context limit (default 120000)
        - safety_margin: buffer (default 2000)
        - single_shot_budget: when > 0 and below computed room, caps single-shot character budget
        - max_chunk_chars: cap per chunk when chunking (default 8000)
        - chunk_overlap: overlap parameter for chunked extraction; surfaced in extraction_meta
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"SOP 文件不存在: {file_path}")

    raw_text = await _extract_raw_text(file_path)
    if not raw_text.strip():
        raise ValueError(f"SOP 文件内容为空: {file_path}")

    sop, meta = await parse_sop_raw_text(
        raw_text,
        invoke_llm_json=invoke_llm_json,
        parse_options=parse_options,
        source_label=str(file_path),
    )
    return sop, meta


async def _extract_raw_text(file_path: Path) -> str:
    """Extract raw text from an SOP file using AutoFileParser when available."""
    try:
        from openjiuwen.core.retrieval.indexing.processor.parser.auto_file_parser import (
            AutoFileParser,
        )

        parser = AutoFileParser()
        if parser.supports(str(file_path)):
            documents = await parser.parse(str(file_path))
            chunks = [doc.text for doc in documents if getattr(doc, "text", "")]
            if chunks:
                return "\n\n".join(chunks)
            logger.warning("[SOPParser] AutoFileParser returned empty, falling back to direct read")
    except ImportError:
        logger.info("[SOPParser] agent-core AutoFileParser unavailable, using direct read")
    except Exception as exc:
        logger.warning("[SOPParser] AutoFileParser error: %s, falling back to direct read", exc)

    suffix = file_path.suffix.lower()
    if suffix in {".md", ".txt", ".json", ".yaml", ".yml"}:
        return file_path.read_text(encoding="utf-8")
    raise ValueError(
        f"无法解析 SOP 文件 {file_path.name}: "
        f"安装 agent-core 以支持 {suffix} 格式，或提供 .md/.txt 文件"
    )
