"""LLM-based distillation of skill patterns from clustered traces."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .cluster import ClusteredQuery
from .models import DistilledPattern

LOGGER = logging.getLogger(__name__)

_LLM_MAX_RETRIES = 4

_DISTILL_PATTERN_PROMPT = """\
You are generating a query pattern for ONE target skill. The pattern must capture the TYPICAL USER INTENT for this skill, blending two sources:

1. SKILL DESCRIPTION (semantic anchor + DISCRIMINATOR): defines what this skill does. The pattern's core meaning MUST stay within this skill's capability, AND MUST preserve the SKILL-SPECIFIC DISCRIMINATOR WORDS from the description verbatim — the skill's name, its signature objects, and its hallmark action. These words are what distinguish this skill from same-domain neighbors; do NOT generalize them into neutral placeholders.

2. SUCCESSFUL QUERIES (real phrasing): show how users actually phrase requests that involve this skill. Borrow natural verbs, objects, and sentence structure from them — but ONLY the clause that maps to the TARGET skill's capability.

CRITICAL — compound queries: The successful queries are COMPOUND (multiple skills per sentence). Extract ONLY the clause that maps to the TARGET skill's description. Any clause belonging to a DIFFERENT skill (e.g. translation when target is docx, marketing when target is docx) must be DROPPED from the pattern entirely — do NOT mention it even as "input" or "context"; replace its output with a neutral placeholder like [内容] if needed. If no clause matches the target skill, derive the pattern purely from the description.

Discriminator principle (most important rule): same-domain skills must produce distinguishable patterns. Preserve each skill's signature word so its pattern does not collide with neighbors:
- skill "tencent-docs" (腾讯文档): keep "腾讯文档"/"在线文档" — NOT just "[文档]"
- skill "document-processor" (二维码生成器): keep "二维码" — NOT "[文档]"
- skill "react-best-practices" (前端代码审计): keep "前端代码" + "审计/优化" — to distinguish from UI-design skills
- skill "awesome-design-md" (UI设计): keep "UI设计页面" — to distinguish from code-audit skills
Generalize ONLY truly variable entities unrelated to the skill domain (e.g. [人名][地点][日期]); keep domain entities concrete. The pattern must read like a standalone request for ONLY this skill, carrying its signature word.

---

SKILL DESCRIPTION (target):
{used_skills}

REPRESENTATIVE QUERY:
{centroid_query}

SUCCESSFUL QUERIES (compound — extract only target-skill clauses):
{member_queries}

Produce a JSON object: {{"pattern_description": "<standalone pattern, preserving skill-specific discriminator words>"}}

Examples:
- Compound queries: "拍这张法语菜单翻译并朗读，根据功效写三个促销口号，做成Word"
Target skill: photo-translate-speak (拍照识别外文并翻译朗读)
Output: {{"pattern_description": "帮我把这张[语言]的[菜单/标签/说明书]拍下来，识别文字翻译成中文并朗读给我听"}}
Target skill: docx (创建编辑Word文档)
Output: {{"pattern_description": "把[内容]做成Word文档，包含[表格/图文/排版要求]"}}
Target skill: marketing-ideas (生成营销促销文案)
Output: {{"pattern_description": "参考[产品信息]写一份针对[目标群体]的营销促销文案，包含[数量]个促销口号"}}
- Discriminator examples (same domain, distinguishable):
Target skill: tencent-docs (腾讯文档)
Output: {{"pattern_description": "在腾讯文档里[创建/编辑]一个[在线文档/在线表格]，记录[内容]"}}
Target skill: document-processor (二维码生成器)
Output: {{"pattern_description": "帮我把这段[网址/图片/文本]转成二维码"}}

