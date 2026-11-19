"""Long SOP: budget-aware chunking, map-reduce merge, optional reconcile.

Chunking is driven by total prompt size (template + SOP content) vs model context limit.
Single-shot when full SOP fits; else recursive bisection at semantic boundaries.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from .models import SOPStructure

logger = logging.getLogger(__name__)

# Max size of prev_context + next_context + current_heading when enriching chunks
_CHUNK_CONTEXT_SLACK = 1100

_CHUNK_PARTIAL_PROMPT = """\
你是企业 SOP 结构化分析专家。以下为完整 SOP 文档的第 {chunk_index}/{chunk_total} 段（section_id={section_id}）。
正文仍以本段为准，但你还会看到少量前后文提示，用于理解代词、省略项、前置条件和“见上一节/下文”的依赖关系。不要编造正文中不存在的规则；只能用前后文来补全本段中已经显式出现但脱离上下文就难以理解的信息。

## 关于 steps 与 knowledge_items 的区分

- `steps`：只放**按时间顺序执行的具体操作动作**（某人/系统在某时间做的事）。
- `knowledge_items`：放**规则、标准、阈值、条件、处罚、速查参数**等非流程性参考知识。每条必须自包含，保留原文中的具体数字/金额/比例。
- `sections`：本段中可见的章节/小节标题和编号。

如果不确定某内容是 step 还是 knowledge_item，优先放 knowledge_items。

## 邻近上下文提示（仅用于补足依赖关系）

### 前文提示
{prev_context}

### 当前段标题
{current_heading}

### 后文提示
{next_context}

## 本段原文

{chunk_text}

## 输出 JSON（仅本段可见内容）

```json
{{
  "title": "",
  "purpose": "",
  "scope": "",
  "sop_type": "",
  "roles": [],
  "steps": [
    {{
      "step_number": "1.1.1",
      "actor": "",
      "action": "",
      "system": "",
      "output": "",
      "notes": ""
    }}
  ],
  "knowledge_items": [
    "自包含的规则/标准/阈值语句"
  ],
  "sections": [
    {{
      "id": "2.1",
      "title": "章节标题",
      "content_summary": "一句话概述"
    }}
  ],
  "decision_points": [],
  "exceptions": [],
  "references": []
}}
```

要求：
- **step_number 与 sections[].id 必须是 JSON 字符串（双引号）**，例如 `"1.1"`、`"2.3.1"`。禁止写未加引号的 `1.1.1`（非法 JSON，会导致整段无法解析）。
- steps 中的 step_number 尽量与原文中的步骤编号一致（全局编号）。
- 若本段只有某步骤的续行而没有编号，可省略该步或合并到 notes。
- knowledge_items 中保留原文的具体数字、金额、比例、时间限制。宁多勿少。
- 若正文中的规则依赖前文定义的主体、标准、适用范围、例外条件，请把补全后的条件写成自包含的 knowledge_item，或写入对应 step 的 notes。
- 若正文存在“在上述情况下”“按前述标准”“同前审批路径”之类表达，请结合邻近上下文改写成完整含义。
- 附加字段: "section_id": {section_id}

