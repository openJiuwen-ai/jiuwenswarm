# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Example: offline skill_train sleep cycle over JiuwenSwarm traces.

Copied from agent-core ``examples/agent_evolving/skill_sleep_example.py`` for
running inside this jiuwenswarm checkout (``uv run`` / project ``.env``).

Prerequisite (daytime): a JiuwenSwarm observation dir with ``traces-*.jsonl``
(for example ``%USERPROFILE%\\.jiuwenswarm\\.trace``). Sleep groups spans by
OTLP ``traceId`` and harvests one SessionDigest per complete conversation.

Gate 通过后自动经 EvolutionStore 归档并写入新 skill 版本。
仅更新轨迹中检测到的 skill（skill_tool 等）；无 hint 的任务会被跳过，不再创建兜底 skill。

多轮会话：每个实质请求切成一段、一段一条 task；问候被丢弃，纠错 / 追问归到前一个请求
并进入 soft rubric。``--rubric-synthesis llm`` 可让 optimizer 模型把 follow-up 进一步合成为
可核查清单。

Usage (from this repo root)::

  uv run python examples/agent_evolving/skill_sleep_example.py ^
    --trajectory-dir %USERPROFILE%\\.jiuwenswarm\\.trace ^
    --skills-base-dir ./outputs/skill_sleep_skills ^
    --backend model

Optimizer/target LLM attempt timeout defaults to 300s (override with
``LLM_ATTEMPT_TIMEOUT``); total budget scales with ``LLM_MAX_ATTEMPTS``.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

from openjiuwen.agent_evolving.checkpointing.evolution_store import EvolutionStore
from openjiuwen.agent_evolving.skill_train.llm_client import ChatLLMClient, make_llm_invoke_policy
from openjiuwen.agent_evolving.skill_train.sleep import SleepConfig, run_sleep_cycle
from openjiuwen.agent_evolving.skill_train.sleep.backend import build_backend
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.llm.model import Model

# DeepSeek-style reasoning models often exceed the default 120s on reflect.
_DEFAULT_ATTEMPT_TIMEOUT = 300.0
_DEFAULT_MAX_ATTEMPTS = 3


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _load_dotenv() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    for env_path in (
        repo_root / ".env",
        Path.home() / ".jiuwenswarm" / "config" / ".env",
    ):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _build_chat_client() -> ChatLLMClient:
    provider = _env("MODEL_PROVIDER", "OPTIMIZER_PROVIDER", "TARGET_PROVIDER", default="openai")
    api_key = _env("API_KEY", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY")
    api_base = _env("API_BASE", "OPENAI_COMPATIBLE_BASE_URL")
    model_name = _env("MODEL_NAME", "OPENAI_COMPATIBLE_MODEL", "OPTIMIZER_MODEL", default="GLM-5.2")
    required = (
        ("API_KEY / OPENAI_COMPATIBLE_API_KEY", api_key),
        ("API_BASE / OPENAI_COMPATIBLE_BASE_URL", api_base),
        ("MODEL_NAME / OPENAI_COMPATIBLE_MODEL", model_name),
    )
    missing = []
    for label, value in required:
        if not value:
            missing.append(label)
    if missing:
        raise ValueError("Missing required environment variables: " + ", ".join(missing))
    attempt_timeout = float(_env("LLM_ATTEMPT_TIMEOUT", default=str(_DEFAULT_ATTEMPT_TIMEOUT)))
    max_attempts = int(_env("LLM_MAX_ATTEMPTS", default=str(_DEFAULT_MAX_ATTEMPTS)))
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider=provider,
            api_key=api_key,
            api_base=api_base,
        ),
        model_config=ModelRequestConfig(model=model_name),
    )
    return ChatLLMClient(
        llm=model,
        model=model_name,
        policy=make_llm_invoke_policy(
            attempt_timeout_secs=attempt_timeout,
            total_budget_secs=attempt_timeout * max_attempts + 30.0,
            max_attempts=max_attempts,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="skill_train sleep cycle (JiuwenSwarm traces)")
    parser.add_argument(
        "--trajectory-dir",
        required=True,
        help="JiuwenSwarm .trace dir (traces-*.jsonl)",
    )
    parser.add_argument(
        "--skills-base-dir",
        required=True,
        help="EvolutionStore skills root (gate 通过后写入新 skill 版本)",
    )
    parser.add_argument(
        "--skill-name",
        default="",
        help="可选：仅当轨迹检测到该 skill 且 store 无内容时，配合 --skill-init 提供初始 baseline",
    )
    parser.add_argument(
        "--skill-init",
        default="",
        help="可选：与 --skill-name 配套的初始 SKILL.md 路径",
    )
    parser.add_argument(
        "--trace-id",
        default=None,
        help="可选：只 harvest 该 OTLP traceId（完整对话轨迹）",
    )
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--staging-root", default="")
    parser.add_argument("--backend", default="model", choices=["mock", "model"])
    parser.add_argument(
        "--rubric-synthesis",
        default="off",
        choices=["off", "llm"],
        help="off: 仅用 follow-up 启发式拼 rubric；llm: 额外调用 optimizer 模型合成可核查的 rubric 清单",
    )
    parser.add_argument(
        "--gate-mode",
        default="on",
        choices=["on", "off", "none", "false", "greedy"],
        help="on: holdout gate 验收后才写 skill；off/greedy/none/false: 跳过 gate，有 edits 就直接更新 skill",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只跑 harvest/mine/consolidate，不写 staging、不落盘",
    )
    args = parser.parse_args()

    traj_dir = Path(args.trajectory_dir).expanduser()
    if not traj_dir.exists():
        raise SystemExit(f"trajectory dir not found: {traj_dir}")

    target_client = None
    optimizer_client = None
    if args.backend == "model":
        _load_dotenv()
        try:
            client = _build_chat_client()
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        target_client = client
        optimizer_client = client

    store = EvolutionStore(args.skills_base_dir)
    cfg = SleepConfig(
        trajectory_store_dir=str(traj_dir),
        skills_base_dir=args.skills_base_dir,
        skill_name=args.skill_name,
        skill_init=args.skill_init,
        trace_id=args.trace_id,
        state_dir=args.state_dir or str(Path("./outputs/skill_sleep_state").resolve()),
        staging_root=args.staging_root or "",
        backend=args.backend,
        rubric_synthesis=args.rubric_synthesis,
        gate_mode=args.gate_mode,
        progress=True,
    )
    outcome = run_sleep_cycle(
        cfg,
        dry_run=args.dry_run,
        backend=build_backend(
            args.backend,
            target_client=target_client,
            optimizer_client=optimizer_client,
        ),
        target_client=target_client,
        optimizer_client=optimizer_client,
        evolution_store=store,
    )

    group_info = ""
    if outcome.report.skill_groups:
        group_info = (
            " groups=["
            + ", ".join(f"{g.skill_name}:{g.status}:accepted={g.accepted}" for g in outcome.report.skill_groups)
            + "]"
        )

    adopt_info = ""
    if outcome.adopted_skills:
        parts = [f"{item.skill_name}:{item.previous_version}->{item.new_version}" for item in outcome.adopted_skills]
        adopt_info = " adopted=[" + ", ".join(parts) + "]"

    logger.info(
        "night=%s accepted=%s tasks=%s staging=%s%s%s",
        outcome.night,
        outcome.report.accepted,
        outcome.report.n_tasks,
        outcome.staging_dir,
        group_info,
        adopt_info,
    )
    if outcome.report.notes:
        logger.info("notes: %s", "; ".join(outcome.report.notes))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
