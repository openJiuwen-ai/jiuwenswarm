# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team/swarm 成员的 context_engine_config 透传。

成员经 ``build_member_deep_agent_spec`` 装配；不显式写入该字段时，成员落到
agent-core 默认值（字符估算），与主 agent 的精确计数口径不一致。
"""

from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
from openjiuwen.core.context_engine.schema.config import ContextEngineConfig

from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec


def _config() -> dict:
    return {
        "react": {
            "context_engine_config": {
                "enable_tiktoken_counter": True,
                "enable_reload": True,
            }
        }
    }


def test_member_inherits_exact_token_counter_switch() -> None:
    spec = build_member_deep_agent_spec(_config(), "team", "teammate", DeepAgentSpec())

    assert isinstance(spec.context_engine_config, ContextEngineConfig)
    assert spec.context_engine_config.enable_tiktoken_counter is True
    assert spec.context_engine_config.enable_reload is True
