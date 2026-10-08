# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Slim character consistency — what already happened, without pasting prior prompts."""

from __future__ import annotations

import re
from typing import Any

from jiuwenswarm.server.runtime.designer.continuity import infer_continuity_lock


_START_VERBS = (
    r"(?:start(?:s|ed|ing)?|begin(?:s|ning)?|begins?|kick(?:s|ed)?\s+off|"
    r"set(?:s|ting)?\s+off|take(?:s|ing)?\s+off|launch(?:es|ed|ing)?)"
)
_MOTION_NOUNS = (
    r"(?:run(?:s|ning)?|jog(?:s|ging)?|sprint(?:s|ing)?|walk(?:s|ing)?|"
    r"leave(?:s|ing)?|exit(?:s|ing)?|depart(?:s|ing)?|sit(?:s|ting)?|"
    r"stand(?:s|ing)?|rise(?:s|ing)?|stood|sat|enter(?:s|ing)?|"
    r"approach(?:es|ing)?|arrive(?:s|ing)?|open(?:s|ing)?|close(?:s|ing)?|"
    r"kiss(?:es|ing)?|hug(?:s|ging)?|hand(?:s|ing)?|wave(?:s|ing)?|"
    r"preach(?:es|ing)?|speak(?:s|ing)?|talk(?:s|ing)?)"
)


