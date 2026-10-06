# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Large parameterized reference-led companion coverage scenarios (#7725).

Proves shared generic wardrobe ``match_terms`` (e.g. 无外套) never mark other
cast members covered. Lead still covers only the reconciled identity; every
distinct companion id still gets a solo sheet (or a combined overflow card).
"""

from __future__ import annotations

import sys
import types

try:
    import openjiuwen.core.kv_cache  # noqa: F401
except ImportError:
    _package = types.ModuleType("openjiuwen")
    _core = types.ModuleType("openjiuwen.core")
    _kv = types.ModuleType("openjiuwen.core.kv_cache")

    class KVCacheAffinityConfig:
        pass

    _kv.KVCacheAffinityConfig = KVCacheAffinityConfig
    _package.core = _core
    _core.kv_cache = _kv
    sys.modules.setdefault("openjiuwen", _package)
    sys.modules.setdefault("openjiuwen.core", _core)
    sys.modules["openjiuwen.core.kv_cache"] = _kv

from jiuwenswarm.server.runtime.designer.pipeline import reference_led as rl
from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
    ROLE_CHARACTER,
    ROLE_MOTION,
    ROLE_PRODUCT,
    ROLE_SCENE,
    ROLE_STYLE,
)
from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

_FOCUS_N = 20_000
_PIPELINE_N = 40_000

_GENERIC_WARDROBE = (
    "无外套",
    "便装",
    "日常装",
    "素色上衣",
    "plain coat",
    "no jacket",
    "casual wear",
    "simple shirt",
)
_LEAD_ALIASES = (
    "lead_alias",
    "xiaoyue",
    "subject_ref",
    "主角",
    "kid_lead",
    "hero_still",
)
_COMPANION_ROLES = (
    ("Mom", "adult caregiver"),
    ("Dad", "adult companion"),
    ("Grandma", "elder companion"),
    ("Uncle", "family adult"),
    ("Sibling", "young companion"),
    ("Neighbor", "supporting adult"),
)
_LOOKS = (
    "ink wash",
    "cel shaded",
    "soft pastel",
    "photoreal",
    "watercolor",
    "storybook",
)
_MEDIUMS = (
    "anime",
    "illustration",
    "live action",
    "3d render",
    "gouache",
)
_BINDINGS = ("verbatim", "condition")
_SLOT_KINDS = ("character", "motion_tagged", "alias_match", "product_plus_cast")
_CAST_SIZES = (3, 4, 5, 6)


def _pick(items: tuple, index: int, salt: int = 0):
    return items[(index + salt) % len(items)]


def _pipeline(node: dict) -> str:
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    return str(cfg.get("pipeline") or cfg.get("role") or "")


def _companion_sheets(graph: dict) -> list[dict]:
    found = []
    for node in graph.get("nodes") or []:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if cfg.get("companion_cast") is True:
            found.append(node)
    return found


def _clips(graph: dict) -> list[dict]:
    return [node for node in graph.get("nodes") or [] if _pipeline(node) == "clip"]


def _covered_companion_ids(companions: list[dict]) -> set[str]:
    """Solo character_id plus any ids folded into a combined_cast card."""
    ids: set[str] = set()
    for node in companions:
        cfg = node.get("config") or {}
        cid = str(cfg.get("character_id") or "").strip()
        if cid:
            ids.add(cid)
        for raw in cfg.get("character_ids") or []:
            text = str(raw or "").strip()
            if text:
                ids.add(text)
    return ids


def _cast_row(
    *,
    cid: str,
    name: str,
    description: str,
    shared_term: str,
    extra_terms: list[str] | None = None,
    aliases: list[str] | None = None,
) -> dict:
    terms = [shared_term]
    if extra_terms:
        terms.extend(extra_terms)
    row: dict = {
        "id": cid,
        "name": name,
        "description": description,
        "match_terms": terms,
    }
    if aliases:
        row["aliases"] = aliases
    return row


def _build_cast(
    index: int,
    *,
    n_companions: int,
    shared_term: str,
    lead_alias: str,
) -> list[dict]:
    lead = _cast_row(
        cid="char_1",
        name=f"Lead-{index % 11}",
        description="user-ref lead",
        shared_term=shared_term,
        extra_terms=[lead_alias, f"lead-term-{index % 7}"],
        aliases=[f"alias-lead-{index % 5}"],
    )
    cast = [lead]
    for i in range(n_companions):
        role_name, role_desc = _pick(_COMPANION_ROLES, index, i + 1)
        cast.append(
            _cast_row(
                cid=f"char_{i + 2}",
                name=f"{role_name}-{index % 9}-{i}",
                description=role_desc,
                shared_term=shared_term,
                extra_terms=[f"wardrobe-{i}-{index % 13}"],
            )
        )
    return cast


def _shots(cast_ids: list[str], index: int, *, include_set: bool) -> list[dict]:
    shot_count = 2 + (index % 3)
    shots = []
    for shot in range(shot_count):
        row = {
            "shot_index": shot + 1,
            "action": f"action-{index}-{shot}",
            "end_state": f"end-{index}-{shot}",
            "camera": "medium",
            "duration_sec": 2 + ((index + shot) % 4),
            "lighting": f"light-{index % 11}",
            "crowd": f"crowd-{index % 7}",
            "character_ids": list(cast_ids),
            "on_screen": list(cast_ids),
        }
        if include_set:
            row["setting_id"] = "set_1"
        shots.append(row)
    return shots


def _analysis_for(
    *,
    index: int,
    slots: list[dict],
    cast: list[dict],
    style_lock: dict,
    scenes: list[dict] | None = None,
) -> dict:
    cast_ids = [str(c["id"]) for c in cast]
    scene_rows = scenes if scenes is not None else []
    return {
        "source": "llm",
        "style_lock": style_lock,
        "characters": cast,
        "scenes": scene_rows,
        "shots": _shots(cast_ids, index, include_set=bool(scene_rows)),
        "audio": {"policy": "silent", "include_speech": False, "include_music": False},
        "creative_intent": {"mode": "reference_led", "slots": slots},
    }


def _graph(analysis: dict) -> dict:
    return build_smart_video_graph(
        project_id=f"proj_match_terms_{hash(str(analysis.get('creative_intent'))) & 0xFFFF:x}",
        prompt="reference-led companion match_terms coverage",
        analysis=analysis,
        optimize_for="quality",
    )


def _slot(
    roles: list[str],
    *,
    binding: str,
    character_id: str = "",
    path: str = "/refs/upload.png",
    node_id: str = "n_ref_01",
    slot: int = 1,
    **extra,
) -> dict:
    entry: dict = {
        "slot": slot,
        "path": path,
        "roles": list(roles),
        "bindings": {role: binding for role in roles},
        "node_id": node_id,
    }
    if character_id:
        entry["character_id"] = character_id
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# ~20k focused unit: coverage helpers ignore shared wardrobe match_terms
# ---------------------------------------------------------------------------


def _assert_focus_scenario(index: int) -> None:
    """Lead covered by user still + shared generic wardrobe → all companions remain.

    Exercises ``_reconcile_slot_ids`` / ``_covered_character_ids`` /
    ``_companion_characters`` directly so the 20k budget stays unit-speed while
    still proving the #7725 intersection bug cannot recur.
    """
    shared = _pick(_GENERIC_WARDROBE, index)
    lead_alias = _pick(_LEAD_ALIASES, index, 1)
    binding = _pick(_BINDINGS, index)
    n_companions = 3
    cast = _build_cast(
        index,
        n_companions=n_companions,
        shared_term=shared,
        lead_alias=lead_alias,
    )
    resolve_mode = index % 3
    if resolve_mode == 0:
        slot_cid = "char_1"
    elif resolve_mode == 1:
        slot_cid = str(cast[0]["name"])
    else:
        slot_cid = lead_alias

    slots = [
        _slot(
            [ROLE_CHARACTER],
            binding=binding,
            character_id=slot_cid,
            path=f"/refs/lead-{index}.png",
        )
    ]
    analysis = {
        "characters": cast,
        "scenes": [],
        "creative_intent": {"mode": "reference_led", "slots": slots},
    }
    rl._reconcile_slot_ids(slots, analysis)
    assert slots[0]["character_id"] == "char_1"

    covered = rl._covered_character_ids(slots, analysis)
    # Identity of the lead is covered; the shared wardrobe token must not be.
    assert rl._norm_id("char_1") in covered
    assert rl._norm_id(cast[0]["name"]) in covered
    assert rl._norm_id(shared) not in covered

    companions = rl._companion_characters(analysis, covered)
    companion_ids = {str(c.get("id") or "") for c in companions}
    expected = {f"char_{i}" for i in range(2, n_companions + 2)}
    assert companion_ids == expected, (
        f"index={index} shared={shared!r} binding={binding} "
        f"got={sorted(companion_ids)} expected={sorted(expected)}"
    )
    assert all(shared in (c.get("match_terms") or []) for c in cast)

    # match_terms still resolve classify aliases onto the lead roster row.
    resolved = rl._resolve_roster_id(lead_alias, cast, single_ok=False)
    assert resolved == "char_1"


def test_shared_match_terms_do_not_skip_companions() -> None:
    """~20k unit budget: shared generic match_terms never skip companions."""
    for index in range(_FOCUS_N):
        _assert_focus_scenario(index)


# ---------------------------------------------------------------------------
# ~40k whole-pipeline: multi-character companion minting / cast completeness
# ---------------------------------------------------------------------------


def _assert_pipeline_scenario(index: int) -> None:
    kind = _pick(_SLOT_KINDS, index)
    shared = _pick(_GENERIC_WARDROBE, index, 3)
    lead_alias = _pick(_LEAD_ALIASES, index, 2)
    binding = _pick(_BINDINGS, index, 1)
    cast_size = _pick(_CAST_SIZES, index)
    n_companions = cast_size - 1
    cast = _build_cast(
        index,
        n_companions=n_companions,
        shared_term=shared,
        lead_alias=lead_alias,
    )
    style = {
        "look": _pick(_LOOKS, index, 1),
        "medium": _pick(_MEDIUMS, index),
    }
    expected_companions = {f"char_{i}" for i in range(2, cast_size + 1)}
    scenes = [
        {
            "id": "set_1",
            "name": f"Place-{index % 17}",
            "description": f"place-{index}",
        }
    ]

    if kind == "character":
        slots = [
            _slot(
                [ROLE_CHARACTER],
                binding=binding,
                character_id="char_1",
                path=f"/refs/pipe-lead-{index}.png",
            )
        ]
    elif kind == "motion_tagged":
        slots = [
            _slot(
                [ROLE_MOTION],
                binding="verbatim",
                character_id=lead_alias if index % 2 else "char_1",
                path=f"/refs/pipe-motion-{index}.png",
            )
        ]
        scenes = []
    elif kind == "alias_match":
        slots = [
            _slot(
                [ROLE_CHARACTER],
                binding=binding,
                character_id=lead_alias,
                path=f"/refs/pipe-alias-{index}.png",
            )
        ]
    else:
        slots = [
            _slot(
                [ROLE_PRODUCT],
                binding="verbatim",
                path=f"/refs/pipe-product-{index}.png",
            )
        ]
        expected_companions = {f"char_{i}" for i in range(1, cast_size + 1)}

    if index % 11 == 0 and kind != "product_plus_cast":
        slots.append(
            _slot(
                [ROLE_STYLE],
                binding="verbatim",
                path=f"/refs/pipe-style-{index}.png",
                node_id="n_ref_02",
                slot=2,
            )
        )

    if index % 7 == 0 and kind in {"character", "alias_match"} and scenes:
        slots.append(
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                path=f"/refs/pipe-scene-{index}.png",
                node_id="n_ref_03",
                slot=3,
                setting_id="set_1",
                set_lock=True,
            )
        )

    graph = _graph(
        _analysis_for(
            index=index,
            slots=slots,
            cast=cast,
            style_lock=style,
            scenes=scenes,
        )
    )
    companions = _companion_sheets(graph)
    covered = _covered_companion_ids(companions)

    assert expected_companions <= covered, (
        f"kind={kind} index={index} missing={sorted(expected_companions - covered)} "
        f"covered={sorted(covered)}"
    )
    if kind != "product_plus_cast":
        assert "char_1" not in covered

    assert all(shared in (c.get("match_terms") or []) for c in cast)
    assert companions, f"expected companion sheets (kind={kind}, index={index})"

    clips = _clips(graph)
    assert clips
    companion_node_ids = {n["id"] for n in companions}
    for clip in clips:
        plan = (clip.get("config") or {}).get("reference_image_plan") or []
        plan_nodes = {str(e.get("node_id") or "") for e in plan if isinstance(e, dict)}
        assert plan_nodes & companion_node_ids, (
            f"clip {clip.get('id')} packs none of {sorted(companion_node_ids)} "
            f"(kind={kind}, index={index})"
        )


def test_reference_led_companion_cast_completeness_pipeline() -> None:
    """~40k pipeline budget: multi-character companion minting stays complete."""
    for index in range(_PIPELINE_N):
        _assert_pipeline_scenario(index)
