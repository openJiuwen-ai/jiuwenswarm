# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Character sheets share one film style in words and draw turnarounds."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.designer.handlers import image_nodes
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.media_model_playbook import (
    authored_visual_style,
)
from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
    compose_character_sheet_prompt,
    is_turnaround_sheet,
)
from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
    compose_practice_prompt,
)

_STYLE = "水墨写意画风，线条写意，墨色浓淡有度，灰褐土色为主调"
_BRIEF = f"# 阿Q\n\n## 八、视觉风格\n\n另一段描述。\n\n## Visual Style\n\n{_STYLE}\n\n## Audio\n\n- Speech: dialogue\n"


def _sheet(nid: str, **cfg: object) -> dict:
    return {
        "id": nid,
        "type": "image",
        "config": {"role": "character", "inputs": ["n_brief"], **cfg},
    }


def _cast_graph() -> dict:
    return {
        "nodes": [
            {"id": "n_brief", "type": "text", "config": {"role": "brief"}},
            _sheet("n_character_1", character_ids=["char_aq"], character_name="阿Q"),
            _sheet("n_character_2", character_ids=["char_wuma"], character_name="吴妈"),
        ],
        "edges": [
            {"id": f"e_brief_n_character_{i}", "source": "n_brief", "target": f"n_character_{i}", "kind": "data"}
            for i in range(1, 3)
        ],
    }


def test_authored_visual_style_reads_the_brief_section() -> None:
    assert authored_visual_style(_BRIEF) == _STYLE
    assert authored_visual_style("Visual Style: flat pastel cartoon\nmore") == "flat pastel cartoon"
    assert authored_visual_style("no style section") == ""


def test_turnaround_only_for_solo_text_drawn_sheets() -> None:
    assert is_turnaround_sheet({"role": "character"})
    assert not is_turnaround_sheet({"cast_form": "group"})
    assert not is_turnaround_sheet({"combined_cast": True})
    assert not is_turnaround_sheet({"reference_image_task": "medium_change"})


def test_solo_prompt_opens_with_the_film_style_and_asks_for_three_views() -> None:
    text = image_nodes._character_prompt("阿Q\n瘦长脸", style=_STYLE + "。")
    lines = text.splitlines()
    assert lines[0] == f"Art style shared by every character in this film: {_STYLE}."
    sheet = lines[1]
    assert "four views in one row from left to right on one continuous pure white background" in sheet
    panels = [
        sheet.index("(1) a head-and-shoulders close-up of the face, front view, as tall as the sheet"),
        sheet.index("(2) full-body front view"),
        sheet.index("(3) full-body side view, a profile facing the left edge of the image"),
        sheet.index("(4) full-body back view"),
    ]
    assert panels == sorted(panels)
    assert "open white space between the figures" in sheet
    assert "no frames and no dividing lines" in sheet
    assert "studio" not in text
    assert "Image" not in text
    assert text.endswith("阿Q\n瘦长脸")


def test_group_prompt_is_a_group_sheet_not_a_turnaround() -> None:
    text = image_nodes._character_prompt("村民\n四人", group=True, style=_STYLE)
    assert "Group sheet" in text
    assert "One person" not in text
    assert "back view" not in text
    assert "Image" not in text


def test_seed_sheet_prompt_is_a_turnaround_without_studio() -> None:
    text = compose_character_sheet_prompt(cfg={"character_name": "Fisherman"}, seed="")
    assert "three-view turnaround" in text
    assert text.index("head-and-shoulders close-up") < text.index("full-body front, side, and back")
    assert "facing the left edge" in text
    assert "pure white background" in text
    assert "no frames and no dividing lines" in text
    assert "neutral background" not in text
    assert "studio" not in text


def _png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"img")
    return path


@pytest.mark.asyncio
async def test_handler_opens_every_sheet_with_the_brief_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text(_BRIEF, encoding="utf-8")
    graph = _cast_graph()
    run = {"node_states": {"n_brief": {"output_ref": {"kind": "text", "uri": brief.as_uri()}}}}
    seen: dict[str, object] = {}

    async def fake_image(prompt, size="1K", reference_images=None, max_tries=2, **kwargs):
        seen["prompt"] = prompt
        seen["refs"] = list(reference_images or [])
        return {"image_path": str(_png(tmp_path / "out.png"))}

    monkeypatch.setattr(image_nodes.handler_io, "generate_designer_image", fake_image)
    node = graph["nodes"][2]
    node["config"]["prompt"] = "吴妈，圆脸，靛蓝布衫"
    await image_nodes.CharacterDesignNodeHandler().execute(
        node,
        NodeExecutionContext(graph=graph, run_id="run_style", node_id="n_character_2", run=run),
    )
    prompt = str(seen["prompt"])
    assert prompt.startswith(f"Art style shared by every character in this film: {_STYLE}.")
    assert "back view" in prompt
    assert seen["refs"] == []


_CLIP_GRAPH = {
    "nodes": [
        _sheet("n_character_1", character_ids=["char_1"], character_name="Fisherman"),
        _sheet("n_character_2", character_ids=["char_2"], character_name="Village Elders", cast_form="group"),
    ],
    "metadata": {
        "script_analysis": {
            "characters": [
                {"id": "char_1", "name": "Fisherman", "cast_form": "single"},
                {"id": "char_2", "name": "Village Elders", "cast_form": "group"},
            ],
        },
    },
}


def _clip_prompt(on_screen: list[str]) -> str:
    return compose_practice_prompt(
        cfg={
            "on_screen": on_screen,
            "clip_action": "They meet at the pier.",
            "scene_specs": {"scene_name": "river pier", "lighting": "morning mist"},
            "content_language": "en",
        },
        graph=_CLIP_GRAPH,
    )


def test_clip_prompt_says_a_turnaround_reference_is_one_person() -> None:
    text = _clip_prompt(["char_1", "char_2"])
    assert "Image 1: each is one person shown from the front, side, and back, with a head close-up" in text
    assert "Image 2: each" not in text


def test_clip_prompt_has_no_turnaround_line_for_group_cast_only() -> None:
    assert "front, side, and back" not in _clip_prompt(["char_2"])
