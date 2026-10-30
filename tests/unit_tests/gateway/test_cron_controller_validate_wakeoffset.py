# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CronController wake_offset 截断行为锚定测试。

测试策略：参照 test_cron_controller_project_binding.py 的真实锚定模式，
直接调用 CronController.create_job / update_job，断言落库的
wake_offset_seconds 已被 _clamp_wake_offset_for_upcoming_run 正确截断。

与生产代码零关联的模拟测试无法提供回归保护——本地实验证实：
- revert 本 PR 唯一生产改动（enterprise 门控移除）后模拟测试仍全绿
- 直接破坏生产 clamp 实现（恒返 0）后模拟测试仍全绿
本文件通过真实调用生产代码解决上述问题。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.cron.controller import CronController


class _RecordingStore:
    """记录 create/update 调用，返回 SimpleNamespace 模拟 CronJob。"""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.update_calls: list[tuple[str, dict]] = []
        self._jobs: dict[str, SimpleNamespace] = {}

    async def create_job(self, **kwargs) -> SimpleNamespace:
        self.create_calls.append(kwargs)
        ns = SimpleNamespace(**kwargs)
        ns.to_dict = lambda: {k: v for k, v in vars(ns).items() if k != "to_dict"}
        self._jobs[ns.job_id or "new"] = ns
        return ns

    async def get_job(self, job_id: str) -> SimpleNamespace | None:
        return self._jobs.get(job_id)

    async def update_job(self, job_id: str, patch: dict) -> SimpleNamespace:
        self.update_calls.append((job_id, patch))
        existing = self._jobs.get(job_id)
        if existing is None:
            raise KeyError("job not found")
        for k, v in patch.items():
            setattr(existing, k, v)
        return existing

    async def list_jobs(self) -> list:
        return list(self._jobs.values())


class _FakeScheduler:
    """CronController 依赖的调度器接口子集。"""

    def __init__(self) -> None:
        self.hiding_projects: set[str] = set()

    def close_project_admission(self, project_id: str) -> None:
        self.hiding_projects.add(project_id)

    def reopen_project_admission(self, project_id: str) -> None:
        self.hiding_projects.discard(project_id)

    async def project_execution_allowed(
        self, project_id=None, user_id=None, group_id=None, bot_id=None
    ) -> bool:
        return True

    async def reload(self) -> None:
        return None


def _make_controller() -> CronController:
    CronController.reset_instance()
    return CronController(store=_RecordingStore(), scheduler=_FakeScheduler())


def _job_params(**overrides) -> dict:
    """构造 create_job 参数。"""
    params: dict = {
        "name": "job",
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "description": "reminder",
        "targets": "web",
    }
    params.update(overrides)
    return params


# ===========================================================================
# create_job 截断锚定测试
# ===========================================================================


class TestCreateJobClamp:
    """真实调用 create_job，断言 wake_offset_seconds 被截断。"""

    @pytest.mark.asyncio
    async def test_keeps_reasonable_offset(self) -> None:
        """合理 offset（小于 delay）保持不变。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="0 9 * * *",
            wake_offset_seconds=30,
        ))
        assert job["wake_offset_seconds"] == 30

    @pytest.mark.asyncio
    async def test_clamps_large_offset(self) -> None:
        """过大 offset 被截断到 floor(delay)。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
        ))
        # delay 最多 120s，截断后应 ≤ 120
        assert isinstance(job["wake_offset_seconds"], int)
        assert 0 <= job["wake_offset_seconds"] <= 120

    @pytest.mark.asyncio
    async def test_zero_offset_stays_zero(self) -> None:
        """offset=0 保持不变。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="0 9 * * *",
            wake_offset_seconds=0,
        ))
        assert job["wake_offset_seconds"] == 0

    @pytest.mark.asyncio
    async def test_none_offset_becomes_zero(self) -> None:
        """offset=None → 落库为 0。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="0 9 * * *",
            wake_offset_seconds=None,
        ))
        assert job["wake_offset_seconds"] == 0

    @pytest.mark.asyncio
    async def test_negative_offset_becomes_zero(self) -> None:
        """负 offset → 落库为 0。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="0 9 * * *",
            wake_offset_seconds=-3600,
        ))
        assert job["wake_offset_seconds"] == 0

    @pytest.mark.asyncio
    async def test_clamp_logs_to_store_create_call(self) -> None:
        """截断后的值确实传给 store.create_job。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
        ))
        stored = cc.store.create_calls[0]
        assert isinstance(stored["wake_offset_seconds"], int)
        assert 0 <= stored["wake_offset_seconds"] <= 120


