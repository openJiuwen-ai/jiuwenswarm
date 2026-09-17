# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Proactive recommendation — decision types, skill discovery, rate limiting,
and LLM analysis helpers.

Prompts are in ``proactive_prompts.py``; the tick loop is in
``proactive_engine.py``; situation report in ``situation_report.py``;
agent build/trigger/init in ``proactive_adapter.py``. This file provides
the building blocks those modules call.
"""

from __future__ import annotations

import json
import logging
import re
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Rate limiting ────────────────────────────────────────────────

# Note: max_recommend_per_day is configured in config.yaml, not hardcoded here
_daily_counts_lock = threading.Lock()
_daily_counts: dict[str, int] = {}


def _today_key() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today_recommend_count() -> int:
    with _daily_counts_lock:
        return _daily_counts.get(_today_key(), 0)


def _increment_daily_count() -> int:
    with _daily_counts_lock:
        key = _today_key()
        _daily_counts[key] = _daily_counts.get(key, 0) + 1
        return _daily_counts[key]


def _prune_daily_counts() -> None:
    """Remove entries older than 2 days."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    with _daily_counts_lock:
        keys_to_remove = [k for k in _daily_counts if k < cutoff]
        for k in keys_to_remove:
            _daily_counts.pop(k, None)


# ── Cooldown ─────────────────────────────────────────────────────

_COOLDOWN_HOURS = 24
_COOLDOWN_SECONDS = _COOLDOWN_HOURS * 3600


def _is_cooled_down(target: str, profile: Any, hours: int = _COOLDOWN_HOURS) -> bool:
    """Check if target is cooled down, using persisted cooldown records from profile."""
    cooldown_records = getattr(profile, "cooldown_records", {})
    last = cooldown_records.get(target, 0.0)
    return (time.time() - last) >= hours * 3600  # hours*3600 而非 _COOLDOWN_SECONDS：保留 hours 参数可被外部覆盖


def _mark_recommended(target: str, profile: Any) -> None:
    """Mark target as recommended, persisting to profile's cooldown records."""
    if not hasattr(profile, "cooldown_records"):
        profile.cooldown_records = {}
    profile.cooldown_records[target] = time.time()


def _prune_cooldown_records(profile: Any) -> None:
    """Remove entries past their cooldown window from profile."""
    if not hasattr(profile, "cooldown_records"):
        return
    cutoff = time.time() - _COOLDOWN_SECONDS
    keys_to_remove = [k for k, v in profile.cooldown_records.items() if v < cutoff]
    for k in keys_to_remove:
        profile.cooldown_records.pop(k, None)


# ── Recommendation decision ──────────────────────────────────────

