"""声明层正负例：通过真实文件加载，禁止执行入口。"""

import copy
import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import (
    load_accelerator_manifest,
    validate_accelerator_manifest,
)


def declaration(ident="prep", order=10):
    return {
        "id": ident, "order": order, "summary": "准备",
        "when_to_use": ["新任务"], "when_not_to_use": ["不适用"],
        "covers": {"stages": [ident], "steps": [ident]},
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputs": {"facts": [ident + "_fact"], "artifacts": [{"path_glob": ident + ".json", "kind": "state"}],
                    "postconditions": [{"kind": "fact", "key": ident + "_fact"}]},
        "entry": {"module": "accel_" + ident, "attr": "root"},
    }


def make_skill(root, *, internal="demo", external="demo-craft", items=None):
    skill = root / external
    codes = skill / "turbo/turbo_codes" / internal
    codes.mkdir(parents=True)
    (codes / (internal + "_gen_root.py")).write_text(
        "from skill_turbo_runtime import PlanNode\nroot = PlanNode(plan_name='r', instruction='i')\n",
        encoding="utf-8",
    )
    items = [declaration()] if items is None else items
    for item in items:
        module = item["entry"]["module"]
        if module.isidentifier():
            (codes / (module + ".py")).write_text("raise RuntimeError('must not import')\n", encoding="utf-8")
    meta = {"external_name": external, "description": "demo", "match_keywords": ["demo"]}
    (skill / "turbo/meta.json").write_text(json.dumps(meta), encoding="utf-8")
    raw = {"manifest_version": 1, "skill": external, "skill_name": internal, "accelerators": items}
    (skill / "turbo/accelerators.json").write_text(json.dumps(raw), encoding="utf-8")
    return skill, meta, raw


def load(skill, meta):
    return load_accelerator_manifest(skill, skill_name="demo", meta=meta)


def test_file_precedence_inline_and_no_manifest(tmp_path):
    skill, meta, raw = make_skill(tmp_path)
    assert [x.id for x in load(skill, meta)] == ["prep"]
    meta["accelerators"] = [declaration("inline")]
    file = skill / "turbo/accelerators.json"
    file.write_text("{bad", encoding="utf-8")
    assert load(skill, meta) == ()
    file.unlink()
    (skill / "turbo/turbo_codes/demo/accel_inline.py").write_text("root=None", encoding="utf-8")
    assert [x.id for x in load(skill, meta)] == ["inline"]
    del meta["accelerators"]
    assert load(skill, meta) == ()


@pytest.mark.parametrize("key,value", [("manifest_version", True), ("manifest_version", 2), ("skill", "wrong"), ("skill_name", "other")])
def test_top_level_fail_closed(tmp_path, key, value):
    skill, meta, raw = make_skill(tmp_path)
    raw[key] = value
    (skill / "turbo/accelerators.json").write_text(json.dumps(raw), encoding="utf-8")
    assert load(skill, meta) == ()
    assert validate_accelerator_manifest(raw, skill="demo-craft", skill_name="demo")


@pytest.mark.parametrize("mutation", [
    {"id": "__full__"}, {"order": True}, {"entry": {"module": "a.b", "attr": "root"}},
    {"entry": {"module": "class", "attr": "root"}},
    {"outputs": {"facts": ["run_dir"], "artifacts": [], "postconditions": []}},
    {"input_schema": {"type": "object", "$ref": "https://invalid.example/schema"}},
    {"input_schema": {"type": "object", "$dynamicRef": "other.json"}},
    {"defaults": {"run_dir": "bad"}},
])
def test_independent_bad_item_removed(tmp_path, mutation, caplog):
    bad = declaration("bad", 20)
    bad.update(mutation)
    skill, meta, _ = make_skill(tmp_path, items=[declaration(), bad])
    assert [x.id for x in load(skill, meta)] == ["prep"]
    assert caplog.records


@pytest.mark.parametrize("conflict", ["id", "order", "entry", "stages", "steps", "facts"])
def test_cross_item_conflict_rejects_whole_manifest(tmp_path, conflict):
    a, b, c = declaration(), declaration("outline", 20), declaration("unrelated", 30)
    if conflict in {"id", "order", "entry"}:
        b[conflict] = copy.deepcopy(a[conflict])
    elif conflict == "facts":
        b["outputs"] = copy.deepcopy(a["outputs"])
    else:
        b["covers"][conflict] = a["covers"][conflict][:]
    skill, meta, _ = make_skill(tmp_path, items=[a, b, c])
    assert load(skill, meta) == ()


@pytest.mark.parametrize("case", ["missing", "disabled", "later", "wrong_fact", "missing_entry"])
def test_producer_rechecked_after_removal(tmp_path, case):
    a, b = declaration(), declaration("outline", 20)
    b["requires"] = [{"kind": "ledger_fact", "key": "prep_fact", "producer": "prep"}]
    if case == "missing":
        b["requires"][0]["producer"] = "absent"
    elif case == "disabled":
        a["enabled"] = False
    elif case == "later":
        a["order"] = 30
    elif case == "wrong_fact":
        b["requires"][0]["key"] = "unknown"
    skill, meta, _ = make_skill(tmp_path, items=[a, b])
    if case == "missing_entry":
        (skill / "turbo/turbo_codes/demo/accel_prep.py").unlink()
    assert load(skill, meta) == ()


