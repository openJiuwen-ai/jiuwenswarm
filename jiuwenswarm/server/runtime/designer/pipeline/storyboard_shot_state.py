# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Storyboard start/end state — sole continuity authority (domain-agnostic).

Each shot is a closed window:
  start_state → action/camera/speech → end_state
Same-setting chain: shot N start_state must match shot N-1 end_state.
No prior-clip Wan text required.
"""

from __future__ import annotations

import re
from typing import Any


def _short(text: str, *, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())[:limit]


def _as_state(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        return {"pose": raw.strip()[:280]}
    return {}


def normalize_shot_state(raw: Any) -> dict[str, Any]:
    """Normalize start_state / end_state to a compact structured dict."""
    st = _as_state(raw)
    out: dict[str, Any] = {}
    pose = _short(str(st.get("pose") or st.get("blocking") or st.get("holds") or ""), limit=280)
    if pose:
        out["pose"] = pose
    seats = st.get("seats") if isinstance(st.get("seats"), dict) else {}
    if not seats and isinstance(st.get("seat_anchors"), dict):
        seats = st["seat_anchors"]
    if seats:
        cleaned: dict[str, Any] = {}
        for cid, anchor in list(seats.items())[:12]:
            key = str(cid or "").strip()
            if not key:
                continue
            if isinstance(anchor, dict):
                cleaned[key] = {
                    str(k): str(v)[:120]
                    for k, v in anchor.items()
                    if str(k).strip() and str(v).strip()
                }
            elif str(anchor).strip():
                cleaned[key] = {"place": str(anchor).strip()[:120]}
        if cleaned:
            out["seats"] = cleaned
    facing = _short(str(st.get("facing") or st.get("gaze") or ""), limit=160)
    if facing:
        out["facing"] = facing
    camera = _short(str(st.get("camera") or ""), limit=120)
    if camera:
        out["camera"] = camera
    exited = [
        str(x).strip()
        for x in (st.get("exited") or st.get("exited_ids") or [])
        if str(x).strip()
    ]
    if exited:
        out["exited"] = exited[:12]
    speech_done = _short(str(st.get("speech_done") or st.get("speech") or ""), limit=160)
    if speech_done:
        out["speech_done"] = speech_done
    on_screen = [str(x).strip() for x in (st.get("on_screen") or []) if str(x).strip()]
    if on_screen:
        out["on_screen"] = on_screen[:12]
    offscreen = [str(x).strip() for x in (st.get("offscreen") or []) if str(x).strip()]
    if offscreen:
        out["offscreen"] = offscreen[:12]
    return out


def _infer_start_from_shot(shot: dict[str, Any]) -> dict[str, Any]:
    staging = shot.get("staging") if isinstance(shot.get("staging"), dict) else {}
    pose = _short(
        str(
            shot.get("start_pose")
            or staging.get("positioning_lock")
            or shot.get("blocking")
            or ""
        ),
        limit=280,
    )
    seats = shot.get("seat_anchors") if isinstance(shot.get("seat_anchors"), dict) else {}
    out: dict[str, Any] = {}
    if pose:
        out["pose"] = pose
    if seats:
        out["seats"] = seats
    camera = _short(str(shot.get("camera") or ""), limit=120)
    if camera:
        out["camera"] = camera
    on_screen = [str(x) for x in (shot.get("on_screen") or []) if str(x)]
    if on_screen:
        out["on_screen"] = on_screen
    offscreen = [str(x) for x in (shot.get("offscreen") or []) if str(x)]
    if offscreen:
        out["offscreen"] = offscreen
    return out


def _norm_phrase(text: str) -> str:
    body = re.sub(r"\s+", " ", str(text or "").strip().lower()).rstrip(".")
    return re.sub(
        r"^(after this shot|by the last frame|the earlier event is already finished)\s*:\s*",
        "",
        body,
    )


def _is_action_restatement(text: str, *actions: str) -> bool:
    """True when text is the previous shot's motion, not a still result."""
    raw = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if "after this shot" in raw or "already past:" in raw:
        return True
    body = _norm_phrase(text)
    if not body:
        return False
    for action in actions:
        act = _norm_phrase(action)
        if act and (body == act or act in body):
            return True
    return False