def _clean_action_snippet(text: str, *, limit: int = 160) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    # Drop lock banners / prior paste markers if a full prompt was passed by mistake.
    for marker in (
        "PRIOR KEYFRAME PROMPT",
        "PRIOR SHOT CONSISTENCY",
        "MASTER SCENE PROMPT",
        "LANGUAGE LOCK",
        "SPEECH LOCK",
        "BGM LOCK",
        "SCENE SPECS",
        "ASPECT LOCK",
        "STYLE LOCK",
        "AUDIO ROUTE",
    ):
        if marker in raw:
            raw = raw.split(marker, 1)[0].strip()
    # Prefer "Action:" / "Primary action" fragments when present.
    for pat in (
        r"Primary action for shot \d+:\s*(.+?)(?:\.|$)",
        r"Action:\s*(.+?)(?:\.|$)",
        r"character_action[:\s]+(.+?)(?:\.|$)",
    ):
        m = re.search(pat, raw, flags=re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            break
    return raw[:limit]


def extract_already_done_beats(
    action_or_prompt: str,
    *,
    shot_index: int | None = None,
    allow_repeat_hints: str = "",
) -> list[str]:
    """Turn a prior beat into short ALREADY_DONE bullets (no full prompt paste).

    Example: \"man starts running\" → \"man already started running (do not restart the run)\".
    Storyboard may opt into a repeat via allow_repeat_hints containing \"repeat\" / \"again\" / \"loop\".
    """
    allow = (allow_repeat_hints or "").lower()
    if any(w in allow for w in ("repeat this shot", "do again", "loop the action", "replay")):
        return []

    text = _clean_action_snippet(action_or_prompt, limit=280)
    if not text:
        return []
    lower = text.lower()
    bullets: list[str] = []
    prefix = f"shot {shot_index}: " if shot_index else ""

    # Starting a motion must not be restaged as a fresh start.
    if re.search(rf"{_START_VERBS}\s+(?:to\s+)?{_MOTION_NOUNS}", lower) or re.search(
        rf"{_MOTION_NOUNS}\s+.*\b(?:start|begin)", lower
    ):
        bullets.append(
            f"{prefix}onset already happened — do not show starting/beginning this motion again "
            f"({text[:120]})"
        )
    elif re.search(rf"\b{_MOTION_NOUNS}\b", lower):
        bullets.append(f"{prefix}prior beat already covered — do not restage: {text[:120]}")
    else:
        bullets.append(f"{prefix}already staged — do not redo: {text[:120]}")

    if re.search(r"\b(?:left|leave|leaving|exit|exited|exiting|walked away|gone)\b", lower):
        bullets.append(f"{prefix}exit already happened — keep them gone / do not leave again")
    if re.search(r"\b(?:sat|sit(?:s|ting)?|seated)\b", lower):
        bullets.append(f"{prefix}seating already established — do not reseat from standing unless storyboard asks")
    if re.search(r"\b(?:stood|stand(?:s|ing)?\s+up|gets?\s+up|rising)\b", lower):
        bullets.append(f"{prefix}stand-up already happened — do not stand up again")

    # Dedupe while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for b in bullets:
        key = b.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(b[:220])
    return out[:8]


def merge_already_done(*groups: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            text = str(item or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text[:220])
    return out[:16]


def architecture_clause_from_bible(bible: dict[str, Any] | None) -> str:
    """Scene architecture only — never include action/cast blocking."""
    if not isinstance(bible, dict) or not bible:
        return ""
    objects = ", ".join(str(x) for x in (bible.get("objects") or [])[:6] if str(x).strip())
    parts = [
        f"scene={bible.get('scene_name') or bible.get('place')}" if (bible.get("scene_name") or bible.get("place")) else "",
        f"lighting={bible.get('lighting')}" if bible.get("lighting") else "",
        f"objects={objects}" if objects else "",
        f"crowd={bible.get('crowd')}" if bible.get("crowd") else "",
        f"coherence={bible.get('coherence_rule')}" if bible.get("coherence_rule") else "",
    ]
    body = "; ".join(p for p in parts if p)
    if not body:
        return ""
    return (
        "SCENE ARCHITECTURE LOCK (keep place/light/props/crowd; change ONLY camera view + "
        f"this shot's on-screen cast/actions): {body}"
    )


def continuity_card_from_prior(
    *,
    prior_action: str = "",
    prior_prompt: str = "",
    shot_index: int | None = None,
    node_id: str = "",
    speech_line: str = "",
    bible: dict[str, Any] | None = None,
    storyboard_hints: str = "",
) -> dict[str, Any]:
    """Build a slim character consistency Director/leaves can inject (never the full prior prompt)."""
    seed = prior_action or _clean_action_snippet(prior_prompt, limit=240)
    done = extract_already_done_beats(
        seed,
        shot_index=shot_index,
        allow_repeat_hints=storyboard_hints,
    )
    lock = infer_continuity_lock(seed)
    card = {
        "from_shot_index": int(shot_index or 0) or None,
        "from_node_id": str(node_id or "") or None,
        "already_done": done,
        "continuity_lock": lock,
        "prior_action_summary": seed[:180],
        "prior_speech_done": str(speech_line or "").strip()[:160],
        "architecture": architecture_clause_from_bible(bible),
    }
    return card


def continuity_card_clause(card: dict[str, Any] | None) -> str:
    """Prompt clause: already-done + forbid — no pasted prior generate/Wan prompt."""
    if not isinstance(card, dict) or not card:
        return ""
    lines: list[str] = [
        "CHARACTER CONSISTENCY (Director): use this to avoid repeats — do NOT copy a prior shot prompt."
    ]
    src = card.get("from_shot_index") or card.get("from_node_id")
    if src:
        lines.append(f"- Source: {src}")
    summary = str(card.get("prior_action_summary") or "").strip()
    if summary:
        lines.append(f"- Prior beat summary (reference only): {summary[:160]}")
    done = [str(x) for x in (card.get("already_done") or []) if str(x).strip()]
    if done:
        lines.append("ALREADY_DONE (do not restage unless storyboard explicitly repeats):")
        lines.extend(f"  - {item}" for item in done[:10])
    lock = card.get("continuity_lock") if isinstance(card.get("continuity_lock"), dict) else {}
    forbid = str(lock.get("forbid") or "").strip()
    motion = str(lock.get("motion") or "").strip()
    if forbid or motion:
        bits = []
        if motion:
            bits.append(f"motion={motion}")
        if forbid:
            bits.append(f"forbid={forbid}")
        lines.append("CONTINUITY LOCK: " + "; ".join(bits))
    speech = str(card.get("prior_speech_done") or "").strip()
    if speech:
        lines.append(f"- Prior speech already delivered (do not repeat the same line): {speech[:160]}")
    arch = str(card.get("architecture") or "").strip()
    if arch:
        lines.append(arch)
    lines.append(
        "Advance THIS shot only. Do not regenerate an earlier onset (e.g. starting to run again) "
        "unless the storyboard asks for an explicit repeat."
    )
    return "\n".join(lines)


def strip_prior_prompt_pastes(prompt: str) -> str:
    """Remove previously injected full prior-prompt blocks from a leaf prompt."""
    text = str(prompt or "")
    if not text:
        return ""
    patterns = (
        r"\n*PRIOR KEYFRAME PROMPT \(do not redo the same beat; advance time\):\n[\s\S]*?(?=\n[A-Z][A-Z _/]{2,}:|\Z)",
        r"\n*PRIOR SHOT CONSISTENCY \(do NOT redo these beats; continue the film forward\):\n[\s\S]*?(?=\n[A-Z][A-Z _/]{2,}:|\Z)",
        r"\n*MASTER SCENE PROMPT:\n[\s\S]*?(?=\n[A-Z][A-Z _/]{2,}:|\Z)",
        r"\n*CHARACTER CONSISTENCY \(Director\):[\s\S]*?(?=\n[A-Z][A-Z _/]{2,}:|\Z)",
    )
    out = text
    for pat in patterns:
        out = re.sub(pat, "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()
