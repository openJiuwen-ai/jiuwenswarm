# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ``jiuwenswarm skill-train`` (jiuwenswarm.cli.skill_train)."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.cli import skill_train as st

openjiuwen_skill_train = pytest.importorskip(
    "openjiuwen.agent_evolving.skill_train",
    reason="openjiuwen with agent_evolving.skill_train is required",
)


def _capture_logger_info(monkeypatch):
    """Record ``logger.info`` messages even when handlers bypass pytest caplog/capsys."""
    recorded: list[str] = []
    original = st.logger.info

    def _info(msg, *args, **kwargs):
        recorded.append(msg % args if args else str(msg))
        return original(msg, *args, **kwargs)

    monkeypatch.setattr(st.logger, "info", _info)
    return recorded


class _StubTrainLaunchOptions:
    """Minimal stand-in so CLI unit tests do not require the launch API in openjiuwen."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    @property
    def is_exec_backend(self) -> bool:
        return getattr(self, "target_backend", "") != "openai_chat"

    def resolved_backend(self) -> str:
        return getattr(self, "target_backend", "")


@pytest.fixture
def stub_launch_api(monkeypatch):
    """Ensure TrainLaunchOptions / run_offline_* exist for CLI imports (CI may lack them)."""
    monkeypatch.setattr(
        openjiuwen_skill_train, "TrainLaunchOptions", _StubTrainLaunchOptions, raising=False,
    )
    # run_skill_train imports both names; install placeholders so ImportError cannot fire.
    monkeypatch.setattr(
        openjiuwen_skill_train, "run_offline_training", lambda opts: None, raising=False,
    )
    monkeypatch.setattr(
        openjiuwen_skill_train, "run_offline_eval",
        lambda opts, *, split: {}, raising=False,
    )
    return openjiuwen_skill_train


class TestParser:
    @staticmethod
    def test_defaults_target_jiuwenswarm_exec(monkeypatch):
        monkeypatch.delenv("TARGET_BACKEND", raising=False)
        monkeypatch.delenv("SKILL_TRAIN_ENV", raising=False)
        args = st.build_parser().parse_args([])
        assert args.env == "searchqa"
        assert args.backend == "jiuwenswarm_cli_exec"
        assert args.eval_only is False
        assert args.no_trace_to_optimizer is False

    @staticmethod
    def test_options_map_to_launch_options(stub_launch_api, monkeypatch):
        monkeypatch.delenv("JIUWENSWARM_CLI_PATH", raising=False)
        args = st.build_parser().parse_args([
            "--env", "docvqa", "--limit", "3", "--workers", "2", "--exec-timeout", "90",
            "--gateway-url", "ws://gw:1/tui", "--name", "inst", "--mode", "code.plan",
            "--cli-path", "bin/jiuwenswarm", "--no-gate", "--no-trace-to-optimizer",
            "--data-root", "./data", "--optimizer-model", "opt",
        ])
        opts = st._build_options(args)
        assert opts.env_name == "docvqa"
        assert opts.target_backend == "jiuwenswarm_cli_exec"
        assert opts.limit == 3 and opts.workers == 2 and opts.exec_timeout == 90
        assert opts.jiuwenswarm_gateway_url == "ws://gw:1/tui"
        assert opts.jiuwenswarm_instance_name == "inst"
        assert opts.jiuwenswarm_chat_mode == "code.plan"
        assert opts.jiuwenswarm_cli_path == "bin/jiuwenswarm"
        assert opts.use_gate is False
        assert opts.jiuwenswarm_trace_to_optimizer is False
        assert opts.data_root == "./data"
        assert opts.optimizer_model == "opt"
        assert opts.is_exec_backend

    @staticmethod
    def test_cli_path_prefers_sibling_script(tmp_path, monkeypatch):
        monkeypatch.delenv("JIUWENSWARM_CLI_PATH", raising=False)
        fake_python = tmp_path / "Scripts" / "python.exe"
        fake_python.parent.mkdir()
        fake_python.write_text("")
        (tmp_path / "Scripts" / "jiuwenswarm.exe").write_text("")
        monkeypatch.setattr(sys, "executable", str(fake_python))
        assert st._resolve_cli_path("") == str(tmp_path / "Scripts" / "jiuwenswarm.exe")
        monkeypatch.setenv("JIUWENSWARM_CLI_PATH", "custom")
        assert st._resolve_cli_path("") == "custom"
        assert st._resolve_cli_path("explicit") == "explicit"


class TestGatewayCheck:
    @staticmethod
    def test_check_gateway_ok():
        async def _mock_connect(url, **kwargs):
            class FakeWs:
                async def recv(self):
                    return json.dumps({"type": "event", "event": "connection.ack", "payload": {}})

                async def close(self):
                    pass

            return FakeWs()

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            assert st.check_gateway("ws://127.0.0.1:19001/tui") is True

    @staticmethod
    def test_check_gateway_refused():
        async def _mock_connect(url, **kwargs):
            raise ConnectionRefusedError("refused")

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            assert st.check_gateway("ws://127.0.0.1:1/tui", timeout=0.5) is False


class TestRun:
    @staticmethod
    def test_gateway_down_returns_3(stub_launch_api, monkeypatch):
        args = st.build_parser().parse_args(["--env", "searchqa"])
        monkeypatch.setattr(st, "check_gateway", lambda url, **kw: False)
        called = []
        monkeypatch.setattr(
            stub_launch_api, "run_offline_training", lambda opts: called.append(opts), raising=False,
        )
        assert st.run_skill_train(args) == st.EXIT_GATEWAY_UNREACHABLE
        assert called == []

    @staticmethod
    def test_training_success(stub_launch_api, monkeypatch):
        args = st.build_parser().parse_args(["--env", "searchqa", "--skip-gateway-check", "--limit", "1"])
        seen = {}
        infos = _capture_logger_info(monkeypatch)

        def _fake_train(opts):
            seen["opts"] = opts
            return SimpleNamespace(best_score=0.75, output_dir="out/x")

        monkeypatch.setattr(stub_launch_api, "run_offline_training", _fake_train, raising=False)
        assert st.run_skill_train(args) == 0
        assert seen["opts"].limit == 1
        assert seen["opts"].target_backend == "jiuwenswarm_cli_exec"
        assert any("best_score=0.7500" in msg for msg in infos)

    @staticmethod
    def test_eval_only(stub_launch_api, monkeypatch):
        args = st.build_parser().parse_args(["--eval-only", "--split", "val", "--skip-gateway-check"])
        seen = {}
        infos = _capture_logger_info(monkeypatch)

        def _fake_eval(opts, *, split):
            seen["split"] = split
            return {
                "env": "searchqa",
                "target_backend": "jiuwenswarm_cli_exec",
                "n_items": 2,
                "hard": 0.5,
                "soft": 0.6,
                "output_dir": "o",
            }

        monkeypatch.setattr(stub_launch_api, "run_offline_eval", _fake_eval, raising=False)
        assert st.run_skill_train(args) == 0
        assert seen["split"] == "val"
        assert any("Eval complete" in msg for msg in infos)

    @staticmethod
    def test_bad_config_returns_2(stub_launch_api, monkeypatch):
        args = st.build_parser().parse_args(["--skip-gateway-check"])

        def _raise(opts):
            raise FileNotFoundError("split_dir not found")

        monkeypatch.setattr(stub_launch_api, "run_offline_training", _raise, raising=False)
        assert st.run_skill_train(args) == st.EXIT_BAD_ARGS

    @staticmethod
    def test_chat_backend_skips_gateway_probe(stub_launch_api, monkeypatch):
        args = st.build_parser().parse_args(["--backend", "openai_chat"])
        monkeypatch.setattr(st, "check_gateway", lambda url, **kw: pytest.fail("must not probe"))
        monkeypatch.setattr(
            stub_launch_api,
            "run_offline_training",
            lambda opts: SimpleNamespace(best_score=1.0, output_dir="o"),
            raising=False,
        )
        assert st.run_skill_train(args) == 0
