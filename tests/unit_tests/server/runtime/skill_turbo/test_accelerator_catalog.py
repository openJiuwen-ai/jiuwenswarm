"""从真实目录经过发现、注册到只读索引及展示快照。"""

import copy
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_accelerator_manifest import declaration, make_skill
from jiuwenswarm.server.runtime.skill_turbo import turbo_package_loader as loader
from jiuwenswarm.server.runtime.skill_turbo.accelerator_registry import AcceleratorRegistry
from jiuwenswarm.server.runtime.skill_turbo.environment import Skill, SkillTurboEnvironment


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill_turbo import skill_turbo_tools as tools
    import jiuwenswarm.common.utils as utils

    monkeypatch.setattr(utils, "resolve_agent_registered_skill_dirs", lambda: [tmp_path])
    monkeypatch.setattr(SkillTurboEnvironment, "_external_scan_cache", {})
    monkeypatch.setattr(SkillTurboEnvironment, "_scan_cache", {})
    monkeypatch.setattr(tools, "_entries_cache", {})
    monkeypatch.setattr(tools, "_last_good_catalogs", {})
    monkeypatch.setattr(tools, "_catalog_failed_roots", set())
    monkeypatch.setattr(tools, "_last_good_entries", {})
    monkeypatch.setattr(tools, "_last_good_roots", None)
    description = tools.skill_turbo.card.description
    modules = {k: v for k, v in sys.modules.items() if k.startswith("skill_turbo_codes_")}
    monkeypatch.setattr(loader, "_PACKAGE_REGISTRY", {})
    monkeypatch.setattr(loader, "_PACKAGE_SKILL_NAMES", {})
    yield tools
    tools.skill_turbo.card.description = description
    for name in list(sys.modules):
        if name.startswith("skill_turbo_codes_"):
            sys.modules.pop(name)
    sys.modules.update(modules)


def env():
    return SkillTurboEnvironment({"skill_codes_dir": "", "skill_code_import_package": ""})


def test_catalog_changes_preserve_legacy_public_tool_schema(isolated):
    schema = isolated.skill_turbo.card.input_params
    assert set(schema['properties']) == {'query', 'fresh_start'}
    assert schema['required'] == ['query']


def test_description_recovery_failure_is_logged_and_clears_catalog(monkeypatch, isolated, caplog):
    monkeypatch.setattr(isolated, '_collect_skill_entries', Mock(side_effect=OSError('initial scan failed')))
    monkeypatch.setattr(isolated, '_resolve_skill_roots', Mock(side_effect=OSError('roots recovery failed')))
    assert isolated.refresh_skill_acceleration_description() == []
    assert 'roots recovery failed' in caplog.text
    assert '当前内部支持：暂无' in isolated.skill_turbo.card.description


def rewrite(skill, raw):
    (skill / "turbo/accelerators.json").write_text(json.dumps(raw), encoding="utf-8")


def test_description_unexpected_failure_never_keeps_another_source(tmp_path, monkeypatch, isolated):
    import jiuwenswarm.common.utils as utils

    make_skill(tmp_path)
    assert isolated.refresh_skill_acceleration_description() == ["demo-craft"]
    old_description = isolated.skill_turbo.card.description
    monkeypatch.setattr(isolated, "_collect_skill_entries", Mock(side_effect=RuntimeError("scan failed")))
    assert isolated.refresh_skill_acceleration_description() == ["demo-craft"]
    assert isolated.skill_turbo.card.description == old_description

    monkeypatch.setattr(utils, "resolve_agent_registered_skill_dirs", lambda: [tmp_path / "different"])
    assert isolated.refresh_skill_acceleration_description() == []
    assert "demo-craft" not in isolated.skill_turbo.card.description


def test_catalog_resolves_roots_once_for_metadata_and_discovery(tmp_path, monkeypatch, isolated):
    import jiuwenswarm.common.utils as utils

    make_skill(tmp_path)
    resolver = Mock(side_effect=[[tmp_path], [tmp_path / "different"]])
    monkeypatch.setattr(utils, "resolve_agent_registered_skill_dirs", resolver)
    catalog = isolated.collect_accelerator_catalog()
    assert resolver.call_count == 1
    assert catalog.ok and "demo-craft" in catalog.accelerators


