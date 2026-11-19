# coding: utf-8
"""stop_reason 透传、配置面旋钮。
"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


def _answer_chunk(output: str, stop_reason: str | None = None):
    payload = {"output": output, "result_type": "answer"}
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    return SimpleNamespace(type="answer", payload=payload)


class TestStopReasonPassthrough:
    """answer 帧的 stop_reason 必须随 chat.final 透传到消费侧。"""

    def test_final_carries_stop_reason(self):
        out = parse_stream_chunk(_answer_chunk("done", "max_rounds"))
        assert out is not None
        assert out["event_type"] == "chat.final"
        assert out["stop_reason"] == "max_rounds"

    def test_budget_total_carried(self):
        out = parse_stream_chunk(_answer_chunk("done", "budget_total"))
        assert out["stop_reason"] == "budget_total"

    def test_no_tag_no_key(self):
        out = parse_stream_chunk(_answer_chunk("done"))
        assert out is not None
        assert "stop_reason" not in out


class TestIterationExhaustedProposal:
    """裁决器第八源用例见 agentserver/test_round_terminal_arbiter.py。"""


class TestConfigLoaderBudgetKnobs:
    """迭代预算（复用 max_iterations）与 max_total_rounds。"""

    def test_defaults_injected(self):
        from jiuwenswarm.agents.harness.team import config_loader

        spec = config_loader.load_team_spec_dict(config_base={})
        assert spec["max_total_rounds"] == 500
        assert spec["agents"]["leader"]["task_loop_iteration_budget"] == 200
        assert spec["agents"]["teammate"]["task_loop_iteration_budget"] == 200

    def test_overrides_respected(self):
        from jiuwenswarm.agents.harness.team import config_loader

        config_base = {
            "modes": {
                "team": {
                    "t1": {
                        "team_name": "t1",
                        "max_total_rounds": 50,
                        "agents": {
                            "leader": {"max_iterations": 7},
                            "teammate": {},
                        },
                    }
                }
            }
        }
        spec = config_loader.load_team_spec_dict(config_base)
        assert spec["max_total_rounds"] == 50
        assert spec["agents"]["leader"]["task_loop_iteration_budget"] == 7
        assert spec["agents"]["teammate"]["task_loop_iteration_budget"] == 200

    def test_shipped_jiuwen_team_template_is_read(self):
        """出厂 config.yaml 的 modes.team.jiuwen_team 两个旋钮真实生效。"""
        from pathlib import Path

        import yaml

        import jiuwenswarm

        from jiuwenswarm.agents.harness.team import config_loader

        yaml_path = Path(jiuwenswarm.__file__).resolve().parent / "resources" / "config.yaml"
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert "jiuwen_team" in data["modes"]["team"]
        spec = config_loader.load_team_spec_dict(
            config_base=data, template_id="jiuwen_team",
        )
        # 与出厂 config.yaml 的 modes.team.jiuwen_team.max_total_rounds 对齐
        assert spec["max_total_rounds"] == 1000
        assert spec["agents"]["leader"]["task_loop_iteration_budget"] == 200
        assert spec["agents"]["teammate"]["task_loop_iteration_budget"] == 200