@pytest.mark.parametrize("field,value", [("artifacts", None), ("artifacts", []), ("postconditions", None), ("postconditions", [])])
def test_outputs_both_required(tmp_path, field, value):
    item = declaration()
    if value is None:
        del item["outputs"][field]
    else:
        item["outputs"][field] = value
    skill, meta, _ = make_skill(tmp_path, items=[item])
    assert load(skill, meta) == ()


def test_nonadjacent_producer_and_defaults(tmp_path):
    a, b, c = declaration(), declaration("middle", 20), declaration("outline", 30)
    c["requires"] = [{"kind": "ledger_fact", "key": "prep_fact", "producer": "prep"}]
    c["hitl"] = {"kind": "review"}
    skill, meta, _ = make_skill(tmp_path, items=[c, a, b])
    specs = load(skill, meta)
    assert [x.id for x in specs] == ["prep", "middle", "outline"]
    assert specs[-1].hitl.continue_after_resume is False
    assert specs[-1].hitl.can_pause is True
    assert specs[0].plan_code.endswith(".demo.accel_prep import root")


@pytest.mark.parametrize("change", ["schema_type", "required", "default_unknown", "default_type", "default_range", "duplicate_fact", "undeclared_fact", "traversal", "absolute"])
def test_schema_and_output_item_errors(tmp_path, change):
    bad = declaration("bad", 20)
    bad["input_schema"] = {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}}}
    if change == "schema_type":
        bad["input_schema"]["properties"]["count"]["type"] = "not_a_type"
    elif change == "required":
        bad["input_schema"]["required"] = ["missing"]
    elif change.startswith("default"):
        bad["defaults"] = {"missing": 1} if change == "default_unknown" else {"count": "bad" if change == "default_type" else 0}
    elif change == "duplicate_fact":
        bad["outputs"]["facts"] *= 2
    elif change == "undeclared_fact":
        bad["outputs"]["postconditions"][0]["key"] = "missing"
    else:
        bad["outputs"]["artifacts"][0]["path_glob"] = "../secret" if change == "traversal" else "C:\\secret"
    skill, meta, _ = make_skill(tmp_path, items=[declaration(), bad])
    assert [s.id for s in load(skill, meta)] == ["prep"]


def test_local_ref_defaults_and_empty_facts_do_not_require_task_file(tmp_path):
    item = declaration()
    item["input_schema"] = {"type": "object", "properties": {"count": {"$ref": "#/$defs/count"}},
                            "$defs": {"count": {"type": "integer", "minimum": 1}}}
    item["defaults"] = {"count": 2}
    item["outputs"]["facts"] = []
    item["outputs"]["postconditions"] = [{"kind": "workspace_file", "path_glob": "future.json"}]
    skill, meta, raw = make_skill(tmp_path, items=[item])
    assert load(skill, meta)[0].defaults == {"count": 2}
    assert validate_accelerator_manifest(raw, skill="demo-craft", skill_name="demo") == []


def test_disabled_owner_not_conflict_but_duplicate_id_still_fatal(tmp_path):
    a, b = declaration(), declaration("disabled", 20)
    b["enabled"] = False
    b["outputs"] = copy.deepcopy(a["outputs"])
    b["covers"] = copy.deepcopy(a["covers"])
    skill, meta, raw = make_skill(tmp_path, items=[a, b])
    assert len(load(skill, meta)) == 2
    raw["accelerators"][1]["id"] = "prep"
    (skill / "turbo/accelerators.json").write_text(json.dumps(raw), encoding="utf-8")
    assert load(skill, meta) == ()


def test_meta_only_extensions_retains_old_semantics(tmp_path):
    from jiuwenswarm.server.runtime.skill_turbo.skill_meta import load_skill_meta

    skill, _, raw = make_skill(tmp_path)
    (skill / "turbo/accelerators.json").unlink()
    (skill / "turbo/meta.json").write_text(json.dumps({"accelerators": raw["accelerators"]}), encoding="utf-8")
    meta = load_skill_meta("demo-craft", skill / "turbo")
    assert meta == {}
    assert load(skill, meta) == ()


def test_schema_external_references_never_retrieve_network(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network access"))
    for key in ("$ref", "$dynamicRef", "$recursiveRef"):
        item = declaration()
        item["input_schema"][key] = "https://invalid.example/schema"
        skill, meta, _ = make_skill(tmp_path / key[1:], items=[item])
        assert load(skill, meta) == ()


def test_schema_id_and_local_refs_validate_without_network(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network access"))
    item = declaration()
    item["input_schema"] = {
        "$id": "https://invalid.example/root",
        "type": "object",
        "properties": {"id": {"$ref": "#/$defs/count"}},
        "$defs": {"count": {"type": "integer", "minimum": 1}},
    }
    item["defaults"] = {"id": 2}
    skill, meta, _ = make_skill(tmp_path, items=[item])
    assert load(skill, meta)[0].defaults == {"id": 2}


def test_rebased_unresolvable_local_ref_discards_item_without_network(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network access"))
    item = declaration("bad", 20)
    item["input_schema"] = {
        "type": "object",
        "properties": {"count": {"$id": "https://invalid.example/other", "$ref": "#/$defs/count"}},
        "$defs": {"count": {"type": "integer"}},
    }
    item["defaults"] = {"count": 2}
    skill, meta, _ = make_skill(tmp_path, items=[declaration(), item])
    assert [s.id for s in load(skill, meta)] == ["prep"]
