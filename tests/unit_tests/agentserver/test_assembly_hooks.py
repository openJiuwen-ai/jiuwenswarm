# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""装配生命周期扩展点（assembly_hooks）与 CodeAdapter 骨架接入测试。

覆盖：
- 注册表：内置三扩展注册序（packages → expert → user_rails）、幂等、clear/restore；
- 触发：按点位过滤、执行序 = 注册序、单扩展失败不中断；
- CodeAdapter 回归：code/design 骨架接入 hooks 后，
  实例重建重置专家态、AFTER_INSTANCE_READY 按 metadata 重放专家——
  修复前 code/design 会话装单专家静默不生效。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

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

from jiuwenswarm.server.runtime.agent_adapter.assembly_hooks import (
    AssemblyPoint,
    clear_assembly_extensions,
    register_assembly_extension,
    register_builtin_assembly_extensions,
    registered_assembly_extensions,
    run_assembly_hooks,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
    JiuwenSwarmCodeAdapter,
)
from jiuwenswarm.server.runtime.expert import expert_store as es


@pytest.fixture(autouse=True)
def _restore_builtin_extensions():
    """每个用例结束后复原内置扩展注册表（防用例间污染）。"""
    yield
    clear_assembly_extensions()
    register_builtin_assembly_extensions()


class _Recorder:
    def __init__(self, name: str, points, sink: list[str], fail: bool = False):
        self.name = name
        self.points = frozenset(points)
        self._sink = sink
        self._fail = fail

    async def on_point(self, point, adapter):
        if self._fail:
            raise RuntimeError(f"{self.name} boom")
        self._sink.append(self.name)


def test_builtin_extensions_registered_in_order() -> None:
    assert [e.name for e in registered_assembly_extensions()] == [
        "packages",
        "expert",
        "user_rails",
    ]


def test_register_is_idempotent_by_name() -> None:
    sink: list[str] = []
    register_assembly_extension(
        _Recorder("packages", {AssemblyPoint.AFTER_INSTANCE_READY}, sink)
    )
    names = [e.name for e in registered_assembly_extensions()]
    assert names.count("packages") == 1
    # 同名替换：新实例挪到末尾
    assert names[-1] == "packages"


@pytest.mark.asyncio
async def test_run_filters_by_point_and_preserves_order() -> None:
    clear_assembly_extensions()
    sink: list[str] = []
    register_assembly_extension(
        _Recorder("a", {AssemblyPoint.AFTER_INSTANCE_READY}, sink)
    )
    register_assembly_extension(
        _Recorder(
            "b",
            {AssemblyPoint.BEFORE_INSTANCE_READY, AssemblyPoint.AFTER_INSTANCE_READY},
            sink,
        )
    )
    await run_assembly_hooks(AssemblyPoint.BEFORE_INSTANCE_READY, object())
    assert sink == ["b"]
    await run_assembly_hooks(AssemblyPoint.AFTER_INSTANCE_READY, object())
    assert sink == ["b", "a", "b"]


@pytest.mark.asyncio
async def test_single_extension_failure_does_not_interrupt() -> None:
    clear_assembly_extensions()
    sink: list[str] = []
    register_assembly_extension(
        _Recorder("bad", {AssemblyPoint.AFTER_INSTANCE_READY}, sink, fail=True)
    )
    register_assembly_extension(
        _Recorder("good", {AssemblyPoint.AFTER_INSTANCE_READY}, sink)
    )
    await run_assembly_hooks(AssemblyPoint.AFTER_INSTANCE_READY, object())
    assert sink == ["good"]


# ── CodeAdapter 骨架回归：修复 code/design 专家三缺失 ──


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


def _make_code_child_adapter(session_id: str) -> JiuwenSwarmCodeAdapter:
    adapter = JiuwenSwarmCodeAdapter()
    adapter._instance = create_deep_agent(
        model=_make_model(),
        system_prompt="你是代码宿主助手。",
        workspace=tempfile.mkdtemp(),
    )
    adapter.mark_as_session_scoped(session_id)
    return adapter


@pytest.fixture
def experts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "experts"
    root.mkdir()
    source = es.LocalDirExpertPackageSource(experts_dir=root)
    monkeypatch.setattr(es, "get_expert_source", lambda: source)
    return root


@pytest.mark.asyncio
async def test_code_adapter_hooks_replay_expert_from_metadata(
    experts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CodeAdapter 走 hooks 后，重建重放路径按 metadata 装回专家。"""
    _make_package(experts_dir, "expert-code", "你是代码专家阿丙。")
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"expert_id": "expert-code"},
    )
    adapter = _make_code_child_adapter("sess-code-1")
    calls: list[str] = []

    async def _rec_packages():
        calls.append("packages")

    async def _rec_user_rails():
        calls.append("user_rails")

    adapter._load_active_packages = _rec_packages
    adapter.load_user_rails = _rec_user_rails

    await run_assembly_hooks(AssemblyPoint.AFTER_INSTANCE_READY, adapter)

    assert calls == ["packages", "user_rails"]
    assert adapter._current_expert_id == "expert-code"
    section = adapter._instance.system_prompt_builder.get_section("identity")
    assert (section.content.get("cn") or "") == "你是代码专家阿丙。"


@pytest.mark.asyncio
async def test_code_adapter_hooks_reset_stale_expert_state() -> None:
    """回归：BEFORE_INSTANCE_READY 丢弃旧实例的专家账本引用。"""
    adapter = _make_code_child_adapter("sess-code-2")
    adapter._expert_load_record = object()
    adapter._current_expert_id = "stale-expert"

    await run_assembly_hooks(AssemblyPoint.BEFORE_INSTANCE_READY, adapter)

    assert adapter._expert_load_record is None
    assert adapter._current_expert_id is None


@pytest.mark.asyncio
async def test_hooks_skip_expert_replay_on_root_adapter(
    experts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """root（非 session 级）适配器不装专家：expert 扩展内部自决跳过。"""
    _make_package(experts_dir, "expert-root", "你是专家。")
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: {"expert_id": "expert-root"},
    )
    adapter = _make_code_child_adapter("sess-code-3")
    adapter._is_session_scoped_adapter = False

    await run_assembly_hooks(AssemblyPoint.AFTER_INSTANCE_READY, adapter)

    assert adapter._current_expert_id is None
