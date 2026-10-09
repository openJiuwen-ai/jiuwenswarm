"""Legacy and canonical writers share storage without reviving deleted rules."""
import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common.file_guard_config import update_file_guard_config
from jiuwenswarm.agents.harness.common.rails.security_lists import store
from jiuwenswarm.agents.harness.common.rails.security_lists.legacy_compat import (
    MARKER, guard_view, update_guard, file_rules_for_enforcement,
)
from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    DuplicateRecordError, SecurityListRecord,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("permissions:\n  enabled: true\n  file_guard:\n    enabled: true\n    paths: []\n  net_guard:\n    enabled: true\n    urls: {}\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    return path


@pytest.mark.parametrize("kind,name,field,value,changed", [
    ("domain", "net_guard", "urls", {"old.example": "deny"}, {"old.example": "allow"}),
    ("file_path", "file_guard", "paths", [{"path": "C:/private", "write": "deny"}], [{"path": "C:/private", "write": "allow"}]),
])
def test_cross_writer_update_delete_does_not_revive(cfg, kind, name, field, value, changed):
    update_guard(name, {field: value})
    record = store.get_security_lists()["user"][0]
    rid = record.id
    record.cells = {"*": {"*" if kind == "domain" else "write": "allow"}}
    store.upsert_record(record)
    assert guard_view(name)[field] == changed
    update_guard(name, {field: value})
    assert store.get_security_lists()["user"][0].id == rid
    assert store.delete_record(rid)
    assert guard_view(name)[field] == ({} if kind == "domain" else [])
    update_guard(name, {field: guard_view(name)[field]})
    assert store.get_security_lists()["user"] == []
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert raw["permissions"][name][field] == ({} if kind == "domain" else [])


def test_bootstrap_is_atomic_and_retains_backup(cfg):
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["permissions"]["net_guard"]["urls"] = {"old.example": "deny"}
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = cfg.read_bytes()
    assert guard_view("net_guard")["urls"] == {"old.example": "deny"}
    assert cfg.read_bytes() == before  # GET has no migration side effect.
    with pytest.raises(ValueError):
        update_guard("net_guard", {"urls": {"https://bad.example/private": "deny"}})
    assert cfg.read_bytes() == before
    store.set_defaults({})
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert raw["permissions"]["net_guard"]["urls"] == {}
    assert raw["security_lists"]["migrations"][MARKER]["backup"]["net_guard"] == {"old.example": "deny"}
    assert guard_view("net_guard")["urls"] == {"old.example": "deny"}


@pytest.mark.parametrize("cells,enabled", [({"default": {"*": "deny"}}, True), ({"*": {"*": "deny"}}, False)])
def test_old_save_preserves_unrepresentable_records(cfg, cells, enabled):
    rec = store.upsert_record(SecurityListRecord(type="domain", pattern="advanced.example", match="exact", cells=cells, enabled=enabled))
    view = guard_view("net_guard")
    assert view["urls"] == {}
    assert view["readonly_rules"][0]["id"] == rec.id
    update_guard("net_guard", {"urls": {"simple.example": "allow"}})
    assert any(r.id == rec.id and r.cells == cells for r in store.get_security_lists()["user"])
    before = cfg.read_bytes()
    with pytest.raises(DuplicateRecordError):
        update_guard("net_guard", {"urls": {"advanced.example": "allow"}})
    assert cfg.read_bytes() == before


def test_independent_file_axes_cannot_be_flattened(cfg):
    rec = store.upsert_record(SecurityListRecord(type="file_path", pattern="C:/data", match="prefix", cells={"*": {"read": "deny", "write": "allow"}}))
    assert guard_view("file_guard")["paths"] == []
    update_file_guard_config({"paths": []})
    assert store.get_security_lists()["user"][0].id == rec.id


def test_controls_only_toggle_owned_records(cfg):
    update_guard("net_guard", {"urls": {"old.example": "deny"}})
    new = store.upsert_record(SecurityListRecord(type="domain", pattern="new.example", match="exact", cells={"*": {"*": "deny"}}))
    update_guard("net_guard", {"enabled": False})
    records = {r.id: r for r in store.get_security_lists()["user"]}
    assert records[new.id].enabled
    assert not next(r for r in records.values() if r.pattern == "old.example").enabled
    assert guard_view("net_guard", owned_only=True)["urls"] == {}
    assert guard_view("net_guard")["urls"]["old.example"] == "deny"
    update_guard("net_guard", {"enabled": True})
    assert all(r.enabled for r in store.get_security_lists()["user"])


def test_approval_entries_survive_old_whole_list_save(cfg):
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    approval = {"path": "C:/approved", "read": "allow", "mode": "default", "created_at": "2026-10-09"}
    raw["permissions"]["file_guard"]["paths"] = [approval]
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    update_file_guard_config({"paths": [{"path": "C:/other", "write": "deny"}]})
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["permissions"]["file_guard"]["paths"] == [approval]
    assert all(r.pattern != "C:/approved" for r in store.get_security_lists()["user"])


def test_file_engine_projection_resolves_current_mode_without_storage_copy(cfg):
    rec = store.upsert_record(SecurityListRecord(type="file_path", pattern="C:/data", match="prefix", cells={"default": {"write": "deny"}, "auto_approve": {"write": "allow"}}))
    before = cfg.read_bytes()
    for mode, expected in [("default", "deny"), ("auto_approve", "allow")]:
        result = file_rules_for_enforcement({"file_guard": {"enabled": True, "paths": []}}, mode=mode)
        assert result["file_guard"]["paths"] == [{"path": rec.pattern, "match": "prefix", "write": expected}]
    assert cfg.read_bytes() == before


def test_legacy_read_deny_retains_three_axis_contract(cfg):
    update_file_guard_config({"paths": [{"path": "C:/data", "read": "deny"}]})
    assert store.get_security_lists()["user"][0].cells == {"*": {"read": "deny", "write": "deny", "exec": "deny"}}


def test_canonical_generic_get_save_keeps_id_cells_and_metadata(cfg):
    rec = store.upsert_record(SecurityListRecord(type="domain", pattern="new.example", match="exact", note="keep", cells={"*": {"*": "deny"}}))
    update_guard("net_guard", {"urls": guard_view("net_guard")["urls"]})
    assert store.get_security_lists()["user"][0] == rec
