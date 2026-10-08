# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gated prior-clip last-frame + speech anti-repeat for Wan continuity.

Last-frame media is used ONLY when the next clip continues the same setting
(soft cut). Hard cuts / new settings break the chain.

Within one scene we accumulate ending stills from every prior same-scene clip
(back to the first shot of that setting) so exited cast can still be localized.
The immediate prior ending remains the primary continuity start; older endings
are history refs. The Wan prompt narrates what led to what.

Prior speech is stamped as already-delivered — next clip must not restate it.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Leave headroom for character solos + scene card in Wan reference_images.
MAX_SCENE_LAST_FRAME_REFS = 4


def _norm_ids(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _on_screen_ids(cfg: dict[str, Any] | None) -> list[str]:
    cfg = cfg if isinstance(cfg, dict) else {}
    ids = _norm_ids(cfg.get("on_screen")) or _norm_ids(cfg.get("character_ids"))
    occ = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    if not ids:
        ids = _norm_ids(occ.get("must_appear"))
    return list(dict.fromkeys(ids))


def _setting_id(cfg: dict[str, Any] | None) -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    return str(cfg.get("setting_id") or "").strip() or "set_1"


def _looks_like_hard_cut(this_cfg: dict[str, Any] | None, prev_cfg: dict[str, Any] | None) -> bool:
    this_cfg = this_cfg if isinstance(this_cfg, dict) else {}
    prev_cfg = prev_cfg if isinstance(prev_cfg, dict) else {}
    blob = " ".join(
        str(x)
        for x in (
            this_cfg.get("shot_action"),
            this_cfg.get("scene_change"),
            this_cfg.get("scene_distinctness"),
            (this_cfg.get("continuity_lock") or {}).get("forbid")
            if isinstance(this_cfg.get("continuity_lock"), dict)
            else "",
            (this_cfg.get("generate") or {}).get("prompt")
            if isinstance(this_cfg.get("generate"), dict)
            else "",
        )
        if x
    ).lower()
    if any(
        tok in blob
        for tok in (
            "hard cut",
            "smash cut",
            "cut to",
            "new location",
            "elsewhere",
            "meanwhile",
            "jump cut to another",
        )
    ):
        return True
    # Different setting always hard for last-frame purposes.
    if _setting_id(this_cfg) != _setting_id(prev_cfg):
        return True
    return False


def should_use_prior_last_frame(
    *,
    this_cfg: dict[str, Any] | None,
    prev_cfg: dict[str, Any] | None,
    require_cast_overlap: bool = True,
) -> bool:
    """Gate: same setting, not a hard cut; optional cast overlap for soft cuts."""
    this_cfg = this_cfg if isinstance(this_cfg, dict) else {}
    prev_cfg = prev_cfg if isinstance(prev_cfg, dict) else {}
    if not prev_cfg:
        return False
    if bool(this_cfg.get("forbid_prior_last_frame")) or bool(
        this_cfg.get("hard_cut")
    ):
        return False
    if _looks_like_hard_cut(this_cfg, prev_cfg):
        return False
    if _setting_id(this_cfg) != _setting_id(prev_cfg):
        return False
    if not require_cast_overlap:
        return True
    prev_cast = set(_on_screen_ids(prev_cfg))
    this_cast = set(_on_screen_ids(this_cfg))
    if not prev_cast or not this_cast:
        # Unknown cast — allow same-setting soft continuity.
        return True
    # Need at least one shared on-screen person (localization target).
    if not (prev_cast & this_cast):
        return False
    return True


def should_keep_scene_frame_chain(
    *,
    later_cfg: dict[str, Any] | None,
    earlier_cfg: dict[str, Any] | None,
) -> bool:
    """Softer gate for history stills inside one scene (exited cast allowed)."""
    return should_use_prior_last_frame(
        this_cfg=later_cfg,
        prev_cfg=earlier_cfg,
        require_cast_overlap=False,
    )


def _clip_nodes_sorted(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
    graph = graph if isinstance(graph, dict) else {}
    clips: list[dict[str, Any]] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        role = str(cfg.get("role") or "")
        nid = str(node.get("id") or "")
        if role not in {"clip", "video"} and not nid.startswith("n_clip_"):
            continue
        idx = int(cfg.get("shot_index") or 0) or 0
        if idx <= 0:
            continue
        clips.append(node)
    return sorted(
        clips,
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0) or 0,
    )


def _short_beat(cfg: dict[str, Any] | None, *, limit: int = 140) -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    raw = str(
        cfg.get("shot_action")
        or cfg.get("character_action")
        or cfg.get("previous_clip_action")
        or ""
    ).strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw[:limit]


def _last_frame_path_for_clip(
    node: dict[str, Any],
    *,
    ctx: Any = None,
) -> Path | None:
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    for key in ("last_frame_path", "previous_clip_last_frame"):
        path = Path(str(cfg.get(key) or "").strip())
        if path.is_file():
            return path.resolve()
    if ctx is None:
        return None
    try:
        from jiuwenswarm.server.runtime.designer.handlers.common import (
            node_output_refs,
            path_from_uri,
        )

        nid = str(node.get("id") or "")
        for ref in node_output_refs(ctx, nid):
            uri = str((ref or {}).get("uri") or "")
            vp = path_from_uri(uri)
            if vp is not None and vp.is_file() and vp.suffix.lower() in {
                ".mp4",
                ".mov",
                ".webm",
            }:
                extracted = extract_last_frame(vp)
                if extracted is not None:
                    return extracted
    except Exception:  # noqa: BLE001
        logger.debug("resolve clip last frame from ctx failed", exc_info=True)
    return None


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
        return exe or None
    except Exception:  # noqa: BLE001
        return None


def extract_last_frame(video_path: Path, dest: Path | None = None) -> Path | None:
    """Extract near-end frame from a completed clip mp4."""
    src = Path(video_path)
    if not src.is_file():
        return None
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        logger.warning("ffmpeg unavailable; cannot extract prior-clip last frame")
        return None
    out = dest or (src.with_name(f"{src.stem}_lastframe.jpg"))
    out.parent.mkdir(parents=True, exist_ok=True)
    # Seek near end; -sseof works on most ffmpeg builds.
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-sseof",
                "-0.15",
                "-i",
                str(src.resolve()),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out.resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 100:
            return out.resolve()
    except Exception:  # noqa: BLE001
        logger.debug("last-frame extract failed", exc_info=True)
    return None


def _normalize_speech(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip().lower())
    raw = re.sub(r"[\"'`]+", "", raw)
    return raw.strip(" .,!?;:")


def speech_already_delivered(this_speech: str, prior_speech: str) -> bool:
    """True when this shot would restate prior dialogue (exact or contained)."""
    a = _normalize_speech(this_speech)
    b = _normalize_speech(prior_speech)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 12 and (a in b or b in a):
        return True
    # High token overlap for short lines.
    ta, tb = set(a.split()), set(b.split())
    if len(ta) >= 3 and len(tb) >= 3:
        overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
        if overlap >= 0.85:
            return True
    return False


def scrub_restated_speech(cfg: dict[str, Any]) -> dict[str, Any]:
    """Clear this clip's speech fields when they duplicate previous_clip_speech."""
    cfg = dict(cfg or {})
    prior = str(cfg.get("previous_clip_speech") or "").strip()
    forbid = [
        str(x).strip()
        for x in (cfg.get("forbidden_speech") or [])
        if str(x).strip()
    ]
    if prior and prior not in forbid:
        forbid.append(prior)

    this_line = str(cfg.get("speech_line") or "").strip()
    by_char = (
        dict(cfg["speech_by_character"])
        if isinstance(cfg.get("speech_by_character"), dict)
        else {}
    )

    cleared = False
    if this_line and any(speech_already_delivered(this_line, p) for p in forbid):
        cfg["speech_line"] = ""
        cleared = True
    cleaned_by: dict[str, str] = {}
    for cid, line in by_char.items():
        text = str(line or "").strip()
        if text and any(speech_already_delivered(text, p) for p in forbid):
            cleared = True
            continue
        if text:
            cleaned_by[str(cid)] = text
    if by_char and cleaned_by != by_char:
        cfg["speech_by_character"] = cleaned_by
        if not cleaned_by:
            cfg["speech_line"] = ""
    elif cleared and not cleaned_by:
        cfg["speech_by_character"] = {}

    if cleared:
        cfg["speech_continuation_only"] = True
        done = [str(x) for x in (cfg.get("already_done") or []) if str(x).strip()]
        note = f"prior dialogue already spoken: {prior[:120]}" if prior else "prior dialogue already spoken"
        if note not in done:
            done.append(note)
        cfg["already_done"] = done
    cfg["forbidden_speech"] = forbid[:8]
    return cfg


def prior_speech_lock_clause(cfg: dict[str, Any] | None) -> str:
    cfg = cfg if isinstance(cfg, dict) else {}
    prior = str(cfg.get("previous_clip_speech") or "").strip()
    forbid = [str(x).strip() for x in (cfg.get("forbidden_speech") or []) if str(x).strip()]
    lines: list[str] = []
    if prior:
        lines.append(
            f"PRIOR SPEECH ALREADY DELIVERED (do NOT restate / restart): \"{prior[:220]}\". "
            "Continue only if this shot has NEW words; otherwise keep mouths matching "
            "ambient / silent continuation — never repeat the previous line."
        )
    elif forbid:
        lines.append(
            "PRIOR SPEECH ALREADY DELIVERED (do NOT restate): "
            + "; ".join(f'"{x[:120]}"' for x in forbid[:4])
        )
    if bool(cfg.get("speech_continuation_only")) and not str(cfg.get("speech_line") or "").strip():
        lines.append(
            "SPEECH LOCK this shot: no new dialogue line — prior line is finished; "
            "do not invent a repeat."
        )
    return "\n".join(lines)


def last_frame_continuity_clause(
    *,
    used: bool = False,
    path: str = "",
    chain: list[dict[str, Any]] | None = None,
) -> str:
    """Narrate same-scene ending stills: what led to what → continue from latest."""
    items = [c for c in (chain or []) if isinstance(c, dict) and str(c.get("path") or "").strip()]
    if not items and not used:
        return ""
    if not items and used:
        return (
            "CONTINUITY FIRST FRAME = last frame of the previous clip "
            f"({path or 'prior ending still'}). "
            "Use it only to localize last-seen people and place. "
            "THIS clip's action/camera come from the storyboard shot — "
            "do not replace that beat with a different plot from the still. "
            "Face/wardrobe authority remains character solo reference images."
        )

    lines: list[str] = [
        "SAME-SCENE CONSISTENCY CHAIN (ending stills localize last-seen people/place; "
        "they are NOT the plot for THIS clip):",
    ]
    for i, item in enumerate(items):
        idx = int(item.get("shot_index") or 0) or "?"
        beat = str(item.get("action") or "").strip() or "(prior beat)"
        speech = str(item.get("speech") or "").strip()
        bit = f"- Shot {idx} ENDED here: {beat}"
        if speech:
            bit += f' [speech already delivered: "{speech[:100]}"]'
        if i == len(items) - 1:
            bit += " ← localization of last-seen people/place only."
        else:
            bit += " (history — localize exited / off-screen people who were last seen here)."
        lines.append(bit)
    if len(items) > 1:
        arrow = " → ".join(f"shot {int(c.get('shot_index') or 0)}" for c in items)
        lines.append(f"Beat progression in this setting: {arrow} → THIS shot.")
    lines.append(
        "Do NOT attach these stills as Wan refs (they steal character1 and restage a crop). "
        "Identity = on-screen solos; geography = scene card LAST; plot = THIS storyboard shot "
        "continuing the previous Wan prompt. People who left stay off-screen — "
        "do not teleport them back or restage walking away unless the storyboard asks."
    )
    return "\n".join(lines)


def collect_same_scene_last_frame_chain(
    cfg: dict[str, Any] | None,
    *,
    graph: dict[str, Any] | None = None,
    ctx: Any = None,
    max_frames: int = MAX_SCENE_LAST_FRAME_REFS,
) -> list[dict[str, Any]]:
    """Oldest→newest gated ending stills from the first clip of this setting to N-1.

    Walks prior clips with the same setting_id. A hard cut / setting change breaks
    the chain. Cast overlap is NOT required for history frames (exits allowed);
    the immediate prior still prefers cast overlap when deciding primary continuity,
    but same-setting history is kept for localization.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    this_idx = int(cfg.get("shot_index") or 0) or 0
    if this_idx <= 1:
        return []
    if bool(cfg.get("forbid_prior_last_frame")) or bool(cfg.get("hard_cut")):
        return []
    if cfg.get("use_prior_last_frame") is False:
        return []

    priors = [
        n
        for n in _clip_nodes_sorted(graph)
        if int((n.get("config") or {}).get("shot_index") or 0) < this_idx
    ]
    if not priors:
        return []

    # Walk newest→oldest; stop at hard cut / setting break.
    selected_rev: list[dict[str, Any]] = []
    later_cfg: dict[str, Any] = cfg
    for node in reversed(priors):
        pcfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        pcfg = pcfg if isinstance(pcfg, dict) else {}
        if not should_keep_scene_frame_chain(later_cfg=later_cfg, earlier_cfg=pcfg):
            break
        path = _last_frame_path_for_clip(node, ctx=ctx)
        # Fall back to stamped chain entry on this cfg for the immediate prior.
        if path is None and not selected_rev:
            stamped = Path(str(cfg.get("previous_clip_last_frame") or "").strip())
            if stamped.is_file():
                path = stamped.resolve()
        if path is None:
            # Missing media — keep walking for older history if still same scene.
            later_cfg = pcfg
            continue
        selected_rev.append(
            {
                "shot_index": int(pcfg.get("shot_index") or 0) or 0,
                "node_id": str(node.get("id") or ""),
                "path": str(path),
                "action": _short_beat(pcfg),
                "speech": str(pcfg.get("speech_line") or "")[:160],
                "setting_id": _setting_id(pcfg),
            }
        )
        later_cfg = pcfg

    chain = list(reversed(selected_rev))
    # Cap: keep the oldest scene opener + the most recent endings.
    cap = max(1, int(max_frames or MAX_SCENE_LAST_FRAME_REFS))
    if len(chain) > cap:
        chain = [chain[0], *chain[-(cap - 1) :]] if cap > 1 else chain[-1:]
    return chain


def resolve_gated_last_frame_chain(
    cfg: dict[str, Any] | None,
    *,
    graph: dict[str, Any] | None = None,
    ctx: Any = None,
    max_frames: int = MAX_SCENE_LAST_FRAME_REFS,
) -> list[dict[str, Any]]:
    """Resolve same-scene chain; empty when gate forbids continuity."""
    cfg = cfg if isinstance(cfg, dict) else {}
    this_idx = int(cfg.get("shot_index") or 0) or 0
    # Hard guard: first clip of a film/shot never attaches prior endings.
    if this_idx <= 1:
        return []
    if bool(cfg.get("forbid_prior_last_frame")) or bool(cfg.get("hard_cut")):
        return []
    if cfg.get("use_prior_last_frame") is False:
        return []
    chain = collect_same_scene_last_frame_chain(
        cfg, graph=graph, ctx=ctx, max_frames=max_frames
    )
    if chain:
        return chain
    # Stamped chain on this clip when graph rebuild found nothing yet.
    stamped_raw = (
        cfg.get("scene_last_frame_chain")
        if isinstance(cfg.get("scene_last_frame_chain"), list)
        else []
    )
    stamped: list[dict[str, Any]] = []
    for item in stamped_raw:
        if not isinstance(item, dict):
            continue
        path = Path(str(item.get("path") or "").strip())
        if path.is_file():
            stamped.append(
                {
                    "shot_index": int(item.get("shot_index") or 0) or 0,
                    "node_id": str(item.get("node_id") or ""),
                    "path": str(path.resolve()),
                    "action": str(item.get("action") or "")[:140],
                    "speech": str(item.get("speech") or "")[:160],
                    "setting_id": _setting_id(cfg),
                }
            )
    if stamped:
        cap = max(1, int(max_frames or MAX_SCENE_LAST_FRAME_REFS))
        return stamped[-cap:]
    one = Path(str(cfg.get("previous_clip_last_frame") or "").strip())
    if one.is_file() and bool(cfg.get("use_prior_last_frame")):
        return [
            {
                "shot_index": int(cfg.get("previous_clip_shot_index") or 0) or 0,
                "node_id": str(cfg.get("previous_clip_node_id") or ""),
                "path": str(one.resolve()),
                "action": str(cfg.get("previous_clip_action") or "")[:140],
                "speech": str(cfg.get("previous_clip_speech") or "")[:160],
                "setting_id": _setting_id(cfg),
            }
        ]
    return []


def chain_paths_newest_first(chain: list[dict[str, Any]] | None) -> list[Path]:
    """Attach order for Wan: newest ending first, then older history."""
    paths: list[Path] = []
    for item in reversed(list(chain or [])):
        if not isinstance(item, dict):
            continue
        p = Path(str(item.get("path") or "").strip())
        if p.is_file():
            resolved = p.resolve()
            if resolved not in paths:
                paths.append(resolved)
    return paths


def stamp_scene_last_frame_chain(
    cfg: dict[str, Any],
    chain: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Persist compact chain metadata on clip config for prompt/rebuild."""
    cfg = dict(cfg or {})
    compact: list[dict[str, Any]] = []
    for item in chain or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        compact.append(
            {
                "shot_index": int(item.get("shot_index") or 0) or 0,
                "node_id": str(item.get("node_id") or ""),
                "path": path,
                "action": str(item.get("action") or "")[:140],
                "speech": str(item.get("speech") or "")[:120],
            }
        )
    cfg["scene_last_frame_chain"] = compact[:MAX_SCENE_LAST_FRAME_REFS]
    if compact:
        cfg["previous_clip_last_frame"] = compact[-1]["path"]
        cfg["use_prior_last_frame"] = True
    return cfg


def chain_prior_speech_across_clips(graph: dict[str, Any]) -> list[str]:
    """Stamp previous_clip_speech onto consecutive clips and scrub restatements.

    Call after Director/storyboard stamps speech_line onto clip configs so shot N+1
    does not restate dialogue already assigned to shot N.
    """
    notes: list[str] = []
    clips: list[tuple[int, dict[str, Any]]] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        role = str(cfg.get("role") or "")
        nid = str(node.get("id") or "")
        if role not in {"clip", "video"} and not nid.startswith("n_clip_"):
            continue
        idx = int(cfg.get("shot_index") or 0) or 0
        if idx <= 0:
            continue
        clips.append((idx, node))
    clips.sort(key=lambda t: t[0])
    prev_speech = ""
    prev_id = ""
    prev_idx = 0
    for idx, node in clips:
        cfg = dict(node.get("config") or {})
        if prev_speech:
            cfg["previous_clip_speech"] = prev_speech[:280]
            forbid = [str(x) for x in (cfg.get("forbidden_speech") or []) if str(x)]
            if prev_speech not in forbid:
                forbid.append(prev_speech)
            cfg["forbidden_speech"] = forbid[:8]
            if prev_id and not str(cfg.get("previous_clip_node_id") or "").strip():
                cfg["previous_clip_node_id"] = prev_id
                cfg["previous_clip_shot_index"] = prev_idx
            before = str(cfg.get("speech_line") or "").strip()
            cfg = scrub_restated_speech(cfg)
            after = str(cfg.get("speech_line") or "").strip()
            if before and not after:
                notes.append(f"{node.get('id')}: scrubbed restated speech from shot {prev_idx}")
            else:
                notes.append(f"{node.get('id')}: prior speech forbid from shot {prev_idx}")
        node["config"] = cfg
        # Carry delivered (or continued) speech forward for the next clip.
        this_speech = str(cfg.get("speech_line") or "").strip()
        if this_speech:
            prev_speech = this_speech
        elif str(cfg.get("previous_clip_speech") or "").strip():
            prev_speech = str(cfg.get("previous_clip_speech") or "").strip()
        else:
            prev_speech = ""
        prev_id = str(node.get("id") or "")
        prev_idx = idx
    return notes


def stamp_last_frame_onto_next_clips(
    graph: dict[str, Any],
    *,
    completed_clip_id: str,
    last_frame_path: str,
    shot_index: int,
    speech_line: str = "",
) -> list[str]:
    """After clip N completes: stamp last-frame path + scene chain + speech onto N+1."""
    notes: list[str] = []
    frame = str(last_frame_path or "").strip()
    if not frame:
        return notes
    by_id = {
        str(n.get("id") or ""): n
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }
    src = by_id.get(str(completed_clip_id or ""))
    src_cfg = src.get("config") if isinstance(src, dict) else {}
    src_cfg = dict(src_cfg) if isinstance(src_cfg, dict) else {}
    speech = (speech_line or str(src_cfg.get("speech_line") or "")).strip()
    src_cfg["last_frame_path"] = frame
    if src is not None:
        src["config"] = src_cfg

    # Build same-scene history ending stills on the completed clip, then extend for next.
    prior_chain = [
        c
        for c in (src_cfg.get("scene_last_frame_chain") or [])
        if isinstance(c, dict) and str(c.get("path") or "").strip()
    ]
    this_entry = {
        "shot_index": int(shot_index or 0),
        "node_id": str(completed_clip_id or ""),
        "path": frame,
        "action": _short_beat(src_cfg),
        "speech": speech[:120],
        "setting_id": _setting_id(src_cfg),
    }
    # Avoid duplicate tip if re-stamping.
    if not prior_chain or str(prior_chain[-1].get("path") or "") != frame:
        prior_chain = [*prior_chain, this_entry]
    src_cfg = stamp_scene_last_frame_chain(src_cfg, prior_chain)
    if src is not None:
        src["config"] = src_cfg
        notes.append(f"{completed_clip_id}: saved last_frame_path + scene chain ({len(prior_chain)})")

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = dict(node.get("config") or {})
        if str(cfg.get("role") or "") not in {"clip", "video"} and not str(
            node.get("id") or ""
        ).startswith("n_clip_"):
            continue
        idx = int(cfg.get("shot_index") or 0) or 0
        nid = str(node.get("id") or "")
        if idx == int(shot_index or 0) or nid == completed_clip_id:
            continue
        is_next = idx == int(shot_index or 0) + 1
        cont = str(cfg.get("continuity_clip_node_id") or "") == str(completed_clip_id)
        if not (is_next or cont):
            continue
        if should_keep_scene_frame_chain(later_cfg=cfg, earlier_cfg=src_cfg):
            cfg = stamp_scene_last_frame_chain(cfg, prior_chain)
            # Immediate prior primary: prefer cast-overlap gate for use flag,
            # but keep chain for localization even when cast diverged.
            if should_use_prior_last_frame(this_cfg=cfg, prev_cfg=src_cfg):
                cfg["use_prior_last_frame"] = True
            else:
                # History-only: still attach chain (exits), but mark soft.
                cfg["use_prior_last_frame"] = True
                cfg["prior_last_frame_history_only"] = True
            notes.append(
                f"{nid}: gated scene last-frame chain ON "
                f"({len(prior_chain)} stills) from {completed_clip_id}"
            )
        else:
            cfg.pop("previous_clip_last_frame", None)
            cfg.pop("scene_last_frame_chain", None)
            cfg["use_prior_last_frame"] = False
            notes.append(f"{nid}: gated prior last-frame OFF (hard cut / setting)")
        if speech:
            cfg["previous_clip_speech"] = speech[:280]
            forbid = [str(x) for x in (cfg.get("forbidden_speech") or []) if str(x)]
            if speech not in forbid:
                forbid.append(speech)
            cfg["forbidden_speech"] = forbid[:8]
            cfg = scrub_restated_speech(cfg)
        node["config"] = cfg
    return notes


def resolve_gated_last_frame_path(
    cfg: dict[str, Any] | None,
    *,
    graph: dict[str, Any] | None = None,
    ctx: Any = None,
) -> Path | None:
    """Return immediate prior last-frame path when gate allows and file exists."""
    chain = resolve_gated_last_frame_chain(cfg, graph=graph, ctx=ctx, max_frames=1)
    if chain:
        path = Path(str(chain[-1].get("path") or "").strip())
        if path.is_file():
            return path.resolve()
    cfg = cfg if isinstance(cfg, dict) else {}
    path = Path(str(cfg.get("previous_clip_last_frame") or "").strip())
    prev_id = str(cfg.get("continuity_clip_node_id") or cfg.get("previous_clip_node_id") or "").strip()
    prev_cfg: dict[str, Any] = {}
    graph = graph if isinstance(graph, dict) else {}
    if prev_id:
        for n in graph.get("nodes") or []:
            if isinstance(n, dict) and str(n.get("id") or "") == prev_id:
                prev_cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                break
        if (not path.is_file()) and isinstance(prev_cfg, dict):
            alt = str(prev_cfg.get("last_frame_path") or "").strip()
            if alt:
                path = Path(alt)
    gated = should_keep_scene_frame_chain(later_cfg=cfg, earlier_cfg=prev_cfg) or bool(
        cfg.get("use_prior_last_frame")
    )
    if cfg.get("use_prior_last_frame") is False:
        return None
    if not gated and not bool(cfg.get("use_prior_last_frame")):
        return None
    if path.is_file():
        return path.resolve()
    if ctx is not None and prev_id:
        try:
            from jiuwenswarm.server.runtime.designer.handlers.common import (
                node_output_refs,
                path_from_uri,
            )

            for ref in node_output_refs(ctx, prev_id):
                uri = str((ref or {}).get("uri") or "")
                vp = path_from_uri(uri)
                if vp is not None and vp.is_file() and vp.suffix.lower() in {".mp4", ".mov", ".webm"}:
                    extracted = extract_last_frame(vp)
                    if extracted is not None:
                        return extracted
        except Exception:  # noqa: BLE001
            logger.debug("resolve prior last frame from ctx failed", exc_info=True)
    return None