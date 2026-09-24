"""IM 学习配置面（GW-01 学习子集）的宿主管道单测。

覆盖：patch 白名单扩展、写前校验、等价快路径（仅改标题不重启）、
运行时重建、失败双回滚，以及 get_im_learning_status / run_im_learning_now
透传与未配置投影。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from openjiuwen.harness.personal_context import PersonalContext

from jiuwenswarm.server.personal_context import host_api as host_module
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


def _pin_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_module, "get_default_models", lambda: [])


class _FakeSnapshot:
    state = "RUNNING"


class FakeImLearningCore:
    """FakeCore 最小面：只实现 im_learning 测试触达的 Core 接口。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.configured: object | None = None
        self.active = False
        self.set_error: BaseException | None = None
        self.activate_error: BaseException | None = None
        self.deactivate_error: BaseException | None = None
        self.snapshot_state = "RUNNING"
        self.im_learning_status: dict[str, object] = {
            "running": True,
            "enabled": True,
            "targets": 2,
        }
        self.im_learning_trigger_result = True

    def _set_embedding_configuration(
        self,
        *,
        model_name: str | None,
        base_url: str | None,
        api_key: str | None,
    ) -> None:
        self.calls.append(("_set_embedding_configuration", None))

    async def snapshot(self) -> object:
        self.calls.append(("snapshot", None))
        snapshot = _FakeSnapshot()
        snapshot.state = self.snapshot_state
        return snapshot

    async def set_configuration(self, config: object) -> None:
        self.calls.append(("set_configuration", None))
        if self.set_error is not None:
            error = self.set_error
            self.set_error = None
            raise error
        self.configured = config

    async def activate_runtime(self) -> None:
        self.calls.append(("activate_runtime", None))
        if self.activate_error is not None:
            error = self.activate_error
            self.activate_error = None
            raise error
        self.active = True

    async def deactivate_runtime(self, *, timeout_seconds: float = 30.0) -> None:
        self.calls.append(("deactivate_runtime", timeout_seconds))
        if self.deactivate_error is not None:
            error = self.deactivate_error
            self.deactivate_error = None
            raise error
        self.active = False

    async def get_im_learning_status(self) -> dict[str, object]:
        self.calls.append(("get_im_learning_status", None))
        return dict(self.im_learning_status)

    async def run_im_learning_now(self) -> bool:
        self.calls.append(("run_im_learning_now", None))
        return self.im_learning_trigger_result


@pytest.fixture
def im_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[PersonalContextHostAPI, FakeImLearningCore]:
    host = PersonalContextHostAPI(home=tmp_path / "personal_context")
    fake = FakeImLearningCore()
    monkeypatch.setattr(host, "_personal_context", fake)
    return host, fake


def _im_learning_node(
    *,
    external_id: str = "G1",
    title: str = "项目群",
    enabled: bool = True,
    interval: float = 300.0,
) -> dict[str, object]:
    return {
        "enabled": enabled,
        "targets": [
            {
                "channel_id": "welink",
                "kind": "group",
                "external_id": external_id,
                "title": title,
            }
        ],
        "since_ms": None,
        "fetch_interval_seconds": interval,
        "fetch_top_n": 30,
    }


def _read_yaml(host: PersonalContextHostAPI) -> dict[str, object]:
    return yaml.safe_load(
        (host._home / "personal_context.yaml").read_text(encoding="utf-8")
    )


