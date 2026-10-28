"""Reference-led and text-film contracts.

Focused topology checks: inject the classify JSON the LLM would return and
assert graph structure. Bindings / set_lock / style_authority / medium fields
alone drive these (no combinatorial NL scenario matrix).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

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

import pytest

from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
    ROLE_CHARACTER,
    ROLE_MOTION,
    ROLE_PRODUCT,
    ROLE_SCENE,
    VIDEO_ANIMATE_IMAGE,
    VIDEO_MULTI_REF,
    compose_reference_clip_prompt,
    stamp_creative_intent,
    video_generation_overrides,
)
from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

_ROOT = Path(__file__).resolve().parents[3]
_REFERENCE_LED = (
    _ROOT
    / "jiuwenswarm"
    / "server"
    / "runtime"
    / "designer"
    / "pipeline"
    / "reference_led.py"
)
_FORBIDDEN = ("moon cake", "月饼", "climb a wall", "as a painting")


@pytest.fixture(autouse=True)
def _no_configured_audio_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {
            "can_speech": False,
            "can_music": False,
            "can_video_audio": False,
            "video_audio_model": "",
        },
    )


def _pipeline(node: dict) -> str:
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    return str(cfg.get("pipeline") or cfg.get("role") or "")


def _clips(graph: dict) -> list[dict]:
    clips = [node for node in graph.get("nodes") or [] if _pipeline(node) == "clip"]
    return sorted(clips, key=lambda node: int(node["config"]["clip_index"]))


def _tasks(graph: dict, task: str) -> list[dict]:
    found = []
    for node in graph.get("nodes") or []:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if str(cfg.get("reference_image_task") or "") == task:
            found.append(node)
    return found


def _scene_nodes(graph: dict) -> list[dict]:
    return [node for node in graph.get("nodes") or [] if _pipeline(node) == "scene"]


def _companion_sheets(graph: dict) -> list[dict]:
    found = []
    for node in graph.get("nodes") or []:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if cfg.get("companion_cast") is True:
            found.append(node)
    return found


def _base_analysis(
    slots: list[dict],
    *,
    style_lock: dict | None = None,
    characters: list[dict] | None = None,
    scenes: list[dict] | None = None,
    video_binding: str | None = None,
) -> dict:
    intent: dict = {"mode": "reference_led", "slots": slots}
    if video_binding:
        intent["video_binding"] = video_binding
    return {
        "source": "llm",
        "style_lock": style_lock if style_lock is not None else {"look": "base-look", "medium": "base-medium"},
        "characters": (
            characters
            if characters is not None
            else [{"id": "char_1", "name": "Subject", "description": "a person"}]
        ),
        "scenes": (
            scenes
            if scenes is not None
            else [{"id": "set_1", "name": "Place", "description": "a-room"}]
        ),
        "clips": [
            {
                "clip_index": 1,
                "action": "beat-one",
                "end_state": "end-one",
                "camera": "medium",
                "duration_sec": 4,
                "setting_id": "set_1",
            },
            {
                "clip_index": 2,
                "action": "beat-two",
                "end_state": "end-two",
                "camera": "medium",
                "duration_sec": 4,
                "setting_id": "set_1",
            },
        ],
        "audio": {"policy": "silent", "include_speech": False, "include_music": False},
        "creative_intent": intent,
    }


def _slot(
    roles: list[str],
    *,
    binding: str | None = None,
    bindings: dict | None = None,
    path: str = "/refs/upload.png",
    node_id: str = "n_ref_01",
    slot: int = 1,
    character_id: str = "",
    setting_id: str = "",
    **extra,
) -> dict:
    entry: dict = {"slot": slot, "path": path, "roles": list(roles), "node_id": node_id}
    if bindings is not None:
        entry["bindings"] = bindings
    elif binding is not None:
        entry["bindings"] = {role: binding for role in roles}
    if character_id:
        entry["character_id"] = character_id
    if setting_id:
        entry["setting_id"] = setting_id
    entry.update(extra)
    return entry


def _intent_graph(
    slots: list[dict],
    *,
    style_lock: dict | None = None,
    characters: list[dict] | None = None,
    scenes: list[dict] | None = None,
    video_binding: str | None = None,
) -> dict:
    return build_smart_video_graph(
        project_id="proj_intent",
        prompt="reference intent case",
        analysis=_base_analysis(
            slots,
            style_lock=style_lock,
            characters=characters,
            scenes=scenes,
            video_binding=video_binding,
        ),
    )


def _node(graph: dict, node_id: str) -> dict:
    return next(node for node in graph["nodes"] if node["id"] == node_id)
def test_reference_led_source_has_no_scene_specific_rules() -> None:
    text = _REFERENCE_LED.read_text(encoding="utf-8").lower()
    for phrase in _FORBIDDEN:
        assert phrase not in text


def test_text_call_shape_is_unchanged_without_a_mode() -> None:
    overrides = video_generation_overrides({}, {}, ["a.png"])
    assert overrides == {
        "first_frame": None,
        "reference_images": ["a.png"],
        "force_reference_mode": True,
    }


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_director_does_not_rebuild_a_reference_led_graph() -> None:
    from jiuwenswarm.server.runtime.designer.orchestration import Director

    graph = _intent_graph(
        [_slot([ROLE_PRODUCT], binding="verbatim")],
        characters=[],
        scenes=[],
    )
    graph_id = graph["graph_id"]
    ack = await Director().design_execution_graph(graph)
    assert ack["source"] == "reference_led"
    assert graph["graph_id"] == graph_id
    assert _clips(graph)


def test_reference_led_canvas_is_titled_by_the_story_not_the_prompt() -> None:
    prompt = "生成一段15秒的奶娃舞蹈动画。要求：可爱的小奶娃在明亮的舞台上跳舞，动作活泼有节奏感"

    def build(story_name: str) -> dict:
        analysis = _base_analysis([_slot([ROLE_PRODUCT], binding="verbatim")], characters=[], scenes=[])
        analysis["story_name"] = story_name
        return build_smart_video_graph(project_id="proj_title", prompt=prompt, analysis=analysis)

    graph = build("奶娃律动秀")
    assert graph["metadata"]["bootstrap"] == "designer.graph.reference_led.v1"
    assert graph["title"] == "奶娃律动秀"
    assert build(prompt)["title"] == "设计项目"




# --- Fix 1: character verbatim skips the character specs ---------------------


def test_character_verbatim_fills_card_and_skips_sheet() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        scenes=[],
    )
    assert not _tasks(graph, "character_specs")
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "character"
    assert ref["output_ref"]["uri"].endswith("upload.png")
    for clip in _clips(graph):
        cfg = clip["config"]
        assert cfg["reference_prompt_contract"] == "character"
        char_entries = [e for e in cfg["reference_image_plan"] if e["role"] == ROLE_CHARACTER]
        assert char_entries
        assert char_entries[0]["node_id"] == "n_ref_01"
        assert char_entries[0]["path"].endswith("upload.png")
        assert not any(e["role"] == ROLE_SCENE for e in cfg["reference_image_plan"])


def test_character_omitted_binding_defaults_to_verbatim() -> None:
    """Missing / invalid binding must not invent a sheet or plate."""
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        BINDING_VERBATIM,
        ROLE_CHARACTER,
        _bindings_for,
    )

    bindings = _bindings_for({"roles": [ROLE_CHARACTER]}, [ROLE_CHARACTER])
    assert bindings[ROLE_CHARACTER] == BINDING_VERBATIM

    graph = _intent_graph(
        [
            {
                "slot": 1,
                "path": "/refs/upload.png",
                "roles": [ROLE_CHARACTER],
                "bindings": {},
                "character_id": "char_1",
                "setting_id": "",
                "node_id": "n_ref_01",
            }
        ],
        scenes=[],
    )
    assert not _tasks(graph, "character_specs")
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "character"


def test_character_condition_image_builds_character_specs() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="condition", character_id="char_1")],
        scenes=[],
    )
    assert len(_tasks(graph, "character_specs")) == 1
    # Restyle character alone must not invent a text set plate.
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert "reference_card_role" not in ref["config"]
    for clip in _clips(graph):
        char_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_CHARACTER
        ]
        assert char_entries and char_entries[0]["node_id"].startswith("n_character_")
        assert char_entries[0]["path"] == ""
        assert not any(e["role"] == ROLE_SCENE for e in clip["config"]["reference_image_plan"])


def test_scene_condition_without_lock_may_invent_plate() -> None:
    """Only an explicit scene restyle invents a T2I plate."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_1",
                slot=1,
                node_id="n_ref_01",
                path="/refs/person.png",
            ),
            _slot(
                [ROLE_SCENE],
                binding="condition",
                setting_id="set_1",
                slot=2,
                node_id="n_ref_02",
                path="/refs/room.png",
            ),
        ]
    )
    assert not _tasks(graph, "character_specs")
    assert len(_scene_nodes(graph)) == 1
    for clip in _clips(graph):
        scene_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_SCENE
        ]
        assert scene_entries and scene_entries[-1]["node_id"].startswith("n_scene_")
        assert scene_entries[-1]["path"] == ""