只输出 JSON。"""


def _chunk_prompt_overhead() -> int:
    """Length of chunk prompt with empty chunk_text and minimal context placeholders."""
    return len(
        _CHUNK_PARTIAL_PROMPT.format(
            chunk_index=1,
            chunk_total=1,
            section_id=0,
            prev_context="",
            current_heading="",
            next_context="",
            chunk_text="",
        )
    )


def _find_split_point(text: str, target: int) -> int:
    """Find a semantic boundary near target index; prefer paragraph or heading breaks."""
    if target <= 0 or target >= len(text):
        return target
    boundaries: list[int] = []
    for m in re.finditer(r"(?m)^#{1,6}\s+.+$|\n\n+", text):
        boundaries.append(m.start())
    if not boundaries:
        return target
    best = target
    best_dist = len(text)
    for b in boundaries:
        if 1 <= b < len(text) - 1:
            d = abs(b - target)
            if d < best_dist:
                best_dist = d
                best = b
    return best


def _bisect_to_fit(text: str, max_chunk_chars: int) -> list[str]:
    """Recursively split text so each piece fits in max_chunk_chars. Prefers semantic boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chunk_chars:
        return [text]
    mid = len(text) // 2
    split_at = _find_split_point(text, mid)
    if split_at <= 0:
        split_at = mid
    if split_at >= len(text):
        split_at = max(1, len(text) - 1)
    left = text[:split_at].rstrip()
    right = text[split_at:].lstrip()
    if not left:
        return _bisect_to_fit(right, max_chunk_chars)
    if not right:
        return _bisect_to_fit(left, max_chunk_chars)
    return _bisect_to_fit(left, max_chunk_chars) + _bisect_to_fit(right, max_chunk_chars)


def _enrich_chunks(chunk_texts: list[str]) -> list[dict[str, Any]]:
    """Add section_id, current_heading, prev_context, next_context to each chunk."""
    result: list[dict[str, Any]] = []
    for idx, chunk_text in enumerate(chunk_texts):
        prev_text = chunk_texts[idx - 1] if idx > 0 else ""
        next_text = chunk_texts[idx + 1] if idx + 1 < len(chunk_texts) else ""
        result.append(
            {
                "section_id": idx,
                "chunk_text": chunk_text,
                "current_heading": _heading_for_chunk(chunk_text),
                "prev_context": _context_snippet(prev_text, from_end=True),
                "next_context": _context_snippet(next_text, from_end=False),
            }
        )
    return result


_RECONCILE_PROMPT = """\
以下 JSON 是由多段 SOP 文本分别抽取后合并的初稿，可能存在：
- 重复步骤或顺序错乱
- 重复的 knowledge_items（同一规则在不同段被提取）
- 章节 sections 的编号冲突

请输出**一份**整理后的 JSON：
1. 合并重复的 steps，修正顺序和编号
2. 合并重复的 knowledge_items（含义相同的只保留更完整的那条）
3. 合并 sections（同 id 只保留更完整的 content_summary）
4. 保留所有不重复的决策点/异常/引用
5. 确定最终的 sop_type（procedural / knowledge / hybrid）
6. 如果后文规则依赖前文定义的适用范围、审批条件、标准或前置动作，请把这种依赖补成自包含表述，不要留下“上述情况”“按前述规则”这类孤立措辞
7. 不要丢失长文档后半段的规则；如果发现 merged 初稿中信息不足，可依据分块摘要恢复更完整的标题、范围、决策与异常

合并稿:
{merged_json}

输出格式与标准 SOP 抽取相同（不要 section_id），只输出 JSON。"""


def _context_snippet(text: str, *, from_end: bool, max_chars: int = 400, max_lines: int = 6) -> str:
    if not text.strip():
        return "无"
    normalized = text.strip()
    snippet = normalized[-max_chars:] if from_end else normalized[:max_chars]
    lines = [line.strip() for line in snippet.splitlines() if line.strip()]
    if not lines:
        return "无"
    if from_end:
        lines = lines[-max_lines:]
    else:
        lines = lines[:max_lines]
    return "\n".join(lines)


