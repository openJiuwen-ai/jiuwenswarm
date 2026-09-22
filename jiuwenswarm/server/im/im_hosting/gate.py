"""Reply gate: keyword / relevant. Does not send or open a turn."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

MATCH_MODES = ("keyword", "relevant")
BUILTIN_GROUP_RULE = {"match_mode": "keyword", "keywords": [], "strip_keywords": False}
BUILTIN_USER_RULE = {"match_mode": "relevant", "keywords": [], "strip_keywords": False}

RelevanceJudge = Callable[[str, Sequence[str]], Awaitable[bool]]

_SPLIT_KEYWORDS = re.compile(r"[,，\n;；]+")


def split_keywords(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = _SPLIT_KEYWORDS.split(raw)
    elif isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            parts.extend(_SPLIT_KEYWORDS.split(str(item)))
    else:
        parts = []
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        word = part.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        out.append(word)
    return out


def normalize_rule(raw: Any, *, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(fallback or BUILTIN_GROUP_RULE)
    mode = str(base.get("match_mode") or "keyword").strip().lower()
    keywords = list(base.get("keywords") or [])
    strip = bool(base.get("strip_keywords") or False)
    if isinstance(raw, dict):
        if raw.get("match_mode"):
            candidate = str(raw.get("match_mode") or "").strip().lower()
            if candidate in MATCH_MODES:
                mode = candidate
        if "keywords" in raw:
            keywords = split_keywords(raw.get("keywords"))
        if "strip_keywords" in raw:
            strip = bool(raw.get("strip_keywords"))
    if mode not in MATCH_MODES:
        mode = "keyword"
    return {"match_mode": mode, "keywords": keywords, "strip_keywords": strip}


def rule_override_is_set(raw: Any) -> bool:
    if not isinstance(raw, dict) or not raw:
        return False
    return bool(raw.get("match_mode")) or "keywords" in raw


def resolve_rule(target: dict[str, Any], channel_policy: dict[str, Any] | None) -> dict[str, Any]:
    kind = str(target.get("target_kind") or "")
    builtin = BUILTIN_USER_RULE if kind == "user" else BUILTIN_GROUP_RULE
    policy = channel_policy or {}
    default_key = "default_user_rule" if kind == "user" else "default_group_rule"
    inherited = normalize_rule(policy.get(default_key), fallback=builtin)
    override = target.get("rule_override")
    if rule_override_is_set(override):
        return normalize_rule(override, fallback=inherited)
    return inherited


def matches_keywords(text: str, keywords: Sequence[str]) -> bool:
    blob = (text or "").casefold()
    if not blob:
        return False
    for word in keywords:
        needle = (word or "").strip().casefold()
        if needle and needle in blob:
            return True
    return False


async def keyword_relevance_judge(text: str, keywords: Sequence[str]) -> bool:
    return matches_keywords(text, keywords)


async def llm_relevance_judge(text: str, keywords: Sequence[str]) -> bool:
    if matches_keywords(text, keywords):
        return True
    verdict = await _ask_llm_yes_no(text, keywords)
    if verdict is None:
        return False
    return verdict


async def _ask_llm_yes_no(text: str, keywords: Sequence[str]) -> Optional[bool]:
    topics = "、".join(keywords)
    prompt = (
        "判断这条即时消息是否属于用户要托管处理的一类问题。\n"
        f"主题：{topics or '（未填写）'}\n"
        f"消息：{(text or '')[:800]}\n"
        "只回答 YES 或 NO。"
    )
    try:
        from openjiuwen.core.foundation.llm import Model
        from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig

        from jiuwenswarm.common.config import _parse_custom_headers, get_config
        from jiuwenswarm.common.local_env_config import read_env
        from jiuwenswarm.common.reasoning_injector import build_reasoning_model_request_kwargs

        cfg = get_config() or {}
        react = cfg.get("react") or {}
        mcc = dict(react.get("model_client_config") or {})
        mco = dict(react.get("model_config_obj") or {})
        name = (
            str(react.get("model_name") or "").strip()
            or str(mcc.get("model_name") or "").strip()
            or read_env("MODEL_NAME", "").strip()
        )
        api_key = (mcc.get("api_key") or read_env("API_KEY") or "").strip()
        api_base = (mcc.get("api_base") or read_env("API_BASE") or "").strip()
        if not name or not (api_key or api_base):
            return None
        if api_base.endswith("/chat/completions"):
            api_base = api_base.rsplit("/chat/completions", 1)[0]
        client_provider = mcc.get("client_provider") or read_env("MODEL_PROVIDER", "OpenAI")
        reasoning_mcc = {**mcc, "client_provider": client_provider, "api_base": api_base}
        model = Model(
            model_config=ModelRequestConfig(
                **build_reasoning_model_request_kwargs(
                    model_client_config=reasoning_mcc,
                    model_config_obj={**mco, "temperature": 0.0, "top_p": 0.3},
                    model_name=name,
                )
            ),
            model_client_config=ModelClientConfig(
                client_id="im_hosting_relevance_client",
                client_provider=client_provider,
                api_key=api_key,
                api_base=api_base,
                verify_ssl=mcc.get("verify_ssl", True),
                ssl_cert=mcc.get("ssl_cert", None),
                timeout=20.0,
                custom_headers=_parse_custom_headers(mcc.get("custom_headers") or read_env("CUSTOM_HEADERS")),
            ),
        )
        response = await model.invoke(
            model=name,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = ""
        if response and isinstance(getattr(response, "content", None), str):
            raw = response.content.strip().upper()
        if raw.startswith("YES") or raw == "Y":
            return True
        if raw.startswith("NO") or raw == "N":
            return False
        if "YES" in raw and "NO" not in raw:
            return True
        return False
    except Exception:
        LOGGER.debug("[im_hosting] relevance llm unavailable", exc_info=True)
        return None


async def message_passes_gate(
    text: str,
    rule: dict[str, Any],
    *,
    relevance_judge: RelevanceJudge | None = None,
) -> tuple[bool, str]:
    mode = str(rule.get("match_mode") or "keyword")
    keywords = list(rule.get("keywords") or [])
    if not (text or "").strip():
        return False, "empty"
    if not keywords:
        return True, "unfiltered"
    if mode == "keyword":
        hit = matches_keywords(text, keywords)
        return hit, "keyword" if hit else "keyword_miss"
    if mode == "relevant":
        judge = relevance_judge or llm_relevance_judge
        hit = bool(await judge(text, keywords))
        return hit, "relevant" if hit else "relevant_miss"
    return False, "unknown_mode"