# --- Fix 3: set_lock / scene verbatim locks the set, no invented plate ------


def test_scene_set_lock_locks_set_and_skips_plate() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="condition",
                character_id="char_1",
                slot=1,
                node_id="n_ref_01",
                path="/refs/person.png",
            ),
            _slot(
                [ROLE_SCENE],
                binding="condition",
                setting_id="set_1",
                slot=2,
                node_id="n_ref_02",
                path="/refs/room.png",
                set_lock=True,
            ),
        ]
    )
    # Character still needs its sheet, but the locked scene is never invented.
    assert len(_tasks(graph, "character_specs")) == 1
    assert not _scene_nodes(graph)
    scene_ref = _node(graph, "n_ref_02")
    assert scene_ref["config"]["reference_card_role"] == "scene"
    assert scene_ref["output_ref"]["uri"].endswith("room.png")
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        scene_entries = [e for e in plan if e["role"] == ROLE_SCENE]
        assert scene_entries and scene_entries[-1]["node_id"] == "n_ref_02"
        assert scene_entries[-1]["path"].endswith("room.png")


# --- Fix 4/5: medium inheritance from an authority image --------------------


def test_style_authority_without_vision_locks_to_reference() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        MATCH_REFERENCE_MEDIUM,
    )

    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
            )
        ],
        style_lock={"look": "photoreal commercial", "medium": "photoreal"},
    )
    lock = graph["metadata"]["style_lock"]
    assert lock["medium"] == MATCH_REFERENCE_MEDIUM
    for clip in _clips(graph):
        prompt = clip["config"]["generate"]["prompt"]
        assert "match the rendering" in prompt.lower()
        assert clip["config"]["style_lock"]["medium"] == MATCH_REFERENCE_MEDIUM