def test_real_discovery_registration_registry(tmp_path, monkeypatch, isolated):
    skill, meta, raw = make_skill(tmp_path)
    found = loader.discover_external_turbo_skills([tmp_path])
    assert found[0].accelerators[0].id == "prep"
    environment = env()
    assert environment.skills["demo"].accelerators == found[0].accelerators
    registry = AcceleratorRegistry(environment)
    assert [s.id for s in registry.list_for("demo-craft")] == ["prep"]
    assert registry.get("absent", "prep") is None
    assert registry.get("demo-craft", "unknown") is None
    assert registry.list_for("absent") == ()
    from jiuwenswarm.server.runtime.skill_turbo.planner import SkillTurboPlanner

    expected = SkillTurboPlanner(environment).build_plan_code("demo")
    monkeypatch.setattr(SkillTurboPlanner, "plan", Mock(side_effect=AssertionError("LLM forbidden")))
    monkeypatch.setattr(SkillTurboPlanner, "match_skill", Mock(side_effect=AssertionError("LLM forbidden")))
    monkeypatch.setattr(Path, "stat", Mock(side_effect=AssertionError("registry must not read disk")))
    assert registry.get("demo-craft", "__full__").plan_code == expected
    assert "__full__" not in [s.id for s in registry.list_for("demo-craft")]


def test_old_constructors_bad_manifest_and_legacy(tmp_path, isolated):
    assert Skill("demo", "d", "").accelerators == ()
    assert loader.ExternalTurboSkill("demo", "demo-craft", "codes").accelerators == ()
    skill, _, _ = make_skill(tmp_path)
    (skill / "turbo/accelerators.json").write_text("bad", encoding="utf-8")
    environment = env()
    assert environment.skills["demo"].accelerators == ()
    assert environment.skills["demo"].manifest_valid is False
    assert AcceleratorRegistry(environment).get("demo-craft", "__full__") is not None


def test_metadata_changes_not_masked_by_future_python(tmp_path, isolated):
    skill, _, raw = make_skill(tmp_path)
    root_file = skill / "turbo/turbo_codes/demo/demo_gen_root.py"
    os.utime(root_file, (4_000_000_000, 4_000_000_000))
    old_max = SkillTurboEnvironment._compute_skill_codes_mtime(skill / "turbo/turbo_codes")
    assert env().skills["demo"].accelerators[0].summary == "准备"
    assert isolated.collect_accelerator_catalog().accelerators["demo-craft"][0].summary == "准备"
    raw["accelerators"][0]["summary"] = "新的说明"
    rewrite(skill, raw)
    assert SkillTurboEnvironment._compute_skill_codes_mtime(skill / "turbo/turbo_codes") == old_max
    assert env().skills["demo"].accelerators[0].summary == "新的说明"
    assert isolated.collect_accelerator_catalog().accelerators["demo-craft"][0].summary == "新的说明"


def test_roots_reordering_selects_new_source(tmp_path, monkeypatch, isolated):
    import jiuwenswarm.common.utils as utils

    a, b = tmp_path / "a", tmp_path / "b"
    assert frozenset([a, b]) == frozenset([b, a])
    s1, _, r1 = make_skill(a)
    s2, _, r2 = make_skill(b)
    r2["accelerators"][0]["summary"] = "来源B"
    rewrite(s2, r2)
    meta = s2 / "turbo/meta.json"
    meta.write_text(json.dumps({"external_name": "demo-craft", "description": "source B"}), encoding="utf-8")
    monkeypatch.setattr(utils, "resolve_agent_registered_skill_dirs", lambda: [a, b])
    assert env().skills["demo"].accelerators[0].summary == "准备"
    assert "source B" not in isolated._collect_skill_entries()[0]["demo-craft"]
    monkeypatch.setattr(utils, "resolve_agent_registered_skill_dirs", lambda: [b, a])
    assert AcceleratorRegistry(env()).get("demo-craft", "prep").summary == "来源B"
    snapshot = isolated.collect_accelerator_catalog()
    assert "source B" in snapshot.entries["demo-craft"]
    assert snapshot.accelerators["demo-craft"][0].summary == "来源B"
    assert str(b) in snapshot.sources["demo-craft"]


