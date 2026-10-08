# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Plan A — generic director contract (no domain hardcodes).

Enriches script analysis with:
  - beat budget from explicit shot count / timelines / duration
  - per-shot on_screen / exiting / staying / relation
  - blocking zones (left|center|right|foreground|background)
  - spatial_lock landmarks derived from the brief
  - exclusivity pairs (stay vs leave) from verbs — not named roles

Safe to call from browser later: set experiment_plan=\"A\" only when enabled.
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

_EXIT_RE = re.compile(
    r"\b(?:leave|leaves|leaving|exit|exits|exiting|walk(?:s|ing)?\s+away|"
    r"gets?\s+up|stands?\s+up|walks?\s+out|pushes?\s+(?:his|her|their)\s+chair|"
    r"storms?\s+out|departs?)\b",
    re.I,
)
# Anticipatory / schedule language — not an on-screen exit this shot.
_FALSE_EXIT_RE = re.compile(
    r"\b(?:"
    r"(?:just\s+)?before\s+(?:leaving|exiting|departing)|"
    r"about\s+to\s+(?:leave|exit|depart)|"
    r"ready\s+to\s+(?:leave|exit|go\s+home)|"
    r"get(?:ting)?\s+off\s+work|off\s+work|leaving\s+work|"
    r"end\s+of\s+(?:the\s+)?(?:day|shift)|clock(?:ing)?\s+out|"
    r"on\s+(?:his|her|their)\s+way\s+(?:out|home)"
    r")\b",
    re.I,
)


def _action_has_exit(action: str) -> bool:
    """True only for real on-screen exits (not anticipatory 'before leaving' language)."""
    text = _FALSE_EXIT_RE.sub(" ", action or "")
    return bool(_EXIT_RE.search(text))

_STAY_SPEAK_RE = re.compile(
    r"\b(?:still\s+(?:speak|preach|read|talk)|continues?\s+(?:speak|preach|read|talk)|"
    r"remains?\s+(?:at|behind)|keeps?\s+(?:speak|read))\b",
    re.I,
)
_HARD_CUT_RE = re.compile(
    r"\b(?:pan(?:s|ning)?\s+to|cut(?:s)?\s+to|then\s+the\s+camera|close-?up\s+on|"
    r"drift(?:s|ing)?\s+to|feature(?:s)?\s+the|flash\s+of|flashback)\b",
    re.I,
)
_DURATION_RE = re.compile(
    r"(?P<n>\d+)\s*(?:-|–|to)\s*(?P<m>\d+)\s*seconds?|"
    r"(?P<a>\d+)\s*-?\s*second|\b(?P<b>\d+)s\b",
    re.I,
)