def test_style_authority_with_vision_inherits_named_medium() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
                style_read={"medium": "anime", "look": "cel-shaded", "palette": "pastel"},
            )
        ],
        style_lock={"look": "photoreal commercial", "medium": "photoreal"},
    )
    lock = graph["metadata"]["style_lock"]
    assert lock["medium"] == "anime"
    assert lock["look"] == "cel-shaded"
    for clip in _clips(graph):
        prompt = clip["config"]["generate"]["prompt"]
        assert "Medium: anime." in prompt
        assert "cel-shaded" in prompt


def test_user_medium_wins_when_no_style_authority() -> None:
    graph = _intent_graph(
        [_slot([ROLE_SCENE], binding="verbatim", setting_id="set_1")],
        style_lock={"look": "live-action", "medium": "photoreal"},
    )
    lock = graph["metadata"]["style_lock"]
    assert lock["medium"] == "photoreal"
    assert lock["look"] == "live-action"


# --- Fix 6: product / motion verbatim --------------------------------------


def test_product_verbatim_keeps_plan_path_and_card() -> None:
    graph = _intent_graph(
        [_slot([ROLE_PRODUCT], binding="verbatim", path="/refs/sku.png")],
        characters=[],
        scenes=[],
    )
    assert not _tasks(graph, "character_specs")
    assert not _tasks(graph, "medium_change")
    assert not _scene_nodes(graph)
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "product"
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        assert plan[0]["role"] == ROLE_PRODUCT
        assert plan[0]["path"].endswith("sku.png")