Output ONLY valid JSON. No markdown, no explanation.
"""


class TraceDistiller:
    """Extract effective/ineffective skill patterns from clustered execution traces.

    Usage::

        distiller = TraceDistiller(
            llm_client=llm,
            llm_model="qwen3-32b",
            skills_info=[{"name": "web_search", "description": "..."}],
            max_workers=8,
        )
        patterns = distiller.run(clusters)
    """

    def __init__(
            self,
            llm_client: Any,
            llm_model: str,
            *,
            skills_info: list[dict[str, str]] | None = None,
            max_workers: int = 8,
            max_success_examples: int = 20,
    ) -> None:
        self._llm_model = llm_model
        if not llm_client or not llm_model:
            raise ValueError("TraceDistiller requires both llm_client and llm_model.")
        self._llm = llm_client
        self._max_workers = max_workers
        self._max_examples = max_success_examples

        # Build skill name → description map
        self._skills_by_name: dict[str, str] = {}
        if skills_info:
            for s in skills_info:
                self._skills_by_name[s["name"]] = s.get("description", "")

    def run(self, clusters: list[ClusteredQuery]) -> list[DistilledPattern]:
        """Run distillation in parallel across clusters."""
        results: list[DistilledPattern | None] = [None] * len(clusters)

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_to_idx: dict[Any, int] = {}
            for i, cluster in enumerate(clusters):
                LOGGER.info("TraceDistiller: submitting cluster %d (id=%d)", i, cluster.cluster_id)
                future = executor.submit(self._distill_one, cluster)
                future_to_idx[future] = i

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                    LOGGER.info("TraceDistiller: finished cluster %d", idx)
                except Exception as exc:
                    # Skip the failed cluster rather than aborting the whole run.
                    # A single transient LLM error (rate limit, server hiccup, bad
                    # JSON) should not discard the patterns distilled for all other
                    # clusters. Downstream `build()` already drops empty patterns,
                    # so leaving results[idx] = None is safe.
                    LOGGER.error(
                        "TraceDistiller: failed cluster %d (id=%d): %s (type=%s) — skipping",
                        idx, clusters[idx].cluster_id, exc,
                        type(exc).__name__,
                    )
                    results[idx] = None

        return [r for r in results if r is not None]

    def _distill_one(self, cluster: ClusteredQuery) -> DistilledPattern | None:
        success_traces = cluster.success_traces
        effective_skills = list(success_traces[0].skills) if success_traces else []

        success_queries = [t.query for t in success_traces]
        used_skill_names = set(effective_skills)

        prompt = self._build_prompt(cluster, success_queries, used_skill_names)

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert at generalizing query patterns. "
                    "Output only valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        try:
            content = self._llm_call_with_retry(messages)
            if not content:
                raise RuntimeError(
                    f"LLM returned empty content for cluster {cluster.cluster_id}"
                )
            pattern_description = self._parse_response(content, cluster.cluster_id)
        except Exception as exc:
            raise RuntimeError(
                f"LLM call failed for cluster {cluster.cluster_id}: {exc}"
            ) from exc

        if not pattern_description:
            return None

        return DistilledPattern(
            cluster_id=cluster.cluster_id,
            effective_skills=effective_skills,
            pattern_description=pattern_description,
        )

    def _llm_call_with_retry(self, messages: list[dict]) -> str:
        """Call the LLM with retry/backoff on transient errors (429/5xx/transport).

        400-class client errors and JSON-parse issues are surfaced immediately
        by the caller; here we only retry on rate-limit / server / transport
        errors so a single 429 doesn't abort the whole multi-thousand-call
        distillation run.
        """
        from openai import APIStatusError, RateLimitError

        last_exc: Exception | None = None
        for attempt in range(_LLM_MAX_RETRIES):
            try:
                response = self._llm.chat.completions.create(
                    model=self._llm_model,
                    messages=messages,
                    max_tokens=256,
                    stream=False,
                    extra_body={"enable_thinking": False, "thinking": {"type": "disabled"}},
                )
                return response.choices[0].message.content or ""
            except (RateLimitError, OSError) as exc:
                last_exc = exc
                wait = min(2 ** attempt, 30)
                LOGGER.warning(
                    "TraceDistiller: transient LLM error (attempt %d), retrying in %ds: %s",
                    attempt + 1, wait, exc,
                )
                time.sleep(wait)
            except APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                if status is not None and 400 <= status < 500:
                    # Client error — retrying won't fix it.
                    raise
                last_exc = exc
                wait = min(2 ** attempt, 30)
                LOGGER.warning(
                    "TraceDistiller: LLM server error %s (attempt %d), retrying in %ds",
                    status, attempt + 1, wait,
                )
                time.sleep(wait)
        raise RuntimeError(f"LLM call failed after {_LLM_MAX_RETRIES} retries: {last_exc}")

    _MERGE_PATTERN_PROMPT = """\