# One shot == one keyframe + one clip in this pipeline, so "N 个关键帧" is the
# same contract as "N 个分镜".
_SHOT_UNIT_CN = r"(?:分镜|镜头|关键帧|帧|幕)"
_THREE_SHOT_RE = re.compile(
    r"\bthree[- ]shot\b|\b3[- ]shot\b|\bthree\s+shots?\b|"
    rf"三分镜|三个?{_SHOT_UNIT_CN}|3\s*个?{_SHOT_UNIT_CN}",
    re.I,
)
_FOUR_SHOT_RE = re.compile(
    r"\bfour[- ]shot\b|\b4[- ]shot\b|\bfour\s+shots?\b|"
    rf"四分镜|四个?{_SHOT_UNIT_CN}|4\s*个?{_SHOT_UNIT_CN}",
    re.I,
)
_N_SHOT_CN_RE = re.compile(
    rf"(?P<n>[二三四五六七八九十两\d]+)\s*个?{_SHOT_UNIT_CN}", re.I
)
_CN_NUM = {
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_TIMELINE_BEAT_RE = re.compile(
    r"(?:0:)?(\d{1,2}):(\d{2})\s*[-–—]\s*(?:0:)?(\d{1,2}):(\d{2})",
    re.M,
)
# Beat separators, CJK + latin. A cue means "the story moves on", i.e. one more beat.
_BEAT_CUE_RE = re.compile(
    r"然后|接着|紧接着|随后|其后|之后|最后|最终|起初|首先|其次|突然|忽然|"
    r"画面(?:切换|切至|切到|转向|转为)|镜头(?:切换|切至|切到|转向|拉近|推近|拉远|摇向)|"
    r"下一(?:幕|镜|个镜头)|"
    r"\band then\b|\bafter that\b|\bfinally\b|\bmeanwhile\b|\bcut(?:s)?\s+to\b|"
    r"\bpan(?:s)?\s+to\b|\bnext\s+(?:shot|scene|beat)\b",
    re.I,
)
_SHOT_LABEL_RE = re.compile(
    r"(?:^|[\n,，。；;])\s*(?:镜头|分镜|shot)\s*[#no.：:]*\s*(?P<n>\d{1,2})\b",
    re.I | re.M,
)


def _cid_list(raw: Any, valid: set[str], by_name: dict[str, str] | None = None) -> list[str]:
    """Accept character ids or display names; map names → ids when by_name provided."""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    name_map = by_name or {}
    for x in raw:
        tok = str(x or "").strip()
        if not tok:
            continue
        cid = tok if tok in valid else name_map.get(tok.lower(), "")
        if not cid:
            # Loose: char_N embedded in a longer token
            m = re.search(r"(char_\d+)", tok, flags=re.I)
            if m and m.group(1) in valid:
                cid = m.group(1)
        if cid and cid in valid and cid not in out:
            out.append(cid)
    return out


def _explicit_shot_count_from_prompt(prompt: str) -> int:
    """Honor explicit N-shot / N分镜 language only (language-agnostic count, not plot rules)."""
    text = prompt or ""
    if _THREE_SHOT_RE.search(text):
        return 3
    if _FOUR_SHOT_RE.search(text):
        return 4
    m = _N_SHOT_CN_RE.search(text)
    if m:
        raw = m.group("n")
        if raw.isdigit():
            return max(1, int(raw))
        if raw in _CN_NUM:
            return max(1, _CN_NUM[raw])
    m2 = re.search(
        r"\b(?P<n>\d+)\s*[- ]?(?:shot|shots|beat|beats|keyframe|keyframes|key\s+frames?)\b",
        text,
        re.I,
    )
    if m2:
        return max(1, int(m2.group("n")))
    return 0


def is_placeholder_analysis(analysis: dict[str, Any] | None) -> bool:
    """True when analysis is not yet LLM-authored (empty source / test fixtures)."""
    data = analysis or {}
    if data.get("llm_pending"):
        return True
    return str(data.get("source") or "") in {"", "heuristic"}


def count_narrative_beats(prompt: str) -> int:
    """Estimate how many beats the prompt itself describes (>=1, language-agnostic)."""
    text = prompt or ""
    labels = [int(m.group("n")) for m in _SHOT_LABEL_RE.finditer(text)]
    sentences = [
        part
        for part in re.split(r"[。！？；!?;]|(?<=[.!?])\s+", text)
        if len(part.strip()) > 8
    ]
    candidates = [
        max(labels) if labels else 1,
        len(_TIMELINE_BEAT_RE.findall(text)) or 1,
        len(_BEAT_CUE_RE.findall(text)) + 1,
        len(sentences) or 1,
    ]
    return max(1, max(candidates))


def infer_shot_budget(prompt: str, analysis: dict[str, Any]) -> int:
    """Domain-agnostic shot budget. Explicit N wins; C slices when runtime > Wan max."""
    from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
        WAN_MAX_CLIP_SEC,
        needs_duration_slicing,
        requested_film_duration_sec,
        sequential_shot_count,
    )

    explicit = _explicit_shot_count_from_prompt(prompt)
    if needs_duration_slicing(prompt):
        asked = int(requested_film_duration_sec(prompt) or 0)
        n_c = sequential_shot_count(asked)
        if explicit >= 1 and explicit * WAN_MAX_CLIP_SEC >= asked:
            return max(1, explicit)
        return max(1, max(n_c, explicit))
    if explicit >= 1:
        return max(1, explicit)
    try:
        target = int(analysis.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        target = 0
    n_shots = len(analysis.get("shots") or [])
    authored = max(target, n_shots)
    # A named director style may forbid a single take (final_frame_reverse).
    style_floor = 0
    try:
        from jiuwenswarm.server.runtime.designer.video_styles import (
            detect_video_style,
            video_style_min_shots,
        )

        style_floor = int(video_style_min_shots(detect_video_style(prompt or "")) or 0)
    except Exception:  # noqa: BLE001
        style_floor = 0
    # A no-LLM skeleton is a floor, never a ceiling — otherwise a failed opening
    # LLM call pins the whole film to its 1-shot placeholder.
    placeholder = is_placeholder_analysis(analysis)
    if authored >= 1 and not placeholder:
        # LLM / prior analysis owns N — do not clamp it to a fixed shot count.
        return max(1, max(authored, style_floor))
    floor = max(authored if placeholder else 0, style_floor)
    beats = _TIMELINE_BEAT_RE.findall(prompt or "")
    if beats:
        return max(1, max(len(beats), floor))
    cues = count_narrative_beats(prompt)
    if cues >= 2:
        return max(1, max(cues, floor))
    dur = None
    m = _DURATION_RE.search(prompt or "")
    if m:
        if m.group("n") and m.group("m"):
            dur = (int(m.group("n")) + int(m.group("m"))) / 2.0
        else:
            dur = float(m.group("a") or m.group("b") or 0)
    if dur and dur > 0:
        # ~7–8s per cinematic beat when no LLM plan yet.
        per_beat = int(round(dur / 8.0)) or 1
        return max(1, max(per_beat, floor))
    return max(1, floor or 1)


def _char_blob(ch: dict[str, Any]) -> str:
    return " ".join(
        str(ch.get(k) or "")
        for k in ("name", "role", "description")
    ).lower()


def _score_in_text(ch: dict[str, Any], text: str) -> int:
    blob = _char_blob(ch)
    t = (text or "").lower()
    score = 0
    name = str(ch.get("name") or "").lower().strip()
    if name and name in t:
        score += 6
    for tok in re.findall(r"[a-z]{3,}", blob):
        if tok in t:
            score += 1
    return score


def _pick_exiting(
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
    on_screen: list[str],
) -> list[str]:
    action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
    if not _action_has_exit(action):
        return []
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    # Prefer characters whose name/role implies leaving / teen / front-row / son
    exit_hints = ("leav", "exit", "depart", "son", "teen", "front", "visitor", "partner")
    stay_hints = ("father", "preach", "pastor", "speaker", "reader", "host")
    ranked: list[tuple[int, str]] = []
    for cid in on_screen:
        ch = by_id.get(cid) or {}
        blob = _char_blob(ch)
        sc = 0
        if any(h in blob for h in exit_hints):
            sc += 5
        if any(h in blob for h in stay_hints) and _STAY_SPEAK_RE.search(action):
            sc -= 8
        sc += _score_in_text(ch, action)
        ranked.append((sc, cid))
    ranked.sort(reverse=True)
    if not ranked:
        return []
    best_sc, best_cid = ranked[0]
    if best_sc < 0:
        return []
    # If stay-speaker language, exclude the highest stay-hint character
    leavers = [best_cid]
    return leavers


def _blocking_for_shot(
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
    on_screen: list[str],
) -> dict[str, Any]:
    action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
    camera = str(shot.get("camera") or "").lower()
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    positions: list[dict[str, str]] = []
    zones = ("center", "left", "right", "foreground", "background")
    for i, cid in enumerate(on_screen):
        ch = by_id.get(cid) or {}
        name = str(ch.get("name") or cid)
        zone = zones[min(i, len(zones) - 1)]
        if "close" in camera and i == 0:
            zone = "center"
        if _action_has_exit(action) and cid in (shot.get("exiting_character_ids") or []):
            zone = "right" if "left" in action.lower() else "left"
            pose = "standing_and_exiting_frame"
            facing = "away_from_primary_speaker_or_camera_subject"
        else:
            pose = "engaged_in_beat"
            facing = "toward_camera_or_scene_focus"
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
                    infer_pose_from_action,
                )

                pose = infer_pose_from_action(action, fallback=pose)
            except Exception:  # noqa: BLE001
                pass
        positions.append(
            {
                "character_id": cid,
                "name": name,
                "zone": zone,
                "pose": pose,
                "facing": facing,
            }
        )
    landmark = ""
    for key in ("table", "pulpit", "desk", "window", "aisle", "pew", "door", "counter", "phone"):
        if key in action.lower() or key in str(shot.get("keyframe_prompt") or "").lower():
            landmark = key
            break
    return {
        "landmark": landmark or "primary_set_landmark",
        "positions": positions,
        "rule": (
            "Place each listed character in their zone relative to the landmark. "
            "Honor pose contact with supports (seat/floor/edge) — never merge bodies "
            "into solid furniture or props. "
            "Do not invent extra named leads. Do not clone one face onto two bodies."
        ),
    }


