# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""UtilityBudgetContextRail — utility-aware token budgeting for agent context.

Implements the Utility-Budgeted Context Manager (UBCM): before every model
call, estimate the outgoing context size; when it exceeds the configured
budget, score each non-protected message by a deterministic utility function
(goal similarity x role weight x recency decay x inverse length penalty) and
trim the lowest-utility items — truncating long tool outputs to a head slice,
and evicting old assistant/user text messages that carry no tool-call pairing.

The utility scoring is deliberately System-1 style (no LLM calls on the hot
path), mirroring the budget-allocation design described in the accompanying
research paper (UBCM for multi-agent LLM systems). The goal-similarity term of
Eq. (1) is approximated with a lexical overlap score between each message and
the current user query (word set + CJK bigrams), keeping the hot path under
~1ms while making scoring goal-conditional rather than purely recency-based.

Safety rules (never violated):
- System messages are always kept.
- The last ``protected_tail`` messages are always kept (recency).
- Assistant messages carrying tool_calls are always kept (protocol pairing),
  and tool messages are only truncated, never dropped.
- The first user message of the conversation is kept (task framing).

Configuration (config.yaml, section ``utility_budget_context``):
    enabled: false            # master switch (default off, opt-in)
    budget_tokens: 64000      # soft budget; rail activates above this
    protected_tail: 6         # tail messages never touched
    truncate_head_ratio: 0.25 # kept head fraction when truncating content
    role_weights:             # utility weight per role
      user: 1.0
      assistant: 0.9
      tool: 0.5
    recency_decay: 0.96       # per-step decay towards older messages
    goal_weight: 0.8          # weight of lexical goal similarity vs. recency term
    min_content_chars: 200    # shorter than this: truncation becomes eviction
"""

from __future__ import annotations

import copy
import math as _math
import re as _re
import time as _time
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import logger


def _rail_log(msg: str) -> None:
    """确定性观测:直写文件,绕过框架日志路由(调试用,极轻量)。"""
    try:
        path = Path.home() / ".jiuwenswarm" / "logs" / "ubcm_rail.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{_time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass

_CHARS_PER_TOKEN = 4  # ASCII estimator; CJK chars count 1 token each (see _est_tokens)

_DEFAULTS = {
    "enabled": False,
    "budget_tokens": 64000,
    "protected_tail": 6,
    "truncate_head_ratio": 0.25,
    "role_weights": {"user": 1.0, "assistant": 0.9, "tool": 0.5},
    "recency_decay": 0.96,
    "goal_weight": 0.8,
    "min_content_chars": 200,
}

_TERM_CACHE: dict[int, dict[str, int]] = {}


def _term_freqs(text: str) -> dict[str, int]:
    """Term frequencies: lowercase ASCII words + CJK bigrams (hot-path safe)."""
    key = hash(text)
    cached = _TERM_CACHE.get(key)
    if cached is not None:
        return cached
    terms: list[str] = _re.findall(r"[a-z0-9]+", text.lower())
    cjk = _re.sub(r"[^一-鿿]", "", text)
    terms.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
    freqs: dict[str, int] = {}
    for t in terms:
        freqs[t] = freqs.get(t, 0) + 1
    if len(_TERM_CACHE) > 2048:  # bounded cache for long sessions
        _TERM_CACHE.clear()
    _TERM_CACHE[key] = freqs
    return freqs


def _bm25(query: str, doc: str, idf: dict[str, float], k1: float = 1.2) -> float:
    """BM25-style weighted lexical relevance of doc to query (rare terms up-weighted)."""
    qf = _term_freqs(query)
    df = _term_freqs(doc)
    if not qf:
        return 0.0
    score = 0.0
    for t, qt in qf.items():
        dt = df.get(t, 0)
        if dt:
            score += idf.get(t, 0.0) * dt * (k1 + 1.0) / (dt + k1)
    return score / (len(qf) ** 0.5)  # length-normalize the query side


def _text_of(content: Any) -> str:
    """Best-effort text extraction from str / list-of-blocks content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item["text"]))
        return "".join(parts)
    return ""