def _heading_for_chunk(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return line[:120]
    return "无"


def split_semantic_chunks(
    text: str,
    max_chunk_chars: int,
    overlap_chars: int,
) -> list[dict[str, Any]]:
    """Split text into chunks; prefer markdown headings and paragraph breaks."""
    if max_chunk_chars <= 0:
        return [
            {
                "section_id": 0,
                "chunk_text": text,
                "current_heading": _heading_for_chunk(text),
                "prev_context": "无",
                "next_context": "无",
            }
        ]
    text = text.strip()
    if len(text) <= max_chunk_chars:
        return [
            {
                "section_id": 0,
                "chunk_text": text,
                "current_heading": _heading_for_chunk(text),
                "prev_context": "无",
                "next_context": "无",
            }
        ]

    # Split on headings or double newlines
    boundaries: list[int] = [0]
    for m in re.finditer(r"(?m)^#{1,3}\s+.+$|\n\n+", text):
        boundaries.append(m.start())
    boundaries.append(len(text))
    boundaries = sorted(set(boundaries))

    segments: list[str] = []
    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        seg = text[start:end].strip()
        if seg:
            segments.append(seg)

    if not segments:
        segments = [text]

    chunks: list[tuple[int, str]] = []
    buf = ""
    section_id = 0
    for seg in segments:
        if not buf:
            buf = seg
        elif len(buf) + 2 + len(seg) <= max_chunk_chars:
            buf = buf + "\n\n" + seg
        else:
            if buf:
                chunks.append((section_id, buf))
                section_id += 1
            if len(seg) <= max_chunk_chars:
                buf = seg
            else:
                # Hard-split long segment by windows with overlap
                start = 0
                while start < len(seg):
                    end = min(len(seg), start + max_chunk_chars)
                    piece = seg[start:end]
                    chunks.append((section_id, piece))
                    section_id += 1
                    if end >= len(seg):
                        break
                    start = max(0, end - overlap_chars)
                buf = ""
        while len(buf) > max_chunk_chars:
            take = buf[:max_chunk_chars]
            chunks.append((section_id, take))
            section_id += 1
            buf = buf[max(0, max_chunk_chars - overlap_chars):]
    if buf:
        chunks.append((section_id, buf))

    enriched: list[dict[str, Any]] = []
    for idx, (section_id, chunk_text) in enumerate(chunks):
        prev_text = chunks[idx - 1][1] if idx > 0 else ""
        next_text = chunks[idx + 1][1] if idx + 1 < len(chunks) else ""
        enriched.append(
            {
                "section_id": section_id,
                "chunk_text": chunk_text,
                "current_heading": _heading_for_chunk(chunk_text),
                "prev_context": _context_snippet(prev_text, from_end=True),
                "next_context": _context_snippet(next_text, from_end=False),
            }
        )
    return enriched


def _normalize_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())[:200]


def _step_key(raw: Any) -> str:
    text = str(raw).strip()
    return text if text else "0"


def _step_sort_key(raw: Any) -> tuple[int, tuple[int | str, ...], str]:
    text = str(raw).strip()
    if not text:
        return (2, (), "")
    if text.isdigit():
        return (0, (int(text),), text)
    parts = re.split(r"[.\-_/]", text)
    if parts and all(part.isdigit() for part in parts if part):
        return (1, tuple(int(part) for part in parts if part), text)
    return (2, (), text)


def _merge_text_field(left: str, right: str) -> str:
    left = str(left or "").strip()
    right = str(right or "").strip()
    if not left:
        return right
    if not right or right == left:
        return left
    if right in left:
        return left
    if left in right:
        return right
    return f"{left}；{right}"


def _append_unique_preserve_order(items: list[str], seen: set[str], raw: Any) -> None:
    value = str(raw or "").strip()
    if not value:
        return
    key = _normalize_key(value)
    if key in seen:
        return
    seen.add(key)
    items.append(value)