You are merging several semantically-overlapping query patterns for the SAME skill into ONE survivor pattern. Each input pattern already expresses the target skill's intent cleanly; your job is to produce a single generalized pattern that subsumes ALL of them without losing any sub-ability.

Rules:
- Preserve the SKILL-SPECIFIC DISCRIMINATOR WORDS already present in the inputs (skill name, signature objects, hallmark action).
- Union the variable placeholders across inputs (e.g. if one pattern has [物体/文字] and another has [色泽/摆盘], combine into [物体/文字/色泽/摆盘]).
- Keep domain entities concrete; do NOT over-generalize.
- The output must read like a single standalone request for ONLY this skill, covering every sub-ability seen in the inputs.
- Drop any clause that does not appear in ANY input pattern (don't invent new sub-tasks).

SKILL (target):
{skill}

EXISTING PATTERNS (merge these):
{patterns}

Output JSON: {{"pattern_description": "<one merged pattern subsuming all inputs>"}}
Output ONLY valid JSON. No markdown, no explanation.
"""

    def distill_merged_pattern(
        self,
        skill: str,
        patterns: list[str],
        fallback: str,
    ) -> str:
        """Merge N same-skill patterns into one survivor pattern via the LLM.

        On any failure (LLM error, empty/invalid output), fall back to
        ``fallback`` (typically the success_count-max pattern) so merging
        never drops a group on the floor.
        """
        if not patterns:
            return fallback
        if len(patterns) == 1:
            return patterns[0]
        desc = self._skills_by_name.get(skill, "")
        skill_line = f"- {skill}: {desc}" if desc else f"- {skill}"
        pat_text = "\n".join(f"- {p!r}" for p in patterns)
        prompt = self._MERGE_PATTERN_PROMPT.format(skill=skill_line, patterns=pat_text)
        try:
            content = self._llm_call_with_retry([
                {"role": "system", "content": "Output only valid JSON."},
                {"role": "user", "content": prompt},
            ])
            merged = self._parse_response(content, -1)
            return merged or fallback
        except Exception as exc:
            LOGGER.warning(
                "TraceDistiller: merge-distill failed for skill=%s (%s); using fallback pattern",
                skill, type(exc).__name__,
            )
            return fallback

    def _build_prompt(
            self,
            cluster: ClusteredQuery,
            success_queries: list[str],
            used_skill_names: set[str] | None = None,
    ) -> str:
        queries_text = "\n".join(f"- {q!r}" for q in success_queries[: self._max_examples])
        if not queries_text:
            queries_text = "  (no successful traces)"

        skills_text = ""
        if self._skills_by_name and used_skill_names:
            lines = []
            for name in sorted(used_skill_names):
                desc = self._skills_by_name.get(name, "")
                if desc:
                    lines.append(f"- {name}: {desc}")
                else:
                    lines.append(f"- {name}")
            if lines:
                skills_text = "\n".join(lines)
        if not skills_text:
            skills_text = "  (no skill descriptions available)"

        return _DISTILL_PATTERN_PROMPT.format(
            cluster_id=cluster.cluster_id,
            centroid_query=cluster.centroid_query,
            member_queries=queries_text,
            used_skills=skills_text,
        )

    @staticmethod
    def _parse_response(response: str, cluster_id: int) -> str:
        raw = response.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.error(
                "TraceDistiller: failed to parse JSON for cluster %d", cluster_id
            )
            return ""

        return data.get("pattern_description", "")


__all__ = ["TraceDistiller"]