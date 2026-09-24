# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""入口 _replay_expert_from_metadata 与 expert_switch_blocked 测试。
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)

from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.config import (
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.harness.factory import create_deep_agent

import jiuwenswarm.server.runtime.session.session_metadata as sm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.expert import expert_store as es

ORIGINAL_IDENTITY = "你是测试宿主助手。"


def _make_model() -> Model:
    return Model(
        ModelClientConfig(
            client_id="test",
            client_provider="openai",
            api_key="dummy",
            api_base="https://example.com/v1",
        ),
        ModelRequestConfig(model_name="dummy-model"),
    )


def _make_package(root: Path, name: str, persona: str) -> Path:
    pkg = root / name
    (pkg / "agents").mkdir(parents=True)
    (pkg / "agents" / "00-identity.md").write_text(persona, encoding="utf-8")
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "packageType": "agent_template",
                "agentCard": {"id": name, "name": name, "description": "测试包"},
                "persona": {"dir": "agents"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return pkg


def _make_child_adapter(session_id: str) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = create_deep_agent(
        model=_make_model(), system_prompt=ORIGINAL_IDENTITY, workspace=tempfile.mkdtemp()
    )
    adapter.mark_as_session_scoped(session_id)
    return adapter


def _identity_text(adapter: JiuWenSwarmDeepAdapter) -> str:
    section = adapter._instance.system_prompt_builder.get_section("identity")
    return section.content.get("cn") or next(iter(section.content.values()))


@pytest.fixture
def experts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "experts"
    root.mkdir()
    source = es.LocalDirExpertPackageSource(experts_dir=root)
    monkeypatch.setattr(es, "get_expert_source", lambda: source)
    return root


@pytest.mark.asyncio
async def test_replay_applies_expert_from_metadata(
        experts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_package(experts_dir, "expert-a", "你是专家阿甲。")
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"expert_id": "expert-a"},
    )
    adapter = _make_child_adapter("sess-1")

    await adapter._replay_expert_from_metadata()

    assert adapter._current_expert_id == "expert-a"
    assert _identity_text(adapter) == "你是专家阿甲。"


@pytest.mark.asyncio
async def test_replay_noop_without_expert_in_metadata(
        experts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"title": "无专家会话"},
    )
    adapter = _make_child_adapter("sess-2")

    await adapter._replay_expert_from_metadata()

    assert adapter._current_expert_id is None
    assert ORIGINAL_IDENTITY in _identity_text(adapter)


