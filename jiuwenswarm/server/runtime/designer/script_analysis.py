# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Analyze user prompts into cast, shots, scenes, and audio for smart graph build.

LLM is required for production analysis. ``heuristic_analysis`` is retained only as a
unit-test helper for cast/shot parsing fixtures — never called on the product path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CHARS = 12
_MAX_SHOTS = 16
_MAX_SCENES = 8
# Keep under typical UI bootstrap budgets while still allowing a real LLM call.
_DEFAULT_LLM_TIMEOUT_SEC = 90.0

# Generic role nouns — not tied to any one story.
_ROLE_NOUNS = (
    "preacher|pastor|priest|minister|teacher|doctor|nurse|soldier|officer|"
    "courier|messenger|king|queen|prince|princess|knight|wizard|witch|"
    "chef|pilot|driver|farmer|scientist|engineer|artist|singer|dancer|"
    "detective|spy|robot|android|hero|villain|warrior|hunter|merchant|"
    "mother|father|parent|brother|sister|friend|stranger|leader|captain|"
    "partner|girlfriend|boyfriend|wife|husband|date|spouse|"
    "boy|girl|child|kid|man|woman|person|guy|lady"
)


def _clamp_list(items: list[Any], limit: int) -> list[Any]:
    return items[: max(1, min(limit, len(items) or 1))]