def test_motion_verbatim_i2v_without_restyle() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png")],
        video_binding=VIDEO_ANIMATE_IMAGE,
        scenes=[],
    )
    assert not _tasks(graph, "medium_change")
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "motion"
    for clip in _clips(graph):
        cfg = clip["config"]
        assert cfg["reference_call_mode"] == "i2v"
        assert cfg["reference_first_frame"].endswith("frame.png")


def test_motion_condition_builds_restyle_node() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="condition", path="/refs/frame.png")],
        video_binding=VIDEO_ANIMATE_IMAGE,
        scenes=[],
    )
    assert len(_tasks(graph, "medium_change")) == 1
    ref = _node(graph, "n_ref_01")
    assert "reference_card_role" not in ref["config"]
    for clip in _clips(graph):
        assert clip["config"]["reference_first_frame_node"] == "n_restyle_01"


def test_animate_image_binding_without_motion_role_animates_the_upload() -> None:
    """A classifier row may name the binding but forget the motion_source role."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", path="/refs/painting.png")],
        video_binding=VIDEO_ANIMATE_IMAGE,
        scenes=[],
    )
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "motion"
    for clip in _clips(graph):
        cfg = clip["config"]
        assert cfg["reference_call_mode"] == "i2v"
        assert cfg["reference_first_frame"].endswith("painting.png")
        # The same upload is the frame, not a second soft reference.
        assert [entry["node_id"] for entry in cfg["reference_image_plan"]] == ["n_ref_01"]
        overrides = video_generation_overrides(cfg, graph, [])
        assert str(overrides["first_frame"]).endswith("painting.png")
        assert not overrides["reference_images"]


def test_animate_image_without_motion_role_keeps_companions_as_refs() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", path="/refs/painting.png")],
        characters=_FAMILY,
        scenes=[],
        video_binding=VIDEO_ANIMATE_IMAGE,
    )
    for node in graph["nodes"]:
        if (node.get("config") or {}).get("companion_cast"):
            node["output_ref"] = {"kind": "image", "uri": f"/out/{node['id']}.png"}
    clip = _clips(graph)[0]
    # Extra cast upgrades the job to R2V, with the animated upload leading.
    assert clip["config"]["reference_call_mode"] == "r2v"
    overrides = video_generation_overrides(clip["config"], graph, [])
    refs = overrides["reference_images"] or []
    assert any(str(p).endswith("painting.png") for p in refs)
    assert any("/out/n_character_" in str(p) for p in refs)


# --- Fix 2: classifier fields persist through stamp_creative_intent ---------


def test_enriched_classify_fields_persist_into_slots() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        absorb_reference_read,
        stamp_creative_intent,
    )

    item = {
        "slot": 1,
        "subject": "scene",
        "roles": [ROLE_SCENE],
        "binding": "verbatim",
        "set_lock": True,
        "style_authority": True,
        "medium": "anime",
        "look": "cel-shaded",
        "palette": "pastel",
        "rationale": "locked cartoon set",
    }
    read = absorb_reference_read(item, 1, "scene")
    assert read["set_lock"] is True
    assert read["style_authority"] is True
    assert "still_complete" not in read
    assert "solo_subject" not in read
    assert "suppress_companions" not in read
    assert read["style_read"] == {"medium": "anime", "look": "cel-shaded", "palette": "pastel"}

    analysis = stamp_creative_intent(
        {"style_lock": {}}, [read], [{"path": "/refs/room.png"}]
    )
    slot = analysis["creative_intent"]["slots"][0]
    assert slot["set_lock"] is True
    assert slot["style_authority"] is True
    assert slot["style_read"]["medium"] == "anime"
    assert slot["bindings"][ROLE_SCENE] == "verbatim"
    assert "still_complete" not in analysis["creative_intent"]


# --- Companion cast / set: uncovered analysis ids get sheets/plates ---------


_FAMILY = [
    {"id": "xiaoyue", "name": "Xiaoyue", "description": "young woman"},
    {"id": "father", "name": "Father", "description": "middle-aged man"},
    {"id": "mother", "name": "Mother", "description": "middle-aged woman"},
]

_PLACES = [
    {"id": "set_1", "name": "Dining room", "description": "family dining room"},
    {"id": "set_2", "name": "Kitchen", "description": "warm kitchen"},
]


def test_verbatim_character_multi_cast_builds_companion_sheets() -> None:
    """Verbatim still covers that id; other analysis characters get sheets."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=[_PLACES[0]],
        style_lock={"look": "ink wash", "medium": "anime"},
    )
    sheets = _tasks(graph, "character_specs")
    assert len(sheets) == 2
    sheet_ids = {str(n["config"].get("character_id") or "") for n in sheets}
    assert sheet_ids == {"father", "mother"}
    assert not any(n["config"].get("character_id") == "xiaoyue" for n in sheets)
    for sheet in sheets:
        assert sheet["config"]["style_lock"]["medium"] == "anime"
        assert sheet["config"].get("companion_cast") is True
        assert "n_ref_01" not in (sheet["config"].get("inputs") or [])
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["force_handler"] is True
    assert ref["config"]["reference_card_role"] == "character"
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        char_entries = [e for e in plan if e["role"] == ROLE_CHARACTER]
        assert char_entries[0]["node_id"] == "n_ref_01"
        assert char_entries[0]["path"].endswith("upload.png")
        assert {e["node_id"] for e in char_entries[1:]} == {
            sheets[0]["id"],
            sheets[1]["id"],
        }
        assert any(e["role"] == ROLE_SCENE for e in plan)
    assert len(_scene_nodes(graph)) == 1