def _spatial_lock_from_prompt(prompt: str, scenes: list[dict[str, Any]]) -> dict[str, Any]:
    scene0 = scenes[0] if scenes else {}
    setting = str(scene0.get("name") or "Primary setting").strip()
    desc = str(scene0.get("description") or "").strip()
    blob = f"{prompt} {setting} {desc}".lower()
    landmarks: list[str] = []
    for key in (
        "table",
        "window",
        "pulpit",
        "pew",
        "aisle",
        "altar",
        "desk",
        "door",
        "kitchen",
        "restaurant",
        "phone",
        "candle",
    ):
        if key in blob:
            landmarks.append(key)
    if not landmarks:
        landmarks = ["primary_furniture", "primary_light_source"]
    return {
        "setting": setting or "Primary setting",
        "architecture": desc[:400]
        or f"one coherent space; landmarks locked: {', '.join(landmarks)}",
        "landmarks": landmarks,
        "static_rule": (
            "STATIC OBJECTS LOCKED: landmarks, furniture layout, and light direction "
            "must match across shots — only camera, framing, and featured action may change."
        ),
        "crowd_rule": (
            "Background extras stay consistent when the brief implies a crowd; "
            "featured cast are distinct people — never clone one face onto two bodies."
        ),
        "light": "match the brief's lighting (golden hour / warm interior / cartoon flat / etc.)",
    }