def _title_case_label(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    if not cleaned:
        return ""
    return cleaned[:1].upper() + cleaned[1:]


def infer_primary_subject_name(prompt: str) -> str:
    """Best-effort subject label from the user prompt when cast extraction is empty."""
    text = _strip_prompt_filler(_strip_reference_appendix(prompt))
    match = re.search(
        rf"\b((?:young|old|elderly|little|small|tall|beautiful|handsome|pretty|"
        rf"sad|happy|angry|scared|lonely|brave)\s+)?(({_ROLE_NOUNS}))\b",
        text,
        flags=re.I,
    )
    if match:
        phrase = f"{match.group(1) or ''}{match.group(2)}".strip()
        label = _title_case_label(phrase)
        if label:
            return label
    art = re.search(rf"\b(?:a|an|the)\s+(({_ROLE_NOUNS}))\b", text, flags=re.I)
    if art:
        label = _title_case_label(art.group(1))
        if label:
            return label
    chunk = text.split(".")[0].split(",")[0].strip()[:48]
    if chunk and len(chunk.split()) <= 6:
        label = _title_case_label(chunk)
        if label:
            return label
    return "Subject"


def _strip_reference_appendix(text: str) -> str:
    """Drop attachment roster so it is not treated as a cinematic beat."""
    cleaned = re.split(
        r"\n+\s*(?:User attached reference media|REFERENCE_MEDIA)\b",
        text or "",
        maxsplit=1,
        flags=re.I,
    )[0]
    return cleaned.strip()


def _strip_prompt_filler(text: str) -> str:
    """Drop leading ask-phrases so storyboard actions are cinematic, not meta."""
    cleaned = re.sub(
        r"^(?:i\s+want(?:\s+the)?(?:\s+video)?(?:\s+of)?|please\s+(?:make|create)|"
        r"create(?:\s+a)?(?:\s+video)?(?:\s+of)?|make(?:\s+a)?(?:\s+video)?(?:\s+of)?)\s+",
        "",
        text.strip(),
        flags=re.I,
    )
    return cleaned.strip(" ,.")


def _contextual_character_name(role: str, clause: str, *, another: bool = False) -> str:
    """General labels from role + optional 'another' / motion cues (domain-agnostic)."""
    cl = clause.lower()
    base = _title_case_label(role)
    if another or re.search(r"\b(?:another|second|other)\b", cl):
        if re.search(r"\b(?:leaves?|gets?\s+up|exits?|walks?\s+out|departs?)\b", cl):
            return f"{base} leaving"
        return f"{base} 2"
    return base


def _match_terms_for_character(name: str, description: str) -> list[str]:
    """Build general match phrases from the character name/description only.

    No domain-specific dictionaries (religion, occupations, etc.) — only lexical cues
    derived from this character's own text so heuristics stay scenario-agnostic.
    """
    name_l = name.lower().strip()
    # Ignore continuity tails after 'while' so they do not bleed into another subject.
    desc_l = re.split(r"\bwhile\b", (description or "").lower(), maxsplit=1)[0]
    terms: list[str] = []

    def add(*xs: str) -> None:
        for x in xs:
            x = x.strip().lower()
            if x and x not in terms and len(x) > 2:
                terms.append(x)

    stop = {
        "with", "from", "that", "this", "their", "there", "about", "while", "when",
        "then", "into", "onto", "have", "been", "were", "what", "which", "where",
        "your", "they", "them", "than", "also", "just", "only", "very", "some",
    }

    if name_l:
        add(name_l)
        for tok in re.split(r"\W+", name_l):
            if len(tok) > 2 and tok not in stop:
                add(tok)

    # Prefer multi-word snippets from the description (first ~12 content words).
    words = [w for w in re.split(r"\W+", desc_l) if len(w) > 2 and w not in stop]
    for i, w in enumerate(words[:12]):
        add(w)
        if i + 1 < len(words):
            add(f"{w} {words[i + 1]}")

    # Generic disambiguators for numbered / alternate subjects.
    if name_l.endswith(" 2") or "another" in desc_l or "leaving" in name_l:
        add("another", "second", "other")
        for verb in ("gets up", "get up", "leaves", "leaving", "exits", "enters", "walks"):
            if verb in desc_l or verb in name_l:
                add(verb)

    terms.sort(key=len, reverse=True)
    return terms


def _cast_id_maps(
    characters: list[dict[str, Any]],
) -> tuple[set[str], dict[str, str]]:
    """valid ids + lowercase name/alias → id (exact name match only)."""
    valid: set[str] = set()
    by_name: dict[str, str] = {}
    for ch in characters:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        valid.add(cid)
        by_name[cid.lower()] = cid
        name = str(ch.get("name") or "").strip()
        if name:
            by_name[name.lower()] = cid
        for alias in ch.get("aliases") or []:
            a = str(alias or "").strip()
            if a:
                by_name[a.lower()] = cid
    return valid, by_name


def resolve_cast_token(token: object, *, valid_ids: set[str], by_name: dict[str, str]) -> str:
    """Map a cast token (id or display name) to a character id; else ''."""
    raw = str(token or "").strip()
    if not raw:
        return ""
    if raw in valid_ids:
        return raw
    hit = by_name.get(raw.lower())
    if hit:
        return hit
    m = re.search(r"(char_\d+)", raw, flags=re.I)
    if m and m.group(1) in valid_ids:
        return m.group(1)
    return ""


def resolve_cast_token_list(
    tokens: object,
    *,
    valid_ids: set[str],
    by_name: dict[str, str],
) -> list[str]:
    if not isinstance(tokens, list):
        return []
    out: list[str] = []
    for tok in tokens:
        cid = resolve_cast_token(tok, valid_ids=valid_ids, by_name=by_name)
        if cid and cid not in out:
            out.append(cid)
    return out


_LEADING_ARTICLE = re.compile(r"^(?:the|a|an|his|her|their)\s+", re.I)
_ROLE_STEMS = frozenset(_ROLE_NOUNS.split("|"))


def _cast_identity_key(name: str) -> str:
    """Fold 'The father' / 'Father' / \"Father's\" onto one key. Keep 'Man 2' distinct."""
    key = re.sub(r"\s+", " ", str(name or "").strip().lower())
    key = _LEADING_ARTICLE.sub("", key)
    key = re.sub(r"['’]s\b", "", key).strip()
    return key


def _role_stem(name: str) -> str:
    key = _cast_identity_key(name)
    parts = [p for p in key.split() if p]
    if not parts:
        return ""
    if parts[-1] in _ROLE_STEMS:
        return parts[-1]
    return ""


def _is_cast_variant(key: str) -> bool:
    """Numbered or exiting duplicates are different people, not article aliases."""
    return bool(re.search(r"(?:^|\s)(?:\d+|leaving)$", key))


def _heuristic_characters(prompt: str) -> list[dict[str, str]]:
    """General cast extraction from role nouns / a|the X phrases."""
    text = prompt.strip()
    found: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(name: str, desc: str) -> None:
        raw = str(name or "").strip()
        key = _cast_identity_key(raw)
        if not key or key in used:
            return
        display = _title_case_label(_LEADING_ARTICLE.sub("", raw)) or raw
        if _is_cast_variant(key):
            used.add(key)
            found.append(
                {
                    "id": f"char_{len(found) + 1}",
                    "name": display,
                    "description": desc[:300],
                    "match_terms": _match_terms_for_character(display, desc),
                }
            )
            return
        stem = _role_stem(display)
        if stem:
            for existing in found:
                ek = _cast_identity_key(str(existing.get("name") or ""))
                if _is_cast_variant(ek) or _role_stem(str(existing.get("name") or "")) != stem:
                    continue
                # "Father" / "The father", or bare "Child" vs "Young child".
                if ek != key and ek != stem and key != stem:
                    continue
                aliases = [str(a) for a in (existing.get("aliases") or []) if str(a)]
                if display not in aliases and display.lower() != str(existing.get("name") or "").lower():
                    aliases.append(display)
                    existing["aliases"] = aliases
                if key != stem and len(key) > len(ek):
                    existing["name"] = display
                    existing["match_terms"] = _match_terms_for_character(
                        display, str(existing.get("description") or desc)
                    )
                used.add(key)
                used.add(ek)
                return
        used.add(key)
        if stem:
            used.add(stem)
        found.append(
            {
                "id": f"char_{len(found) + 1}",
                "name": display,
                "description": desc[:300],
                "match_terms": _match_terms_for_character(display, desc),
            }
        )

    for match in re.finditer(
        # Allow 0–2 adjectives before a role noun from _ROLE_NOUNS.
        rf"\b(?:a|an|the|his|her|their)\s+(?:[A-Za-z-]+\s+){{0,2}}((?:{_ROLE_NOUNS}))\b",
        text,
        flags=re.I,
    ):
        role = match.group(1)
        ahead = text[max(0, match.start() - 8) : match.start()].lower()
        if ahead.rstrip().endswith("what"):
            continue
        start_i = match.start()
        end_i = min(len(text), match.end() + 80)
        clause = text[start_i:end_i].split(".")[0].strip(" ,.;")
        nxt = re.search(
            rf"\b(?:a|an|the|another|a second|the other|his|her|their)\s+"
            rf"(?:[A-Za-z-]+\s+){{0,2}}(?:{_ROLE_NOUNS})\b",
            clause[len(match.group(0)) :],
            flags=re.I,
        )
        if nxt:
            clause = clause[: len(match.group(0)) + nxt.start()].strip(" ,.;")
        if re.match(r"^(?:the|a|an)\s+man\s+is\s+saying\b", clause, flags=re.I):
            continue
        label = _contextual_character_name(role, clause, another=False)
        # Prefer fuller phrase when adjectives present ("Young Man").
        full = match.group(0).strip()
        full_label = _title_case_label(
            re.sub(r"^(?:a|an|the|his|her|their)\s+", "", full, flags=re.I)
        )
        if full_label and len(full_label.split()) <= 4:
            label = full_label
        # "a date" (appointment) is not a character; "his partner/date" is.
        if role.lower() == "date" and not re.search(
            r"\b(?:his|her|their)\s+date\b", match.group(0), flags=re.I
        ):
            continue
        if _cast_identity_key(label) in used and role.lower() == "man":
            continue
        add(label, clause or f"{label} from the user prompt")
        if len(found) >= _MAX_CHARS:
            break

    # Bare role mentions without article (any role from _ROLE_NOUNS).
    for match in re.finditer(
        rf"(?:^|[.!?]\s+|,\s+)((?:[A-Za-z-]+\s+){{0,1}}(?:{_ROLE_NOUNS}))\b",
        text,
        flags=re.I,
    ):
        phrase = match.group(1).strip()
        role = phrase.split()[-1].lower()
        if role in {"person", "people", "guy", "date"}:
            continue
        label = _title_case_label(phrase)
        if _cast_identity_key(label) in used:
            continue
        if len(label.split()) > 3:
            continue
        if re.match(r"^(?:while|when|and|or|as|if|with)\b", label, flags=re.I):
            continue
        add(label, phrase)
        if len(found) >= _MAX_CHARS:
            break

    for match in re.finditer(
        rf"\b(?:another|a second|the other)\s+((?:{_ROLE_NOUNS}))\b([^.!?\n]{{0,80}})",
        text,
        flags=re.I,
    ):
        role = match.group(1)
        clause = match.group(0).strip(" ,.;")
        clause = re.split(r"\bwhile\b", clause, maxsplit=1)[0].strip(" ,.;") or clause
        label = _contextual_character_name(role, clause, another=True)
        add(label, clause)

    for ch in found:
        if not ch.get("match_terms"):
            ch["match_terms"] = _match_terms_for_character(
                str(ch.get("name") or ""), str(ch.get("description") or "")
            )

    if not found:
        add(infer_primary_subject_name(text), "primary subject inferred from the user prompt")
    return _clamp_list(found, _MAX_CHARS)


def _score_character_in_text(ch: dict[str, Any], text: str) -> int:
    cl = text.lower()
    score = 0
    for term in ch.get("match_terms") or []:
        if term and term in cl:
            score += max(3, len(term))
    name = str(ch.get("name") or "").lower().strip()
    if name and re.search(rf"\b{re.escape(name)}\b", cl):
        score += max(6, len(name))
    return score


def _focus_character_ids(chunk: str, characters: list[dict[str, Any]]) -> list[str]:
    """Assign only characters this shot is actually about (no shared 'man' leak)."""
    cl = chunk.lower()
    scored: list[tuple[int, str]] = []
    for ch in characters:
        sc = _score_character_in_text(ch, chunk)
        name = str(ch.get("name") or "").lower().strip()
        if name and re.search(rf"\b{re.escape(name)}\b", cl):
            sc = max(sc, 8)
        if sc > 0:
            scored.append((sc, str(ch["id"])))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]))
    # Absolute floor so co-focus cast survives a high-scoring lead.
    focus = [cid for sc, cid in scored if sc >= 4]
    if not focus:
        focus = [scored[0][1]]
    return list(dict.fromkeys(focus))


