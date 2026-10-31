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

from datetime import datetime as _RealDateTime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

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


# ---------------------------------------------------------------------------
# 时间冻结辅助：使 _clamp_wake_offset_for_upcoming_run 的 delay 计算确定性化
# ---------------------------------------------------------------------------

_FROZEN_NOW = _RealDateTime(2026, 1, 15, 10, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


class _FrozenDateTime(_RealDateTime):
    """替换 controller.datetime，使 now() 返回固定时刻。

    冻结在 2026-01-15 10:00:30 Asia/Shanghai：
    - ``*/2 * * * *`` → 下次触发 10:02:00，delay=90s，floor=90
    - ``0 9 * * *``   → 下次触发 次日 09:00:00，delay=82770s，floor=82770
    """

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW.astimezone(tz) if tz else _FROZEN_NOW


@pytest.fixture
def frozen_now():
    """冻结 controller 的 datetime.now，使 clamp 计算确定性化。"""
    with patch("gateway.cron.controller.datetime", _FrozenDateTime):
        yield


# ===========================================================================
# create_job 截断锚定测试
# ===========================================================================


class TestCreateJobClamp:
    """真实调用 create_job，断言 wake_offset_seconds 被截断。"""

    @pytest.mark.asyncio
    async def test_keeps_reasonable_offset(self, frozen_now) -> None:
        """合理 offset（小于 delay）保持不变。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="0 9 * * *",
            wake_offset_seconds=30,
        ))
        assert job["wake_offset_seconds"] == 30

    @pytest.mark.asyncio
    async def test_clamps_large_offset(self, frozen_now) -> None:
        """过大 offset 被截断到 floor(delay)。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
        ))
        # 冻结在 10:00:30，*/2 下次触发 10:02:00，delay=90s → floor=90
        assert isinstance(job["wake_offset_seconds"], int)
        assert job["wake_offset_seconds"] == 90

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

    @pytest.fixture
    def personal_env(self, monkeypatch):
        """显式锁定个人版环境，防止 CI 环境变量污染。"""
        monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    @pytest.mark.asyncio
    async def test_clamp_logs_to_store_create_call(self, frozen_now) -> None:
        """截断后的值确实传给 store.create_job。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
        ))
        stored = cc.store.create_calls[0]
        assert isinstance(stored["wake_offset_seconds"], int)
        assert stored["wake_offset_seconds"] == 90

    @pytest.mark.asyncio
    async def test_personal_create_with_mcp_and_clamp(
        self, personal_env, frozen_now
    ) -> None:
        """个人版创建：is_enterprise()=False 分支——mcp 被 normalize（非 None），clamp 仍正确执行。"""
        cc = _make_controller()
        job = await cc.create_job(_job_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
            mcp=["files", "files", ""],  # 个人版 normalize → ["files"]
        ))
        assert job["wake_offset_seconds"] == 90
        stored = cc.store.create_calls[0]
        # 个人版保留 mcp（非 None），与企业版 mcp=None 形成对照
        assert stored["mcp"] == ["files"]


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
        created = await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="0 9 * * *",
            wake_offset_seconds=1800,
        ))
        offset_after_create = created["wake_offset_seconds"]
        updated = await cc.update_job(existing_job_id, {"name": "new-name"})
        # clamp 未触发：store patch 不含 wake_offset_seconds，且值与 create 后一致。
        # 不断言精确值（如 1800），因为 create 时的 clamp 会用真实墙钟计算剩余
        # delay 并可能截断（cron 为上海每天 9 点，08:30–09:00 窗口内 delay<1800），
        # 导致落库值随墙钟变化。改为断言"更新前后不变"以与墙钟解耦。
        stored_patch = cc.store.update_calls[0][1]
        assert "wake_offset_seconds" not in stored_patch
        assert updated["wake_offset_seconds"] == offset_after_create

    @pytest.mark.asyncio
    async def test_keeps_reasonable_offset_change(self, existing_job_id, frozen_now) -> None:
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
    async def test_clamps_large_offset_change(self, existing_job_id, frozen_now) -> None:
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
        # 冻结在 10:00:30，*/2 下次触发 10:02:00，delay=90s → floor=90
        assert updated["wake_offset_seconds"] == 90

    @pytest.mark.asyncio
    async def test_clamps_simultaneous_cron_and_large_offset(self, existing_job_id, frozen_now) -> None:
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
        # 冻结在 10:00:30，*/2 下次触发 10:02:00，delay=90s → floor=90
        assert updated["wake_offset_seconds"] == 90

    @pytest.mark.asyncio
    async def test_accepts_simultaneous_cron_and_reasonable_offset(self, existing_job_id, frozen_now) -> None:
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
    async def test_triggers_on_cron_expr_change_only(self, existing_job_id, frozen_now) -> None:
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
        # 冻结在 10:00:30，*/2 下次触发 10:02:00，delay=90s → floor=90
        # 现有 offset=43200 对新 delay 过大，应截断到 90
        assert updated["wake_offset_seconds"] == 90

    @pytest.mark.asyncio
    async def test_triggers_on_timezone_change_only(self, existing_job_id, frozen_now) -> None:
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
    async def test_clamp_applied_to_store_update_call(self, existing_job_id, frozen_now) -> None:
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
        # 冻结在 10:00:30，*/2 下次触发 10:02:00，delay=90s → floor=90
        assert stored_patch["wake_offset_seconds"] == 90

    @pytest.fixture
    def personal_env(self, monkeypatch):
        """显式锁定个人版环境，防止 CI 环境变量污染。"""
        monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)

    @pytest.mark.asyncio
    async def test_personal_update_keeps_mcp_alongside_clamp(
        self, existing_job_id, personal_env, frozen_now
    ) -> None:
        """个人版更新：is_enterprise()=False 分支——mcp 保留在 patch 中（非剥离），clamp 仍正确执行。"""
        cc = _make_controller()
        await cc.create_job(_job_params(
            id=existing_job_id,
            cron_expr="*/2 * * * *",
            wake_offset_seconds=30,
        ))
        await cc.update_job(existing_job_id, {
            "wake_offset_seconds": 999999,
            "mcp": ["files", "files", ""],  # 个人版 normalize → ["files"]
        })
        stored_patch = cc.store.update_calls[0][1]
        assert stored_patch["wake_offset_seconds"] == 90
        # 个人版保留 mcp（非剥离），与企业版 pop mcp 形成对照
        assert stored_patch["mcp"] == ["files"]
# ===========================================================================


class TestEnterpriseCreateJobClamp:
    """企业版 create_job 的 wake_offset 截断行为。

    企业版与个人版的 clamp 逻辑相同，但前置路径不同：
    - 身份校验：group_id/bot_id/user_id 必须非空（_required_enterprise_tenant_id）
    - 模型校验：resolve_enterprise_cron_model 核对企业库
    - 副作用：mcp=None、credential_ref=''、model_selection=None
    需验证这些差异不阻断 clamp 的正确执行。
    """

    @pytest.fixture
    def enterprise_env(self, monkeypatch):
        """启用企业版并 mock 模型校验。"""
        monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
        monkeypatch.setattr(
            "gateway.cron.enterprise_model.resolve_enterprise_cron_model",
            AsyncMock(return_value="ent-model"),
        )

    def _ent_params(self, **overrides) -> dict:
        """构造企业版 create_job 参数（含必需身份和模型）。"""
        params = _job_params(
            group_id="team-1",
            bot_id="bot-1",
            user_id="alice",
            model_name="ent-model",
        )
        params.update(overrides)
        return params

    @pytest.mark.asyncio
    async def test_ent_clamps_large_offset(self, enterprise_env, frozen_now) -> None:
        """企业版创建：过大 offset 被截断到 floor(delay)。"""
        cc = _make_controller()
        job = await cc.create_job(self._ent_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
        ))
        # 冻结在 10:00:30，*/2 下次触发 10:02:00，delay=90s → floor=90
        assert job["wake_offset_seconds"] == 90

    @pytest.mark.asyncio
    async def test_ent_keeps_reasonable_offset(self, enterprise_env, frozen_now) -> None:
        """企业版创建：合理 offset 保持不变。"""
        cc = _make_controller()
        job = await cc.create_job(self._ent_params(
            cron_expr="0 9 * * *",
            wake_offset_seconds=30,
        ))
        assert job["wake_offset_seconds"] == 30

    @pytest.mark.asyncio
    async def test_ent_missing_identity_raises_before_clamp(self, enterprise_env) -> None:
        """企业版创建：缺 group_id → ValueError，不执行 clamp。"""
        cc = _make_controller()
        with pytest.raises(ValueError, match="enterprise cron requires"):
            await cc.create_job(self._ent_params(
                cron_expr="*/2 * * * *",
                wake_offset_seconds=999999,
                group_id="   ",  # 空白 → _required_enterprise_tenant_id 抛 ValueError
            ))
        assert cc.store.create_calls == []

    @pytest.mark.asyncio
    async def test_ent_clamp_alongside_stripped_fields(self, enterprise_env, frozen_now) -> None:
        """企业版创建：clamp 执行的同时，mcp=None、credential_ref=''、model_selection=None。"""
        cc = _make_controller()
        await cc.create_job(self._ent_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
            mcp=["files"],            # 企业版应忽略 → None
        ))
        stored = cc.store.create_calls[0]
        assert stored["wake_offset_seconds"] == 90
        assert stored["mcp"] is None
        assert stored["credential_ref"] == ""
        assert stored["model_selection"] is None

    @pytest.mark.asyncio
    async def test_ent_model_resolve_failure_skips_clamp(
        self, monkeypatch, frozen_now
    ) -> None:
        """企业版创建：resolve_enterprise_cron_model 抛 ValueError → clamp 不执行。"""
        monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
        monkeypatch.setattr(
            "gateway.cron.enterprise_model.resolve_enterprise_cron_model",
            AsyncMock(side_effect=ValueError("Unknown model 'bad-model'")),
        )
        cc = _make_controller()
        with pytest.raises(ValueError, match="Unknown model"):
            await cc.create_job(self._ent_params(
                cron_expr="*/2 * * * *",
                wake_offset_seconds=999999,
                model_name="bad-model",
            ))
        assert cc.store.create_calls == []


# ===========================================================================
# 企业版 update_job wake_offset 截断锚定测试
# ===========================================================================


class TestEnterpriseUpdateJobClamp:
    """企业版 update_job 的 wake_offset 截断行为。

    企业版 update 比个人版更严格：
    - _owns_job 要求 group_id/bot_id/user_id 全部非空且精确匹配
    - patch 中 mcp/credential_ref/model_selection 被剥离
    需验证身份不匹配时 clamp 不执行，身份匹配时 clamp 正常执行。
    """

    @pytest.fixture
    def enterprise_env(self, monkeypatch):
        """启用企业版并 mock 模型校验。"""
        monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
        monkeypatch.setattr(
            "gateway.cron.enterprise_model.resolve_enterprise_cron_model",
            AsyncMock(return_value="ent-model"),
        )

    _ENT_ID = {"user_id": "alice", "group_id": "team-1", "bot_id": "bot-1"}

    async def _create_ent_job(self, cc: CronController, **overrides) -> str:
        """创建一条企业版任务，返回 job_id。"""
        params = _job_params(
            id="ent-job",
            cron_expr="*/2 * * * *",
            wake_offset_seconds=30,
            group_id="team-1",
            bot_id="bot-1",
            user_id="alice",
            model_name="ent-model",
        )
        params.update(overrides)
        await cc.create_job(params)
        return "ent-job"

    @pytest.mark.asyncio
    async def test_ent_update_clamps_large_offset(self, enterprise_env, frozen_now) -> None:
        """企业版更新：过大 offset 被截断。"""
        cc = _make_controller()
        job_id = await self._create_ent_job(cc)
        updated = await cc.update_job(
            job_id,
            {"wake_offset_seconds": 999999},
            **self._ENT_ID,
        )
        # 冻结在 10:00:30，*/2 下次触发 10:02:00，delay=90s → floor=90
        assert updated["wake_offset_seconds"] == 90

    @pytest.mark.asyncio
    async def test_ent_update_wrong_identity_raises_before_clamp(self, enterprise_env) -> None:
        """企业版更新：身份不匹配 → KeyError，不执行 clamp。"""
        cc = _make_controller()
        job_id = await self._create_ent_job(cc)
        with pytest.raises(KeyError, match="job not found"):
            await cc.update_job(
                job_id,
                {"wake_offset_seconds": 999999},
                user_id="bob",        # 不匹配
                group_id="team-1",
                bot_id="bot-1",
            )
        assert cc.store.update_calls == []

    @pytest.mark.asyncio
    async def test_ent_update_missing_identity_raises_before_clamp(self, enterprise_env) -> None:
        """企业版更新：缺 bot_id → KeyError，不执行 clamp。"""
        cc = _make_controller()
        job_id = await self._create_ent_job(cc)
        with pytest.raises(KeyError, match="job not found"):
            await cc.update_job(
                job_id,
                {"wake_offset_seconds": 999999},
                user_id="alice",
                group_id="team-1",
                bot_id="",            # 企业版缺身份 → _owns_job 返回 False
            )
        assert cc.store.update_calls == []

    @pytest.mark.asyncio
    async def test_ent_update_clamp_alongside_stripped_fields(
        self, enterprise_env, frozen_now
    ) -> None:
        """企业版更新：clamp 执行的同时，patch 中 mcp/credential_ref 被剥离。"""
        cc = _make_controller()
        job_id = await self._create_ent_job(cc)
        await cc.update_job(
            job_id,
            {
                "wake_offset_seconds": 999999,
                "mcp": ["files"],
                "credential_ref": "secret",
            },
            **self._ENT_ID,
        )
        stored_patch = cc.store.update_calls[0][1]
        assert stored_patch["wake_offset_seconds"] == 90
        assert "mcp" not in stored_patch
        assert "credential_ref" not in stored_patch


# ===========================================================================
# 个人版 vs 企业版 clamp 一致性测试
# ===========================================================================


class TestClampEditionConsistency:
    """同一 cron/offset 在个人版和企业版下产生相同 clamp 结果。

    clamp 逻辑（_clamp_wake_offset_for_upcoming_run）不区分版本，
    此测试锚定该不变量，防止未来版本分支引入差异。
    """

    @pytest.mark.asyncio
    async def test_same_clamp_across_editions(self, frozen_now, monkeypatch) -> None:
        """个人版和企业版对同一输入产生相同 wake_offset_seconds。"""
        # --- 个人版 ---
        cc_personal = _make_controller()
        job_p = await cc_personal.create_job(_job_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
        ))
        # --- 切换到企业版 ---
        monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
        monkeypatch.setattr(
            "gateway.cron.enterprise_model.resolve_enterprise_cron_model",
            AsyncMock(return_value="ent-model"),
        )
        cc_enterprise = _make_controller()
        job_e = await cc_enterprise.create_job(_job_params(
            cron_expr="*/2 * * * *",
            wake_offset_seconds=999999,
            group_id="team-1",
            bot_id="bot-1",
            user_id="alice",
            model_name="ent-model",
        ))
        assert job_p["wake_offset_seconds"] == job_e["wake_offset_seconds"] == 90
