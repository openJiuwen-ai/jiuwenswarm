# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Domain-agnostic Wan R2V / reference-image best practices (all scenes).

Sources (summarized, not scene-genre hardcodes):
- Alibaba Model Studio Wan prompt guide (Character + Action + Lines + Scene;
  character1… identifiers; Image n / Video n order = upload order)
- Wan R2V API: reference_urls order → character1, character2…; one subject
  per character reference; ≤5 total refs
- Community Wan prompting: over-specify shot/camera/constraints; constrain
  motion so the model does not invent extras or odd posing

Used by Wan binding, staging locks, leaf skills, and call-time locks.
"""

from __future__ import annotations

import re
from typing import Any

# Universal contact verbs → pose tokens (any furniture / prop / floor — not dinner-only).
_POSE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:sit(?:s|ting)?|seated|sat)\b", re.I), "seated_on_surface"),
    (re.compile(r"\b(?:kneel(?:s|ing)?|knelt)\b", re.I), "kneeling_on_surface"),
    (re.compile(r"\b(?:lie|lies|lying|lay|reclining)\b", re.I), "reclining_on_surface"),
    (re.compile(r"\b(?:lean(?:s|ing)?|leant|leaned)\b", re.I), "leaning_on_support"),
    (re.compile(r"\b(?:stand(?:s|ing)?|stood)\s+(?:at|by|beside|near|behind|in front of)\b", re.I), "standing_beside_support"),
    (re.compile(r"\b(?:hold(?:s|ing)?|held|grasp(?:s|ing)?|clutch(?:es|ing)?)\b", re.I), "holding_prop"),
    (re.compile(r"\b(?:walk(?:s|ing)?|exit(?:s|ing)?|enter(?:s|ing)?|leave|leaves|leaving)\b", re.I), "standing_and_moving"),
    (re.compile(r"\b(?:crouch(?:es|ing)?|squat(?:s|ting)?)\b", re.I), "crouching_on_surface"),
]


def infer_pose_from_action(action: str, *, fallback: str = "engaged_in_beat") -> str:
    """Map generic action language → pose token (domain-agnostic)."""
    text = str(action or "").strip()
    if not text:
        return fallback
    for pat, pose in _POSE_PATTERNS:
        if pat.search(text):
            return pose
    return fallback


def contact_anti_penetration_clause(*, for_clip: bool = True) -> str:
    """Hard spatial physics for placing people into environment refs — any set."""
    where = "this clip" if for_clip else "this still"
    return (
        f"CONTACT / ANTI-PENETRATION LOCK ({where}, all scenes): "
        "Bodies occupy empty volume only — never intersect solid furniture, walls, "
        "props, floors-as-solids, or other bodies. "
        "If posture is seated/kneeling/reclining/leaning: visible contact with the "
        "support surface (seat, floor, edge, rail) — weight rests ON the surface, "
        "not floating and not sunk THROUGH it. "
        "Solo identity sheets may show standing studio poses; when the shot requires "
        "contact with set geometry, REPOSE the body for This shot (do not paste the "
        "standing sheet pose into the room). "
        "Hands/props: grasp exteriors; do not bury limbs inside objects. "
        "Depth order: people in front of or behind props as storyboarded — never "
        "merged into the prop volume."
    )


def wan_r2v_prompt_formula() -> str:
    """Short reference-video formula shared by Wan, Seedance, and MiniMax-H3."""
    return (
        "VIDEO PROMPT FORMULA: positive story form only — rewrite locks as narrative. "
        "The scene is as in Image N: <place>. "
        "<Name> from Image k, wearing <wardrobe>, <placement>, in the scene from Image N, "
        "is <action from this storyboard row>. "
        "Name only on_screen / partial cast. Omit exited cast until returned. "
        "The camera <one move>. <Speaker> says: \"<this shot's line>\". "
        "One look phrase at the end. Never forbid lists, 'do not', examples, or sit/stand defaults."
    )


def wan_reference_media_rules(*, prior_ending_count: int = 0) -> str:
    """Attach-order + media authority rules (general)."""
    lines = [
        "WAN REFERENCE MEDIA RULES (general):",
        "- Solo sheets = identity/wardrobe ONLY (face + clothing). Not final blocking.",
        "- Scene / environment ref (LAST among set refs) = place geography ONLY — empty of cast.",
        "- Prompt places character1… into the environment with contact against supports.",
        "- Upload/attach order defines labels; prompt must use the same order.",
        "- Prefer short explicit constraints over vague 'cinematic' adjectives.",
        "- Keep faces unique; keep landmarks fixed; keep medium stable across the film.",
    ]
    if int(prior_ending_count or 0) > 0:
        lines.append(
            "- Prior ending stills are not attached. "
            "This clip plays the current storyboard shot in the same setting, with the same placement."
        )
    else:
        lines.append(
            "- First clip of a setting: no prior ending still — play the storyboard shot "
            "using identity solos + scene specs."
        )
    return "\n".join(lines)


def wan_leaf_skill_block(*, role: str = "clip") -> str:
    """Compact skill text for leaf DeepAgents authoring video prompts."""
    r = str(role or "clip").strip().lower()
    if r == "clip":
        return "\n".join(
            [
                wan_r2v_prompt_formula(),
                "Seedance uses @Image N; MiniMax/Wan use Image N. Always 'from Image k' in story form.",
                "ORDER: (1) scene + present-cast story sentences with wardrobe/placement/visibility, "
                "(2) THIS storyboard action + camera + speech, "
                "(3) same-setting continue only — never across setting_id.",
                "Omit exited cast from the video call. No negatives, no worked examples.",
                "Director keep concise faithful story-form; rewrite lock essays.",
                "STYLE: one look phrase copied from the brief/storyboard.",
            ]
        )
    return "\n".join(
        [
            wan_r2v_prompt_formula(),
            wan_reference_media_rules(prior_ending_count=0),
            contact_anti_penetration_clause(for_clip=False),
            (
                "STYLE: copy the brief/storyboard medium. If the brief could not infer one, "
                "use its cartoonish default for the whole film."
            ),
        ]
    )


def ensure_style_lock(
    style: dict[str, Any] | None,
    *,
    prompt: str = "",
    scene_desc: str = "",
) -> dict[str, str]:
    """Return the brief's style lock; unspecified briefs use the cartoonish default."""
    if isinstance(style, dict) and (
        str(style.get("look") or "").strip()
        or str(style.get("medium") or "").strip()
    ):
        return {k: str(v)[:280] for k, v in style.items() if str(v).strip()}
    try:
        from jiuwenswarm.server.runtime.designer.media_model_playbook import (
            default_style_lock,
        )

        return default_style_lock(prompt, scene_desc)
    except Exception:  # noqa: BLE001
        return {
            "look": (
                "cartoonish animated feature look, flat shapes, soft rendering, rounded forms, "
                "coherent across all sheets/frames/clips"
            ),
            "lens": "animated cinematic framing; soft readable shapes; no comic/grid UI",
            "palette": "match the brief's palette; keep colors and line weight stable across shots",
            "medium": "stylized_animation",
            "forbid": (
                "no style drift between shots, no outfit redesign, no new architecture, "
                "no subtitles/watermarks, no medium switch mid-film"
            ),
        }