def _hold_state(state: dict[str, Any], *actions: str) -> dict[str, Any]:
    """Drop a pose that would make the next clip replay the previous motion."""
    out = dict(state)
    if _is_action_restatement(str(out.get("pose") or ""), *actions):
        out.pop("pose", None)
    if _is_action_restatement(str(out.get("irreversible") or ""), *actions):
        out.pop("irreversible", None)
    return {key: value for key, value in out.items() if value not in ("", [], {})}


def _infer_end_from_shot(shot: dict[str, Any]) -> dict[str, Any]:
    action = _short(str(shot.get("action") or shot.get("keyframe_prompt") or ""), limit=220)
    result = _short(str(shot.get("irreversible") or ""), limit=160)
    speech = _short(str(shot.get("speech_line") or ""), limit=160)
    exiting = [
        str(x)
        for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
        if str(x)
    ]
    out: dict[str, Any] = {}
    if result and not _is_action_restatement(result, action):
        out["pose"] = result
        out["irreversible"] = result
    if speech:
        out["speech_done"] = speech
    if exiting:
        out["exited"] = exiting
    # Remaining on_screen after exits.
    on_screen = [str(x) for x in (shot.get("on_screen") or []) if str(x)]
    remain = [c for c in on_screen if c not in set(exiting)]
    if remain:
        out["on_screen"] = remain
    offscreen = [str(x) for x in (shot.get("offscreen") or []) if str(x)]
    if offscreen or exiting:
        out["offscreen"] = list(dict.fromkeys(offscreen + exiting))
    camera = _short(str(shot.get("camera") or ""), limit=120)
    if camera:
        out["camera"] = camera
    seats = shot.get("seat_anchors") if isinstance(shot.get("seat_anchors"), dict) else {}
    if seats:
        # Drop exited seats.
        out["seats"] = {
            k: v for k, v in seats.items() if str(k) not in set(exiting)
        }
    return out


_EMOTIONS = {
    "setup": "setup",
    "hook": "setup",
    "opening": "setup",
    "rise": "rise",
    "rising": "rise",
    "development": "rise",
    "turn": "rise",
    "problem": "rise",
    "desire": "rise",
    "proof": "rise",
    "demonstration": "rise",
    "climax": "climax",
    "peak": "climax",
    "release": "release",
    "payoff": "release",
    "cta": "release",
    "ending": "release",
}


def emotion_label(raw: Any) -> str:
    text = str(raw or "").strip().lower().replace("-", " ").replace("_", " ")
    return _EMOTIONS.get(text, "")