@pytest.mark.parametrize("inline", [False, True])
def test_delete_add_manifest_switches_source(tmp_path, isolated, inline):
    skill, meta, raw = make_skill(tmp_path)
    file = skill / "turbo/accelerators.json"
    if inline:
        item = declaration("inline", 20)
        (skill / "turbo/turbo_codes/demo/accel_inline.py").write_text("root=None", encoding="utf-8")
        meta["accelerators"] = [item]
        (skill / "turbo/meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert isolated.collect_accelerator_catalog().accelerators["demo-craft"][0].id == "prep"
    file.unlink()
    expected = ["inline"] if inline else []
    assert [s.id for s in isolated.collect_accelerator_catalog().accelerators["demo-craft"]] == expected
    assert [s.id for s in env().skills["demo"].accelerators] == expected
    rewrite(skill, raw)
    assert isolated.collect_accelerator_catalog().accelerators["demo-craft"][0].id == "prep"


def test_io_failure_keeps_only_same_source_display_and_recovers(tmp_path, monkeypatch, isolated):
    make_skill(tmp_path)
    before = isolated.collect_accelerator_catalog()
    original = loader.external_metadata_snapshot
    monkeypatch.setattr(loader, "external_metadata_snapshot", Mock(side_effect=PermissionError("IO unavailable")))
    failed = isolated.collect_accelerator_catalog()
    assert failed.ok is False and failed.entries == before.entries
    assert failed.collected_at == before.collected_at
    assert failed.sources == before.sources and failed.failed_at is not None
    assert env().skills == {}
    assert AcceleratorRegistry(env()).ok is False
    import jiuwenswarm.common.utils as utils
    monkeypatch.setattr(utils, "resolve_agent_registered_skill_dirs", lambda: [tmp_path / "other"])
    assert isolated.collect_accelerator_catalog().entries == {}
    monkeypatch.setattr(utils, "resolve_agent_registered_skill_dirs", lambda: [tmp_path])
    monkeypatch.setattr(loader, "external_metadata_snapshot", original)
    recovered = isolated.collect_accelerator_catalog()
    assert recovered.ok and recovered.collected_at >= before.collected_at


@pytest.mark.parametrize("mode", ["disabled", "invalid", "turbo_off"])
def test_reliable_shutdown_not_revived_by_last_good(tmp_path, isolated, mode):
    skill, meta, raw = make_skill(tmp_path)
    isolated.refresh_skill_acceleration_description()
    if mode == "disabled":
        raw["enabled"] = False
        rewrite(skill, raw)
    elif mode == "invalid":
        rewrite(skill, [])
    else:
        meta["enable_turbo"] = False
        (skill / "turbo/meta.json").write_text(json.dumps(meta), encoding="utf-8")
    snapshot = isolated.collect_accelerator_catalog()
    assert snapshot.ok
    assert not snapshot.accelerators.get("demo-craft")
    if mode == "invalid":
        assert snapshot.manifest_valid["demo-craft"] is False
    if mode == "turbo_off":
        assert "demo-craft" not in isolated.refresh_skill_acceleration_description()
        assert env().skills == {}


def test_entry_delete_restore_and_cache_isolation(tmp_path, monkeypatch, isolated):
    skill, _, _ = make_skill(tmp_path)
    environment = env()
    snapshot = isolated.collect_accelerator_catalog()
    snapshot.accelerators["demo-craft"][0].input_schema["properties"]["bad"] = {}
    environment.skills["demo"].accelerators[0].input_schema["properties"]["other"] = {}
    assert env().skills["demo"].accelerators[0].input_schema["properties"] == {}
    assert isolated.collect_accelerator_catalog().accelerators["demo-craft"][0].input_schema["properties"] == {}
    entry = skill / "turbo/turbo_codes/demo/accel_prep.py"
    entry.unlink()
    assert isolated.collect_accelerator_catalog().accelerators["demo-craft"] == ()
    assert env().skills["demo"].accelerators == ()
    entry.write_text("root=None", encoding="utf-8")
    assert len(env().skills["demo"].accelerators) == 1


def test_unchanged_source_skips_parse_and_safety_scan(tmp_path, monkeypatch, isolated):
    make_skill(tmp_path)
    env()
    isolated.collect_accelerator_catalog()
    monkeypatch.setattr(loader, "load_manifest_record", Mock(side_effect=AssertionError("must use cache")))
    monkeypatch.setattr(SkillTurboEnvironment, "_validate_skill_code_dir", Mock(side_effect=AssertionError("no AST repeat")))
    assert env().skills["demo"].accelerators
    assert isolated.collect_accelerator_catalog().ok


def test_external_name_ambiguity_and_safety_rejection(tmp_path, isolated, caplog):
    a, _, _ = make_skill(tmp_path)
    b, meta, _ = make_skill(tmp_path, internal="other", external="other-craft")
    meta["external_name"] = "demo-craft"
    (b / "turbo/meta.json").write_text(json.dumps(meta), encoding="utf-8")
    registry = AcceleratorRegistry(env())
    assert registry.list_for("demo-craft") == ()
    assert registry.get("demo-craft", "__full__") is None
    assert "歧义" in caplog.text
    (a / "turbo/turbo_codes/demo/accel_prep.py").write_text("import subprocess\nroot=None", encoding="utf-8")
    assert "demo" not in env().skills




def test_real_directory_io_failure_and_read_failure_do_not_register_stale(tmp_path, monkeypatch, isolated):
    skill, _, raw = make_skill(tmp_path)
    before = isolated.collect_accelerator_catalog()
    env()
    original_iterdir = Path.iterdir

    def deny_directory(path):
        if path == tmp_path:
            raise PermissionError("injected directory IO")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", deny_directory)
    failed = isolated.collect_accelerator_catalog()
    assert not failed.ok and failed.sources == before.sources
    assert env().skills == {}
    monkeypatch.setattr(Path, "iterdir", original_iterdir)
    assert isolated.collect_accelerator_catalog().ok
    env()
    key = (str(tmp_path.resolve()),)
    cached = isolated._entries_cache[key]
    isolated._entries_cache[key] = replace(cached, collected_at=cached.collected_at - 61)
    state = SkillTurboEnvironment._external_scan_cache[key]
    SkillTurboEnvironment._external_scan_cache[key] = (state[0], state[1], state[2] - 61)
    original_read = Path.read_text

    def deny_manifest(path, *args, **kwargs):
        if path == skill / "turbo/accelerators.json":
            raise PermissionError("injected manifest read")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_manifest)
    failed = isolated.collect_accelerator_catalog()
    assert not failed.ok and failed.collected_at == cached.collected_at
    environment = env()
    assert environment.skills == {} and environment.external_scan_ok is False
    monkeypatch.setattr(Path, "read_text", original_read)
    raw["enabled"] = False
    rewrite(skill, raw)
    recovered = isolated.collect_accelerator_catalog()
    assert recovered.ok and recovered.accelerators["demo-craft"] == ()
    assert env().skills["demo"].accelerators == ()


def test_ttl_rereads_unchanged_stats_and_registry_filters_disabled(tmp_path, monkeypatch, isolated):
    disabled = declaration("disabled", 20)
    disabled["enabled"] = False
    make_skill(tmp_path, items=[declaration(), disabled])
    snapshot = isolated.collect_accelerator_catalog()
    key = (str(tmp_path.resolve()),)
    isolated._entries_cache[key] = replace(snapshot, collected_at=snapshot.collected_at - 61)
    original = loader.load_manifest_record
    counted = Mock(wraps=original)
    monkeypatch.setattr(loader, "load_manifest_record", counted)
    assert isolated.collect_accelerator_catalog().ok
    assert counted.call_count == 1
    registry = AcceleratorRegistry(env())
    assert [s.id for s in registry.list_for("demo-craft")] == ["prep"]
    assert registry.get("demo-craft", "disabled") is None
    result = registry.list_for("demo-craft")
    result[0].input_schema["properties"]["mutated"] = {}
    assert registry.list_for("demo-craft")[0].input_schema["properties"] == {}
