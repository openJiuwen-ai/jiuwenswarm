"""一次性提醒改为周期任务的回归测试。"""

from jiuwenswarm.gateway.cron.store import CronJobStore


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