def _merge_step_dict(prev: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if prev is None:
        return dict(current)
    merged = dict(prev)
    merged["step_number"] = current.get("step_number", prev.get("step_number"))
    for field in ("actor", "action", "system", "output", "notes"):
        merged[field] = _merge_text_field(prev.get(field, ""), current.get(field, ""))
    return merged


def merge_partial_dicts(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic merge of chunk-level partial JSON dicts."""
    title = ""
    purpose = ""
    scope = ""
    sop_type_votes: list[str] = []
    roles: list[str] = []
    role_seen: set[str] = set()
    steps_by_num: dict[str, dict[str, Any]] = {}
    knowledge_items: list[str] = []
    ki_seen: set[str] = set()
    sections_by_id: dict[str, dict[str, Any]] = {}
    dps: list[str] = []
    dp_seen: set[str] = set()
    exs: list[str] = []
    ex_seen: set[str] = set()
    refs: list[str] = []
    ref_seen: set[str] = set()

    for p in partials:
        if not isinstance(p, dict):
            continue
        if str(p.get("title", "")).strip():
            title = title or str(p.get("title", "")).strip()
        if str(p.get("purpose", "")).strip():
            purpose = purpose or str(p.get("purpose", "")).strip()
        if str(p.get("scope", "")).strip():
            scope = scope or str(p.get("scope", "")).strip()
        st_val = str(p.get("sop_type", "")).strip().lower()
        if st_val in ("procedural", "knowledge", "hybrid"):
            sop_type_votes.append(st_val)
        for r in p.get("roles", []) or []:
            _append_unique_preserve_order(roles, role_seen, r)
        for st in p.get("steps", []) or []:
            if not isinstance(st, dict):
                continue
            step_number = st.get("step_number", "")
            if not str(step_number).strip():
                continue
            key = _step_key(step_number)
            prev = steps_by_num.get(key)
            steps_by_num[key] = _merge_step_dict(prev, st)
        for ki in p.get("knowledge_items", []) or []:
            _append_unique_preserve_order(knowledge_items, ki_seen, ki)
        for sec in p.get("sections", []) or []:
            if not isinstance(sec, dict):
                continue
            sid = str(sec.get("id", "")).strip()
            if not sid:
                continue
            prev_sec = sections_by_id.get(sid)
            if prev_sec is None:
                sections_by_id[sid] = dict(sec)
                continue
            merged_sec = dict(prev_sec)
            for field in ("title", "content_summary"):
                merged_sec[field] = _merge_text_field(prev_sec.get(field, ""), sec.get(field, ""))
            sections_by_id[sid] = merged_sec
        for x in p.get("decision_points", []) or []:
            _append_unique_preserve_order(dps, dp_seen, x)
        for x in p.get("exceptions", []) or []:
            _append_unique_preserve_order(exs, ex_seen, x)
        for x in p.get("references", []) or []:
            _append_unique_preserve_order(refs, ref_seen, x)

    if sop_type_votes:
        from collections import Counter
        most_common = Counter(sop_type_votes).most_common(1)[0][0]
        if len(set(sop_type_votes)) > 1:
            sop_type = "hybrid"
        else:
            sop_type = most_common
    else:
        sop_type = ""

    ordered_steps = [steps_by_num[k] for k in sorted(steps_by_num.keys(), key=_step_sort_key)]
    ordered_sections = sorted(sections_by_id.values(), key=lambda s: str(s.get("id", "")))
    return {
        "title": title,
        "purpose": purpose,
        "scope": scope,
        "sop_type": sop_type,
        "roles": roles,
        "steps": ordered_steps,
        "knowledge_items": knowledge_items,
        "sections": ordered_sections,
        "decision_points": dps,
        "exceptions": exs,
        "references": refs,
    }


def _compute_room_for_chunk(
    max_context_chars: int,
    safety_margin: int,
    max_chunk_chars: int | None,
) -> int:
    """Max chars for chunk_text such that full prompt fits in context."""
    room = max_context_chars - _chunk_prompt_overhead() - _CHUNK_CONTEXT_SLACK - safety_margin
    if max_chunk_chars is not None and max_chunk_chars > 0 and room > max_chunk_chars:
        room = max_chunk_chars
    return max(1, room)


def _reconcile_has_usable_structure(rec: dict[str, Any]) -> bool:
    for key in ("steps", "knowledge_items", "sections", "decision_points", "exceptions", "title"):
        if rec.get(key):
            return True
    return False


async def extract_structure_chunked(
    raw_text: str,
    invoke_llm_json: Callable[..., Any],
    *,
    max_context_chars: int,
    safety_margin: int,
    max_chunk_chars: int | None = None,
    chunk_overlap: int = 0,
    run_reconcile: bool = True,
) -> tuple[SOPStructure, dict[str, Any]]:
    """Map-reduce extraction over budget-aware recursive bisection chunks."""
    room = _compute_room_for_chunk(max_context_chars, safety_margin, max_chunk_chars)
    chunk_texts = _bisect_to_fit(raw_text.strip(), room)
    chunks = _enrich_chunks(chunk_texts)
    meta: dict[str, Any] = {
        "mode": "chunked",
        "chunk_count": len(chunks),
        "overlap": chunk_overlap,
        "max_chunk_chars": room,
        "merge_warnings": [],
    }

    partials: list[dict[str, Any]] = []
    meta["chunk_summaries"] = []
    for idx, chunk in enumerate(chunks, start=1):
        prompt = _CHUNK_PARTIAL_PROMPT.format(
            chunk_index=idx,
            chunk_total=len(chunks),
            section_id=chunk["section_id"],
            prev_context=chunk["prev_context"],
            current_heading=chunk["current_heading"],
            next_context=chunk["next_context"],
            chunk_text=chunk["chunk_text"],
        )
        data = await invoke_llm_json(
            prompt,
            fallback={},
            trace_tag=f"sop_chunk_partial_{idx}_of_{len(chunks)}",
        )
        meta["chunk_summaries"].append(
            {
                "chunk_index": idx,
                "section_id": chunk["section_id"],
                "heading": chunk["current_heading"],
                "char_len": len(chunk["chunk_text"]),
                "empty": not bool(data),
            }
        )
        if isinstance(data, dict) and data:
            data.pop("section_id", None)
            partials.append(data)
        else:
            meta["merge_warnings"].append(f"chunk_{idx}_empty_llm_response")

    if not partials:
        logger.warning("[SOPChunkMerge] All chunk extractions empty")
        meta["merge_warnings"].append("all_chunks_empty")
        return SOPStructure(), meta

    merged = merge_partial_dicts(partials)
    meta["reconcile_applied"] = False
    if run_reconcile and len(chunks) > 1:
        reconcile_payload = {
            "merged_structure": merged,
            "chunk_context": [
                {
                    "section_id": chunk["section_id"],
                    "heading": chunk["current_heading"],
                    "prev_context": chunk["prev_context"],
                    "next_context": chunk["next_context"],
                }
                for chunk in chunks
            ],
        }
        merged_json = json.dumps(reconcile_payload, ensure_ascii=False, indent=2)
        rec_prompt = _RECONCILE_PROMPT.format(merged_json=merged_json)
        rec = await invoke_llm_json(rec_prompt, fallback={}, trace_tag="sop_chunk_reconcile")
        if isinstance(rec, dict) and _reconcile_has_usable_structure(rec):
            merged = rec
            meta["reconcile_applied"] = True

    sop = SOPStructure.from_dict(merged)
    return sop, meta


async def extract_structure_single_shot(
    raw_text_for_prompt: str,
    invoke_llm_json: Callable[..., Any],
    *,
    full_prompt_template: str,
    single_shot_budget: int,
    prompt_truncated: bool,
) -> tuple[SOPStructure, dict[str, Any]]:
    """One LLM call; raw_text_for_prompt is what is embedded in the prompt (may be truncated)."""
    prompt = full_prompt_template.format(raw_text=raw_text_for_prompt)
    data = await invoke_llm_json(prompt, fallback={}, trace_tag="sop_extract_single_shot")
    meta: dict[str, Any] = {
        "mode": "single",
        "chunk_count": 1,
        "overlap": 0,
        "single_shot_budget": single_shot_budget,
        "prompt_truncated": prompt_truncated,
        "merge_warnings": [],
    }
    if not isinstance(data, dict) or not data:
        meta["merge_warnings"].append("empty_llm_response")
        return SOPStructure(), meta
    sop = SOPStructure.from_dict(data)
    return sop, meta