def test_verbatim_character_solo_cast_skips_companion_sheets() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=[{"id": "xiaoyue", "name": "Xiaoyue", "description": "young woman"}],
        scenes=[],
    )
    assert not _tasks(graph, "character_specs")
    assert not _scene_nodes(graph)


def test_condition_character_sheets_self_plus_companions() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="condition", character_id="xiaoyue")],
        characters=_FAMILY,
    )
    sheets = _tasks(graph, "character_specs")
    assert len(sheets) == 3
    by_id = {str(n["config"].get("character_id") or ""): n for n in sheets}
    assert set(by_id) == {"xiaoyue", "father", "mother"}
    # Condition still feeds its own sheet; companions do not.
    assert "n_ref_01" in (by_id["xiaoyue"]["config"].get("inputs") or [])
    assert by_id["xiaoyue"].get("config", {}).get("companion_cast") is not True
    assert by_id["father"]["config"].get("companion_cast") is True
    assert "n_ref_01" not in (by_id["father"]["config"].get("inputs") or [])


def test_scene_verbatim_builds_plates_for_uncovered_settings() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        MATCH_REFERENCE_MEDIUM,
    )

    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
            )
        ],
        characters=[],
        scenes=_PLACES,
        style_lock={"look": "photoreal", "medium": "photoreal"},
    )
    plates = _scene_nodes(graph)
    assert len(plates) == 1
    assert plates[0]["config"]["setting_id"] == "set_2"
    # Authority still without vision inherits match-reference medium.
    assert plates[0]["config"]["style_lock"]["medium"] == MATCH_REFERENCE_MEDIUM
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "scene"
    for clip in _clips(graph):
        scene_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_SCENE
        ]
        assert scene_entries[0]["node_id"] == "n_ref_01"
        assert scene_entries[0]["path"].endswith("upload.png")
        assert scene_entries[-1]["node_id"] == plates[0]["id"]
        assert scene_entries[-1]["path"] == ""


