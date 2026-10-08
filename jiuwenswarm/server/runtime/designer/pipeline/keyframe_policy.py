# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Keyframe policies — domain-agnostic (any brief / any setting).

Plan A commercial path (ensemble_master / setting-master):
  Per setting_id, the FIRST keyframe is a MASTER still that GENERATES the environment
  together with ALL named cast who appear in that setting (clearly visible, blocked).
  No scene specs template. Solo sheets = identity locks only (few Image-N slots).
  Later same-set keyframes EDIT the prior/master (reframe / zoom / pose / exits).
  New setting_id → new master with that setting's ensemble, then edit again.

Plan B (master_still): shot-1 master with featured cast; later edit prior.
"""

from __future__ import annotations

from typing import Any


def compute_setting_ensembles(
    shots: list[dict[str, Any]],
    characters: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """setting_id → ordered unique human character ids who appear in any shot there."""
    prop_chars = {
        str(c.get("id"))
        for c in (characters or [])
        if isinstance(c, dict)
        and c.get("id")
        and (
            c.get("is_prop")
            or str(c.get("cast_kind") or "") in {"brand_mascot", "prop"}
        )
    }
    by_set: dict[str, list[str]] = {}
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        bucket = by_set.setdefault(sid, [])
        prop_ids = {str(x) for x in (shot.get("prop_ids") or []) if str(x)} | prop_chars
        sources = (
            list(shot.get("ensemble_cast_ids") or [])
            or list(shot.get("compose_cast_ids") or [])
            or (
                list(shot.get("character_ids") or [])
                + list(shot.get("on_screen") or [])
                + list(shot.get("visible_cast_ids") or [])
                + list(shot.get("offscreen") or [])
                + list(shot.get("off_screen") or [])
                + list(shot.get("off_screen_cast_ids") or [])
                + list(shot.get("featured_cast_ids") or [])
            )
        )
        for cid in sources:
            c = str(cid).strip()
            if c and c not in bucket and c not in prop_ids:
                bucket.append(c)
    return by_set


def identity_ref_priority(
    *,
    ensemble: list[str],
    featured: list[str],
    limit: int | None = None,
) -> list[str]:
    """Order identity refs: featured first, then rest of ensemble.

    Compose keyframes must keep **every** ensemble solo available as a graph
    reference (no hard 3-cap). Callers that need a tight Image-N budget may
    pass an explicit ``limit``.
    """
    if limit is None:
        lim = max(1, len(ensemble) or len(featured) or 1)
    else:
        lim = max(1, int(limit or 1))
    out: list[str] = []
    for cid in list(featured) + list(ensemble):
        c = str(cid).strip()
        if c and c not in out:
            out.append(c)
        if len(out) >= lim:
            break
    return out


def _crowd_lock_for_setting(
    shot: dict[str, Any],
    *,
    is_first_of_set: bool,
    prev_crowd: dict[str, Any] | None,
) -> dict[str, Any]:
    """Anonymous extras / congregation must persist across same-setting keyframes."""
    prev = prev_crowd if isinstance(prev_crowd, dict) else {}
    blob = " ".join(
        str(shot.get(k) or "")
        for k in ("action", "keyframe_prompt", "scene_objects", "comment", "title")
    ).lower()
    mentions_crowd = any(
        k in blob
        for k in (
            "crowd",
            "audience",
            "extras",
            "spectators",
            "onlookers",
            "background people",
            "bystanders",
            "group of people",
        )
    )
    present = bool(prev.get("present")) or (is_first_of_set and mentions_crowd)
    if not present and prev.get("present"):
        present = True
    exiting_crowd = any(
        k in blob
        for k in (
            "crowd leaves",
            "crowd exits",
            "audience leaves",
            "extras leave",
            "people file out",
            "crowd disperses",
            "empty the room",
        )
    )
    if exiting_crowd:
        present = False
    count_hint = str(prev.get("density") or "").strip()
    if is_first_of_set and mentions_crowd and not count_hint:
        count_hint = "same anonymous background group as established"
    return {
        "present": present,
        "density": count_hint or ("none" if not present else "locked_background_group"),
        "rule": (
            "CROWD LOCK: if the first keyframe of this setting shows a crowd/extras/"
            "congregation, EVERY later same-setting keyframe and clip must keep that "
            "same group (count, placement, wardrobe vibe) unless the storyboard says "
            "they exit/disperse. NEVER invent a new crowd mid-setting; NEVER erase a "
            "locked crowd without an exit beat."
        ),
    }


def _cid_list(raw: Any, *, exclude: set[str] | None = None) -> list[str]:
    ban = exclude or set()
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        c = str(item).strip()
        if c and c not in ban and c not in out:
            out.append(c)
    return out


def _cast_actions_map(shot: dict[str, Any]) -> dict[str, str]:
    """Per-character doing-what for this shot (composer / storyboard authority)."""
    raw = shot.get("cast_actions") or shot.get("doing") or shot.get("character_actions")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            cid = str(k).strip()
            act = str(v or "").strip()
            if cid and act:
                out[cid] = act[:240]
        return out
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or item.get("character_id") or "").strip()
            act = str(item.get("action") or item.get("doing") or item.get("pose") or "").strip()
            if cid and act:
                out[cid] = act[:240]
    return out


def resolve_shot_cast_roles(
    shot: dict[str, Any],
    *,
    setting_ensemble: list[str],
    prop_ids: set[str],
) -> dict[str, list[str]]:
    """Per-shot visible / offscreen / absent — not every film character in every frame.

    - visible (on_screen): drawn in this keyframe
    - offscreen: in this setting but not in frame this shot
    - absent: film cast not in this setting at all (never drawn here)
    """
    ban = set(prop_ids)
    ensemble = [c for c in setting_ensemble if c not in ban]
    visible = _cid_list(
        shot.get("on_screen")
        or shot.get("visible_cast_ids")
        or shot.get("featured_cast_ids")
        or shot.get("character_ids"),
        exclude=ban,
    )
    offscreen = _cid_list(
        shot.get("offscreen")
        or shot.get("off_screen")
        or shot.get("off_screen_cast_ids"),
        exclude=ban,
    )
    # Fail closed: never promote the whole setting ensemble to on_screen.
    # Empty visible → featured-only or first explicit character_id, else [].
    if not visible:
        featured_only = _cid_list(shot.get("featured_cast_ids"), exclude=ban)
        visible = list(featured_only[:1]) if featured_only else []
    # Visible wins over offscreen if both listed.
    off_set = {c for c in offscreen if c not in visible}
    # Anyone in setting ensemble not visible → offscreen (still "in scene").
    for c in ensemble:
        if c not in visible and c not in off_set:
            off_set.add(c)
    offscreen = [c for c in offscreen if c in off_set] or sorted(off_set)
    # Keep storyboard order for visible; do NOT fall back to full ensemble.
    if ensemble:
        ens_set = set(ensemble)
        visible = [c for c in visible if c in ens_set] or [
            c for c in ensemble if c in set(visible)
        ]
        offscreen = [c for c in offscreen if c in ens_set and c not in visible]
    featured = _cid_list(
        shot.get("featured_cast_ids") or visible[:1],
        exclude=ban,
    )
    featured = [c for c in featured if c in visible] or list(visible[:1])
    return {
        "visible": visible,
        "offscreen": offscreen,
        "featured": featured,
        "ensemble": ensemble or list(dict.fromkeys(visible + offscreen)),
    }


def apply_compose_solos_setting_policy(analysis: dict[str, Any]) -> dict[str, Any]:
    """Per setting_id: first KF composes THIS SCENE's visible cast into a generated setting;
    later same-set KFs edit that composed keyframe (architecture locked).

    Not every film character appears in every scene. Storyboard decides per shot who is
    visible, who is offscreen, and what each visible person is doing.
    """
    out = analysis if isinstance(analysis, dict) else {}
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}
    prop_ids = {
        str(c.get("id"))
        for c in characters
        if c.get("id")
        and (
            c.get("is_prop")
            or str(c.get("cast_kind") or "") in {"brand_mascot", "prop"}
        )
    }
    all_human_ids = [
        str(c.get("id"))
        for c in characters
        if c.get("id") and str(c.get("id")) not in prop_ids
    ]
    ensembles = compute_setting_ensembles(shots, characters)
    # Do NOT force orphan cast into scene 1 visuals — solo cards still come from characters[].
    out["setting_ensembles"] = ensembles
    # Scene specs are the geography lock for clip R2V (Qwen stills).
    out["scene_continuity_mode"] = "scene_card_plus_clip_shots"

    # Distinct place text per setting_id (from scenes[] or first compose action).
    scenes = [s for s in (out.get("scenes") or []) if isinstance(s, dict)]
    scene_by_id = {str(s.get("id") or "").strip(): s for s in scenes if s.get("id")}
    setting_place: dict[str, str] = {}
    for sid, sc in scene_by_id.items():
        place = str(sc.get("description") or sc.get("name") or sid).strip()
        if place:
            setting_place[sid] = place[:280]

    prev_set = ""
    crowd_by_set: dict[str, dict[str, Any]] = {}
    compose_actions_by_set: dict[str, dict[str, str]] = {}
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            user_asked_coverage,
        )

        coverage = user_asked_coverage(str(out.get("user_prompt") or ""))
    except Exception:  # noqa: BLE001
        coverage = False
    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        setting_changed = bool(prev_set) and sid != prev_set
        is_first_of_set = (i == 1) or setting_changed or not prev_set

        roles = resolve_shot_cast_roles(
            shot,
            setting_ensemble=list(ensembles.get(sid) or []),
            prop_ids=prop_ids,
        )
        ensemble = roles["ensemble"]
        visible = roles["visible"]
        offscreen = roles["offscreen"]
        featured = roles["featured"]
        actions = _cast_actions_map(shot)
        # If no per-char actions, fall back to shared action line for featured.
        shared = str(shot.get("action") or shot.get("keyframe_prompt") or "").strip()
        if shared:
            for cid in visible:
                actions.setdefault(cid, shared[:240])

        shot["ensemble_cast_ids"] = ensemble
        shot["on_screen"] = list(visible)
        shot["visible_cast_ids"] = list(visible)
        shot["offscreen"] = list(offscreen)
        shot["off_screen_cast_ids"] = list(offscreen)
        shot["featured_cast_ids"] = featured
        shot["character_ids"] = list(visible)
        shot["cast_actions"] = actions
        # Compose scenes ONLY visible people; identity refs = visible (+ offscreen solos optional).
        shot["compose_cast_ids"] = list(visible)
        shot["identity_ref_ids"] = identity_ref_priority(
            ensemble=list(dict.fromkeys(visible + offscreen)),
            featured=featured,
            limit=None,
        )
        _expand_blocking_to_visible(shot, by_id, visible, actions)

        crowd = _crowd_lock_for_setting(
            shot,
            is_first_of_set=is_first_of_set,
            prev_crowd=crowd_by_set.get(sid),
        )
        crowd_by_set[sid] = crowd
        shot["crowd_lock"] = crowd

        exited = {
            str(x)
            for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
            if str(x)
        }
        absent = [c for c in all_human_ids if c not in ensemble]
        place = setting_place.get(sid) or str(shot.get("setting_description") or "").strip()
        if is_first_of_set and shared and not place:
            place = shared[:200]
            setting_place[sid] = place
        if place:
            shot["setting_description"] = place

        if is_first_of_set:
            shot["keyframe_strategy"] = "compose_from_solo_refs"
            shot["same_setting_prior_edit"] = False
            shot["ensemble_master"] = False
            shot["compose_setting_master"] = True
            shot["is_scene_master"] = True
            compose_actions_by_set[sid] = dict(actions)
            shot["scene_compose_authority"] = True
            other_sets = [s for s in setting_place if s != sid]
            distinct = (
                f"NEW SCENE `{sid}` — different scene from {', '.join(other_sets)}. "
                if other_sets
                else f"ESTABLISH SCENE `{sid}`. "
            )
            shot["scene_distinctness"] = (
                distinct
                + (f"Scene: {place}. " if place else "")
                + "Do not reuse architecture/landmarks from other setting_ids. "
                "This keyframe IS the scene master (setting + visible cast) — not an empty plate."
            )
        else:
            # Same setting: still compose from character solos (not edit prior image).
            # Consistency comes from shared scene_specs + prompt handoff from master KF.
            shot["keyframe_strategy"] = "compose_from_solo_refs"
            shot["same_setting_prior_edit"] = False
            shot["ensemble_master"] = False
            shot["compose_setting_master"] = False
            shot["is_scene_master"] = False
            shot["scene_compose_authority"] = False
            prior_actions = compose_actions_by_set.get(sid) or {}
            shot["setting_lock"] = {
                "setting_id": sid,
                "scene_name": place or setting_place.get(sid) or "",
                "rule": (
                    (
                        "SAME SCENE LOCK: compose again from solo character sheets using the "
                        "scene specs + master scene prompt (handoff). Keep architecture, lighting, "
                        "landmarks, crowd, and fixed props identical. Change ONLY camera view + "
                        "on_screen + cast_actions. Never invent new set dressing; never borrow "
                        "another setting_id."
                    )
                    if coverage
                    else (
                        "SAME SCENE LOCK: same scene, lighting, landmarks, and wardrobe. "
                        "This clip is the NEXT time window — film THIS shot's action only. "
                        "Do not restage the previous window from a new angle. Never borrow "
                        "another setting_id."
                    )
                ),
                "compose_cast_actions": prior_actions,
            }
            if str(shot.get("shot_relation") or "").lower() == "hard_cut":
                shot["master_still_hard_cut"] = True

        # Angle orbit only when the user asked for coverage. Otherwise each clip
        # is its own time window, not front/left/right of one empty action.
        view_cycle = ("front", "left", "right", "side", "top", "bottom")
        view_i = int(shot.get("shot_index") or 1) - 1
        relation = str(shot.get("shot_relation") or "").strip().lower()
        raw_view = str(shot.get("view_key") or "").strip().lower()
        if coverage or relation == "angle_variant":
            view_key = raw_view or view_cycle[view_i % len(view_cycle)]
            shot["view_key"] = view_key
        else:
            view_key = "sequence"
            shot["view_key"] = ""
        shot["scene_specs"] = build_scene_specs_for_setting(
            setting_id=sid,
            scene_name=place or setting_place.get(sid) or "",
            crowd=crowd,
            shot=shot,
            view_key=view_key,
        )

        doing_bits = "; ".join(
            f"{by_id.get(cid, {}).get('name') or cid}: {actions[cid]}"
            for cid in visible
            if actions.get(cid)
        )
        shot["occupancy"] = {
            **(shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}),
            "must_appear": [c for c in visible if c not in exited],
            "offscreen": list(offscreen),
            "must_not_appear": sorted(set(absent) | exited | set(offscreen)),
            "featured": list(featured),
            "cast_actions": actions,
            "doing": doing_bits,
            "crowd_lock": crowd,
            "rule": (
                (
                    "SCENE COMPOSE: generate THIS setting only; include ONLY on_screen/"
                    "must_appear people doing cast_actions. Offscreen cast stay out of "
                    "frame. Absent cast (other scenes) must not appear. "
                    if is_first_of_set
                    else "SAME-SCENE COMPOSE: reuse scene specs + master prompt; include ONLY "
                    "this shot's on_screen + cast_actions from solo sheets. Do not erase "
                    "must_appear; do not draw offscreen/absent; do not invent new props. "
                )
                + str(crowd.get("rule") or "")
            ),
        }
        prev_set = sid

    # Per-setting scene locks (hierarchical views) for Director / graph stamps.
    scene_locks: dict[str, dict[str, Any]] = {}
    for shot in shots:
        sid = str(shot.get("setting_id") or "set_1")
        bible = shot.get("scene_specs") if isinstance(shot.get("scene_specs"), dict) else None
        if bible and sid not in scene_locks:
            scene_locks[sid] = dict(bible)
        elif bible and sid in scene_locks:
            # Merge view coverage into the shared bible.
            views = dict(scene_locks[sid].get("views") or {})
            views.update(dict(bible.get("views") or {}))
            scene_locks[sid]["views"] = views

    out["shots"] = shots
    out["crowd_locks_by_setting"] = crowd_by_set
    out["setting_places"] = setting_place
    out["scene_locks"] = scene_locks
    out["scene_continuity_mode"] = "scene_card_plus_clip_shots"
    out["keyframe_policy"] = "scene_card_plus_clip_shots"
    out["keyframe_policy_notes"] = {
        "version": "plan_a.scene_card_clip.v17_time_lock",
        "rule": (
            "Scene specs per setting_id (Qwen image) + solo sheets. "
            "Clips use R2V: on-screen solos + empty scene. Same-setting clips "
            "continue from previous_clip_wan_prompt. Lock time-of-day / lighting / "
            "language / style across every same-setting shot."
        ),
    }
    return out


def build_scene_specs_for_setting(
    *,
    setting_id: str,
    scene_name: str,
    crowd: dict[str, Any] | None,
    shot: dict[str, Any],
    view_key: str,
) -> dict[str, Any]:
    """Deterministic scene specs with hierarchical view coverage."""
    crowd = crowd if isinstance(crowd, dict) else {}
    place_bit = (scene_name or str(shot.get("setting_description") or "") or "coherent scene").strip()
    lighting_raw = shot.get("lighting")
    if not lighting_raw and isinstance(shot.get("setting_lock"), dict):
        lighting_raw = (shot.get("setting_lock") or {}).get("lighting")
    tod_raw = ""
    if isinstance(shot.get("time_of_day_lock"), dict):
        tod_raw = str((shot.get("time_of_day_lock") or {}).get("time_of_day") or "")
        if not lighting_raw:
            lighting_raw = (shot.get("time_of_day_lock") or {}).get("lighting")
    if not lighting_raw:
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                infer_time_of_day_lock,
            )

            tod_lock = infer_time_of_day_lock(place_bit, str(shot.get("action") or ""))
            lighting_raw = tod_lock.get("lighting")
            tod_raw = tod_raw or str(tod_lock.get("time_of_day") or "")
        except Exception:  # noqa: BLE001
            lighting_raw = "motivated key light with stable direction; no relight mid-scene"
    lighting = str(lighting_raw or "motivated key light with stable direction; no relight mid-scene").strip()
    from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
        visual_place_name,
        visual_prop_list,
    )

    named = visual_place_name(scene_name) or visual_place_name(
        str(shot.get("setting_description") or "")
    )
    place_bit = named or (place_bit if visual_place_name(place_bit) else "")
    objects = visual_prop_list(shot.get("scene_objects"))
    view_defs = {
        "front": f"VIEW front: facing primary landmark of `{setting_id}`; show full width of locked props.",
        "left": "VIEW left: 90° left of front; same objects must remain fixed — no new props.",
        "right": "VIEW right: 90° right of front; mirrored coverage of the same locked set.",
        "side": "VIEW side: oblique ~45°; reveal depth while keeping object L/R continuity.",
        "top": "VIEW top/high: elevated angle; floor plan of props matches other views.",
        "bottom": "VIEW bottom/low: low angle looking up; ceiling/sky consistent with lighting lock.",
        "sequence": (
            "TIME WINDOW: film this shot's own action. Do not restage another "
            "window of the same scene from front, left, or right."
        ),
    }
    active = str(view_key or "sequence").strip().lower() or "sequence"
    if active not in view_defs:
        active = "sequence"
    return {
        "setting_id": setting_id,
        "scene_name": place_bit[:400],
        "architecture": place_bit[:400],
        "lighting": lighting[:320],
        "time_of_day": (tod_raw or "unspecified")[:40],
        "objects": [str(x)[:160] for x in objects[:12]],
        "crowd": {
            "present": crowd.get("present"),
            "density": crowd.get("density"),
            "rule": str(crowd.get("rule") or "")[:280],
        },
        "views": view_defs,
        "active_view": active,
        "coherence_rule": (
            "All views of this setting describe ONE scene at ONE time of day. "
            "Objects/lighting/crowd exist in every view even if off-camera; nothing "
            "pops into existence when the camera moves; no day↔night jump mid-scene."
        ),
    }


def _expand_blocking_to_visible(
    shot: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    visible: list[str],
    actions: dict[str, str],
) -> None:
    """Blocking for visible cast only (not offscreen / not other-scene cast)."""
    blocking = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
    positions = list(blocking.get("positions") or []) if isinstance(blocking, dict) else []
    have = {
        str((p or {}).get("character_id") or "")
        for p in positions
        if isinstance(p, dict)
    }
    zones = ("center", "left", "right", "foreground", "background")
    for i, cid in enumerate(visible):
        if cid in have:
            # Refresh pose from cast_actions when provided.
            for p in positions:
                if isinstance(p, dict) and str(p.get("character_id") or "") == cid:
                    if actions.get(cid):
                        p["pose"] = actions[cid][:160]
                    break
            continue
        ch = by_id.get(cid) or {}
        positions.append(
            {
                "character_id": cid,
                "name": str(ch.get("name") or cid),
                "zone": zones[i % len(zones)],
                "pose": actions.get(cid) or "present_in_frame_clearly_visible",
                "facing": "toward_primary_action_focus",
            }
        )
    # Drop blocking entries for people not visible this shot.
    positions = [
        p
        for p in positions
        if isinstance(p, dict) and str(p.get("character_id") or "") in set(visible)
    ]
    landmark = str((blocking or {}).get("landmark") or "primary_set_landmark")
    shot["blocking"] = {
        "landmark": landmark,
        "positions": positions,
        "rule": (
            "Place ONLY on_screen / visible cast. Offscreen characters must not appear. "
            "Pose = cast_actions / doing for this shot. Same-scene edits keep landmark "
            "and seats unless storyboard moves someone."
        ),
    }


def _expand_blocking_to_ensemble(
    shot: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    ensemble: list[str],
) -> None:
    """Legacy helper — prefer visible-only blocking."""
    actions = _cast_actions_map(shot)
    _expand_blocking_to_visible(shot, by_id, ensemble, actions)
