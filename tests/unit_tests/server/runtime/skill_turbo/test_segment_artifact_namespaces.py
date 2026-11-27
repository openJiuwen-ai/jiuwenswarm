from segment_state_fixtures import storage
from jiuwenswarm.server.runtime.skill_turbo.node_artifact_store import (
    SKILL_TURBO_NODE_ARTIFACTS_KEY, clear_namespace, clear_node_artifacts, load_node_artifacts,
    save_node_artifacts, segment_artifact_namespace,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_run_ledger import SkillRunLedger


async def test_legacy_tasks_segments_and_hashes_are_isolated(storage, tmp_path):
    session = storage.session()
    await save_node_artifacts(session, skill="legacy", nodes={"legacy": {"info": "old"}})
    a = segment_artifact_namespace("task-a", "demo-craft", "prep")
    b = segment_artifact_namespace("task-a", "demo-craft", "outline")
    c = segment_artifact_namespace("task-b", "demo-craft", "prep")
    for namespace, value in ((a, "a"), (b, "b"), (c, "c")):
        await save_node_artifacts(storage.session(), skill="demo-craft", nodes={"node": {"value": value}},
                                  namespace=namespace, plan_code_hash="h")
    for namespace, value in ((a, "a"), (b, "b"), (c, "c")):
        loaded = await load_node_artifacts(storage.session(), namespace=namespace, plan_code_hash="h")
        assert loaded["nodes"] == {"node": {"value": value}}
        assert await load_node_artifacts(storage.session(), namespace=namespace, plan_code_hash="wrong") is None
    assert await load_node_artifacts(storage.session(), namespace=a) is None
    reader = storage.session()
    await reader.pre_run(inputs=None)
    assert (await load_node_artifacts(reader))["nodes"] == {"legacy": {"info": "old"}}
    await save_node_artifacts(storage.session(), skill="demo-craft", nodes={"replacement": {"v": 2}},
                              namespace=a, plan_code_hash="h2")
    assert await load_node_artifacts(storage.session(), namespace=a, plan_code_hash="h") is None
    assert (await load_node_artifacts(storage.session(), namespace=a, plan_code_hash="h2"))["nodes"] == {"replacement": {"v": 2}}
    ledger = await SkillRunLedger.open(storage.session(), skill="demo-craft", workspace_base=str(tmp_path))
    marker = tmp_path / "user-file.txt"
    marker.write_text("keep", encoding="utf-8")
    await clear_namespace(storage.session(), a)
    assert await load_node_artifacts(storage.session(), namespace=a, plan_code_hash="h2") is None
    assert (await load_node_artifacts(storage.session(), namespace=b, plan_code_hash="h"))["nodes"]
    assert (await load_node_artifacts(storage.session(), namespace=c, plan_code_hash="h"))["nodes"]
    assert await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=ledger.task_key)
    assert marker.read_text(encoding="utf-8") == "keep"


async def test_legacy_writes_and_clear_preserve_segment_buckets(storage):
    namespace = segment_artifact_namespace("task", "demo-craft", "prep")
    await save_node_artifacts(storage.session(), skill="demo", nodes={"new": {"v": 1}}, namespace=namespace, plan_code_hash="h")
    session = storage.session()
    await save_node_artifacts(session, skill="legacy", nodes={"old": {"v": 2}})
    assert (await load_node_artifacts(storage.session(), namespace=namespace, plan_code_hash="h"))["nodes"] == {"new": {"v": 1}}
    session = storage.session()
    await session.pre_run(inputs=None)
    await clear_node_artifacts(session)
    await session.post_run()
    reader = storage.session()
    await reader.pre_run(inputs=None)
    assert await load_node_artifacts(reader) is None
    assert (await load_node_artifacts(reader, namespace=namespace, plan_code_hash="h"))["nodes"]


async def test_namespace_read_only_deepcopy_and_old_hash_compatibility(storage):
    session = storage.session()
    await save_node_artifacts(session, skill="legacy", nodes={"old": {"v": [1]}})
    before = storage.cp.writes
    assert await load_node_artifacts(storage.session(), namespace="new", plan_code_hash="h") is None
    assert storage.cp.writes == before
    reader = storage.session()
    await reader.pre_run(inputs=None)
    old = await load_node_artifacts(reader)
    assert old["plan_code_hash"] == ""
    namespace = "task/demo/prep"
    original = {"node": {"v": [1]}}
    await save_node_artifacts(storage.session(), skill="demo", nodes=original, namespace=namespace, plan_code_hash="h")
    original["node"]["v"].append(2)
    result = await load_node_artifacts(storage.session(), namespace=namespace, plan_code_hash="h")
    result["nodes"]["node"]["v"].append(3)
    assert (await load_node_artifacts(storage.session(), namespace=namespace, plan_code_hash="h"))["nodes"] == {"node": {"v": [1]}}


async def test_bucket_clear_survives_old_session_finally(storage):
    session = storage.session()
    await session.pre_run(inputs=None)
    await save_node_artifacts(session, skill="demo", nodes={"node": {"v": 1}}, namespace="task/demo/prep", plan_code_hash="h")
    await clear_namespace(storage.session(), "task/demo/prep")
    await session.post_run()
    assert await load_node_artifacts(storage.session(), namespace="task/demo/prep", plan_code_hash="h") is None


async def test_interrupt_style_bucket_save_leaves_caller_lifecycle_open(storage):
    caller = storage.session()
    await caller.pre_run(inputs=None)
    await save_node_artifacts(caller, skill="demo", nodes={"partial": {"v": 1}},
                              namespace="task/demo/prep", plan_code_hash="h", skip_post_run=True)
    assert not caller._post_run_done and not caller.is_stream_emitter_closed()
    assert (await load_node_artifacts(storage.session(), namespace="task/demo/prep", plan_code_hash="h"))["nodes"]


async def test_sdk_sqlite_checkpointer_and_session_reconstruction(storage, tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine
    from openjiuwen.core.session.checkpointer import CheckpointerFactory
    from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider

    db = tmp_path / "isolated-state.db"
    engine = create_async_engine("sqlite+aiosqlite:///" + str(db))
    provider = PersistenceCheckpointerProvider()
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", await provider.create({"db_client": engine}))
    ledger = await SkillRunLedger.open(storage.session(), skill="demo-craft", workspace_base=str(tmp_path))
    ledger.data["revision"] = 1
    ledger.data["facts"]["topic"] = "saved to sqlite"
    assert await ledger.save(storage.session())
    await save_node_artifacts(storage.session(), skill="demo-craft", nodes={"root": {"answer": 42}},
                              namespace="task/demo/prep", plan_code_hash="h")
    await engine.dispose()
    second_engine = create_async_engine("sqlite+aiosqlite:///" + str(db))
    try:
        monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", await provider.create({"db_client": second_engine}))
        loaded = await SkillRunLedger.load(storage.session(), skill="demo-craft", task_key=ledger.task_key)
        assert loaded.data["revision"] == 1 and loaded.data["facts"]["topic"] == "saved to sqlite"
        assert (await load_node_artifacts(storage.session(), namespace="task/demo/prep", plan_code_hash="h"))["nodes"] == {"root": {"answer": 42}}
        main = storage.session(turbo=False)
        await main.pre_run(inputs=None)
        assert main.get_state(SkillRunLedger.KEY) is None and main.get_state(SKILL_TURBO_NODE_ARTIFACTS_KEY) is None
        await main.close_stream()
    finally:
        await second_engine.dispose()
