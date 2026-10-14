"""Cron 任务更新与持久化的回归测试。"""

import asyncio

import pytest

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.runtime.cron.store import CronJobStore
from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver


async def test_schedule_change_resets_delete_after_run(tmp_path):
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    job = await store.create_job(
        name="提醒回微信",
        cron_expr="0 16 15 11 9 ? 2026",
        timezone="Asia/Shanghai",
        description="回复微信消息",
        targets="web",
        delete_after_run=True,
    )

    await store.update_job(job.id, {"cron_expr": "0 */5 * * * * *"})

    saved = await CronJobStore(path=store.path).get_job(job.id)
    assert saved is not None
    assert saved.cron_expr == "0 */5 * * * * *"
    assert saved.delete_after_run is False
    assert saved.expired is False


@pytest.mark.parametrize("model_name", ["GLM-5.2", "other-config-model", None])
async def test_model_name_update_clears_migrated_selection(tmp_path, model_name):
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    job = await store.create_job(
        name="job",
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="reminder",
        targets="web",
        model_name="old-config-model",
        model_selection={"type": "model", "id": "old-config-model-id"},
    )

    await store.update_job(job.id, {"model_name": model_name})

    saved = await CronJobStore(path=store.path).get_job(job.id)
    assert saved.model_name == model_name
    assert saved.model_selection is None


@pytest.mark.parametrize("explicit_selection", [False, True])
async def test_model_selection_preserved_when_not_overridden_by_name(
    tmp_path, monkeypatch, explicit_selection
):
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    selection = {"type": "model", "id": "config-model-id"}
    job = await store.create_job(
        name="job",
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="reminder",
        targets="web",
        model_name="config-model",
        model_selection=selection,
    )
    patch = {"name": "renamed"}
    if explicit_selection:
        selection = {"type": "model", "id": "new-config-model-id"}
        catalog = ModelCatalog({
            "models": {
                "defaults": [{
                    "model_id": selection["id"],
                    "model_client_config": {"model_name": "new-config-model"},
                }],
            },
        })
        resolver = ModelSelectionResolver(catalog)
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
            lambda: resolver,
        )
        patch.update(model_name="new-config-model", model_selection=selection)

    await store.update_job(job.id, patch)

    saved = await CronJobStore(path=store.path).get_job(job.id)
    assert saved.name == "renamed"
    assert saved.model_selection == ModelSelection.model_validate(selection).model_dump()


async def test_concurrent_update_job_fields_both_survive(tmp_path):
    """回归 #4964：触发点标记 expired 与执行回写 last_session_id 并发时不得互覆。

    同刻两个写入者各改一个字段：旧实现里两个 update_job 都基于同一份
    过期快照整条写回，后写者必然抹掉先写者的字段。
    """
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    jobs = []
    for name in ("job-a", "job-b"):
        jobs.append(
            await store.create_job(
                name=name,
                cron_expr="0 */5 * * * * *",
                timezone="Asia/Shanghai",
                description="并发回写回归",
                targets="web",
            )
        )

    # 两种调度顺序都覆盖：先标记后回写 / 先回写后标记。
    await asyncio.gather(
        store.update_job(jobs[0].id, {"expired": True}),
        store.update_job(jobs[0].id, {"last_session_id": "sess-a"}),
    )
    await asyncio.gather(
        store.update_job(jobs[1].id, {"last_session_id": "sess-b"}),
        store.update_job(jobs[1].id, {"expired": True}),
    )

    reader = CronJobStore(path=store.path)
    for job, sid in zip(jobs, ("sess-a", "sess-b")):
        saved = await reader.get_job(job.id)
        assert saved is not None
        assert saved.expired is True
        assert saved.last_session_id == sid


async def test_update_job_error_contract(tmp_path):
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    with pytest.raises(ValueError):
        await store.update_job("", {"name": "x"})
    with pytest.raises(KeyError):
        await store.update_job("no-such-job", {"name": "x"})