# ===========================================================================
# update_job 截断锚定测试
# ===========================================================================


class TestUpdateJobClamp:
    """真实调用 update_job，断言 wake_offset_seconds 被截断。"""

    @pytest.fixture
    def existing_job_id(self) -> str:
        """预先创建一个基础 job，返回其 id。"""
        return "job-to-update"

    @pytest.mark.asyncio
    async def test_ignores_offset_when_not_in_patch(self, existing_job_id) -> None:
        """patch 不含 wake_offset_seconds/cron_expr/timezone → 不触发 clamp。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",
            wake_offset_seconds=1800,
        ))
        updated = await cc.update_job(existing_job_id, {"name": "new-name"})
        assert updated["wake_offset_seconds"] == 1800  # 不变

    @pytest.mark.asyncio
    async def test_keeps_reasonable_offset_change(self, existing_job_id) -> None:
        """合理 offset 保持不变。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",
            wake_offset_seconds=1800,
        ))
        updated = await cc.update_job(
            existing_job_id, {"wake_offset_seconds": 30},
        )
        assert updated["wake_offset_seconds"] == 30

    @pytest.mark.asyncio
    async def test_clamps_large_offset_change(self, existing_job_id) -> None:
        """过大 offset 被截断。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="*/2 * * * *",
            wake_offset_seconds=30,
        ))
        updated = await cc.update_job(
            existing_job_id, {"wake_offset_seconds": 999999},
        )
        assert 0 <= updated["wake_offset_seconds"] <= 120

    @pytest.mark.asyncio
    async def test_clamps_simultaneous_cron_and_large_offset(self, existing_job_id) -> None:
        """同时改 cron_expr 和 offset（新 offset 对新 delay 过大）→ 截断。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",   # 每天
            wake_offset_seconds=1800,
        ))
        updated = await cc.update_job(existing_job_id, {
            "cron_expr": "*/2 * * * *",   # 改为每 2 分钟
            "wake_offset_seconds": 999999,
        })
        assert 0 <= updated["wake_offset_seconds"] <= 120

    @pytest.mark.asyncio
    async def test_accepts_simultaneous_cron_and_reasonable_offset(self, existing_job_id) -> None:
        """同时改 cron_expr 和 offset（新 offset 合理）→ 保持不变。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",
            wake_offset_seconds=1800,
        ))
        updated = await cc.update_job(existing_job_id, {
            "cron_expr": "0 9 * * *",
            "wake_offset_seconds": 30,
        })
        assert updated["wake_offset_seconds"] == 30

    @pytest.mark.asyncio
    async def test_zero_offset_stays_zero(self, existing_job_id) -> None:
        """offset 改成 0 → 落库为 0。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",
            wake_offset_seconds=1800,
        ))
        updated = await cc.update_job(
            existing_job_id, {"wake_offset_seconds": 0},
        )
        assert updated["wake_offset_seconds"] == 0

    @pytest.mark.asyncio
    async def test_triggers_on_cron_expr_change_only(self, existing_job_id) -> None:
        """只改 cron_expr 也触发 clamp（用 existing 的 offset 重新 clamp）。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",      # 每天
            wake_offset_seconds=43200,   # 对每天合理
        ))
        updated = await cc.update_job(existing_job_id, {
            "cron_expr": "*/2 * * * *",  # 改为每 2 分钟
        })
        # 现有 offset=43200 对新 delay（≤120s）过大，应截断
        assert 0 <= updated["wake_offset_seconds"] <= 120

    @pytest.mark.asyncio
    async def test_triggers_on_timezone_change_only(self, existing_job_id) -> None:
        """只改 timezone 也触发 clamp。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",
            wake_offset_seconds=30,
        ))
        updated = await cc.update_job(existing_job_id, {
            "timezone": "UTC",
        })
        assert updated["wake_offset_seconds"] == 30  # 合理 offset 不变

    @pytest.mark.asyncio
    async def test_clamp_applied_to_store_update_call(self, existing_job_id) -> None:
        """截断后的值确实传给 store.update_job。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="*/2 * * * *",
            wake_offset_seconds=30,
        ))
        await cc.update_job(existing_job_id, {"wake_offset_seconds": 999999})
        stored_patch = cc.store.update_calls[0][1]
        assert isinstance(stored_patch["wake_offset_seconds"], int)
        assert 0 <= stored_patch["wake_offset_seconds"] <= 120