def _est_tokens(content: Any) -> int:
    """CJK-aware token estimator:CJK 字符按 1 token,其余按 4 字符/token。

    近似估算用于预算控制,无需精确 tokenizer(保持 <1ms 热路径开销)。
    """
    text = _text_of(content)
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    ascii_chars = len(text) - cjk
    return max(1, cjk + ascii_chars // _CHARS_PER_TOKEN)


class UtilityBudgetContextRail(DeepAgentRail):
    """Utility-aware context budget rail (UBCM)."""

    priority = 38  # after prompt-assembly rails, before stream/usage rails

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._agent = None
        self._cfg = {**_DEFAULTS, **(config or {})}
        self._current_query = ""
        self._goal_scores: dict[int, float] = {}
        self._stats: dict[str, int] = {}
        self._logged_under_budget = False
        logger.info("[UtilityBudgetContextRail] initialized: enabled=%s budget=%s",
                    self._cfg.get("enabled"), self._cfg.get("budget_tokens"))
        _rail_log(f"__init__ cfg={self._cfg}")

    def init(self, agent) -> None:
        self._agent = agent

    def uninit(self, agent) -> None:
        self._agent = None

    # -- helpers ----------------------------------------------------------
    def _enabled(self) -> bool:
        cfg = (get_config() or {}).get("utility_budget_context") or {}
        return bool(cfg.get("enabled", self._cfg["enabled"]))

    def _role_weight(self, role: str) -> float:
        weights = self._cfg["role_weights"]
        return float(weights.get(role, 0.8))

    def _utility(self, role: str, distance_from_tail: int, content: Any) -> float:
        """Deterministic utility: goal-conditional blend of BM25 lexical goal
        similarity with the current user query and the recency term (role weight
        x recency decay x length bonus). Mirrors Eq. (1) of the paper (goal
        relevance + recency), with the embedding similarity approximated by a
        BM25-style weighted lexical scorer (System-1, no LLM calls)."""
        text = _text_of(content)
        length_bonus = 1.0 / (1.0 + max(0, len(text) - 400) / 4000.0)
        recency_term = self._role_weight(role) * (
            self._cfg["recency_decay"] ** distance_from_tail) * length_bonus
        goal_term = self._goal_scores.get(hash(text), 0.0)
        gw = float(self._cfg["goal_weight"])
        return gw * goal_term + (1.0 - gw) * recency_term

    # -- rail hooks -------------------------------------------------------
    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled():
            _rail_log("before_model_call SKIP (disabled)")
            return
        context = getattr(ctx, "context", None)
        if context is None:
            return
        try:
            messages = context.get_messages()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[UtilityBudgetContextRail] get_messages failed: %s", exc)
            return
        if not messages:
            return

        budget = int(self._cfg["budget_tokens"])
        # 预算覆盖整个上下文 = system 提示(若可获取) + 会话消息
        system_est = 0
        prompt_builder = getattr(self._agent, "prompt_builder", None) or getattr(
            self._agent, "system_prompt_builder", None)
        if prompt_builder is not None:
            try:
                system_est = _est_tokens(str(prompt_builder.build()))
            except Exception:  # noqa: BLE001
                system_est = 0
        conv_est = sum(_est_tokens(m.content) for m in messages)
        total = system_est + conv_est
        if total <= budget:
            if not self._logged_under_budget:
                self._logged_under_budget = True
                _rail_log(f"under budget: est={total} (system={system_est}, "
                          f"conv={conv_est}, msgs={len(messages)}) budget={budget}")
                logger.info(
                    "[UtilityBudgetContextRail] under budget: est=%d (system=%d, "
                    "conv=%d, msgs=%d) budget=%d", total, system_est, conv_est,
                    len(messages), budget,
                )
            self._stats = {"before_tokens": total, "after_tokens": total,
                           "system_tokens": system_est,
                           "truncated": 0, "evicted": 0}
            ctx.extra["utility_budget_stats"] = dict(self._stats)
            return

        protected_tail = max(2, int(self._cfg["protected_tail"]))
        keep_len = len(messages) - protected_tail
        first_user_idx = next((i for i, m in enumerate(messages) if m.role == "user"), 0)

        # Session-level goal = concatenation of the most recent user messages
        # (a stable, task-level signal rather than a single-turn query).
        user_msgs = [m for m in messages if m.role == "user"]
        self._current_query = " ".join(_text_of(m.content) for m in user_msgs[-8:])

        # BM25 goal scores for all candidate texts (idf from this conversation's
        # document frequencies; scores normalized by the max over candidates).
        texts = [_text_of(m.content) for m in messages]
        doc_freq: dict[str, int] = {}
        for t in texts:
            for term in _term_freqs(t):
                doc_freq[term] = doc_freq.get(term, 0) + 1
        n_docs = max(1, len(texts))
        idf = {term: _math.log1p((n_docs - df + 0.5) / (df + 0.5))
               for term, df in doc_freq.items()}
        raw_scores = {hash(t): _bm25(self._current_query, t, idf) for t in texts}
        max_score = max(raw_scores.values()) if raw_scores else 0.0
        self._goal_scores = ({k: v / max_score for k, v in raw_scores.items()}
                             if max_score > 0 else raw_scores)

        truncated = 0
        evicted = 0
        candidates: list[tuple[int, Any, str]] = []
        for idx, msg in enumerate(messages):
            text = _text_of(msg.content)
            is_protected = (
                msg.role == "system"
                or idx >= keep_len
                or idx == first_user_idx
                or (msg.role == "assistant" and bool(getattr(msg, "tool_calls", None)))
            )
            if not is_protected:
                candidates.append((idx, msg, text))

        # iterate from the least useful until under budget (lowest utility first)
        current = total
        evicted_idx: set[int] = set()
        replacements: dict[int, Any] = {}
        while current > budget and candidates:
            pick = min(
                range(len(candidates)),
                key=lambda i: self._utility(
                    candidates[i][1].role,
                    len(messages) - candidates[i][0],  # distance from tail
                    candidates[i][2],
                ),
            )
            idx, msg, text = candidates.pop(pick)
            if msg.role == "tool":
                # tool messages: truncate only (protocol requires presence);
                # mutate a copy so shared references to the original message
                # (other rails, retry logic, session history) stay intact.
                keep = max(self._cfg["min_content_chars"],
                           int(len(text) * self._cfg["truncate_head_ratio"]))
                new_msg = copy.deepcopy(msg)
                new_msg.content = text[:keep] + (
                    f"\n...[UtilityBudgetContextRail truncated {len(text) - keep} chars, "
                    f"budget={budget}]"
                )
                replacements[idx] = new_msg
                truncated += 1
                current -= _est_tokens(text) - _est_tokens(new_msg.content)
            else:
                # old user/assistant text: evict
                evicted_idx.add(idx)
                evicted += 1
                current -= _est_tokens(msg.content)

        # rebuild in original order, skipping evicted and substituting the
        # deep-copied truncated tool messages for the untouched originals.
        final = [replacements.get(idx, m)
                 for idx, m in enumerate(messages) if idx not in evicted_idx]
        try:
            context.set_messages(final)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[UtilityBudgetContextRail] set_messages failed: %s", exc)
            return

        after = sum(_est_tokens(m.content) for m in final)
        self._stats = {"before_tokens": total, "after_tokens": after,
                       "truncated": truncated, "evicted": evicted}
        ctx.extra["utility_budget_stats"] = dict(self._stats)
        _rail_log(f"budget applied: {total} -> {after} est tokens "
                  f"(truncated={truncated}, evicted={evicted})")
        logger.info(
            "[UtilityBudgetContextRail] budget applied: %d -> %d est tokens "
            "(truncated=%d, evicted=%d)", total, after, truncated, evicted,
        )

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """Attach budget stats to the usage report when available."""
        if not self._stats:
            return
        report = getattr(ctx, "context_usage_report", None)
        if report is None:
            inputs = getattr(ctx, "inputs", None)
            report = getattr(inputs, "context_usage_report", None)
        if isinstance(report, dict):
            report.setdefault("utility_budget", dict(self._stats))
        elif report is not None and hasattr(report, "extra"):
            try:
                report.extra = {**(getattr(report, "extra", None) or {}),
                                "utility_budget": dict(self._stats)}
            except Exception:  # noqa: BLE001
                pass
        self._stats = {}