@pytest.mark.asyncio
async def test_replay_failure_degrades_to_no_expert(
        experts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重放失败（包不存在/仓库不可达）降级无专家、不抛穿、不清 metadata。"""
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"expert_id": "missing-pkg"},
    )
    adapter = _make_child_adapter("sess-3")

    await adapter._replay_expert_from_metadata()  # 不应抛出

    assert adapter._current_expert_id is None
    assert adapter._expert_load_record is None
    assert ORIGINAL_IDENTITY in _identity_text(adapter)


@pytest.mark.asyncio
async def test_replay_read_metadata_failure_is_safe(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(session_id, cache_bust=False, **_):
        raise OSError("disk error")

    monkeypatch.setattr(sm, "get_session_metadata", _boom)
    adapter = _make_child_adapter("sess-4")

    await adapter._replay_expert_from_metadata()  # 不应抛出
    assert adapter._current_expert_id is None


def test_switch_blocked_by_active_counter() -> None:
    root = JiuWenSwarmDeepAdapter()
    root._active_session_ids["sess-1"] = 1
    assert root.expert_switch_blocked("sess-1") is True
    assert root.expert_switch_blocked("sess-2") is False


def test_switch_blocked_by_child_executing() -> None:
    root = JiuWenSwarmDeepAdapter()
    child = SimpleNamespace(_is_session_live=lambda sid: True)
    root._session_adapters["sess-1"] = child
    assert root.expert_switch_blocked("sess-1") is True


def test_switch_allowed_when_idle() -> None:
    root = JiuWenSwarmDeepAdapter()
    child = SimpleNamespace(_is_session_live=lambda sid: False)
    root._session_adapters["sess-1"] = child
    assert root.expert_switch_blocked("sess-1") is False
    # 无子适配器（未装配）也不阻塞
    assert root.expert_switch_blocked("sess-new") is False


async def _noop_refresh(**kwargs):
    return None


def _patch_session_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔掉既有分支里的会话上下文刷新（真实实现读历史落盘，与本测试无关）。"""
    import jiuwenswarm.agents.harness.common.session_ops_service as sos

    monkeypatch.setattr(sos, "refresh_session_context_if_stale", _noop_refresh)


@pytest.mark.asyncio
async def test_replayed_persona_survives_identity_rail(
        experts_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端钉：replay 装载人设后，IdentityRail 的 before_model_call 不得摘除它。

    IdentityRail 每轮无条件 remove+重装 identity section——专家 persona 与
    IDENTITY.md 共用 identity 槽位，rail 须对"外来"（非默认/非自装）identity 让路，
    否则人设装载后活不过当轮第一次模型调用。
    """
    from jiuwenswarm.agents.harness.common.rails.identity_rail import IdentityRail

    _make_package(experts_dir, "expert-a", "你是专家阿甲。")
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"expert_id": "expert-a"},
    )
    adapter = _make_child_adapter("sess-rail")
    await adapter._replay_expert_from_metadata()
    assert _identity_text(adapter) == "你是专家阿甲。"

    rail = IdentityRail(identity_md_path=str(tmp_path / "IDENTITY.md"))  # 文件不存在 → 默认分支
    rail.init(adapter._instance)
    await rail.before_model_call(None)

    assert _identity_text(adapter) == "你是专家阿甲。"


@pytest.mark.asyncio
async def test_reuse_reconcile_replays_when_metadata_has_expert(
        experts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预热洞回归钉：实例先于 metadata 建完（无专家），复用分支补挂人设。"""
    _make_package(experts_dir, "expert-a", "你是专家阿甲。")
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"expert_id": "expert-a"},
    )
    _patch_session_refresh(monkeypatch)
    root = JiuWenSwarmDeepAdapter()
    child = _make_child_adapter("sess-pre")
    root._session_adapters["sess-pre"] = child

    adapter = await root._get_or_create_session_adapter("sess-pre")

    assert adapter is child
    assert child._current_expert_id == "expert-a"
    assert _identity_text(child) == "你是专家阿甲。"


@pytest.mark.asyncio
async def test_reuse_reconcile_idempotent_when_expert_already_applied(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已挂专家的适配器：对账早退，不重放（幂等）。"""
    child = _make_child_adapter("sess-r1")
    child._current_expert_id = "expert-a"
    called = False

    async def _spy():
        nonlocal called
        called = True

    monkeypatch.setattr(child, "_replay_expert_from_metadata", _spy)

    await child._reconcile_expert_binding_on_reuse()

    assert called is False


@pytest.mark.asyncio
async def test_reuse_reconcile_noop_without_expert_in_metadata(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无专家会话零打扰：metadata 无 expert_id，不重放。"""
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"title": "无专家会话"},
    )
    child = _make_child_adapter("sess-r2")
    called = False

    async def _spy():
        nonlocal called
        called = True

    monkeypatch.setattr(child, "_replay_expert_from_metadata", _spy)

    await child._reconcile_expert_binding_on_reuse()

    assert called is False
    assert child._expert_reuse_reconciled is False  # 未触发重放不消耗一次性旗标


@pytest.mark.asyncio
async def test_reuse_reconcile_skips_team_binding(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """专家团绑定走 team 线冷构造，对账不掺和（省读盘、防误挂）。"""
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {
            "expert_id": "some-team",
            "expert_type": "team",
        },
    )
    child = _make_child_adapter("sess-r3")
    called = False

    async def _spy():
        nonlocal called
        called = True

    monkeypatch.setattr(child, "_replay_expert_from_metadata", _spy)

    await child._reconcile_expert_binding_on_reuse()

    assert called is False


@pytest.mark.asyncio
async def test_reuse_reconcile_one_shot_on_failure(
        experts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重放失败（包缺失）只试一次：旗标置位防逐轮重试刷日志，会话照常可用。"""
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"expert_id": "missing-pkg"},
    )
    child = _make_child_adapter("sess-r4")
    calls = 0
    real_replay = child._replay_expert_from_metadata

    async def _spy():
        nonlocal calls
        calls += 1
        await real_replay()

    monkeypatch.setattr(child, "_replay_expert_from_metadata", _spy)

    await child._reconcile_expert_binding_on_reuse()
    await child._reconcile_expert_binding_on_reuse()

    assert calls == 1
    assert child._expert_reuse_reconciled is True
    assert child._current_expert_id is None  # 降级无专家
    assert ORIGINAL_IDENTITY in _identity_text(child)