def _id_list(raw: Any) -> list[str]:
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def _normalize_cast_states(shot: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Per-shot wardrobe / emotion / presence. Face identity stays on the character sheet."""
    on_screen = set(_id_list(shot.get("on_screen")))
    offscreen = set(_id_list(shot.get("offscreen")))
    exited = set(
        _id_list(shot.get("exiting_character_ids") or shot.get("exiting"))
    )
    raw = shot.get("cast_states") if isinstance(shot.get("cast_states"), dict) else {}
    out: dict[str, dict[str, str]] = {}
    for cid, state in raw.items():
        key = str(cid or "").strip()
        if not key:
            continue
        item = state if isinstance(state, dict) else {"emotion": state}
        entry: dict[str, str] = {}
        wardrobe = _short(str(item.get("wardrobe") or item.get("costume") or ""), limit=160)
        feeling = _short(str(item.get("emotion") or ""), limit=80)
        presence = str(item.get("presence") or "").strip().lower()
        if wardrobe:
            entry["wardrobe"] = wardrobe
        if feeling:
            entry["emotion"] = feeling
        if presence in {"on_screen", "offscreen", "exited", "absent"}:
            entry["presence"] = presence
        if entry:
            out[key] = entry
    ids = list(dict.fromkeys([*_id_list(shot.get("character_ids")), *on_screen, *offscreen, *exited]))
    for cid in ids:
        entry = dict(out.get(cid) or {})
        if cid in exited:
            entry["presence"] = "exited"
        elif cid in offscreen:
            entry["presence"] = "offscreen"
        elif cid in on_screen:
            entry["presence"] = "on_screen"
        elif "presence" not in entry:
            entry["presence"] = "absent"
        if entry:
            out[cid] = entry
    return out


def apply_story_curve(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One climax, an irreversible end, and a carried character state.

    The next shot starts from the previous end. Face identity is not stored here.
    """
    rows = [shot for shot in shots if isinstance(shot, dict)]
    count = len(rows)
    if not count:
        return shots
    for shot in rows:
        shot["emotion"] = emotion_label(shot.get("emotion") or shot.get("beat"))
    climaxes = [i for i, shot in enumerate(rows) if shot.get("emotion") == "climax"]
    if len(climaxes) > 1:
        keep = climaxes[-1]
        for index in climaxes:
            if index != keep:
                rows[index]["emotion"] = "rise"
        climaxes = [keep]
    if not climaxes and count >= 2:
        index = count - 2 if count >= 3 else count - 1
        rows[index]["emotion"] = "climax"
    climax_index = next(
        (i for i, shot in enumerate(rows) if shot.get("emotion") == "climax"),
        None,
    )
    for index, shot in enumerate(rows):
        if shot.get("emotion"):
            continue
        if index == 0:
            shot["emotion"] = "setup"
        elif climax_index is not None and index > climax_index:
            shot["emotion"] = "release"
        else:
            shot["emotion"] = "rise"
    # Re-find after fills. A lone climax must survive the fill above.
    climaxes = [i for i, shot in enumerate(rows) if shot.get("emotion") == "climax"]
    if len(climaxes) > 1:
        keep = climaxes[-1]
        for index in climaxes:
            if index != keep:
                rows[index]["emotion"] = "rise"
    carried_by_setting: dict[str, dict[str, dict[str, str]]] = {}
    prior_event = ""
    for shot in rows:
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        states = _normalize_cast_states(shot)
        carried = carried_by_setting.get(sid) or {}
        changes: list[str] = []
        for cid, entry in list(states.items()):
            previous = carried.get(cid) or {}
            if not entry.get("wardrobe") and previous.get("wardrobe"):
                entry["wardrobe"] = previous["wardrobe"]
            if not entry.get("emotion") and previous.get("emotion"):
                entry["emotion"] = previous["emotion"]
            wardrobe = str(entry.get("wardrobe") or "").strip()
            previous_wardrobe = str(previous.get("wardrobe") or "").strip()
            if wardrobe and previous_wardrobe and wardrobe != previous_wardrobe:
                changes.append(f"{cid} now wears {wardrobe}")
            states[cid] = entry
        shot["cast_states"] = states
        shot["wardrobe_changes"] = changes[:6]
        carried_by_setting[sid] = {
            cid: dict(entry) for cid, entry in states.items()
        }
        end = normalize_shot_state(shot.get("end_state"))
        action = str(shot.get("action") or "")
        irreversible = _short(
            str(shot.get("irreversible") or end.get("irreversible") or ""),
            limit=160,
        )
        if _is_action_restatement(irreversible, action):
            irreversible = ""
        if irreversible:
            shot["irreversible"] = irreversible
            end["irreversible"] = irreversible
            shot["end_state"] = end
        authored = [
            _short(str(item), limit=160)
            for item in (shot.get("do_not_replay") or [])
            if str(item).strip()
        ]
        replay: list[str] = []
        for item in ([prior_event] if prior_event else []) + authored:
            if item and item not in replay and not _is_action_restatement(item, action):
                replay.append(item)
        shot["do_not_replay"] = replay[:6]
        prior_event = irreversible or prior_event
    return shots


def ensure_shot_start_end_states(
    shots: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Fill start_state/end_state and chain same-setting start from prior end."""
    out: list[dict[str, Any]] = []
    last_end_by_setting: dict[str, dict[str, Any]] = {}
    last_action_by_setting: dict[str, str] = {}
    for raw in shots or []:
        if not isinstance(raw, dict):
            continue
        shot = dict(raw)
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
        start = normalize_shot_state(shot.get("start_state"))
        end = normalize_shot_state(shot.get("end_state"))
        if not start:
            prior_end = last_end_by_setting.get(sid) or {}
            if prior_end:
                # Keep where everyone is. Drop the previous motion so this clip
                # does not play the last shot's tail again.
                start = _hold_state(prior_end, last_action_by_setting.get(sid) or "")
            else:
                start = _infer_start_from_shot(shot)
        elif _is_action_restatement(str(start.get("pose") or ""), last_action_by_setting.get(sid) or ""):
            start = _hold_state(start, last_action_by_setting.get(sid) or "")
        if not end:
            end = _infer_end_from_shot(shot)
        elif _is_action_restatement(str(end.get("pose") or ""), action):
            end = _hold_state(end, action)
        # Carry seats from start into end when end omitted seats.
        if start.get("seats") and not end.get("seats"):
            exited = set(end.get("exited") or [])
            end["seats"] = {
                k: v for k, v in (start.get("seats") or {}).items() if k not in exited
            }
        shot["start_state"] = start
        shot["end_state"] = end
        # Seat anchors for this clip open = start seats.
        if isinstance(start.get("seats"), dict) and start["seats"]:
            shot["seat_anchors"] = dict(start["seats"])
        last_end_by_setting[sid] = end
        last_action_by_setting[sid] = action
        out.append(shot)
    return apply_story_curve(out)


def validate_storyboard_state_chain(
    shots: list[dict[str, Any]] | None,
) -> list[str]:
    """Return domain-agnostic validation notes (empty = ok)."""
    notes: list[str] = []
    last_end_by_setting: dict[str, dict[str, Any]] = {}
    for shot in shots or []:
        if not isinstance(shot, dict):
            continue
        idx = int(shot.get("shot_index") or 0) or "?"
        sid = str(shot.get("setting_id") or "").strip() or "set_1"
        start = normalize_shot_state(shot.get("start_state"))
        end = normalize_shot_state(shot.get("end_state"))
        if not start:
            notes.append(f"shot{idx}:missing_start_state")
        if not end:
            notes.append(f"shot{idx}:missing_end_state")
        prior = last_end_by_setting.get(sid)
        if prior and start:
            # Soft structural check: prior exited should not be on_screen at start
            # unless storyboard returned them.
            prior_exited = {str(x) for x in (prior.get("exited") or []) if str(x)}
            start_on = {str(x) for x in (start.get("on_screen") or shot.get("on_screen") or []) if str(x)}
            leaked = sorted(prior_exited & start_on)
            # Return is allowed — only flag if start explicitly lists them in exited too.
            start_exited = {str(x) for x in (start.get("exited") or []) if str(x)}
            bad = sorted(prior_exited & start_exited & start_on)
            if bad:
                notes.append(f"shot{idx}:exited_cast_marked_on_screen:{','.join(bad)}")
        last_end_by_setting[sid] = end or prior or {}
    return notes


def stamp_shot_states_on_clip_cfg(
    cfg: dict[str, Any],
    *,
    shot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy this storyboard row's start/end onto clip config."""
    out = dict(cfg or {})
    shot = shot if isinstance(shot, dict) else {}
    start = normalize_shot_state(shot.get("start_state") or out.get("start_state"))
    end = normalize_shot_state(shot.get("end_state") or out.get("end_state"))
    if start:
        out["start_state"] = start
        if isinstance(start.get("seats"), dict) and start["seats"]:
            out["seat_anchors"] = dict(start["seats"])
        holds = [str(x) for x in (out.get("pose_holds") or []) if str(x).strip()]
        pose = str(start.get("pose") or "").strip()
        if pose:
            hold = pose if pose.lower().startswith("already") else f"Opening hold: {pose}"
            if hold not in holds:
                holds.insert(0, hold[:200])
            out["pose_holds"] = holds[:8]
        facing = str(start.get("facing") or "").strip()
        if facing:
            hold = f"Opening facing: {facing}"
            if hold not in (out.get("pose_holds") or []):
                out.setdefault("pose_holds", [])
                out["pose_holds"] = [hold, *list(out.get("pose_holds") or [])][:8]
    if end:
        out["end_state"] = end
        # Exited after this shot — for next storyboard row, not prior-clip handoff.
        exited = [str(x) for x in (end.get("exited") or []) if str(x)]
        if exited:
            out["exiting_character_ids"] = exited
    emotion = emotion_label(shot.get("emotion") or out.get("emotion"))
    if emotion:
        out["emotion"] = emotion
    irreversible = _short(str(shot.get("irreversible") or out.get("irreversible") or ""), limit=160)
    if irreversible:
        out["irreversible"] = irreversible
    replay = [
        _short(str(item), limit=160)
        for item in (shot.get("do_not_replay") or out.get("do_not_replay") or [])
        if str(item).strip()
    ]
    if replay:
        out["do_not_replay"] = replay[:6]
    if isinstance(shot.get("cast_states"), dict) and shot.get("cast_states"):
        out["cast_states"] = shot["cast_states"]
    changes = [
        _short(str(item), limit=160)
        for item in (shot.get("wardrobe_changes") or [])
        if str(item).strip()
    ]
    if changes:
        out["wardrobe_changes"] = changes[:6]
    # Storyboard row is authority — do not require prior clip wiring.
    out.pop("continuity_clip_node_id", None)
    out["previous_clip_handoff_ready"] = False
    return out


def start_end_story_lines(cfg: dict[str, Any] | None) -> list[str]:
    """Opening placement from start_state. Finished action poses stay off the video body."""
    cfg = cfg if isinstance(cfg, dict) else {}
    lines: list[str] = []
    action = str(cfg.get("shot_action") or cfg.get("action") or "")
    prior_action = str(cfg.get("previous_clip_action") or "")
    start = normalize_shot_state(cfg.get("start_state"))
    end = normalize_shot_state(cfg.get("end_state"))
    if start.get("pose") and not _is_action_restatement(
        str(start["pose"]), action, prior_action
    ):
        lines.append(str(start["pose"]).rstrip(".") + ".")
    elif start.get("facing"):
        lines.append(f"Opening: facing {start['facing']}.")
    seats = start.get("seats") if isinstance(start.get("seats"), dict) else {}
    for cid, anchor in list(seats.items())[:4]:
        if isinstance(anchor, dict):
            place = anchor.get("place") or anchor.get("zone") or anchor.get("seat") or ""
            if place:
                lines.append(f"{cid} begins at {place}.")
        elif str(anchor).strip():
            lines.append(f"{cid} begins at {anchor}.")
    irreversible = _short(str(cfg.get("irreversible") or end.get("irreversible") or ""), limit=160)
    if irreversible and not _is_action_restatement(irreversible, action):
        lines.append(f"By the last frame, {irreversible.rstrip('.')}.")
    if emotion_label(cfg.get("emotion")) == "climax":
        lines.append("This shot is the film's single climax.")
    for change in list(cfg.get("wardrobe_changes") or [])[:3]:
        text = _short(str(change), limit=160)
        if text:
            lines.append(text.rstrip(".") + ".")
    return lines[:8]
