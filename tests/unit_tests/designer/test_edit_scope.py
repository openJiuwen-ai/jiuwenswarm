# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Attribute edits stay on the named object."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.edit_scope import (
    restore_document_attribute_scope,
    restore_graph_attribute_scope,
)

_MESSAGE = (
    "把第二镜和第三镜中同一只白色陶瓷咖啡杯统一改成红色陶瓷杯，"
    "更新相关场景、镜头描述、大纲和分镜表。"
    "第一镜、人物、动作、机位和各镜时长保持不变，仍为3镜15秒。只编辑，不执行或生成图片视频。"
)
_BEFORE = (
    "将深色咖啡缓慢倒入一只白色陶瓷杯，蒸汽升起；"
    "吧台木质台面、磨豆机与倒扣白杯。"
    "吧台上有磨豆机与几只倒扣的白色陶瓷杯。"
)
_SPILL = (
    "将深色咖啡缓慢倒入一只红色陶瓷杯，蒸汽升起；"
    "吧台木质台面、磨豆机与倒扣红杯。"
    "吧台上有磨豆机与几只倒扣的红色陶瓷杯。"
)
_KEPT = (
    "将深色咖啡缓慢倒入一只红色陶瓷杯，蒸汽升起；"
    "吧台木质台面、磨豆机与倒扣白杯。"
    "吧台上有磨豆机与几只倒扣的白色陶瓷杯。"
)


def _graph(prompt: str, objects: list[str] | None = None) -> dict:
    config = {"prompt": prompt, "pipeline": "scene"}
    if objects is not None:
        config["scene_specs"] = {"objects": objects}
    return {"nodes": [{"id": "n_scene_2", "type": "image", "config": config}], "edges": []}


def test_named_cup_changes_while_inverted_cups_keep_their_color() -> None:
    before = _graph(_BEFORE, ["白色陶瓷杯", "倒扣的白色陶瓷杯"])
    after = _graph(_SPILL, ["红色陶瓷杯", "倒扣的红色陶瓷杯"])
    restored = restore_graph_attribute_scope(before, after, _MESSAGE)
    prompt = restored["nodes"][0]["config"]["prompt"]
    objects = restored["nodes"][0]["config"]["scene_specs"]["objects"]
    assert "一只红色陶瓷杯" in prompt
    assert "倒扣白杯" in prompt
    assert "几只倒扣的白色陶瓷杯" in prompt
    assert "倒扣红" not in prompt
    assert objects == ["红色陶瓷杯", "倒扣的白色陶瓷杯"]


def test_already_scoped_edit_is_unchanged() -> None:
    before = _graph(_BEFORE)
    after = _graph(_KEPT)
    assert restore_graph_attribute_scope(before, after, _MESSAGE) is after


def test_request_that_names_the_inverted_cup_may_recolor_it() -> None:
    before = _graph("磨豆机与倒扣白杯")
    after = _graph("磨豆机与倒扣红杯")
    message = "把倒扣白杯改成红色，其他内容保持不变。"
    assert restore_graph_attribute_scope(before, after, message) is after


def test_recoloring_every_cup_is_not_limited_to_one_object() -> None:
    before = _graph("倒扣白杯")
    after = _graph("倒扣红杯")
    message = "把所有白色杯子统一改成红色杯子。"
    assert restore_graph_attribute_scope(before, after, message) is after


def test_storyboard_spill_is_restored_after_document_sync() -> None:
    before = _graph(_BEFORE)
    texts = {"n_storyboard": "Shot2 倒扣红杯，主杯是红色陶瓷杯。"}
    restored = restore_document_attribute_scope(before, {"n_storyboard": "Shot2 倒扣白杯"}, texts, _MESSAGE)
    assert restored["n_storyboard"] == "Shot2 倒扣白杯，主杯是红色陶瓷杯。"


def test_english_background_collection_keeps_its_color() -> None:
    message = "Change the white bicycle to red. Keep the camera."
    before = _graph("one white bicycle and several parked white bicycles")
    after = _graph("one red bicycle and several parked red bicycles")
    restored = restore_graph_attribute_scope(before, after, message)
    prompt = restored["nodes"][0]["config"]["prompt"]
    assert prompt == "one red bicycle and several parked white bicycles"


def test_verb_glued_to_the_named_cup_still_changes() -> None:
    before = _graph("倒入一只白色陶瓷杯。磨豆机与倒扣白杯。")
    before["description"] = "店员在吧台将咖啡倒入白色陶瓷杯。"
    after = _graph("倒入一只红色陶瓷杯。磨豆机与倒扣红杯。")
    after["description"] = "店员在吧台将咖啡倒入红色陶瓷杯。"
    restored = restore_graph_attribute_scope(before, after, _MESSAGE)
    assert restored["description"] == "店员在吧台将咖啡倒入红色陶瓷杯。"
    prompt = restored["nodes"][0]["config"]["prompt"]
    assert "一只红色陶瓷杯" in prompt
    assert "倒扣白杯" in prompt


def test_unrequested_duration_change_is_rolled_back() -> None:
    before = _graph("倒入一只白色陶瓷杯。每镜5秒。磨豆机与倒扣白杯。")
    after = _graph("倒入一只红色陶瓷杯。每镜8秒。磨豆机与倒扣红杯。")
    restored = restore_graph_attribute_scope(before, after, _MESSAGE)
    prompt = restored["nodes"][0]["config"]["prompt"]
    assert prompt == "倒入一只红色陶瓷杯。每镜5秒。磨豆机与倒扣白杯。"


def test_existing_qualified_phrase_keeps_the_new_color() -> None:
    before = _graph("倒扣白杯。面板上有倒扣红色按钮。一只白色陶瓷杯。")
    after = _graph("倒扣红杯。面板上有倒扣红色按钮。一只红色陶瓷杯。另外倒扣红杯。")
    restored = restore_graph_attribute_scope(before, after, _MESSAGE)
    prompt = restored["nodes"][0]["config"]["prompt"]
    assert prompt == "倒扣白杯。面板上有倒扣红色按钮。一只红色陶瓷杯。另外倒扣白杯。"


def test_existing_english_qualified_phrase_keeps_the_new_color() -> None:
    message = "Change the white bicycle to red. Keep the camera."
    before = _graph("parked red signs and one white bicycle and several parked white bicycles")
    after = _graph("parked red signs and one red bicycle and several parked red bicycles")
    restored = restore_graph_attribute_scope(before, after, message)
    prompt = restored["nodes"][0]["config"]["prompt"]
    assert prompt == "parked red signs and one red bicycle and several parked white bicycles"


def test_full_rewrite_is_left_to_the_model() -> None:
    message = "把白天街景改成霓虹夜景，整段重写这一镜的提示词。"
    before = _graph("白天街景，倒扣白杯")
    after = _graph("霓虹夜景，倒扣红杯")
    assert restore_graph_attribute_scope(before, after, message) is after