def _safe_urgency(value: Any, default: float = 0.5) -> float:
    """Parse urgency to float in [0,1]; return default on failure.

    LLM may output urgency as string ("0.8") or non-numeric; bare float() would
    raise ValueError and discard the whole decision (type/target/reason lost).
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f < 0.0:  # NaN or negative
        return default
    return min(f, 1.0)


@dataclass
class RecommendationDecision:
    """What the engine decided to proactively tell the user."""

    type: str  # "skill_recommend" | "task_reminder" | "need_exploration"
    target: str  # skill name, task description, or exploration direction
    reason: str  # internal reason (for LLM message generation)
    urgency: float = 0.5  # 0.0..1.0


# ── Skill discovery ─────────────────────────────────────────────

def _parse_skill_md(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    meta: dict[str, Any] = {}
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if fm_match:
        body = fm_match.group(2).strip()
        try:
            import yaml
            loaded = yaml.safe_load(fm_match.group(1))
            if isinstance(loaded, dict):
                meta = {str(k): v for k, v in loaded.items()}
        except Exception as exc:
            logger.debug("[ProactiveEngine] skill %s frontmatter parse failed: %s", path, exc)
        if not meta:
            for line in fm_match.group(1).splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^(\w[\w_-]*)\s*:\s*(.*)", line)
                if m:
                    meta[m.group(1)] = m.group(2).strip().strip("'\"")
        if "name" not in meta:
            meta["name"] = path.stem
        meta.setdefault("description", body[:500])
    else:
        meta["name"] = path.stem
        meta["description"] = text[:500]
    return meta


def _get_all_skills() -> tuple[set[str], list[dict[str, Any]]]:
    """Discover available skills by scanning disk, filtered to "usable now".

    只暴露用户当下能真正使用的 skill = 已安装且 enabled=true。具体剔除：
    - 未安装：已卸载的目录磁盘上不在（不进集合）；builtin 目录里尚未安装的
      跳过（installed=False，不进候选池，也不进校验集合）。
    - 已安装但被禁用：skill 级禁用（enabled=false）与"插件禁用但未 reload"
      两种状态只动 state、目录仍在原位，磁盘扫描会照扫到，故交叉 state 把
      它们一并剔除。插件级禁用并 reload 过的目录被物理移进 _disabled_plugins
      （下划线开头），已被 startswith("_") 跳过，这里不重复处理。
    """
    try:
        from jiuwenswarm.common.utils import get_agent_skills_dir, get_builtin_skills_dir
        skills_dir = get_agent_skills_dir()
        builtin_dir = get_builtin_skills_dir()
        skills: list[dict[str, Any]] = []
        installed_names: set[str] = set()
        seen: set[str] = set()
        for child_dir, source, is_installed in [
            (skills_dir, "local", True), (builtin_dir, "builtin", False),
        ]:
            if not child_dir or not child_dir.exists():
                continue
            for child in child_dir.iterdir():
                if not child.is_dir() or child.name.startswith("_"):
                    continue
                md_path = child / "SKILL.md"
                if not md_path.exists():
                    continue
                meta = _parse_skill_md(md_path)
                if meta is None or meta.get("name") in seen:
                    continue
                seen.add(meta["name"])
                meta.setdefault("name", child.name)
                meta["installed"] = is_installed
                meta.setdefault("source", source)
                meta.setdefault("description", meta.get("body", ""))
                meta.pop("body", None)
                skills.append(meta)
                if is_installed:
                    installed_names.add(meta["name"])

        # 只推"能用"的：候选池与校验集合都只保留已安装的 skill。builtin 未安装
        # 的不进候选池（用户当下用不了），也不进校验集合（LLM 即便选了也会被 reject）。
        skills = [s for s in skills if s.get("installed")]

        # 交叉 state：剔除已安装但被禁用（enabled=false）的 skill。涵盖 skill 级
        # 禁用与"插件禁用但未 reload"两种目录仍在原位的状态。
        try:
            from jiuwenswarm.server.runtime.skill.skilldev import (
                load_execution_disabled_skills,
            )
            disabled = set(load_execution_disabled_skills())
        except Exception as exc:
            logger.debug("[ProactiveEngine] load disabled skills failed: %s", exc)
            disabled = set()
        if disabled:
            skills = [s for s in skills if s.get("name") not in disabled]

        installed_names = {s.get("name") for s in skills if s.get("name")}
        return installed_names, skills
    except Exception as exc:
        logger.warning("[ProactiveEngine] skill list load failed: %s", exc)
        return set(), []


# ── LLM helpers ──────────────────────────────────────────────────


def _extract_output_text(invoke_result: Any) -> str:
    """Extract the model's text output from a DeepAgent.invoke result.

    ``invoke`` returns a Dict like ``{"output": <str>, "result_type": ...}``.
    Strictly reads ``output``; returns "" when absent/wrong type so the caller
    treats it as "no decision" instead of misreading ``result_type`` ("answer")
    as the output (which would fail JSON parsing).
    """
    if invoke_result is None:
        return ""
    if isinstance(invoke_result, str):
        return invoke_result
    if isinstance(invoke_result, dict):
        out = invoke_result.get("output")
        if isinstance(out, str):
            return out
        if isinstance(out, list):
            return "\n".join(s for s in out if isinstance(s, str))
        # 不兜底扫其他值——result_type 也是 str，误取会解析失败。output 缺失返回空。
        return ""
    return ""


def _extract_json_from_response(text: str) -> str:
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0).strip()
    return ""


def _get_model(temperature: float = 0.0) -> Any:
    try:
        from jiuwenswarm.common.config import get_default_models
        from openjiuwen.core.foundation.llm import (
            Model, ModelClientConfig, ModelRequestConfig,
        )
    except ImportError:
        return None
    entries = get_default_models()
    if not entries:
        return None
    entry = entries[0]
    mcc = entry.get("model_client_config", {})
    model_name = mcc.get("model_name", "")
    mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
    # 关闭 thinking：proactive 决策是轻量单轮 JSON 产出（skill 选择/痛点判断），
    # 不需要思维链，开了反而拖慢 tick、占 token。与 symphony 其它模块（openai_api
    # client、fast planner、tree llm_runtime、graph matcher）保持一致。
    # ModelRequestConfig 的 model_config={"extra":"allow"}，extra_body 会经
    # base_model_client._build_request_params 的 model_dump 透传到底层
    # chat.completions.create(extra_body=...)。
    try:
        return Model(
            model_client_config=ModelClientConfig(**mcc_fields),
            model_config=ModelRequestConfig(
                model=model_name,
                temperature=temperature,
                extra_body={
                    "thinking": {"type": "disabled"},
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ),
        )
    except Exception:
        return None


# ── Unified analysis prompt ──────────────────────────────────────
# prompt 模板拆到 proactive_prompts.py，便于独立维护话术。

from jiuwenswarm.agents.harness.common.recommendation.proactive_prompts import (
    UNIFIED_ANALYSIS_PROMPT,
    DIRECTIVE_PROMPT,
)


@dataclass
class AnalysisResult:
    """Output from the unified LLM analysis call."""
    decision: RecommendationDecision | None = None


async def _analyze_and_decide(
    report_text: str,
    profile: Any,
    skills: list[dict[str, Any]],
    proactive_agent: Any,
) -> AnalysisResult:
    """Single call via the proactive agent: update profile + decide whether to recommend.

    ``proactive_agent`` is a lightweight DeepAgent (no tools, no task_loop,
    single-round) built by app_agentserver. It replaces the old bare
    ``model.invoke([SystemMessage, UserMessage])`` — going through the agent
    framework's invoke chain (rails, model selection, observation) instead of
    hand-rolling Model/SystemMessage/UserMessage.

    ``report_text`` is the fully rendered situation report (from
    ``SituationReport.render_for_llm``) and already contains the user profile,
    conversation summary, recommendation history, pending commitments,
    calendar events, and candidate skills. The prompt therefore takes only
    ``conversation_summary`` — re-formatting those sections here would
    duplicate them in the LLM context.
    """
    prompt = UNIFIED_ANALYSIS_PROMPT.format(
        conversation_summary=report_text,
    )

    # conversation_id 每次 tick 用唯一值——若用固定 id，DeepAgent 的 context
    # engine 会累积历次 tick 的 prompt+response 进同一 context（实测累积到
    # 2904 条消息），导致决策极慢/超时/无响应。每次独立 session 避免累积。
    import time as _time
    conv_id = f"proactive_analysis_{int(_time.time() * 1000)}"

    try:
        # 专用 agent 单轮 invoke：query 进、{"output": <文本>} 出。
        # system prompt（"你是用户洞察与推荐助手。严格输出 JSON 对象。"）
        # 在建实例时已配好，这里只传 query。
        result = await proactive_agent.invoke({
            "query": prompt,
            "conversation_id": conv_id,
        })
        content = _extract_output_text(result)
        json_str = _extract_json_from_response(content)
        if not json_str:
            return AnalysisResult()

        delta = json.loads(json_str)
        if not isinstance(delta, dict):
            return AnalysisResult()

        # Extract decision (画像已废弃——所有推荐基于当前对话)
        decision = None
        decision = None
        raw_decision = delta.get("decision")
        if isinstance(raw_decision, dict) and raw_decision.get("type") and raw_decision.get("target"):
            dec_type = raw_decision["type"]
            dec_target = raw_decision["target"]
            # Validate: skill_recommend must target an actually-existing skill.
            # LLM may recommend a skill mentioned in chat history but no longer
            # installed; reject such decisions so we don't push a non-existent skill.
            if dec_type == "skill_recommend":
                valid_skill_names = {s.get("name") for s in skills if s.get("name")}
                if dec_target not in valid_skill_names:
                    logger.warning(
                        "[ProactiveEngine] decision rejected: skill_recommend target '%s' not in available skills %s",
                        dec_target, sorted(valid_skill_names)[:10],
                    )
                    decision = None
                else:
                    decision = RecommendationDecision(
                        type=dec_type,
                        target=dec_target,
                        reason=raw_decision.get("reason", ""),
                        urgency=_safe_urgency(raw_decision.get("urgency", 0.5)),
                    )
            else:
                decision = RecommendationDecision(
                    type=dec_type,
                    target=dec_target,
                    reason=raw_decision.get("reason", ""),
                    urgency=_safe_urgency(raw_decision.get("urgency", 0.5)),
                )

        logger.info(
            "[ProactiveEngine] analysis: decision=%s",
            f"{decision.type}:{decision.target}" if decision else "null",
        )

        return AnalysisResult(
            decision=decision,
        )
    except Exception as exc:
        logger.warning("[ProactiveEngine] LLM analysis failed: %s", exc)
        return AnalysisResult()
    finally:
        # 决策是一次性的——本次 invoke 完立即释放 checkpointer 持久化的 session
        # 记录，避免孤儿数据无限累积（每次 tick 一条，不清会持续增长）。
        # proactive_agent 是无状态单次决策，不需要跨 tick 保留 context。
        try:
            from openjiuwen.core.session.checkpointer import CheckpointerFactory
            await CheckpointerFactory.get_checkpointer().release(conv_id)
        except Exception as exc:
            logger.debug("[ProactiveEngine] checkpoint release failed: %s", exc)


async def _trigger_main_agent(
    session_id: str,
    channel_id: str | None,
    decision: RecommendationDecision,
    trigger_callback: Any,
    on_delivered: Any = None,
) -> bool:
    """Trigger the main agent to run one round and generate the recommendation message.

    Constructs a directive-style query containing the decision (type/target/reason)
    and hands it to ``trigger_callback`` (injected by app_agentserver), which drives
    ``adapter.process_message_stream`` on the target session. The main agent then:

    - generates the recommendation message in its own voice → enters its context
      engine (``save_contexts`` persists it, so future dialogue sees it)
    - streams the output to the frontend via the normal E2A push path

    避让：trigger_callback 内部检查 ``is_deep_agent_executing_for_session``，
    目标 session 正忙时返回 False（跳过本次 tick，下个 tick 再来）。

    ``on_delivered``: fire-and-forget 后台 task 真正跑完（推荐确实送达）时回调。
    用于让调用方在"推荐确实送达"时再做计数/状态持久化，避免后台失败却已计数。

    Returns:
        True if the main agent was triggered (后台异步跑), False if the session was busy
        or delivery failed.
    """
    query = DIRECTIVE_PROMPT.format(
        rec_type=decision.type,
        target=decision.target,
        reason=decision.reason,
    )
    try:
        return bool(await trigger_callback(session_id, channel_id, query, decision,
                                           on_delivered=on_delivered))
    except Exception as exc:
        logger.warning("[ProactiveEngine] trigger_main_agent failed: %s", exc, exc_info=True)
        return False