# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Feedback collector for proactive recommendation optimization.

Collects explicit (like/dislike) and implicit (user reply) feedback,
stores in buffer for batch processing in next tick.
"""

from __future__ import annotations

import logging
import time

from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Recommendation metadata (feedback 关联的推荐元数据) ────────────


@dataclass
class RecMeta:
    """调用方已知的推荐元数据（显式反馈入口从 chunk payload 的
    proactive_type/proactive_target 拿到）。history 尚未写入（_on_delivered 异步、
    可能晚于用户点赞）时用它兜底填充反馈记录，避免时序竞态丢反馈。

    聚合成对象而非散参：record_feedback 签名 6 参超 huawei-too-many-arguments（≤5），
    这三个一组语义内聚，合一个 meta 参数降到 4 参合规。
    """

    rec_type: str = ""
    rec_target: str = ""
    rec_content: str = ""


# ── Implicit Feedback ───────────────────────────────────────────────
# 隐式反馈不在此做情感分类——关键词正则（"好"子串撞"不好"等）误判率高，错误 label
# 喂给梯度更新会让推荐持续恶化。改为直接把用户回复原文存进 buffer，feedback_type
# 占位 "implicit"，情感与规则提取都交给梯度更新器里的 LLM（它本就要调 LLM）
# 看 user_reply 原文判断。分流靠 user_reply 非空（gradient_updater），不靠 label。


# ── Feedback Recording ──────────────────────────────────────────────

def record_feedback(
    rec_id: str,
    feedback_type: str,
    user_reply: str = "",
    meta: RecMeta | None = None,
) -> None:
    """Record feedback to buffer (no LLM call, zero cost).

    Feedback is buffered and consumed in next tick for batch gradient update.

    Args:
        rec_id: Recommendation ID (links to recommendation_history)
        feedback_type: "explicit_like" | "explicit_dislike" | "implicit_positive" | "implicit_negative"
        user_reply: User reply text (for implicit feedback)
        meta: 调用方已知的推荐元数据（显式反馈入口可从 chunk payload 的
            proactive_type/proactive_target 拿到）。用于在 recommendation_history
            尚未写入（_on_delivered 异步、可能晚于用户点赞）时仍能填充反馈记录，
            避免时序竞态丢反馈。
    """
    from jiuwenswarm.agents.harness.common.recommendation.profile_extractor import (
        load_recommendation_state,
        save_recommendation_state,
    )

    state = load_recommendation_state()
    meta = meta or RecMeta()

    # Find recommendation record（可能还没写入：_on_delivered 在后台主 agent 跑完后
    # 才执行，而 chunk 流式推送在前，用户可能在 history 写入前就点赞）。
    rec_data = None
    for rec in state.recommendation_history:
        if rec.get("id") == rec_id:
            rec_data = rec
            break

    if not rec_data:
        # 查不到 history 不丢弃反馈——用调用方传入的元数据（或空值）照样记录，
        # 否则时序竞态会让点赞丢失，buffer 永远空，梯度更新无米下锅。
        logger.debug(
            "[FeedbackCollector] recommendation %s not in history yet (race with _on_delivered), "
            "recording feedback with caller-provided metadata",
            rec_id,
        )
        rec_data = {
            "type": meta.rec_type,
            "target": meta.rec_target,
            "content": meta.rec_content,
        }

    new_feedback = {
        "rec_id": rec_id,
        "rec_type": rec_data.get("type", "") or meta.rec_type,
        "rec_target": rec_data.get("target", "") or meta.rec_target,
        "rec_content": rec_data.get("content", "") or meta.rec_content,
        "feedback_type": feedback_type,
        "user_reply": user_reply,
        "timestamp": time.time(),
    }

    # 去重：同一条推荐，显式反馈（赞/踩）和隐式反馈（用户回复原文）各保留第一条。
    # 两类信号都有效、都喂梯度——显式是明确表态、隐式是用户补充的具体场景文本，
    # 互补不互斥。同一类后续反馈丢弃：显式是单选语义（二次点赞/踩丢），
    # 隐式只采紧跟推荐后的第一条回复（后续对话往往转到别的话题）。
    new_is_explicit = feedback_type in ("explicit_like", "explicit_dislike")
    existing_idx = None
    for i, fb in enumerate(state.feedback_buffer):
        if fb.get("rec_id") != rec_id:
            continue
        existing_is_explicit = fb.get("feedback_type", "") in ("explicit_like", "explicit_dislike")
        # 同大类（显式/隐式）已有记录 → 丢弃新的（各留第一条）
        if existing_is_explicit == new_is_explicit:
            existing_idx = i
            break

    if existing_idx is not None:
        logger.debug(
            "[FeedbackCollector] duplicate %s feedback for rec=%s dropped, keeping first",
            "explicit" if new_is_explicit else "implicit", rec_id,
        )
        return

    state.feedback_buffer.append(new_feedback)

    # FIFO, limit capacity
    if len(state.feedback_buffer) > 20:
        state.feedback_buffer = state.feedback_buffer[-20:]

    state.touch()
    save_recommendation_state(state)

    logger.info("[FeedbackCollector] recorded feedback for rec=%s, type=%s",
                rec_id, feedback_type)


def record_explicit_feedback(
    rec_id: str,
    feedback_type: str,
    meta: RecMeta | None = None,
) -> None:
    """Record explicit feedback (like/dislike button).

    Args:
        rec_id: Recommendation ID
        feedback_type: "explicit_like" | "explicit_dislike"
        meta: 调用方已知的推荐元数据（来自 chunk payload 的 proactive_type/
            proactive_target），history 未写入时兜底填充。
    """
    record_feedback(rec_id, feedback_type, "", meta)


def record_implicit_feedback(rec_id: str, user_reply: str) -> None:
    """Record implicit feedback (user text reply).

    不在此做情感分类：关键词正则误判率高（"好"子串撞"不好"等），错误 label 会污染
    梯度更新。直接把回复原文存进 buffer，feedback_type 占位 "implicit"，情感与规则
    提取交给梯度更新器里的 LLM（看 user_reply 原文判断）。中性回复也不再预先丢弃——
    是否产生梯度规则由梯度 LLM 自行决定（无 operations 即等效中性，无需在此猜）。

    Args:
        rec_id: Recommendation ID
        user_reply: User reply text
    """
    record_feedback(rec_id, "implicit", user_reply)


def consume_feedback_buffer(state: Any) -> list[dict]:
    """Consume feedback buffer and clear it.

    Called in tick before gradient update.

    Args:
        state: RecommendationState

    Returns:
        List of feedback records (buffer contents)
    """
    from jiuwenswarm.agents.harness.common.recommendation.profile_extractor import (
        save_recommendation_state,
    )

    if not state.feedback_buffer:
        return []

    feedbacks = list(state.feedback_buffer)
    state.feedback_buffer.clear()
    state.touch()
    save_recommendation_state(state)

    logger.info("[FeedbackCollector] consumed %d feedbacks from buffer", len(feedbacks))
    return feedbacks


def find_latest_recommendation(
    session_id: str,
    max_age_seconds: float = 0.0,
) -> dict | None:
    """Find the latest recommendation for a session.

    Used for implicit feedback: 关联用户回复到该会话最近一条推荐。不设时间窗——
    "很久后的回复是否相关、是否产生梯度"交给梯度更新器的模型判断（看 rec_content
    + user_reply 语义），不靠时间硬挡。配合 record_feedback "同 rec_id 只采紧跟
    第一条、后续丢弃"，每条推荐只关联它之后紧跟的第一条用户回复。

    Args:
        session_id: Session ID
        max_age_seconds: 0（默认）= 不做过期检查，返回该 session 最新一条推荐。
            >0 时只返回 tick_at 在该秒数内的推荐（历史用法，现已不用）。

    Returns:
        Latest recommendation record or None
    """
    from jiuwenswarm.agents.harness.common.recommendation.profile_extractor import (
        load_recommendation_state,
    )

    state = load_recommendation_state()
    now = time.time()
    cutoff = now - max_age_seconds if max_age_seconds > 0 else 0.0

    # Find latest recommendation for this session (history is append-order;
    # reversed gives newest first)
    for rec in reversed(state.recommendation_history):
        if rec.get("session_id") != session_id:
            continue
        # 防御性 tick_at 解析：脏数据/旧记录可能缺字段或非数值，跳过而非崩
        try:
            tick_at = float(rec.get("tick_at", 0.0))
        except (TypeError, ValueError):
            continue
        if tick_at < cutoff:
            # reversed 已是最新的优先，遇到过期项说明更新鲜的都没命中，停。
            return None
        return rec

    return None