def _split_prompt_beats(prompt: str) -> list[str]:
    """Split into cinematic beats; avoid treating continuity 'while still…' as a new shot."""
    text = _strip_prompt_filler(_strip_reference_appendix(prompt))
    if not text:
        return ["Establish the scene"]

    # Do not use bare \bnext\b — it false-splits on "next to her".
    split_re = re.compile(
        r"(?:"
        r"\b(?:and then|after that|finally|afterward|afterwards|之后|然后|接着)\b"
        r"|\bnext(?:ly)?\s*,"
        r"|\bnext\s+(?:we|shot|scene|beat|the camera)\b"
        r"|\bwhile\s+(?:another|a second|the other)\b"
        r"|(?<=[.!?])\s+"
        r"|\b(?:the camera\s+(?:then\s+)?)?pans?\s+to\b"
        r"|\bcut(?:s)?\s+to\b"
        r")",
        flags=re.I,
    )
    parts: list[str] = []
    last = 0
    for m in split_re.finditer(text):
        left = text[last : m.start()].strip(" ,.")
        if left and len(left) > 8:
            parts.append(left)
        cue = m.group(0).strip().lower()
        last = m.start() if re.search(r"\b(?:pan|cut)\b", cue) else m.end()
    tail = text[last:].strip(" ,.")
    if tail and len(tail) > 8:
        parts.append(tail)

    merged: list[str] = []
    for part in parts:
        if (
            merged
            and len(part) < 70
            and re.match(
                r"^(?:this|that|the same)\s+(?:man|woman|person)\b|"
                r"^to\s+her\b|"
                r"^listening\b",
                part,
                flags=re.I,
            )
        ):
            merged[-1] = f"{merged[-1]}, {part}"
            continue
        if merged and len(part) < 25:
            merged[-1] = f"{merged[-1]} {part}"
            continue
        merged.append(part)

    if len(merged) < 2:
        beats: list[str] = []
        for match in re.finditer(
            r"[^.!?\n]*(?:\b(?:pan|zoom|cut|tilt|track|dolly|close-?up|wide shot|"
            r"enters?|exits?|leaves?|walks?|runs?|speaks?|looks?|turns?|sits?|"
            r"stands?|cries?|smiles?|listens?|nods?)\b)[^.!?\n]*",
            text,
            flags=re.I,
        ):
            piece = match.group(0).strip(" ,.")
            if len(piece) > 10:
                beats.append(piece)
        merged = beats or [text[:280]]

    out: list[str] = []
    for p in merged:
        key = re.sub(r"\s+", " ", p.lower())[:80]
        if out and key in re.sub(r"\s+", " ", out[-1].lower()):
            continue
        out.append(p)
    return _clamp_list(out, _MAX_SHOTS)