def test_scene_verbatim_with_cast_builds_companion_sheets() -> None:
    """Locked set + multi-cast analysis → companion sheets, no plate for locked set."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                style_authority=True,
                style_read={"medium": "anime", "look": "cel-shaded"},
            )
        ],
        characters=_FAMILY,
        scenes=[_PLACES[0]],
    )
    assert not _scene_nodes(graph)
    sheets = _tasks(graph, "character_specs")
    assert len(sheets) == 3
    for sheet in sheets:
        assert sheet["config"]["style_lock"]["medium"] == "anime"


def test_product_with_cast_builds_companion_sheets() -> None:
    graph = _intent_graph(
        [_slot([ROLE_PRODUCT], binding="verbatim", path="/refs/sku.png")],
        characters=_FAMILY[:2],
        scenes=[],
    )
    sheets = _tasks(graph, "character_specs")
    assert len(sheets) == 2
    ref = _node(graph, "n_ref_01")
    assert ref["config"]["reference_card_role"] == "product"
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        assert plan[0]["role"] == ROLE_PRODUCT
        assert any(e["node_id"].startswith("n_character_") for e in plan)


def test_motion_multi_cast_mints_companions_ignoring_stale_flags() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=[],
        video_binding=VIDEO_ANIMATE_IMAGE,
    )
    sheets = _tasks(graph, "character_specs")
    assert len(sheets) == 2
    assert {n["config"]["character_id"] for n in sheets} == {"father", "mother"}
    for clip in _clips(graph):
        # Extra cast upgrades animate_image to R2V so companions pack as refs.
        assert clip["config"]["reference_call_mode"] == "r2v"
        plan = clip["config"]["reference_image_plan"]
        assert plan[0]["path"].endswith("frame.png")
        assert any(e["node_id"].startswith("n_character_") for e in plan)

    stale = _intent_graph(
        [
            _slot(
                [ROLE_MOTION],
                binding="verbatim",
                path="/refs/frame.png",
                character_id="xiaoyue",
                still_complete=True,
                solo_subject=True,
                suppress_companions=True,
            )
        ],
        characters=_FAMILY,
        scenes=[],
        video_binding=VIDEO_ANIMATE_IMAGE,
    )
    assert len(_tasks(stale, "character_specs")) == 2


def test_multi_image_covers_each_id_only_uncovered_get_companions() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="xiaoyue",
                slot=1,
                node_id="n_ref_01",
                path="/refs/xiaoyue.png",
            ),
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="father",
                slot=2,
                node_id="n_ref_02",
                path="/refs/father.png",
            ),
        ],
        characters=_FAMILY,
    )
    sheets = _tasks(graph, "character_specs")
    assert len(sheets) == 1
    assert sheets[0]["config"]["character_id"] == "mother"
    for clip in _clips(graph):
        char_entries = [
            e for e in clip["config"]["reference_image_plan"] if e["role"] == ROLE_CHARACTER
        ]
        assert {e["node_id"] for e in char_entries} == {
            "n_ref_01",
            "n_ref_02",
            sheets[0]["id"],
        }


def test_slot_character_id_matches_analysis_name() -> None:
    """Normalize so slot character_id can match analysis name/id."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="Xiaoyue")],
        characters=[
            {"id": "char_xy", "name": "Xiaoyue", "description": "young woman"},
            {"id": "father", "name": "Father", "description": "dad"},
        ],
    )
    sheets = _tasks(graph, "character_specs")
    assert len(sheets) == 1
    assert sheets[0]["config"]["character_id"] == "father"


def test_stale_solo_subject_does_not_wipe_uncovered_companions() -> None:
    """T1: leftover solo_subject=true is ignored; uncovered cast still gets sheets."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_1",
                solo_subject=True,
                suppress_companions=True,
                still_complete=True,
            )
        ],
        characters=[
            {"id": "char_1", "name": "Lead", "description": "lead"},
            {"id": "char_2", "name": "Two", "description": "two"},
            {"id": "char_3", "name": "Three", "description": "three"},
            {"id": "char_4", "name": "Four", "description": "four"},
        ],
        scenes=[],
    )
    sheets = _companion_sheets(graph)
    assert len(sheets) == 3
    assert {n["config"]["character_id"] for n in sheets} == {"char_2", "char_3", "char_4"}
    assert not any(n["config"].get("character_id") == "char_1" for n in _tasks(graph, "character_specs"))


def test_xiaoyue_alias_covers_char_1_and_parents_are_companions() -> None:
    """T3: classify id xiaoyue reconciles to char_1 / 小月; parents only are companions."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=[
            {
                "id": "char_1",
                "name": "小月",
                "description": "young woman",
                "match_terms": ["xiaoyue", "小月"],
            },
            {"id": "father", "name": "Father", "description": "dad"},
            {"id": "mother", "name": "Mother", "description": "mom"},
        ],
        scenes=[],
    )
    sheets = _companion_sheets(graph)
    assert {n["config"]["character_id"] for n in sheets} == {"father", "mother"}
    assert not any(n["config"].get("character_id") in {"xiaoyue", "char_1"} for n in sheets)
    slot = graph["metadata"]["script_analysis"]["creative_intent"]["slots"][0]
    assert slot["character_id"] == "char_1"


