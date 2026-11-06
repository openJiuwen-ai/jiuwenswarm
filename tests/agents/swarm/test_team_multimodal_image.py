# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team leader 原生多模态：能力位解析、leader-only rail 挂载、上传清单随 build context。

覆盖 S_22 在 team 模式下的三个断点（原先 leader 既无 rail、能力位又默认 False、
上传清单只镜像到 adapter 的 ``self._instance``）：

* ``enable_read_image_multimodal`` 按成员模型条目的 ``modalities`` 解析（声明式，
  不探测、不按模型名猜）；
* ``swarm.multimodal_image`` 只挂 leader，teammate 不挂；
* provider 从 ``SwarmBuildContext.request_metadata`` 取本轮上传清单并绑定到 rail。
"""

from __future__ import annotations

import logging

import pytest

from openjiuwen.agent_teams.schema.deep_agent_spec import (
    DeepAgentSpec,
    RailSpec,
    TeamModelConfig,
)
from openjiuwen.core.foundation.llm.schema.config import (
    ModelClientConfig,
    ModelRequestConfig,
)

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
from jiuwenswarm.agents.swarm.context import SwarmBuildContext


def _config(*, modalities="image", budgets=None) -> dict:
    """Minimal config: one default model entry plus optional image budgets."""
    return {
        "models": {
            "defaults": [
                {
                    "model_client_config": {"model_name": "vision-model"},
                    "modalities": modalities,
                    "is_default": True,
                }
            ]
        },
        "react": budgets or {},
    }


def _pinned_model_spec(model_name: str) -> DeepAgentSpec:
    return DeepAgentSpec(
        model=TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_provider="openai",
                api_key="sk-unit-test",
                api_base="http://127.0.0.1:1",
            ),
            model_request_config=ModelRequestConfig(model_name=model_name),
        )
    )


def _leader_rail(spec: DeepAgentSpec) -> RailSpec | None:
    return next(
        (rail for rail in spec.rails or [] if rail.type == registry.MULTIMODAL_IMAGE),
        None,
    )


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
def test_leader_gets_multimodal_rail_and_capability_flag(mode: str) -> None:
    """leader 必须挂 rail，且能力位按模型 modalities 写成显式 bool。"""

    config = _config(modalities=["text", "image"])

    leader = build_member_deep_agent_spec(config, mode, "leader", DeepAgentSpec())

    assert leader.enable_read_image_multimodal is True
    rail = _leader_rail(leader)
    assert rail is not None
    assert rail.params["enable_image_multimodal"] is True
    # leader 额外拿到"把图交给成员时保留引用行"的提示开关。
    assert rail.params["dispatch_hint"] is True


def test_teammate_gets_multimodal_rail_without_dispatch_hint() -> None:
    """teammate 也挂 rail（可按引用行原生看图），但不需要"转发给成员"的提示。"""

    config = _config(modalities=["text", "image"])

    teammate = build_member_deep_agent_spec(config, "team", "teammate", DeepAgentSpec())

    assert teammate.enable_read_image_multimodal is True
    rail = _leader_rail(teammate)
    assert rail is not None
    assert rail.params["enable_image_multimodal"] is True
    assert "dispatch_hint" not in rail.params


def test_text_only_model_disables_native_image() -> None:
    """modalities 不含 image → 能力位 False，rail 走「过滤视图」分支（N2/N3）。"""

    config = _config(modalities="text")

    leader = build_member_deep_agent_spec(config, "team", "leader", DeepAgentSpec())

    assert leader.enable_read_image_multimodal is False
    rail = _leader_rail(leader)
    assert rail is not None
    assert rail.params["enable_image_multimodal"] is False


def test_member_model_entry_matches_by_model_name() -> None:
    """成员 spec 指定了模型时按该条目判定，而不是默认条目。"""

    config = {
        "models": {
            "defaults": [
                {
                    "model_client_config": {"model_name": "vision-model"},
                    "modalities": "text,image",
                    "is_default": True,
                },
                {
                    "model_client_config": {"model_name": "text-model"},
                    "modalities": "text",
                },
            ]
        }
    }

    leader = build_member_deep_agent_spec(
        config, "team", "leader", _pinned_model_spec("text-model")
    )

    assert leader.enable_read_image_multimodal is False


def test_unknown_pinned_model_does_not_inherit_default_modalities(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """成员 pinned 的模型名未声明时判 False，不得借用默认条目的能力位（N2）。"""

    config = _config(modalities=["text", "image"])

    with caplog.at_level(
        logging.WARNING, logger="jiuwenswarm.agents.swarm.config_specs"
    ):
        leader = build_member_deep_agent_spec(
            config, "team", "leader", _pinned_model_spec("not-listed-model")
        )

    assert leader.enable_read_image_multimodal is False
    rail = _leader_rail(leader)
    assert rail is not None
    assert rail.params["enable_image_multimodal"] is False
    # warning 里带成员模型名，便于定位配置漂移。
    assert "not-listed-model" in caplog.text


def test_pinned_model_matches_alias() -> None:
    """成员 spec 用 alias 声明模型时，按条目顶层 alias 命中同一份能力声明。"""

    config = {
        "models": {
            "defaults": [
                {
                    "alias": "vision",
                    "model_client_config": {"model_name": "vision-model"},
                    "modalities": ["text", "image"],
                }
            ]
        }
    }

    leader = build_member_deep_agent_spec(
        config, "team", "leader", _pinned_model_spec("vision")
    )

    assert leader.enable_read_image_multimodal is True


def test_config_without_models_section_disables_native_image() -> None:
    """无 models 段（env 兜底形态）→ 未声明即不支持，不再按 env 条目判定（N2）。"""

    leader = build_member_deep_agent_spec(
        {"react": {}}, "team", "leader", DeepAgentSpec()
    )

    assert leader.enable_read_image_multimodal is False


def test_legacy_single_default_entry_is_honored() -> None:
    """旧格式 ``models.default`` 单对象同样按声明解析，不因缺 defaults 列表判 False。"""

    config = {
        "models": {
            "default": {
                "model_client_config": {"model_name": "vision-model"},
                "modalities": "text,image",
            }
        }
    }

    leader = build_member_deep_agent_spec(config, "team", "leader", DeepAgentSpec())

    assert leader.enable_read_image_multimodal is True


class _DeepCopyProbe:
    """探针：模型条目一旦被 deepcopy（旧实现 ``get_default_models`` 的解密路径）即报错。"""

    def __deepcopy__(self, memo: dict[int, object]) -> _DeepCopyProbe:
        raise AssertionError("能力位解析不得对模型条目做 deepcopy / api_key 解密")


def test_capability_resolution_skips_entry_copy_and_decryption() -> None:
    """能力位只读声明字段；不因每成员每轮调用而深拷贝 / 解密模型条目。"""

    config = _config(modalities=["text", "image"])
    config["models"]["defaults"][0]["copy_probe"] = _DeepCopyProbe()

    leader = build_member_deep_agent_spec(config, "team", "leader", DeepAgentSpec())

    assert leader.enable_read_image_multimodal is True


def test_explicit_spec_flag_wins_over_modalities() -> None:
    """blueprint 显式写了能力位时以显式值为准（不覆盖用户配置）。"""

    config = _config(modalities=["text", "image"])
    base = DeepAgentSpec(enable_read_image_multimodal=False)

    leader = build_member_deep_agent_spec(config, "team", "leader", base)

    assert leader.enable_read_image_multimodal is False
    rail = _leader_rail(leader)
    assert rail is not None
    assert rail.params["enable_image_multimodal"] is False


def test_rail_params_carry_image_budgets() -> None:
    """三预算从 ``react.multimodal_image_*`` 烘进 RailSpec.params（0 表示不限）。"""

    config = _config(
        modalities=["text", "image"],
        budgets={
            "multimodal_image_budget": 1048576,
            "multimodal_image_token_budget": 2048,
            "multimodal_image_max_count": 4,
        },
    )

    leader = build_member_deep_agent_spec(config, "team", "leader", DeepAgentSpec())

    assert _leader_rail(leader).params == {
        "enable_image_multimodal": True,
        "max_total_bytes": 1048576,
        "max_visual_tokens": 2048,
        "max_count": 4,
        "dispatch_hint": True,
    }


def test_provider_binds_round_manifest_from_build_context() -> None:
    """provider 构造 rail 时快照 build context 里的本轮上传清单。"""

    config = _config(modalities=["text", "image"])
    leader = build_member_deep_agent_spec(config, "team", "leader", DeepAgentSpec())
    rail_spec = _leader_rail(leader)
    context = SwarmBuildContext(
        session_id="session-1",
        request_metadata={
            "multimodal_image_files": [
                {
                    "type": "image",
                    "path": "/tmp/0.jpg",
                    "filename": "0.jpg",
                    "mime_type": "image/jpeg",
                }
            ]
        },
    )

    rail = RailSpec(type=rail_spec.type, params=rail_spec.params).build(
        language="cn", context=context
    )

    assert rail is not None
    assert rail._bound_image_files == [
        {
            "type": "image",
            "path": "/tmp/0.jpg",
            "filename": "0.jpg",
            "mime_type": "image/jpeg",
        }
    ]
    assert rail._enable_image_multimodal is True
    assert rail._dispatch_hint is True


def test_teammate_provider_binds_the_same_round_manifest() -> None:
    """teammate 的 rail 同样从 build context 拿到本轮清单（是否出图由引用行决定）。"""

    config = _config(modalities=["text", "image"])
    teammate = build_member_deep_agent_spec(config, "team", "teammate", DeepAgentSpec())
    rail_spec = _leader_rail(teammate)
    context = SwarmBuildContext(
        session_id="session-1",
        request_metadata={
            "multimodal_image_files": [
                {
                    "type": "image",
                    "path": "/tmp/1.jpg",
                    "filename": "1.jpg",
                    "mime_type": "image/jpeg",
                }
            ]
        },
    )

    rail = RailSpec(type=rail_spec.type, params=rail_spec.params).build(
        language="cn", context=context
    )

    assert rail is not None
    assert [item["path"] for item in rail._bound_image_files] == ["/tmp/1.jpg"]
    assert rail._dispatch_hint is False


def test_provider_without_manifest_leaves_rail_unbound() -> None:
    """request_metadata 里没有清单时返回未绑定 rail（回退 agent 属性 / ContextVar）。"""

    config = _config(modalities=["text", "image"])
    leader = build_member_deep_agent_spec(config, "team", "leader", DeepAgentSpec())
    rail_spec = _leader_rail(leader)
    context = SwarmBuildContext(session_id="session-1")

    rail = RailSpec(type=rail_spec.type, params=rail_spec.params).build(
        language="cn", context=context
    )

    assert rail is not None
    assert rail._bound_image_files is None
