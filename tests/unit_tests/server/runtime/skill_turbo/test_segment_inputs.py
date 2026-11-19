import copy
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from segment_state_fixtures import storage
from test_accelerator_manifest import declaration
from jiuwenswarm.server.runtime.skill_turbo.accelerator_manifest import AcceleratorSpec
from jiuwenswarm.server.runtime.skill_turbo.segment_inputs import (
    SegmentInputError, assemble_segment_inputs, finalize_segment_result,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger


def make_spec(*, ident="prep", properties=None, required=(), requires=(), facts=None):
    raw = declaration(ident, 10)
    raw["input_schema"]["properties"] = properties or {}
    raw["input_schema"]["required"] = list(required)
    raw["requires"] = list(requires)
    if facts is not None:
        raw["outputs"]["facts"] = facts
        raw["outputs"]["postconditions"] = [{"kind": "fact", "key": key} for key in facts]
    return AcceleratorSpec.model_validate(raw)


async def setup(storage, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    ledger = await SkillRunLedger.open(storage.session(), skill="demo-craft", workspace_base=str(workspace))
    ctx = {"metadata": {"output_dir": str(workspace)}, "skill_root": str(skill_root), "request_id": "req-1"}
    return ledger, ctx


async def test_five_layers_none_reserved_unknown_and_schema_projection(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    ctx["inputs"] = {"count": 1, "topic": "context", "request_id": "req-1"}
    ledger.data["facts"].update(count=2, topic="ledger", run_dir="must not win")
    spec = make_spec(properties={"count": {"type": "integer"}, "topic": {"type": "string"}}, required=["topic"])
    spec = spec.model_copy(update={"defaults": {"count": 4}})
    inputs, warnings = await assemble_segment_inputs(ctx, spec, {"count": 5}, ledger,
                                                    {"facts": {"count": 3, "run_dir": "must not win"}})
    assert inputs["count"] == 5 and inputs["topic"] == "ledger"
    assert inputs["run_dir"] == ledger.data["run_dir"] and inputs["request_id"] == "req-1"
    assert any(w["code"] == "fact_conflict" for w in warnings)
    inputs, _ = await assemble_segment_inputs(ctx, spec, {"count": None}, ledger, {"facts": {"count": 3}})
    assert inputs["count"] == 4
    for invalid in ({"unknown": 1}, {"run_dir": "override"}, {"count": "bad"}):
        with pytest.raises(SegmentInputError) as caught:
            await assemble_segment_inputs(ctx, spec, invalid, ledger)
        assert caught.value.status == "invalid_args"


async def test_required_from_verified_nonadjacent_multiple_producers(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data["facts"].update(prep_fact="first", outline_fact="second")
    ledger.data["facts_provenance"].update({
        "prep_fact": {"produced_by": "prep", "verified": True, "revision": 1},
        "outline_fact": {"produced_by": "outline", "verified": True, "revision": 2},
    })
    spec = make_spec(ident="export", properties={"prep_fact": {"type": "string"}, "outline_fact": {"type": "string"}},
                     required=["prep_fact", "outline_fact"], requires=[
                         {"kind": "ledger_fact", "key": "prep_fact", "producer": "prep"},
                         {"kind": "ledger_fact", "key": "outline_fact", "producer": "outline"}])
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    assert inputs["prep_fact"] == "first" and inputs["outline_fact"] == "second"
    ledger.data["facts_provenance"]["prep_fact"]["produced_by"] = "wrong"
    with pytest.raises(SegmentInputError, match="fact_provenance_mismatch"):
        await assemble_segment_inputs(ctx, spec, {}, ledger)
    soft = spec.model_copy(update={"requires": [spec.requires[0].model_copy(update={"level": "soft"})]})
    _, warnings = await assemble_segment_inputs(ctx, soft, {}, ledger)
    assert any(w["code"] == "fact_provenance_mismatch" for w in warnings)


async def test_explicit_files_producer_manual_edit_and_deletion(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    file = Path(ledger.data["run_dir"]) / "outline.md"
    file.write_text("original", encoding="utf-8")
    manual = Path(ledger.data["run_dir"]) / "manual.md"
    manual.write_text("manual", encoding="utf-8")
    allowed = make_spec(properties={"outline_path": {"type": "string"}},
                         requires=[{"kind": "argument", "key": "outline_path"}])
    inputs, _ = await assemble_segment_inputs(ctx, allowed, {"outline_path": "manual.md"}, ledger)
    assert inputs["outline_path"] == str(manual.resolve())
    ledger.data["facts"]["outline_path"] = str(file)
    ledger.data["facts_provenance"]["outline_path"] = {"produced_by": "outline", "verified": True, "revision": 1}
    ledger.data["artifacts"] = [{"path": str(file), "size": file.stat().st_size,
                                  "mtime_ns": file.stat().st_mtime_ns, "sha256": hashlib.sha256(b"original").hexdigest()}]
    strict = allowed.model_copy(update={"requires": [
        allowed.requires[0].model_copy(update={"kind": "ledger_fact", "producer": "outline"})]})
    with pytest.raises(SegmentInputError, match="fact_provenance_mismatch"):
        await assemble_segment_inputs(ctx, strict, {"outline_path": "manual.md"}, ledger)
    file.write_text("edited by ordinary tool", encoding="utf-8")
    inputs, warnings = await assemble_segment_inputs(ctx, strict, {"outline_path": "outline.md"}, ledger)
    assert Path(inputs["outline_path"]).read_text(encoding="utf-8") == "edited by ordinary tool"
    assert any(w["code"] == "artifact_modified" for w in warnings)
    file.unlink()
    with pytest.raises(SegmentInputError) as caught:
        await assemble_segment_inputs(ctx, strict, {}, ledger)
    assert caught.value.status == "blocked"
    assert any(w["code"] == "artifact_missing" for w in caught.value.warnings)
    assert not ledger.data["facts_provenance"]["outline_path"]["verified"]


async def test_workspace_no_fallback_and_real_junction_target(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec(properties={"source_path": {"type": "string"}})
    for replacement in ({}, {"metadata": {}, "effective_project_dir": ledger.data["workspace_base"]}):
        with pytest.raises(SegmentInputError, match="workspace_missing"):
            await assemble_segment_inputs(replacement, spec, {}, ledger)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("private", encoding="utf-8")
    link = Path(ledger.data["run_dir"]) / "linked"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SegmentInputError, match="path_outside_allowed_roots"):
        await assemble_segment_inputs(ctx, spec, {"source_path": "linked/data.txt"}, ledger)


async def test_partial_verifies_facts_individually_for_downstream(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec(properties={"a": {"type": "integer"}, "b": {"type": "integer"}}, facts=["a", "b"])
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    (Path(inputs["run_dir"]) / "prep.json").write_text("{}", encoding="utf-8")
    output = await finalize_segment_result(ctx, spec, {"status": "ok", "facts": {"a": 7, "b": "bad"}}, inputs, initial_inputs=copy.deepcopy(inputs))
    assert output["status"] == "partial" and output["facts"] == {"a": 7}
    assert output["facts_provenance"]["a"]["verified"] and not output["facts_provenance"]["b"]["verified"]
    ledger.record_segment(spec=spec, result=output, plan_code_hash="h")
    consumer = make_spec(ident="export", properties={"a": {"type": "integer"}}, requires=[
        {"kind": "ledger_fact", "key": "a", "producer": "prep"}])
    assert (await assemble_segment_inputs(ctx, consumer, {}, ledger))[0]["a"] == 7
    invalid_consumer = make_spec(ident="export", requires=[{"kind": "ledger_fact", "key": "b", "producer": "prep"}])
    with pytest.raises(SegmentInputError):
        await assemble_segment_inputs(ctx, invalid_consumer, {}, ledger)


@pytest.mark.parametrize("bad", ["x" * 2049, float("nan"), object()])
async def test_invalid_big_nonjson_and_undeclared_facts_are_not_submitted(storage, tmp_path, bad):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec(facts=["good", "bad"])
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    (Path(inputs["run_dir"]) / "prep.json").write_text("{}", encoding="utf-8")
    output = await finalize_segment_result(ctx, spec, {"facts": {"good": 1, "bad": bad, "unknown": "discard"}}, inputs, initial_inputs=copy.deepcopy(inputs))
    assert output["status"] == "partial" and output["facts"] == {"good": 1}
    assert output["facts_provenance"]["good"]["verified"] and not output["facts_provenance"]["bad"]["verified"]
    assert any(w["code"] == "fact_undeclared" for w in output["warnings"])


@pytest.mark.parametrize("status", ["failed", "blocked", "not_applicable", "paused", "cancelled"])
async def test_files_never_upgrade_business_failure_or_pause(storage, tmp_path, status):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec()
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    (Path(inputs["run_dir"]) / "prep.json").write_text("{}", encoding="utf-8")
    output = await finalize_segment_result(ctx, spec, {"status": status, "facts": {"prep_fact": "value"}}, inputs, initial_inputs=copy.deepcopy(inputs))
    assert output["status"] == status and not output["facts_provenance"]["prep_fact"]["verified"]


async def test_rerun_missing_output_does_not_reverify_old_input(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data["facts"]["prep_fact"] = "old"
    ledger.data["facts_provenance"]["prep_fact"] = {"produced_by": "prep", "verified": False, "revision": 1}
    spec = make_spec()
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    (Path(inputs["run_dir"]) / "prep.json").write_text("{}", encoding="utf-8")
    output = await finalize_segment_result(ctx, spec, {"status": "ok"}, inputs, initial_inputs=copy.deepcopy(inputs))
    assert output["status"] == "partial" and "prep_fact" not in output["facts"]
    ledger.record_segment(spec=spec, result=output, plan_code_hash="h")
    assert not ledger.data["facts_provenance"]["prep_fact"]["verified"]


async def test_handoff_path_must_match_declared_artifacts_and_exist(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec(facts=["result_path"])
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    declared = Path(inputs["run_dir"]) / "prep.json"
    declared.write_text("{}", encoding="utf-8")
    unrelated = Path(inputs["run_dir"]) / "unrelated.txt"
    unrelated.write_text("not an artifact", encoding="utf-8")
    output = await finalize_segment_result(ctx, spec, {"status": "ok"}, inputs,
        {"facts": {"result_path": str(unrelated)}, "artifacts": [str(unrelated)]}, initial_inputs=copy.deepcopy(inputs))
    assert output["status"] == "partial" and output["facts"] == {}
    assert [a["path"] for a in output["artifacts"]] == [str(declared)]
    assert any(w["code"] == "handoff_artifact_undeclared" for w in output["warnings"])


async def test_ordinary_save_failure_allows_verified_batch_facts_not_fake_cross_call(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    producer = make_spec(facts=["topic"])
    inputs, _ = await assemble_segment_inputs(ctx, producer, {}, ledger)
    (Path(inputs["run_dir"]) / "prep.json").write_text("{}", encoding="utf-8")
    output = await finalize_segment_result(ctx, producer, {"facts": {"topic": "actual"}}, inputs, initial_inputs=copy.deepcopy(inputs))
    ledger.record_segment(spec=producer, result=output, plan_code_hash="h")
    storage.cp.mode = "before"
    assert not await ledger.save(storage.session())
    consumer = make_spec(ident="outline", properties={"topic": {"type": "string"}}, required=["topic"],
        requires=[{"kind": "ledger_fact", "key": "topic", "producer": "prep"}])
    assert (await assemble_segment_inputs(ctx, consumer, {}, ledger, output))[0]["topic"] == "actual"
    storage.cp.mode = ""
    reloaded = await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=ledger.task_key)
    with pytest.raises(SegmentInputError, match="ledger_fact_missing"):
        await assemble_segment_inputs(ctx, consumer, {}, reloaded)


async def test_fact_updated_by_keeps_downstream_history_file_and_consumed_revision(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    producer = make_spec()
    initial = {"status": "ok", "facts": {"prep_fact": "old"}, "facts_provenance": {"prep_fact": {"verified": True}}}
    ledger.record_segment(spec=producer, result=initial, plan_code_hash="h")
    downstream = make_spec(ident="outline")
    ledger.record_segment(spec=downstream, result={"status": "ok", "consumed_facts": {"prep_fact": 1}}, plan_code_hash="o")
    downstream_before = copy.deepcopy(ledger.data["latest_runs"]["outline"])
    file = Path(ledger.data["run_dir"]) / "downstream.txt"
    file.write_text("consumed old", encoding="utf-8")
    assert await ledger.save(storage.session()) and await ledger.prepare_rerun(storage.session(), spec=producer)
    changed = {"status": "ok", "facts": {"prep_fact": "new"}, "facts_provenance": {"prep_fact": {"verified": True}}}
    ledger.record_segment(spec=producer, result=changed, plan_code_hash="h")
    assert changed["fact_updated_by"]["prep_fact"] == {"produced_by": "prep", "previous_revision": 1, "revision": 3}
    assert ledger.data["latest_runs"]["outline"] == downstream_before
    assert file.read_text(encoding="utf-8") == "consumed old"


async def test_conflicts_redact_secrets_and_large_objects(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data["facts"].update(api_token="secret", blob={"data": "large"})
    spec = make_spec(properties={"api_token": {"type": "string"}, "blob": {"type": "object"}})
    _, warnings = await assemble_segment_inputs(ctx, spec, {"api_token": "another secret", "blob": {"different": 1}}, ledger)
    assert warnings == [{"code": "fact_conflict", "key": "api_token", "ledger_value": "<redacted>", "explicit_value": "<redacted>"},
                        {"code": "fact_conflict", "key": "blob", "ledger_value": "<dict>", "explicit_value": "<dict>"}]


async def test_current_batch_path_overrides_missing_old_path(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    ledger.data["facts"]["outline_path"] = str(Path(ledger.data["run_dir"]) / "missing.md")
    ledger.data["facts_provenance"]["outline_path"] = {"produced_by": "outline", "verified": True, "revision": 1}
    current = Path(ledger.data["run_dir"]) / "current.md"
    current.write_text("current batch output", encoding="utf-8")
    upstream = {"facts": {"outline_path": str(current)}, "facts_provenance": {
        "outline_path": {"produced_by": "outline", "verified": True, "revision": 2}}}
    spec = make_spec(properties={"outline_path": {"type": "string"}}, requires=[
        {"kind": "ledger_fact", "key": "outline_path", "producer": "outline"}])
    assert (await assemble_segment_inputs(ctx, spec, {}, ledger, upstream))[0]["outline_path"] == str(current)


async def test_explicit_override_of_current_batch_fact_reports_conflict(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec(properties={"topic": {"type": "string"}})
    inputs, warnings = await assemble_segment_inputs(ctx, spec, {"topic": "manual"}, ledger,
                                                     {"facts": {"topic": "batch output"}})
    assert inputs["topic"] == "manual"
    assert warnings == [{"code": "fact_conflict", "key": "topic", "ledger_value": "batch output", "explicit_value": "manual"}]


async def test_workspace_file_and_capability_hard_soft_requirements(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec(requires=[{"kind": "workspace_file", "path_glob": "ready.json"},
                               {"kind": "capability", "key": "export", "level": "soft"}])
    with pytest.raises(SegmentInputError, match="artifact_missing"):
        await assemble_segment_inputs(ctx, spec, {}, ledger)
    (Path(ledger.data["run_dir"]) / "ready.json").write_text("{}", encoding="utf-8")
    _, warnings = await assemble_segment_inputs(ctx, spec, {}, ledger)
    assert warnings[0]["code"] == "capability_missing"
    ctx["capabilities"] = ["export"]
    assert (await assemble_segment_inputs(ctx, spec, {}, ledger))[1] == []


async def test_runtime_local_schema_ref_failure_is_invalid_args_without_network(storage, tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network access"))
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec(properties={"item": {"$ref": "#/$defs/missing"}})
    with pytest.raises(SegmentInputError) as caught:
        await assemble_segment_inputs(ctx, spec, {"item": 1}, ledger)
    assert caught.value.status == "invalid_args"


async def test_explicit_failure_status_and_malformed_facts_remain_honest(storage, tmp_path):
    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec()
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    (Path(inputs["run_dir"]) / "prep.json").write_text("{}", encoding="utf-8")
    output = await finalize_segment_result(ctx, spec, {"status": "blocked", "success": False, "facts": {}}, inputs, initial_inputs=copy.deepcopy(inputs))
    assert output["status"] == "blocked"
    malformed = await finalize_segment_result(ctx, spec, {"facts": "invalid"}, inputs, {"artifacts": None}, initial_inputs=copy.deepcopy(inputs))
    assert malformed["status"] == "partial" and not malformed["facts_provenance"]["prep_fact"]["verified"]


async def test_file_checks_run_off_event_loop_and_are_bounded(storage, tmp_path, monkeypatch):
    import asyncio
    import threading
    import time
    from jiuwenswarm.server.runtime.skill_turbo import segment_inputs as module

    ledger, ctx = await setup(storage, tmp_path)
    spec = make_spec()
    inputs, _ = await assemble_segment_inputs(ctx, spec, {}, ledger)
    (Path(inputs["run_dir"]) / "prep.json").write_text("{}", encoding="utf-8")
    original = module._file_record
    loop_thread = threading.get_ident()
    lock = threading.Lock()
    active, peak = 0, 0
    threads = set()

    def observed(path):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            threads.add(threading.get_ident())
        try:
            time.sleep(0.02)
            return original(path)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(module, "_file_io", asyncio.Semaphore(4))
    monkeypatch.setattr(module, "_file_record", observed)
    outputs = await asyncio.gather(*(finalize_segment_result(ctx, spec, {"facts": {"prep_fact": i}}, inputs,
                                   initial_inputs=copy.deepcopy(inputs)) for i in range(9)))
    assert all(output["status"] == "ok" for output in outputs)
    assert loop_thread not in threads and 1 < peak <= 4