def test_shared_generic_match_terms_do_not_skip_companion_sheets() -> None:
    """#7725: shared wardrobe match_terms (无外套) must not mark other cast covered."""
    shared = "无外套"
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        characters=[
            {
                "id": "char_1",
                "name": "小月",
                "description": "child lead",
                "match_terms": [shared, "小月"],
            },
            {
                "id": "char_2",
                "name": "妈妈",
                "description": "mom",
                "match_terms": ["围裙"],
            },
            {
                "id": "char_3",
                "name": "爸爸",
                "description": "dad",
                "match_terms": [shared],
            },
            {
                "id": "char_4",
                "name": "奶奶",
                "description": "grandma",
                "match_terms": [shared, "灰发"],
            },
        ],
        scenes=[],
    )
    sheets = _companion_sheets(graph)
    assert {n["config"]["character_id"] for n in sheets} == {"char_2", "char_3", "char_4"}
    assert not any(n["config"].get("character_id") == "char_1" for n in sheets)


def test_character_image_plates_uncovered_analysis_scenes() -> None:
    """T6: character specs + two storyboard rooms → companion plates."""
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        characters=[{"id": "char_1", "name": "Lead", "description": "lead"}],
        scenes=_PLACES,
    )
    plates = _scene_nodes(graph)
    assert len(plates) >= 1
    assert {p["config"]["setting_id"] for p in plates} == {"set_1", "set_2"}