def _heuristic_shots(prompt: str, characters: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Split prompt into camera/action beats with focus cast per beat."""
    chunks = _split_prompt_beats(prompt)
    shots: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        cl = chunk.lower()
        focus = _focus_character_ids(chunk, characters)
        camera = "medium / eye-level"
        if re.search(r"\bpan\b", cl):
            camera = "medium / slow pan"
        elif re.search(r"\b(?:wide|crowd|establishing)\b", cl):
            camera = "wide / slight high"
        elif re.search(r"\b(?:close-?up|close up|detail|tears?)\b", cl):
            camera = "close-up / eye-level"
        action = _strip_prompt_filler(chunk)[:400]
        shots.append(
            {
                "shot_index": idx,
                "title": f"Shot {idx}",
                "action": action,
                "camera": camera,
                "character_ids": focus,
                "keyframe_prompt": action[:500],
                "timeline": f"{(idx - 1) * 2.0:.1f}-{idx * 2.0:.1f}s",
            }
        )

    # Ensure every character has a dedicated or shared beat — prefer new shot over
    # dumping them onto an unrelated establishing shot.
    # Respect explicit N-shot / N分镜 ceiling when present.
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        shot_ceiling = _explicit_shot_count_from_prompt(prompt) or _MAX_SHOTS
    except Exception:  # noqa: BLE001
        shot_ceiling = _MAX_SHOTS
    covered = {cid for s in shots for cid in s.get("character_ids") or []}
    for ch in characters:
        cid = str(ch.get("id") or "")
        if not cid or cid in covered:
            continue
        desc = str(ch.get("description") or ch.get("name") or "")
        best_i = None
        best_sc = 0
        for i, shot in enumerate(shots):
            sc = _score_character_in_text(ch, str(shot.get("action") or ""))
            if sc > best_sc:
                best_sc = sc
                best_i = i
        if best_i is not None and best_sc >= 4:
            shots[best_i]["character_ids"] = list(
                dict.fromkeys([*(shots[best_i].get("character_ids") or []), cid])
            )
            covered.add(cid)
            continue
        if len(shots) < min(_MAX_SHOTS, shot_ceiling):
            shots.append(
                {
                    "shot_index": len(shots) + 1,
                    "title": f"Focus {ch.get('name')}",
                    "action": f"Feature {ch.get('name')}: {desc}"[:400],
                    "camera": "medium / eye-level",
                    "character_ids": [cid],
                    "keyframe_prompt": desc[:500],
                    "timeline": f"{len(shots) * 2.0:.1f}-{(len(shots) + 1) * 2.0:.1f}s",
                }
            )
            covered.add(cid)
        elif shots and best_i is not None and best_sc >= 4:
            # Budget full: only attach when the action already mentions them.
            shots[best_i]["character_ids"] = list(
                dict.fromkeys([*(shots[best_i].get("character_ids") or []), cid])
            )
            covered.add(cid)

    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
        from jiuwenswarm.server.runtime.designer.node_labels import derive_shot_name

        shot["title"] = derive_shot_name(shot, fallback_index=i)
        # If a shot still has no cast, pick the single best character — not a round-robin leak.
        if not shot.get("character_ids") and characters:
            ranked = sorted(
                (
                    (_score_character_in_text(ch, str(shot.get("action") or "")), str(ch["id"]))
                    for ch in characters
                ),
                reverse=True,
            )
            if ranked and ranked[0][0] > 0:
                shot["character_ids"] = [ranked[0][1]]
            # else leave empty — fail closed; Director must fill on_screen
    return _clamp_list(shots, min(_MAX_SHOTS, shot_ceiling))


def _heuristic_scenes(prompt: str) -> list[dict[str, str]]:
    """Infer a primary setting from place nouns in the prompt (domain-agnostic)."""
    lower = prompt.lower()
    # Capture the place word itself — no genre templates.
    place_re = re.compile(
        r"\b(?:in|at|on|inside|outside|near|from)\s+(?:a|an|the|his|her|their)?\s*"
        r"([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,2})\b",
        flags=re.I,
    )
    stop = {
        "the",
        "a",
        "an",
        "his",
        "her",
        "their",
        "this",
        "that",
        "moment",
        "time",
        "day",
        "night",
        "way",
        "while",
        "front",
        "back",
    }
    scenes: list[dict[str, str]] = []
    for match in place_re.finditer(lower):
        phrase = re.sub(r"\s+", " ", match.group(1).strip())
        words = [w for w in phrase.split() if w not in stop]
        if not words:
            continue
        name = " ".join(words)[:48]
        if name.lower() in {str(s.get("name") or "").lower() for s in scenes}:
            continue
        scenes.append(
            {
                "id": f"scene_{len(scenes) + 1}",
                "name": name.title(),
                "description": f"setting mentioned in the prompt: {name}",
            }
        )
        if len(scenes) >= 2:
            break
    if not scenes:
        scenes.append(
            {
                "id": "scene_1",
                "name": "Primary setting",
                "description": "main location inferred from the user prompt",
            }
        )
    return _clamp_list(scenes, _MAX_SCENES)


def _select_shots_for_budget(
    shots: list[dict[str, Any]],
    budget: int,
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep up to budget shots while preserving cast coverage (do not drop uncovered cast)."""
    if len(shots) <= budget:
        return list(shots)
    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    remaining = list(shots)

    def take(idx: int) -> None:
        shot = remaining.pop(idx)
        selected.append(shot)
        covered.update(str(x) for x in (shot.get("character_ids") or []))

    # Always keep first establishing beat when present.
    if remaining:
        take(0)
    while len(selected) < budget and remaining:
        # Prefer a shot that introduces uncovered cast.
        best_i = 0
        best_gain = -1
        for i, shot in enumerate(remaining):
            cids = {str(x) for x in (shot.get("character_ids") or [])}
            gain = len(cids - covered)
            # Slight preference for earlier story order when gain ties.
            score = gain * 10 - i
            if score > best_gain:
                best_gain = score
                best_i = i
        take(best_i)

    # Uncovered cast stays uncovered (Director must list them).
    # Never fold into on_screen/character_ids via action-text scoring — that bleeds
    # later-meet people into early beats.
    for i, shot in enumerate(selected, start=1):
        shot["shot_index"] = i
    return selected


def _director_pipeline_decisions(
    prompt: str,
    characters: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Director-style layout + shot budget from prompt length and cast coverage."""
    n_chars = len(characters)
    n_shots = max(1, len(shots))
    words = len((prompt or "").split())
    if words < 40:
        budget = min(2, n_shots)
    elif words < 120:
        budget = min(3, n_shots)
    else:
        budget = min(4, n_shots)
    # Multi-cast stories need enough beats so later subjects survive budget trim.
    if n_chars >= 3:
        budget = max(budget, min(4, n_shots, max(3, n_chars - 1)))
    # Explicit N-shot / N分镜 language is a HARD ceiling (and floor when larger).
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        explicit = _explicit_shot_count_from_prompt(prompt or "")
        if explicit >= 1:
            budget = explicit
    except Exception:  # noqa: BLE001
        pass
    budget = max(1, min(_MAX_SHOTS, budget))

    multi = any(len(s.get("character_ids") or []) >= 2 for s in shots if isinstance(s, dict))
    solo = any(len(s.get("character_ids") or []) == 1 for s in shots if isinstance(s, dict))
    if n_chars <= 1:
        cast_layout = "single"
        prefer_combined = False
        prefer_split = False
    elif multi and solo:
        cast_layout = "hybrid"
        prefer_combined = True
        prefer_split = True
    elif multi:
        cast_layout = "combined"
        prefer_combined = True
        prefer_split = False
    else:
        cast_layout = "split"
        prefer_combined = False
        prefer_split = True

    return {
        "target_shot_count": budget,
        "cast_layout": cast_layout,
        "prefer_combined_cast": prefer_combined,
        "prefer_split_cast": prefer_split,
    }


def _heuristic_lean_shot(
    prompt: str,
    characters: list[dict[str, Any]],
    *,
    story_name: str = "",
    setting_id: str = "set_1",
) -> dict[str, Any]:
    """One full-narrative beat: no LLM → one scene card + one clip (no keyframe)."""
    from jiuwenswarm.server.runtime.designer.node_labels import derive_shot_name

    all_ids = [str(c.get("id")) for c in characters if str(c.get("id") or "").strip()]
    title = (story_name or derive_shot_name({"title": "", "action": prompt}, fallback_index=1))[:80]
    body = _strip_prompt_filler(prompt)[:1200] or prompt[:1200]
    cast_actions = {
        cid: body[:180] for cid in all_ids[:3]
    }
    return {
        "shot_index": 1,
        "title": title or "Full narrative",
        "action": body[:800],
        "camera": "medium / eye-level",
        "character_ids": list(all_ids),
        "on_screen": list(all_ids),
        "featured_cast_ids": list(all_ids[:1]),
        "ensemble_cast_ids": list(all_ids),
        "offscreen": [],
        "keyframe_prompt": body[:1200],
        "setting_id": str(setting_id or "set_1").strip() or "set_1",
        "timeline": "0-8s",
        "cast_actions": cast_actions,
        "occupancy": {
            "must_appear": list(all_ids),
            "offscreen": [],
            "cast_actions": cast_actions,
        },
    }


def _assign_heuristic_setting_ids(
    shots: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
) -> None:
    """Stamp setting_id on heuristic shots so each unique place becomes a Scene N card."""
    if not shots:
        return
    scene_ids = [
        str(s.get("id") or f"set_{i}").strip() or f"set_{i}"
        for i, s in enumerate(scenes or [], start=1)
    ] or ["set_1"]
    # Normalize scene ids used as setting keys.
    for i, sc in enumerate(scenes or []):
        if isinstance(sc, dict) and not str(sc.get("id") or "").strip():
            sc["id"] = scene_ids[i] if i < len(scene_ids) else f"set_{i + 1}"
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        if str(shot.get("setting_id") or "").strip():
            continue
        action = str(shot.get("action") or shot.get("title") or "").lower()
        matched = ""
        for sc in scenes or []:
            if not isinstance(sc, dict):
                continue
            name = str(sc.get("name") or "").strip().lower()
            sid = str(sc.get("id") or "").strip()
            if name and name in action and sid:
                matched = sid
                break
        shot["setting_id"] = matched or scene_ids[min(i, len(scene_ids) - 1)]


def heuristic_analysis(prompt: str) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis
    from jiuwenswarm.server.runtime.designer.media_model_playbook import default_style_lock
    from jiuwenswarm.server.runtime.designer.node_labels import derive_story_name
    from jiuwenswarm.server.runtime.designer.skills_loader import detect_audio_intent

    prompt = _strip_reference_appendix(prompt)
    characters = _heuristic_characters(prompt)
    scenes = _heuristic_scenes(prompt)
    # Align scene ids with setting_id keys used by smart_graph scene cards.
    for i, sc in enumerate(scenes):
        if isinstance(sc, dict):
            sc["id"] = str(sc.get("id") or f"set_{i + 1}").strip() or f"set_{i + 1}"
    audio = detect_audio_intent(prompt)
    lower = prompt.lower()
    if any(
        w in lower
        for w in ("speaking", "speaks", "says", "said", "voice", "dialogue", "talking", "narrat")
    ):
        if audio.get("policy") != "silent":
            audio = {
                **audio,
                "include_speech": True,
                "policy": "speech_and_music" if audio.get("include_music") else "speech",
                "notes": "Dialogue/speech cues detected; include speech.",
            }

    story_name = derive_story_name(prompt=prompt)
    explicit = 0
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        explicit = int(_explicit_shot_count_from_prompt(prompt) or 0)
    except Exception:  # noqa: BLE001
        explicit = 0

    primary_setting = str((scenes[0] or {}).get("id") or "set_1") if scenes else "set_1"

    # No LLM: default to ONE scene card + ONE clip (Wan reference mode: solos + scene refs).
    # Explicit N-shot / N分镜 is the only general multi-shot escape hatch.
    if explicit >= 2:
        shots = _heuristic_shots(prompt, characters)
        decisions = _director_pipeline_decisions(prompt, characters, shots)
        decisions["target_shot_count"] = max(1, min(_MAX_SHOTS, explicit))
        shots = _select_shots_for_budget(
            shots, int(decisions["target_shot_count"]), characters
        )
        for i, shot in enumerate(shots, start=1):
            shot["shot_index"] = i
        decisions = _director_pipeline_decisions(prompt, characters, shots)
        decisions["target_shot_count"] = max(1, min(_MAX_SHOTS, explicit))
        if len(shots) > explicit:
            shots = shots[:explicit]
        _assign_heuristic_setting_ids(shots, scenes)
    else:
        shots = [
            _heuristic_lean_shot(
                prompt, characters, story_name=story_name, setting_id=primary_setting
            )
        ]
        n_chars = len(characters)
        decisions = {
            "target_shot_count": 1,
            "cast_layout": "combined" if n_chars > 1 else "single",
            "prefer_combined_cast": n_chars > 1,
            "prefer_split_cast": False,
        }

    payload = {
        "schema_version": "designer-script-analysis.v1",
        "source": "heuristic",
        "user_prompt": prompt,
        "story_name": story_name,
        "characters": characters,
        "scenes": scenes,
        "shots": shots,
        "style_lock": default_style_lock(prompt),
        "audio": audio,
        "scene_continuity_mode": "scene_card_plus_clip_shots",
        "summary": (
            f"{len(characters)} characters, {len(scenes)} scenes, {len(shots)} shots, "
            f"cast={decisions['cast_layout']}, lean={explicit < 2}, scene_cards=on"
        ),
        **decisions,
    }
    return ensure_audio_locks_on_analysis(payload, prompt)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object from model output.

    Tolerates markdown fences, leading prose, and trailing chatter so slight
    messiness does not force a heuristic fallback.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    elif raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```\s*$", "", raw).strip()

    def _as_dict(value: Any) -> dict[str, Any] | None:
        return value if isinstance(value, dict) else None

    try:
        return _as_dict(json.loads(raw))
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start < 0:
        return None

    try:
        data, _end = json.JSONDecoder().raw_decode(raw[start:])
        return _as_dict(data)
    except json.JSONDecodeError:
        pass

    # Brace-balanced fallback when raw_decode fails on lightly broken JSON tails.
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = raw[start : i + 1]
                try:
                    return _as_dict(json.loads(chunk))
                except json.JSONDecodeError:
                    return None
    return None


def _prompt_mentions_duration(prompt: str) -> tuple[bool, int | None]:
    """Detect duration cues; return (has_explicit_runtime, target_duration_sec)."""
    from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
        requested_film_duration_sec,
    )

    dur = requested_film_duration_sec(prompt)
    if dur is not None:
        return True, dur
    low = (prompt or "").lower()
    if re.search(r"\b(short|one[- ]shot|single[- ]shot|movie clip|6s)\b", low):
        return True, 6
    return False, None


_REFERENCE_SUBJECTS = {
    "character": "character",
    "person": "character",
    "human": "character",
    "portrait": "character",
    "people": "character",
    "人物": "character",
    "角色": "character",
    "人像": "character",
    "scene": "scene",
    "place": "scene",
    "location": "scene",
    "environment": "scene",
    "background": "scene",
    "setting": "scene",
    "场景": "scene",
    "地点": "scene",
    "环境": "scene",
    "object": "object",
    "product": "object",
    "prop": "object",
    "item": "object",
    "goods": "object",
    "sku": "object",
    "产品": "object",
    "物品": "object",
    "道具": "object",
    "商品": "object",
}


def _reference_subject(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _REFERENCE_SUBJECTS:
        return _REFERENCE_SUBJECTS[text]
    for token, subject in _REFERENCE_SUBJECTS.items():
        if token and token in text:
            return subject
    return ""


def _reference_reads(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only image routes the model actually assigned after seeing the files."""
    raw = parsed.get("reference_reads")
    if not isinstance(raw, list):
        raw = parsed.get("reads")
    if not isinstance(raw, list):
        return []
    reads: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        subject = _reference_subject(item.get("subject") or item.get("kind") or item.get("type"))
        from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
            absorb_reference_read,
        )

        read = absorb_reference_read(item, index, subject)
        if read is None:
            continue
        reads.append(read)
    return reads


def _normalize_llm_analysis(
    parsed: dict[str, Any],
    base: dict[str, Any],
    *,
    allow_empty_cast: bool = False,
) -> dict[str, Any] | None:
    characters = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
    scenes = parsed.get("scenes") if isinstance(parsed.get("scenes"), list) else []
    shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
    if allow_empty_cast and len(shots) < 1:
        shots = [
            {
                "shot_index": 1,
                "action": str(base.get("user_prompt") or "")[:500],
                "setting_id": "set_1",
            }
        ]
    if (not allow_empty_cast and len(characters) < 1) or len(shots) < 1:
        return None
    norm_chars: list[dict[str, Any]] = []
    for i, ch in enumerate(characters[:_MAX_CHARS], start=1):
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name") or f"Character {i}")
        desc = str(ch.get("description") or ch.get("name") or "")
        entry: dict[str, Any] = {
            "id": str(ch.get("id") or f"char_{i}"),
            "name": name,
            "description": desc,
            "match_terms": _match_terms_for_character(name, desc),
        }
        norm_chars.append(entry)
    if not norm_chars and not allow_empty_cast:
        return None
    valid_ids, by_name = _cast_id_maps(norm_chars)
    norm_scenes: list[dict[str, str]] = []
    for i, sc in enumerate(scenes[:_MAX_SCENES], start=1):
        if not isinstance(sc, dict):
            continue
        norm_scenes.append(
            {
                "id": str(sc.get("id") or f"scene_{i}"),
                "name": str(sc.get("name") or f"Scene {i}"),
                "description": str(sc.get("description") or ""),
            }
        )
    if not norm_scenes:
        # Derive placeholder scenes from shot setting_ids after the shot loop if needed.
        norm_scenes = []
    norm_shots: list[dict[str, Any]] = []
    for i, sh in enumerate(shots[:_MAX_SHOTS], start=1):
        if not isinstance(sh, dict):
            continue
        cids = resolve_cast_token_list(
            sh.get("character_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        )
        # Fail closed: never invent round-robin cast when the shot omitted ids.
        ensemble = resolve_cast_token_list(
            sh.get("ensemble_cast_ids") or sh.get("character_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        ) or list(cids)
        featured = resolve_cast_token_list(
            sh.get("featured_cast_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        ) or list(cids[:1])
        exiting = resolve_cast_token_list(
            sh.get("exiting_character_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        )
        on_screen = resolve_cast_token_list(
            sh.get("on_screen")
            or sh.get("visible_cast_ids")
            or sh.get("featured_cast_ids")
            or [],
            valid_ids=valid_ids,
            by_name=by_name,
        )
        # Fail closed: do not promote full character_ids / ensemble when on_screen absent.
        if not on_screen:
            on_screen = resolve_cast_token_list(
                (sh.get("character_ids") or [])[:1],
                valid_ids=valid_ids,
                by_name=by_name,
            )
        offscreen = [
            cid
            for cid in resolve_cast_token_list(
                sh.get("offscreen") or sh.get("off_screen_cast_ids") or [],
                valid_ids=valid_ids,
                by_name=by_name,
            )
            if cid not in on_screen
        ]
        cast_actions: dict[str, str] = {}
        raw_actions = sh.get("cast_actions") or sh.get("doing")
        if isinstance(raw_actions, dict):
            for k, v in raw_actions.items():
                cid = resolve_cast_token(k, valid_ids=valid_ids, by_name=by_name)
                if cid and str(v or "").strip():
                    cast_actions[cid] = str(v).strip()[:240]
        strategy = str(sh.get("keyframe_strategy") or "").strip()
        setting_id = str(sh.get("setting_id") or sh.get("scene_id") or f"set_{i}").strip()
        # Prefer on_screen as the drawn cast for this shot.
        cids = list(on_screen) or cids
        entry: dict[str, Any] = {
            "shot_index": i,
            "title": str(sh.get("title") or ""),
            "action": str(sh.get("action") or "")[:500],
            "camera": str(sh.get("camera") or "medium / eye-level"),
            "character_ids": cids,
            "on_screen": on_screen or list(cids),
            "visible_cast_ids": on_screen or list(cids),
            "offscreen": offscreen,
            "off_screen_cast_ids": offscreen,
            "ensemble_cast_ids": ensemble,
            "featured_cast_ids": featured,
            "exiting_character_ids": exiting,
            "setting_id": setting_id or f"set_{i}",
            "keyframe_prompt": str(sh.get("keyframe_prompt") or sh.get("action") or "")[:600],
            "timeline": str(sh.get("timeline") or f"{(i - 1) * 2:.1f}-{i * 2:.1f}s"),
        }
        if cast_actions:
            entry["cast_actions"] = cast_actions
        from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
            emotion_label,
        )

        labeled = emotion_label(sh.get("emotion") or sh.get("beat"))
        if labeled:
            entry["emotion"] = labeled
        irreversible = str(sh.get("irreversible") or "").strip()
        if irreversible:
            entry["irreversible"] = irreversible[:160]
        if isinstance(sh.get("cast_states"), dict) and sh.get("cast_states"):
            entry["cast_states"] = sh["cast_states"]
        if strategy in {"compose_from_solo_refs", "edit_prior_keyframe"}:
            entry["keyframe_strategy"] = strategy
        from jiuwenswarm.server.runtime.designer.node_labels import derive_shot_name

        entry["title"] = derive_shot_name(entry, fallback_index=i)
        norm_shots.append(entry)
    if not norm_scenes and norm_shots:
        seen: dict[str, str] = {}
        for sh in norm_shots:
            sid = str(sh.get("setting_id") or "").strip() or "set_1"
            if sid not in seen:
                seen[sid] = sid
        norm_scenes = [
            {"id": sid, "name": sid, "description": ""} for sid in seen
        ]
    if not norm_shots:
        return None
    audio = parsed.get("audio") if isinstance(parsed.get("audio"), dict) else base.get("audio")
    try:
        tsc = int(parsed.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        tsc = 0
    from jiuwenswarm.server.runtime.designer.node_labels import derive_story_name

    story_name = derive_story_name(
        analysis={
            "story_name": parsed.get("story_name") or parsed.get("film_title") or parsed.get("title"),
            "title": parsed.get("title"),
        },
        prompt=str(base.get("user_prompt") or base.get("summary") or ""),
        graph_title=str(parsed.get("story_name") or ""),
    )
    user_prompt = str(base.get("user_prompt") or base.get("summary") or "")
    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
        STYLE_LOCK_DEFAULT,
        default_style_lock,
    )

    # Explicit user language is authoritative. Otherwise accept the Director's
    # inferred style, and finally use the product's cartoonish default.
    user_style = default_style_lock(user_prompt)
    raw_style = parsed.get("style_lock")
    visual_style = str(
        parsed.get("visual_style")
        or parsed.get("style")
        or (raw_style if isinstance(raw_style, str) else "")
        or ""
    ).strip()
    if user_style.get("look") != STYLE_LOCK_DEFAULT.get("look"):
        style_lock = user_style
    elif isinstance(raw_style, dict) and any(str(v or "").strip() for v in raw_style.values()):
        style_lock = {
            str(k): str(v)[:280]
            for k, v in raw_style.items()
            if str(v or "").strip()
        }
    elif visual_style:
        style_lock = default_style_lock(visual_style)
        if style_lock.get("look") == STYLE_LOCK_DEFAULT.get("look"):
            style_lock["look"] = visual_style[:280]
            style_lock["medium"] = "brief_specified"
    else:
        style_lock = user_style
    decisions = _director_pipeline_decisions(user_prompt, norm_chars, norm_shots)
    heuristic_budget = int(decisions["target_shot_count"])
    explicit = 0
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        explicit = int(_explicit_shot_count_from_prompt(user_prompt) or 0)
    except Exception:  # noqa: BLE001
        explicit = 0
    # LLM owns N via shots[] / target_shot_count. Explicit user N-shot is a hard ceiling.
    # Soft safety only: never exceed _MAX_SHOTS. Heuristic budget is fallback when LLM omits N.
    if explicit >= 1:
        decisions["target_shot_count"] = max(1, min(_MAX_SHOTS, explicit))
    elif 1 <= tsc <= _MAX_SHOTS:
        decisions["target_shot_count"] = tsc
    elif norm_shots:
        decisions["target_shot_count"] = max(1, min(_MAX_SHOTS, len(norm_shots)))
    else:
        decisions["target_shot_count"] = heuristic_budget
    # Honor explicit LLM layout only when it matches co-appearance reality.
    raw_layout = str(parsed.get("cast_layout") or "").strip().lower()
    if raw_layout in {"single", "split", "combined", "hybrid"}:
        multi = any(len(s.get("character_ids") or []) >= 2 for s in norm_shots)
        if raw_layout == "combined" and multi:
            decisions["cast_layout"] = "combined"
            decisions["prefer_combined_cast"] = True
            decisions["prefer_split_cast"] = False
        elif raw_layout == "hybrid" and multi:
            decisions["cast_layout"] = "hybrid"
            decisions["prefer_combined_cast"] = True
            decisions["prefer_split_cast"] = True
        elif raw_layout == "split" and len(norm_chars) > 1 and not multi:
            decisions["cast_layout"] = "split"
            decisions["prefer_combined_cast"] = False
            decisions["prefer_split_cast"] = True
        elif raw_layout == "single" and len(norm_chars) <= 1:
            decisions["cast_layout"] = "single"
            decisions["prefer_combined_cast"] = False
            decisions["prefer_split_cast"] = False
    # Prefer coverage-preserving selection over naive first-N truncate.
    ceiling = max(1, min(int(decisions["target_shot_count"]), _MAX_SHOTS))
    if explicit < 1 and len(norm_shots) > ceiling and 1 <= tsc <= _MAX_SHOTS:
        # shots[] longer than declared target_shot_count → trust the longer list (soft max).
        ceiling = max(1, min(len(norm_shots), _MAX_SHOTS))
    decisions["target_shot_count"] = ceiling
    norm_shots = _select_shots_for_budget(norm_shots, ceiling, norm_chars)
    layout_decisions = _director_pipeline_decisions(user_prompt, norm_chars, norm_shots)
    for key in ("cast_layout", "prefer_combined_cast", "prefer_split_cast"):
        if key in layout_decisions:
            decisions[key] = layout_decisions[key]
    # Keep LLM-owned N after layout refresh (do not re-clamp to heuristic 2–4).
    if explicit >= 1:
        decisions["target_shot_count"] = max(1, min(explicit, _MAX_SHOTS))
    else:
        decisions["target_shot_count"] = max(1, min(len(norm_shots) or ceiling, _MAX_SHOTS))
    if len(norm_shots) > int(decisions["target_shot_count"]):
        norm_shots = norm_shots[: int(decisions["target_shot_count"])]
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
        ensure_shot_start_end_states,
    )

    norm_shots = ensure_shot_start_end_states(norm_shots)
    out: dict[str, Any] = {
        "schema_version": "designer-script-analysis.v1",
        "source": "llm",
        "story_name": story_name,
        "characters": norm_chars,
        "scenes": norm_scenes,
        "shots": norm_shots,
        "style_lock": style_lock,
        "audio": audio,
        "scene_continuity_mode": "scene_card_plus_clip_shots",
        "summary": str(parsed.get("summary") or "")[:500]
        or f"{len(norm_chars)} characters, {len(norm_shots)} shots",
        **decisions,
    }
    reads = _reference_reads(parsed)
    if reads:
        out["reference_reads"] = reads
    try:
        tds = int(parsed.get("target_duration_sec") or 0)
    except (TypeError, ValueError):
        tds = 0
    if 1 <= tds <= 30:
        out["target_duration_sec"] = tds
    return out


async def analyze_creative_brief(
    prompt: str,
    *,
    timeout_sec: float = _DEFAULT_LLM_TIMEOUT_SEC,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """LLM cast/shot analysis. Designer requires a configured chat model."""
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        LLM_API_ERROR,
        call_model_tool,
        model_text_or_raise,
    )

    # Minimal merge context — never invent cast/shots via heuristic_analysis.
    base = {
        "user_prompt": prompt,
        "summary": (prompt or "")[:500],
        "scenes": [],
        "audio": {},
    }

    short_clip, target_duration_sec = _prompt_mentions_duration(prompt)
    duration_sec = target_duration_sec or 6
    try:

        story_enrichment_rule = (
            "Treat explicit user facts and constraints as authoritative. When the request is "
            "sparse (for example only a topic, format, and duration), creatively develop the "
            "unspecified content into a coherent video concept instead of repeating the premise. "
            "Give the film a clear progression and payoff: for narrative/celebration content use "
            "setup → development/turn → climax or emotional payoff; for advertising use "
            "hook → desire/problem → product demonstration or proof → payoff/CTA. "
            "Create enough distinct visual shots to earn the requested runtime; every shot must "
            "advance the idea, reveal new information, or change the emotional state. No filler, "
            "duplicate actions, or same-moment camera coverage presented as new content. "
            "Use one emotion curve and exactly one climax. Each shot needs emotion "
            "(setup, rise, climax, or release) and irreversible (what is newly true at the end). "
            "The next shot starts from the previous end. cast_states may change wardrobe or "
            "emotion for that shot; face identity stays the character description. "
        )
        shot_count_rule = (
            "Shots are consecutive TIME windows that concatenate to the film. "
            "Each shot.action describes ONLY that window — do not paste the user prompt "
            "into actions and do not restage the whole story from a new camera "
            "(angle coverage only if the user asked for multi-cam / same-moment angles). "
            "New setting_id / hard cut / wardrobe / on-screen cast change → new shot. "
            "Qwen KF: lock identity+wardrobe; first setting KF = compose_from_solo_refs, "
            "later same setting = edit_prior_keyframe; prefer ≤2–3 people with refs. "
            "Clip prompt = this shot's motion and camera only. "
            "Explicit user N-shot / N分镜 is a HARD ceiling (hard max 16). "
        )
        if target_duration_sec:
            duration_rule = (
                f"Film ~{duration_sec}s total: set target_duration_sec={duration_sec}. "
                "Each clip covers only its own advancing story window and may be as long as that window."
            )
        else:
            duration_rule = ""
        # Compact schema — long prompts make deepseek-flash return prose/empty.
        system = (
            "You are the Designer Director. Domain-agnostic. "
            + story_enrichment_rule
            + "Do not alter explicit people, places, brand facts, claims, or requested events. "
            "Extract EVERY named human into characters[]. Anonymous crowd is not a character. "
            "Each character description MUST lock wardrobe garments: shirt/top style+color, "
            "trousers/skirt/bottom style+color, footwear, outerwear/accessories if any "
            "(example: 'light blue short-sleeve shirt; dark charcoal trousers; black sneakers'). "
            "Per shot also lock staging: cast_actions (posture/doing), blocking positions "
            "(zone/facing), who looks_at whom, who talks_to whom, adjacency (next_to). "
            "Shots grouped by setting_id (different places = different setting_id). "
            "Infer one film-wide visual style from the user's exact wording. Preserve "
            "non-English style cues verbatim. If no style can be inferred, use cartoonish "
            "animation with flat shapes, soft rendering, and rounded forms. "
            "NOT every character in every scene. Per shot: on_screen (visible), offscreen "
            "(in scene, not in frame), cast_actions {id: doing-what}. "
            + shot_count_rule
            + duration_rule
            + (
                "Attached images are image 1, image 2, ... in that order. Look at each one. "
                "reference_reads.subject is character when the image is a person who still "
                "needs a character-sheet pass (set character_id to that characters[].id); "
                "scene when it is a place (set setting_id to the matching shot setting_id); "
                "object when it is a prop or product that must appear inside the clips. "
                if reference_images
                else ""
            )
            + " Output ONLY one JSON object (no markdown). "
            '{"story_name":"short film title any language",'
            '"style_lock":{"look":"...","medium":"..."},'
            '"characters":[{"id":"char_1","name":"...","description":"..."}],'
            '"shots":[{"shot_index":1,"title":"2-4 word beat name NEVER Shot N",'
            '"action":"...","camera":"...","on_screen":["char_1"],'
            '"offscreen":[],"cast_actions":{"char_1":"..."},"featured_cast_ids":["char_1"],'
            '"ensemble_cast_ids":["char_1"],"setting_id":"set_1","keyframe_prompt":"...","timeline":"0-5s",'
            '"emotion":"setup","irreversible":"what is newly true at the end",'
            '"cast_states":{"char_1":{"wardrobe":"","emotion":"","presence":"on_screen"}}}],'
            '"target_shot_count":N'
            + (f',"target_duration_sec":{duration_sec}' if target_duration_sec else "")
            + (
                ',"reference_reads":[{"slot":1,"subject":"character",'
                '"character_id":"char_1","setting_id":""}]'
                if reference_images
                else ""
            )
            + "}"
        )

        async def _call(*, reinforce_json: bool = False) -> dict[str, Any] | None:
            """Return normalized LLM analysis, or None on soft failure (caller retries)."""
            sys_msg = system
            payload: dict[str, Any] = {
                "user_prompt": prompt[:3000] if not reinforce_json else prompt[:2000],
                "instructions": "JSON only. Every named human in characters[].",
            }
            if reinforce_json:
                sys_msg = (
                    "Output ONLY one JSON object starting with '{'. "
                    + story_enrichment_rule
                    + "The shots must be distinct, sequential content shots that fill the "
                    "requested duration without repetition. "
                    '{"style_lock":{"look":"...","medium":"..."},'
                    '"characters":[{"id":"char_1","name":"...","description":"..."}],'
                    '"shots":[{"shot_index":1,"action":"...","camera":"...",'
                    '"character_ids":["char_1"],"ensemble_cast_ids":["char_1"],'
                    '"featured_cast_ids":["char_1"],"setting_id":"set_1",'
                    '"keyframe_prompt":"...","timeline":"0-5s"}]}'
                )
                payload["retry"] = True
            result = await call_model_tool(
                prompt=json.dumps(payload, ensure_ascii=False),
                system=sys_msg,
                optimize_for="quality",
                max_tokens=32768,
                images=list(reference_images or []) or None,
            )
            if result.get("unavailable") or result.get("ok") is False or result.get("fallback"):
                raise DesignerLlmError.from_call_result(result)
            text = model_text_or_raise(result)
            parsed = _extract_json_object(text)
            if not parsed:
                logger.info(
                    "LLM script analysis returned non-JSON (len=%s); soft-fail",
                    len(text),
                )
                return None
            chars = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
            placeholder = False
            for ch in chars:
                if not isinstance(ch, dict):
                    continue
                name = str(ch.get("name") or "").strip()
                if name in {"", "...", "…", "string", "name"}:
                    placeholder = True
                    break
            allow_empty_cast = bool(reference_images)
            if allow_empty_cast and (
                not isinstance(parsed.get("shots"), list) or not parsed.get("shots")
            ):
                parsed["shots"] = [
                    {
                        "shot_index": 1,
                        "action": (prompt or "")[:500],
                        "setting_id": "set_1",
                    }
                ]
            if placeholder or (not chars and not allow_empty_cast):
                logger.info("LLM script analysis looked like schema echo; soft-fail")
                return None
            if short_clip:
                parsed.setdefault("target_duration_sec", duration_sec)
            normalized = _normalize_llm_analysis(
                parsed, base, allow_empty_cast=allow_empty_cast
            )
            if not normalized:
                return None
            if short_clip and normalized.get("shots"):
                shots_n = list(normalized["shots"])
                n = max(1, len(shots_n))
                for i, shot in enumerate(shots_n, start=1):
                    shot["shot_index"] = i
                    if not str(shot.get("timeline") or "").strip():
                        half = float(duration_sec) / n
                        shot["timeline"] = f"{(i - 1) * half:.1f}-{i * half:.1f}s"
                normalized["shots"] = shots_n
                normalized["target_duration_sec"] = duration_sec
                normalized["target_shot_count"] = len(shots_n)
            normalized["source"] = "llm"
            return normalized

        # Prefer LLM; one reinforce if first reply empty/non-JSON (max 2 attempts).
        # Billing / credential failures raise immediately (no second attempt).
        first = await asyncio.wait_for(_call(), timeout=max(3.0, float(timeout_sec)))
        if isinstance(first, dict) and first.get("source") == "llm":
            return first
        remaining = max(8.0, float(timeout_sec) * 0.4)
        second = await asyncio.wait_for(_call(reinforce_json=True), timeout=remaining)
        if isinstance(second, dict) and second.get("source") == "llm":
            return second
        raise DesignerLlmError(
            "Chat model did not return a usable cast/shot analysis.",
            code=LLM_API_ERROR,
        )
    except DesignerLlmError:
        raise
    except asyncio.TimeoutError as exc:
        logger.info("LLM script analysis timed out after %.1fs", timeout_sec)
        raise DesignerLlmError(
            f"Chat model timed out after {timeout_sec:.0f}s during script analysis.",
            code=LLM_API_ERROR,
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.info("LLM script analysis failed: %s", exc)
        raise DesignerLlmError(
            f"Chat model request failed during script analysis: {exc}",
            code=LLM_API_ERROR,
        ) from exc


def analyze_creative_brief_sync(
    prompt: str,
    *,
    timeout_sec: float = _DEFAULT_LLM_TIMEOUT_SEC,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """Sync wrapper — always uses a dedicated event loop (never skips LLM on nest)."""

    def _run() -> dict[str, Any]:
        return asyncio.run(
            analyze_creative_brief(
                prompt,
                timeout_sec=timeout_sec,
                reference_images=reference_images,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    # Already on a loop: run in a worker thread with its own loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_run)
        return fut.result(timeout=max(30.0, float(timeout_sec) + 30.0))