@pytest.mark.asyncio
async def test_patch_im_learning_persists_and_rebuilds_runtime(
    im_host: tuple[PersonalContextHostAPI, FakeImLearningCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_models(monkeypatch)
    host, fake = im_host
    await host.set_collection_enabled(True)
    fake.calls.clear()

    result = await host.patch_runtime_config(
        {"im_learning": _im_learning_node()}
    )

    assert result["im_learning"]["enabled"] is True
    assert result["im_learning"]["targets"][0]["external_id"] == "G1"
    assert result["im_learning"]["fetch_interval_seconds"] == 300.0

    stored = _read_yaml(host)
    assert stored["im_learning"]["enabled"] is True
    assert stored["im_learning"]["targets"][0]["external_id"] == "G1"

    # 非等价变更（新增 targets）必须走 deactivate → set → activate 重建。
    names = [name for name, _ in fake.calls]
    assert names == [
        "snapshot",
        "deactivate_runtime",
        "set_configuration",
        "_set_embedding_configuration",
        "activate_runtime",
    ]
    assert fake.active is True
    assert fake.configured is not None
    assert fake.configured.im_learning.enabled is True
    assert fake.configured.im_learning.targets[0].external_id == "G1"


@pytest.mark.asyncio
async def test_patch_im_learning_title_only_takes_fast_path(
    im_host: tuple[PersonalContextHostAPI, FakeImLearningCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_models(monkeypatch)
    host, fake = im_host
    await host.set_collection_enabled(True)
    await host.patch_runtime_config(
        {"im_learning": _im_learning_node(title="旧标题")}
    )
    fake.calls.clear()

    result = await host.patch_runtime_config(
        {"im_learning": _im_learning_node(title="新标题")}
    )

    # title 是纯展示元数据：仅改标题不触发运行时重启（等价快路径）。
    names = [name for name, _ in fake.calls]
    assert names == ["snapshot"]

    # yaml 已落新标题；Core 内存保持旧配置（title 不参与调度语义）。
    stored = _read_yaml(host)
    assert stored["im_learning"]["targets"][0]["title"] == "新标题"
    assert result["im_learning"]["targets"][0]["title"] == "新标题"
    assert fake.configured.im_learning.targets[0].title == "旧标题"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "im_learning",
    [
        # enabled=true 必须有 targets（Core model_validator）。
        {"enabled": True, "targets": []},
        # 目标三元组必须唯一。
        {
            "enabled": True,
            "targets": [
                {"channel_id": "welink", "kind": "group", "external_id": "G1"},
                {"channel_id": "welink", "kind": "group", "external_id": "G1"},
            ],
        },
        # 周期与批量越界。
        {"fetch_interval_seconds": 0},
        {"fetch_interval_seconds": 40_000_000},
        {"fetch_top_n": 0},
        {"fetch_top_n": 201},
        {"since_ms": -1},
        # extra=forbid：未知键拒绝。
        {"unknown_key": 1},
        # im_learning 是必选结构，不接受整节置空。
        None,
    ],
)
async def test_patch_im_learning_invalid_is_rejected_before_write(
    im_host: tuple[PersonalContextHostAPI, FakeImLearningCore],
    monkeypatch: pytest.MonkeyPatch,
    im_learning: object,
) -> None:
    _pin_models(monkeypatch)
    host, fake = im_host
    await host.set_collection_enabled(True)
    yaml_path = host._home / "personal_context.yaml"
    yaml_before = yaml_path.read_bytes()
    fake.calls.clear()

    with pytest.raises(PersonalContext.Error):
        await host.patch_runtime_config({"im_learning": im_learning})

    # 写前校验：yaml 未被写入，Core 未被触碰。
    assert yaml_path.read_bytes() == yaml_before
    assert fake.calls == []
    assert await host.get_runtime_config() == await host.get_runtime_config()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        # distill 生产装配缺失，节只读不写（方案 D7）。
        {"distill": {"enabled": True}},
        # fetch_services / 模型选择走各自的专用 API。
        {"fetch_services": []},
        {"model_index": 0},
    ],
)
async def test_patch_still_rejects_non_whitelisted_top_level_keys(
    im_host: tuple[PersonalContextHostAPI, FakeImLearningCore],
    monkeypatch: pytest.MonkeyPatch,
    patch: dict[str, object],
) -> None:
    _pin_models(monkeypatch)
    host, fake = im_host
    await host.set_collection_enabled(True)
    fake.calls.clear()

    with pytest.raises(PersonalContext.Error, match="unsupported fields"):
        await host.patch_runtime_config(patch)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_im_learning_status_and_run_now_passthrough(
    im_host: tuple[PersonalContextHostAPI, FakeImLearningCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, fake = im_host

    # 未配置：安全投影 + host_active=False，不触碰 Core。
    assert await host.get_im_learning_status() == {
        "running": False,
        "enabled": False,
        "host_active": False,
    }
    assert await host.run_im_learning_now() is False
    assert fake.calls == []

    _pin_models(monkeypatch)
    await host.set_collection_enabled(True)
    fake.im_learning_status = {
        "running": True,
        "enabled": True,
        "targets": 2,
        "backfill": {"ready": False, "pending": [{"channel_id": "welink"}]},
    }
    fake.calls.clear()

    status = await host.get_im_learning_status()
    assert status == {
        "host_active": True,
        "running": True,
        "enabled": True,
        "targets": 2,
        "backfill": {"ready": False, "pending": [{"channel_id": "welink"}]},
    }

    fake.im_learning_trigger_result = False
    assert await host.run_im_learning_now() is False
    assert [name for name, _ in fake.calls] == [
        "get_im_learning_status",
        "run_im_learning_now",
    ]


@pytest.mark.asyncio
async def test_patch_im_learning_failure_rolls_back_yaml_and_runtime(
    im_host: tuple[PersonalContextHostAPI, FakeImLearningCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_models(monkeypatch)
    host, fake = im_host
    await host.set_collection_enabled(True)
    await host.patch_runtime_config(
        {"im_learning": _im_learning_node(external_id="G1")}
    )
    yaml_before = (host._home / "personal_context.yaml").read_bytes()
    fake.calls.clear()

    fake.set_error = RuntimeError("core set failed")
    with pytest.raises(PersonalContext.Error):
        await host.patch_runtime_config(
            {"im_learning": _im_learning_node(external_id="G2")}
        )

    # yaml 与 runtime 双回滚到旧配置。
    assert (host._home / "personal_context.yaml").read_bytes() == yaml_before
    names = [name for name, _ in fake.calls]
    assert names.count("set_configuration") == 2  # 新配置失败 + 回滚旧配置
    assert names.count("activate_runtime") == 1  # 回滚后恢复运行
    assert fake.configured is not None
    assert fake.configured.im_learning.targets[0].external_id == "G1"
    assert fake.active is True
    assert host._stored_config is not None
    rollback_node = host._stored_config["im_learning"]
    assert rollback_node["targets"][0]["external_id"] == "G1"


def test_semantic_dump_ignores_im_learning_target_titles() -> None:
    def _config(title: str) -> PersonalContext.Config:
        return PersonalContext.Config.from_dict(
            {
                "collection_enabled": False,
                "agent_use_enabled": False,
                "strategy_profile": "rules",
                "fetch_services": [],
                "im_learning": _im_learning_node(title=title),
            }
        )

    left = _config("标题甲")
    right = _config("标题乙")
    assert left != right
    assert host_module._configs_equivalent(left, right)

    right_interval = _config("标题甲")
    # 直接构造仅周期不同的配置验证语义比较仍敏感。
    object.__setattr__(
        right_interval.im_learning, "fetch_interval_seconds", 900.0
    )
    assert not host_module._configs_equivalent(left, right_interval)