def test_scene_image_plates_only_uncovered_setting() -> None:
    """T7: locked set_1 of two analysis sets → plate set_2 only."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
            )
        ],
        characters=[],
        scenes=_PLACES,
    )
    plates = _scene_nodes(graph)
    assert len(plates) == 1
    assert plates[0]["config"]["setting_id"] == "set_2"


def test_plan_cap_keeps_uploads_over_generated_companions() -> None:
    """T8: 2 uploads + 3 companions + plate → ≤5 and both uploads stay."""
    graph = _intent_graph(
        [
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_1",
                slot=1,
                node_id="n_ref_01",
                path="/refs/a.png",
            ),
            _slot(
                [ROLE_CHARACTER],
                binding="verbatim",
                character_id="char_2",
                slot=2,
                node_id="n_ref_02",
                path="/refs/b.png",
            ),
        ],
        characters=[
            {"id": "char_1", "name": "One", "description": "one"},
            {"id": "char_2", "name": "Two", "description": "two"},
            {"id": "char_3", "name": "Three", "description": "three"},
            {"id": "char_4", "name": "Four", "description": "four"},
            {"id": "char_5", "name": "Five", "description": "five"},
        ],
        scenes=_PLACES[:1],
    )
    companions = _companion_sheets(graph)
    assert len(companions) >= 2
    assert any(c["config"].get("combined_cast") for c in companions) or len(companions) == 3
    assert _scene_nodes(graph)
    for clip in _clips(graph):
        plan = clip["config"]["reference_image_plan"]
        assert len(plan) <= 5
        upload_ids = {e["node_id"] for e in plan if e.get("path")}
        assert {"n_ref_01", "n_ref_02"} <= upload_ids


# --- R1–R7: video_binding, packing resolve, Image-N, combined cast ----------


def test_stamp_persists_video_binding_default_multi_ref() -> None:
    analysis = stamp_creative_intent(
        {"characters": [], "scenes": []},
        [
            {
                "slot": 1,
                "subject": "character",
                "roles": [ROLE_CHARACTER],
                "binding": "verbatim",
                "video_binding": VIDEO_MULTI_REF,
            }
        ],
        [{"path": "/refs/a.png"}],
    )
    assert analysis["creative_intent"]["video_binding"] == VIDEO_MULTI_REF


def test_motion_role_alone_does_not_force_i2v() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png", character_id="char_1")],
        characters=_FAMILY,
        scenes=[],
        # no video_binding → multi_ref_story
    )
    for clip in _clips(graph):
        assert clip["config"]["reference_call_mode"] == "r2v"
        assert clip["config"].get("reference_video_binding") == VIDEO_MULTI_REF


def test_dinner_scene_plus_motion_role_stays_r2v() -> None:
    graph = _intent_graph(
        [
            _slot(
                [ROLE_SCENE, ROLE_MOTION],
                binding="verbatim",
                setting_id="set_1",
                set_lock=True,
                path="/refs/room.png",
            )
        ],
        characters=_FAMILY,
        scenes=_PLACES,
        video_binding=VIDEO_MULTI_REF,
    )
    for clip in _clips(graph):
        assert clip["config"]["reference_call_mode"] == "r2v"
        plan = clip["config"]["reference_image_plan"]
        assert sum(1 for e in plan if e["node_id"] == "n_ref_01") == 1
        assert any(e["node_id"].startswith("n_character_") for e in plan)


def test_r2v_merge_keeps_companion_paths_with_upload() -> None:
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=_PLACES[:1],
        video_binding=VIDEO_MULTI_REF,
    )
    for node in graph["nodes"]:
        cfg = node.get("config") or {}
        if cfg.get("companion_cast"):
            node["output_ref"] = {"kind": "image", "uri": f"/out/{node['id']}.png"}
    clip = _clips(graph)[0]
    overrides = video_generation_overrides(
        clip["config"], graph, ["/edge/from_inputs.png"]
    )
    refs = overrides["reference_images"] or []
    assert any(str(p).endswith("upload.png") or "upload" in str(p) for p in refs) or any(
        str(p).endswith(".png") for p in refs
    )
    assert any("/out/n_character_" in str(p) for p in refs)
    assert "/edge/from_inputs.png" in refs


def test_compose_prompt_labels_every_plan_image() -> None:
    prompt = compose_reference_clip_prompt(
        {
            "clip_index": 1,
            "clip_action": "wave",
            "camera": "medium",
            "timeline": "0.0-4.0s",
            "duration_sec": 4,
            "style_lock": {"look": "anime", "medium": "anime"},
            "reference_call_mode": "r2v",
            "reference_prompt_contract": "character",
            "reference_image_plan": [
                {"role": ROLE_CHARACTER, "path": "/a.png", "node_id": "n_ref_01"},
                {
                    "role": ROLE_CHARACTER,
                    "path": "",
                    "node_id": "n_character_1",
                    "names": ["Mom", "Dad"],
                },
                {"role": ROLE_SCENE, "path": "", "node_id": "n_scene_1"},
            ],
        }
    )
    assert "Image 1 is the person" in prompt
    assert "Image 2 is Mom and Dad" in prompt
    assert "Image 3 is the place" in prompt


def test_overflow_mints_combined_cast_card() -> None:
    cast = [{"id": f"char_{i}", "name": f"P{i}", "description": f"c{i}"} for i in range(1, 8)]
    graph = _intent_graph(
        [_slot([ROLE_CHARACTER], binding="verbatim", character_id="char_1")],
        characters=cast,
        scenes=_PLACES[:1],
        video_binding=VIDEO_MULTI_REF,
    )
    combined = [n for n in _companion_sheets(graph) if n["config"].get("combined_cast")]
    assert len(combined) == 1
    assert len(combined[0]["config"].get("character_ids") or []) >= 2
    for clip in _clips(graph):
        assert any(e["node_id"] == combined[0]["id"] for e in clip["config"]["reference_image_plan"])


def test_animate_image_cast_packs_refs_not_null() -> None:
    graph = _intent_graph(
        [_slot([ROLE_MOTION], binding="verbatim", path="/refs/frame.png", character_id="xiaoyue")],
        characters=_FAMILY,
        scenes=[],
        video_binding=VIDEO_ANIMATE_IMAGE,
    )
    for node in graph["nodes"]:
        if (node.get("config") or {}).get("companion_cast"):
            node["output_ref"] = {"kind": "image", "uri": f"/out/{node['id']}.png"}
    clip = _clips(graph)[0]
    assert clip["config"]["reference_call_mode"] == "r2v"
    overrides = video_generation_overrides(clip["config"], graph, [])
    refs = overrides["reference_images"] or []
    assert any(str(p).endswith("frame.png") for p in refs)
    assert any("/out/n_character_" in str(p) for p in refs)