def _exclusivity_locks(
    characters: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Stay-vs-leave exclusivity from verbs — no named domain roles required."""
    locks: dict[str, Any] = {
        "geography": "same set geography locked unless the brief changes location",
    }
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    stay_ids: list[str] = []
    leave_ids: list[str] = []
    for shot in shots:
        action = str(shot.get("action") or "")
        on_screen = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        exiting = [str(x) for x in (shot.get("exiting_character_ids") or []) if str(x)]
        leave_ids.extend(exiting)
        if _STAY_SPEAK_RE.search(action) or (
            _action_has_exit(action) and len(on_screen) >= 2
        ):
            for cid in on_screen:
                if cid not in exiting:
                    stay_ids.append(cid)
    stay_ids = list(dict.fromkeys(stay_ids))
    leave_ids = list(dict.fromkeys(leave_ids))
    pairs: list[dict[str, Any]] = []
    for sid in stay_ids:
        for lid in leave_ids:
            if sid == lid:
                continue
            sn = str((by_id.get(sid) or {}).get("name") or sid)
            ln = str((by_id.get(lid) or {}).get("name") or lid)
            pairs.append(
                {
                    "stay_id": sid,
                    "leave_id": lid,
                    "rule": f"{sn} must never share a body/face with {ln}",
                }
            )
    if pairs:
        locks["exclusivity_pairs"] = pairs
        locks["stay"] = {
            "ids": stay_ids,
            "must": "remain in place / keep speaking or reading when someone exits",
        }
        locks["leave"] = {
            "ids": leave_ids,
            "must": "only the exiting character(s) leave; never reseat them later",
        }
    # Parent/child-ish adjacency when both appear
    for shot in shots:
        cids = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        if len(cids) < 2:
            continue
        names = [(cid, _char_blob(by_id.get(cid) or {})) for cid in cids]
        adults = [c for c, b in names if any(k in b for k in ("mother", "woman", "father", "parent"))]
        kids = [c for c, b in names if any(k in b for k in ("child", "kid", "boy", "girl", "son", "teen"))]
        if adults and kids and adults[0] != kids[0]:
            locks.setdefault(
                "adjacent_focus",
                {
                    "ids": [adults[0], kids[0]],
                    "must": "keep as distinct people side-by-side when co-featured",
                },
            )
            break
    return locks


def _apply_exit_propagation(shots: list[dict[str, Any]]) -> None:
    exited: list[str] = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        leaving = [str(x) for x in (shot.get("exiting_character_ids") or []) if str(x)]
        cids = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        # Strip already-exited from later featured casts
        if exited:
            cids = [c for c in cids if c not in exited]
            shot["character_ids"] = cids
            shot["on_screen"] = list(cids)
        cont = dict(shot.get("continuity_lock") or {})
        if exited:
            cont["exited_ids"] = ",".join(exited)
            cont["forbid"] = "do not reseat or re-show characters who already left"
        shot["continuity_lock"] = cont
        for cid in leaving:
            if cid not in exited:
                exited.append(cid)


def _ensure_stay_on_leave_beats(
    prompt: str,
    characters: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> None:
    """If a shot has an exit while someone keeps speaking/reading, keep both on screen."""
    blob = (prompt or "").lower()
    needs_dual = bool(_STAY_SPEAK_RE.search(blob) or _action_has_exit(blob))
    if not needs_dual:
        return
    stay_hints = (
        "father",
        "preach",
        "pastor",
        "speaker",
        "reader",
        "host",
        "minister",
        "lead",
    )
    for shot in shots:
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
        if not _action_has_exit(action) and not _action_has_exit(blob):
            continue
        if not (_STAY_SPEAK_RE.search(action) or _STAY_SPEAK_RE.search(blob)):
            # Still useful when prompt implies simultaneous stay+leave
            if "while" not in blob and "still" not in blob:
                continue
        on_screen = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        exiting = [str(x) for x in (shot.get("exiting_character_ids") or []) if str(x)]
        if not exiting:
            exiting = _pick_exiting(shot, characters, on_screen)
            shot["exiting_character_ids"] = exiting
        stay_candidates = []
        for ch in characters:
            cid = str(ch.get("id") or "")
            if not cid or cid in exiting:
                continue
            if any(h in _char_blob(ch) for h in stay_hints):
                stay_candidates.append(cid)
        if not stay_candidates:
            for ch in characters:
                cid = str(ch.get("id") or "")
                if cid and cid not in exiting:
                    stay_candidates.append(cid)
                    break
        for sid in stay_candidates[:1]:
            if sid not in on_screen:
                on_screen.insert(0, sid)
        shot["character_ids"] = list(dict.fromkeys(on_screen))
        shot["on_screen"] = list(shot["character_ids"])
        shot["staying"] = [c for c in shot["character_ids"] if c not in exiting]
        # Prefer continuation so prior geography can carry while exit happens
        if str(shot.get("shot_relation") or "") == "hard_cut" and len(shot["character_ids"]) >= 2:
            shot["shot_relation"] = "continuation"


def _split_prompt_into_beats(prompt: str, budget: int) -> list[str]:
    """Split a narrative prompt into up to ``budget`` beat descriptions."""
    text = re.sub(r"\s+", " ", (prompt or "").strip())
    # Explicit timed beats: 0:00 - 0:07 | Title: desc
    timed = re.findall(
        r"(?:0:)?\d{1,2}:\d{2}\s*[-–—]\s*(?:0:)?\d{1,2}:\d{2}\s*\|\s*([^|]+?)(?=(?:0:)?\d{1,2}:\d{2}|$)",
        text,
    )
    if timed:
        return [t.strip()[:400] for t in timed[:budget] if t.strip()]
    # Prefer the storyboard shot splitter (drops style/logo meta; keeps booking≠dinner).
    try:
        from jiuwenswarm.server.runtime.designer.script_analysis import (
            _is_non_story_beat,
            _split_prompt_beats,
        )

        smart = [
            b for b in _split_prompt_beats(prompt) if b and not _is_non_story_beat(b)
        ]
        if smart:
            if len(smart) >= budget:
                return [s[:400] for s in smart[:budget]]
            while len(smart) < budget:
                smart.append(smart[-1])
            return [s[:400] for s in smart[:budget]]
    except Exception:  # noqa: BLE001
        pass
    # Sentence / clause split fallback — take first N story clauses (not evenly spaced skips).
    parts = re.split(r"(?<=[.!;])\s+|\s+[—–-]\s+|\band then\b|\bthen\b", text, flags=re.I)
    parts = [p.strip() for p in parts if len(p.strip()) > 40]
    if not parts:
        return [text[:400]] * max(1, budget)
    return [p[:400] for p in parts[:budget]] + (
        [parts[-1][:400]] * max(0, budget - len(parts)) if len(parts) < budget else []
    )


def _expand_shots_to_budget(
    prompt: str,
    shots: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    budget: int,
) -> list[dict[str, Any]]:
    if budget < 1:
        return shots
    beats = _split_prompt_into_beats(prompt, budget)
    # Always align actions to story beats when expanding OR when count already matches
    # but actions are meta/wrong (keeps three-shot fidelity).
    valid = {str(c.get("id")) for c in characters if c.get("id")}
    out: list[dict[str, Any]] = []
    template = dict(shots[0]) if shots else {}
    for i in range(budget):
        if i < len(shots):
            shot = dict(shots[i])
        else:
            shot = dict(template)
            shot.pop("blocking", None)
            shot.pop("exiting_character_ids", None)
            shot.pop("exiting", None)
        action = beats[i] if i < len(beats) else str(shot.get("action") or (beats[-1] if beats else ""))[:400]
        shot["shot_index"] = i + 1
        shot["title"] = f"Shot {i + 1}"
        shot["action"] = action[:500]
        shot["keyframe_prompt"] = action[:500]
        # Focus cast: score against this shot
        if characters:
            ranked = sorted(
                ((_score_in_text(ch, action), str(ch.get("id"))) for ch in characters),
                reverse=True,
            )
            top = [cid for sc, cid in ranked if sc > 0 and cid in valid][:2]
            if not top and ranked:
                top = [ranked[0][1]]
            # Last beat: if the action co-mentions another cast member ("with …"),
            # keep the top two scored humans — no role/place hardcodes.
            if i == budget - 1 and len(ranked) > 1:
                if re.search(
                    r"\bwith\s+(?:his|her|their|a|an|the)\s+\w+",
                    action,
                    re.I,
                ) or len([sc for sc, _ in ranked if sc > 0]) >= 2:
                    top = [cid for sc, cid in ranked if sc > 0 and cid in valid][:2]
                    if len(top) < 2 and ranked:
                        top = list(
                            dict.fromkeys(
                                top + [cid for _, cid in ranked if cid in valid]
                            )
                        )[:2]
            shot["character_ids"] = list(dict.fromkeys(top))[:3]
        half = 15.0 / budget
        shot["timeline"] = f"{i * half:.1f}-{(i + 1) * half:.1f}s"
        out.append(shot)
    return out


def enrich_analysis_heuristically(prompt: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """Deterministic director pass (works without LLM)."""
    out = deepcopy(analysis)
    out["user_prompt"] = prompt or out.get("user_prompt") or ""
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    valid = {str(c.get("id")) for c in characters if c.get("id")}
    by_name: dict[str, str] = {}
    for ch in characters:
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        by_name[cid.lower()] = cid
        name = str(ch.get("name") or "").strip()
        if name:
            by_name[name.lower()] = cid
        for alias in ch.get("aliases") or []:
            a = str(alias or "").strip()
            if a:
                by_name[a.lower()] = cid
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
    budget = infer_shot_budget(prompt, out)
    # Explicit multi-shot language must win over short-clip single-shot analysis.
    shots = _expand_shots_to_budget(prompt, shots, characters, budget)
    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
        on_screen = _cid_list(
            shot.get("on_screen") or shot.get("character_ids"),
            valid,
            by_name,
        )
        if not on_screen and characters:
            # Score cast against action text
            ranked = sorted(
                (
                    (
                        _score_in_text(ch, str(shot.get("action") or shot.get("keyframe_prompt") or "")),
                        str(ch.get("id")),
                    )
                    for ch in characters
                ),
                reverse=True,
            )
            on_screen = [ranked[0][1]] if ranked else []
        shot["character_ids"] = on_screen
        shot["on_screen"] = list(on_screen)
        exiting = _cid_list(
            shot.get("exiting_character_ids") or shot.get("exiting"),
            valid,
            by_name,
        )
        if not exiting:
            exiting = _pick_exiting(shot, characters, on_screen)
        shot["exiting_character_ids"] = exiting
        shot["exiting"] = list(exiting)
        staying = [c for c in on_screen if c not in exiting]
        shot["staying"] = staying
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
        explicit_rel = str(shot.get("shot_relation") or "").strip().lower()
        if explicit_rel in {"hard_cut", "angle_variant", "continuation"}:
            relation = explicit_rel
        elif i == 1:
            relation = "hard_cut"
        elif _HARD_CUT_RE.search(action) or (
            shots[i - 2].get("character_ids")
            and set(on_screen).isdisjoint(set(shots[i - 2].get("character_ids") or []))
        ):
            relation = "hard_cut"
        elif exiting:
            relation = "continuation"
        else:
            relation = "hard_cut"
        shot["shot_relation"] = relation
        shot["blocking"] = (
            shot.get("blocking")
            if isinstance(shot.get("blocking"), dict)
            else _blocking_for_shot(shot, characters, on_screen)
        )
        # Fold blocking into action so leaf prompts see it even without schema awareness
        blk = shot["blocking"]
        pos_bits = []
        for p in blk.get("positions") or []:
            if not isinstance(p, dict):
                continue
            pos_bits.append(
                f"{p.get('name') or p.get('character_id')}@{p.get('zone')} "
                f"({p.get('pose')}, {p.get('facing')})"
            )
        if pos_bits:
            extra = (
                f" BLOCKING: landmark={blk.get('landmark')}; "
                + "; ".join(pos_bits)
                + f". {blk.get('rule')}"
            )
            base_action = str(shot.get("action") or "")
            if "BLOCKING:" not in base_action:
                shot["action"] = (base_action + extra)[:500]
                kp = str(shot.get("keyframe_prompt") or base_action)
                if "BLOCKING:" not in kp:
                    shot["keyframe_prompt"] = (kp + extra)[:500]
    _ensure_stay_on_leave_beats(prompt, characters, shots)
    # Refresh blocking after stay/leave fix
    for shot in shots:
        on_screen = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        shot["blocking"] = _blocking_for_shot(shot, characters, on_screen)
        blk = shot["blocking"]
        pos_bits = [
            f"{p.get('name') or p.get('character_id')}@{p.get('zone')}"
            for p in (blk.get("positions") or [])
            if isinstance(p, dict)
        ]
        if pos_bits:
            extra = f" BLOCKING: landmark={blk.get('landmark')}; " + "; ".join(pos_bits)
            base_action = str(shot.get("action") or "")
            # Replace prior BLOCKING suffix if present
            base_action = re.split(r"\sBLOCKING:", base_action, maxsplit=1)[0]
            shot["action"] = (base_action + extra)[:500]
    _apply_exit_propagation(shots)
    out["shots"] = shots
    out["target_shot_count"] = len(shots)
    scenes = [s for s in (out.get("scenes") or []) if isinstance(s, dict)]
    if not isinstance(out.get("spatial_lock"), dict) or not out.get("spatial_lock"):
        out["spatial_lock"] = _spatial_lock_from_prompt(prompt, scenes)
    out["role_locks"] = _exclusivity_locks(characters, shots)
    out["director_contract"] = {
        "version": "plan_a.v1",
        "source": "heuristic",
        "shot_count": len(shots),
    }
    out["experiment_plan"] = "A"
    out["keyframe_policy"] = "compose_solos"
    out["skip_domain_role_locks"] = True
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import apply_shot_scope

        out = apply_shot_scope(out, prompt)
    except Exception:  # noqa: BLE001
        logger.info("apply_shot_scope skipped", exc_info=True)
    return out


async def enrich_analysis_with_llm(
    prompt: str,
    analysis: dict[str, Any],
    *,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """LLM director pass. Chat model is required; failures raise ``DesignerLlmError``."""
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        LLM_API_ERROR,
        call_model_tool,
        model_text_or_raise,
    )

    # Structural floor for merge — not a product substitute when the LLM fails.
    base = enrich_analysis_heuristically(prompt, analysis)
    try:
        characters = base.get("characters") or []
        shots = base.get("shots") or []
        system = (
            "You are a film DIRECTOR writing a production shot sheet for a multi-shot AI video. "
            "Domain-agnostic: never assume church, dinner, or office unless the prompt says so. "
            "Return ONE compact raw JSON object only — no markdown fences, no commentary. "
            "For EACH shot provide: shot_index, title, action (full detail for THIS window: "
            "blocking, posture, gaze, speech — not a one-liner, not the entire remaining plot), "
            "on_screen (character ids featured ON CAMERA now), "
            "off_camera (other cast ids who exist in the setting but must stay OFF FRAME this shot), "
            "exiting (ids leaving This shot), staying, "
            "shot_relation (hard_cut|continuation; angle_variant only if the user asked "
            "for same-moment multi-cam coverage), "
            "blocking={landmark, positions:[{character_id, zone, pose, facing}]}, "
            "motion_detail (second-by-second acting beats for THIS timeline window only — "
            "pose, gaze, hands, weight shifts like a movie shot sheet), "
            "speech_line (exact dialogue spoken in this shot, or empty string if silent — "
            "never invent lines for mute beats), "
            "wardrobe_lock (dress/outfit color, hair, facial features for EACH on_screen id), "
            "camera_framing_rule (how to frame so off_camera people are not visible). "
            "Rules: (1) shot count matches brief budget; shots are consecutive TIME "
            "windows that concatenate to the film — do not restage the full user prompt "
            "in every shot and do not paste the entire user_prompt into every action; "
            "each action is that window's full blocking/speech/wardrobe; "
            "(2) after exit, id not in later on_screen; "
            "(3) leave vs stay are DIFFERENT ids; "
            "(4) blocking zones concrete; "
            "(5) spatial_lock={setting, architecture, landmarks, static_rule, light}; "
            "(6) location changes reflected per beat; "
            "(7) off_camera people are NOT deleted — camera simply excludes them. "
            "Schema: "
            '{"shots":[...],"spatial_lock":{...},"target_shot_count":N,"notes":"..."}'
        )
        user_payload = {
            "user_prompt": prompt,
            "characters": characters,
            "draft_shots": shots,
            "budget": base.get("target_shot_count"),
            "instruction": (
                "Refine draft_shots into a strict director contract. "
                "Keep character ids unchanged. Obey budget."
            ),
        }
        result = await call_model_tool(
            prompt=json.dumps(user_payload, ensure_ascii=False),
            system=system,
            max_tokens=16384,
        )
        text = model_text_or_raise(result)
        # Extract JSON object (tolerate markdown fences / trailing chatter)
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if fence:
            text = fence.group(1).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise DesignerLlmError(
                "Chat model did not return a usable director contract JSON.",
                code=LLM_API_ERROR,
            )
        raw = text[start : end + 1]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Director LLM JSON parse failed")
            raise DesignerLlmError(
                "Chat model returned invalid director contract JSON.",
                code=LLM_API_ERROR,
            ) from exc
        if not isinstance(parsed, dict):
            raise DesignerLlmError(
                "Chat model did not return a usable director contract JSON.",
                code=LLM_API_ERROR,
            )
        valid = {str(c.get("id")) for c in characters if isinstance(c, dict) and c.get("id")}
        by_name: dict[str, str] = {}
        for ch in characters:
            if not isinstance(ch, dict):
                continue
            cid = str(ch.get("id") or "").strip()
            if not cid:
                continue
            by_name[cid.lower()] = cid
            name = str(ch.get("name") or "").strip()
            if name:
                by_name[name.lower()] = cid
        new_shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else None
        if not new_shots:
            raise DesignerLlmError(
                "Chat model did not return any usable director contract shots.",
                code=LLM_API_ERROR,
            )
        budget = int(parsed.get("target_shot_count") or base.get("target_shot_count") or len(new_shots))
        asked_n = _explicit_shot_count_from_prompt(prompt)
        if asked_n >= 1:
            budget = asked_n
        else:
            budget = max(budget, len(new_shots))
        budget = max(1, budget)
        norm: list[dict[str, Any]] = []
        for i, sh in enumerate(new_shots[:budget], start=1):
            if not isinstance(sh, dict):
                continue
            on_screen = _cid_list(
                sh.get("on_screen") or sh.get("character_ids"), valid, by_name
            )
            exiting = _cid_list(
                sh.get("exiting") or sh.get("exiting_character_ids"), valid, by_name
            )
            draft = deepcopy(shots[i - 1]) if i - 1 < len(shots) else {}
            merged = dict(draft)
            merged.update({k: v for k, v in sh.items() if v is not None})
            merged["shot_index"] = i
            merged["character_ids"] = on_screen or list(draft.get("character_ids") or [])
            merged["on_screen"] = list(merged["character_ids"])
            merged["exiting_character_ids"] = exiting
            merged["exiting"] = list(exiting)
            merged["staying"] = [
                c for c in merged["character_ids"] if c not in exiting
            ]
            rel = str(sh.get("shot_relation") or merged.get("shot_relation") or "").lower()
            if rel not in {"hard_cut", "angle_variant", "continuation"}:
                rel = "hard_cut" if i == 1 else str(draft.get("shot_relation") or "hard_cut")
            merged["shot_relation"] = rel
            if isinstance(sh.get("blocking"), dict):
                merged["blocking"] = sh["blocking"]
            elif not isinstance(merged.get("blocking"), dict):
                merged["blocking"] = _blocking_for_shot(
                    merged, characters, merged["character_ids"]
                )
            for craft_key in (
                "motion_detail",
                "speech_line",
                "wardrobe_lock",
                "camera_framing_rule",
            ):
                if str(sh.get(craft_key) or "").strip():
                    merged[craft_key] = str(sh.get(craft_key))[:500]
            off_cam = _cid_list(
                sh.get("off_camera") or sh.get("off_camera_cast_ids"), valid, by_name
            )
            if off_cam:
                merged["off_camera_cast_ids"] = off_cam
            blk = merged.get("blocking") if isinstance(merged.get("blocking"), dict) else {}
            pos_bits = []
            for p in blk.get("positions") or []:
                if not isinstance(p, dict):
                    continue
                pos_bits.append(
                    f"{p.get('name') or p.get('character_id')}@{p.get('zone')} "
                    f"({p.get('pose')}, {p.get('facing')})"
                )
            if pos_bits:
                extra = (
                    f" BLOCKING: landmark={blk.get('landmark')}; "
                    + "; ".join(pos_bits)
                    + f". {blk.get('rule')}"
                )
                base_action = str(merged.get("action") or "")
                if "BLOCKING:" not in base_action:
                    merged["action"] = (base_action + extra)[:500]
                kp = str(merged.get("keyframe_prompt") or base_action)
                if "BLOCKING:" not in kp:
                    merged["keyframe_prompt"] = (kp + extra)[:500]
            norm.append(merged)
        if not norm:
            raise DesignerLlmError(
                "Chat model did not return any usable director contract shots.",
                code=LLM_API_ERROR,
            )
        # Explicit multi-beat prompts can outrank a short LLM shot list.
        budget = max(budget, infer_shot_budget(prompt, base))
        norm = _expand_shots_to_budget(prompt, norm, characters, budget)
        _ensure_stay_on_leave_beats(prompt, characters, norm)
        _apply_exit_propagation(norm)
        out = deepcopy(base)
        out["shots"] = norm
        out["target_shot_count"] = len(norm)
        if isinstance(parsed.get("spatial_lock"), dict):
            out["spatial_lock"] = {
                **(out.get("spatial_lock") or {}),
                **parsed["spatial_lock"],
            }
        out["role_locks"] = _exclusivity_locks(characters, norm)
        out["director_contract"] = {
            "version": "plan_a.v1",
            "source": "llm",
            "shot_count": len(norm),
            "notes": str(parsed.get("notes") or "")[:400],
        }
        out["experiment_plan"] = "A"
        out["keyframe_policy"] = "compose_solos"
        out["skip_domain_role_locks"] = True
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                apply_shot_scope,
            )

            out = apply_shot_scope(out, prompt)
        except Exception:  # noqa: BLE001
            logger.info("apply_shot_scope skipped after director LLM", exc_info=True)
        return out
    except DesignerLlmError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info("Director LLM pass failed", exc_info=True)
        raise DesignerLlmError(
            f"Chat model request failed during director contract: {exc}",
            code=LLM_API_ERROR,
        ) from exc


async def apply_director_contract(
    prompt: str,
    analysis: dict[str, Any],
    *,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """LLM director enrich + Plan A v2. Failures raise ``DesignerLlmError``."""
    out = await enrich_analysis_with_llm(prompt, analysis, timeout_sec=timeout_sec)
    from jiuwenswarm.server.runtime.designer.pipeline.plan_a_v2 import (
        apply_plan_a_v2,
    )

    return apply_plan_a_v2(prompt, out)


def validate_director_contract(analysis: dict[str, Any]) -> list[str]:
    """Return human-readable gate failures (empty = ok)."""
    errors: list[str] = []
    shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
    if not shots:
        errors.append("no shots")
        return errors
    exited: set[str] = set()
    for shot in shots:
        idx = shot.get("shot_index")
        on_screen = {str(x) for x in (shot.get("character_ids") or []) if str(x)}
        bad = on_screen & exited
        if bad:
            errors.append(f"shot {idx}: featured exited ids {sorted(bad)}")
        if not isinstance(shot.get("blocking"), dict):
            errors.append(f"shot {idx}: missing blocking")
        if str(shot.get("shot_relation") or "") not in {
            "hard_cut",
            "angle_variant",
            "continuation",
        }:
            errors.append(f"shot {idx}: bad shot_relation")
        for cid in shot.get("exiting_character_ids") or []:
            exited.add(str(cid))
    return errors
