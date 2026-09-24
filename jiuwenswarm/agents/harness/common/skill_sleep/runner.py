# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Background skill_train sleep cycle launcher (non-blocking)."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.skill_sleep.counter import SkillCallCounter

logger = logging.getLogger(__name__)

_DEFAULT_ATTEMPT_TIMEOUT = 300.0
_DEFAULT_MAX_ATTEMPTS = 3


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for env_path in (
        Path.home() / ".jiuwenswarm" / "config" / ".env",
        Path.cwd() / ".env",
    ):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _build_chat_client() -> Any:
    from openjiuwen.agent_evolving.skill_train.llm_client import (
        ChatLLMClient,
        make_llm_invoke_policy,
    )
    from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.foundation.llm.model import Model

    provider = _env("MODEL_PROVIDER", "OPTIMIZER_PROVIDER", "TARGET_PROVIDER", default="openai")
    api_key = _env("API_KEY", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY")
    api_base = _env("API_BASE", "OPENAI_COMPATIBLE_BASE_URL")
    model_name = _env(
        "MODEL_NAME", "OPENAI_COMPATIBLE_MODEL", "OPTIMIZER_MODEL", default="GLM-5.2"
    )
    required = (
        ("API_KEY / OPENAI_COMPATIBLE_API_KEY", api_key),
        ("API_BASE / OPENAI_COMPATIBLE_BASE_URL", api_base),
        ("MODEL_NAME / OPENAI_COMPATIBLE_MODEL", model_name),
    )
    missing = [label for label, value in required if not value]
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


def run_sleep_cycle_sync(
    *,
    trajectory_dir: str | Path,
    skills_base_dir: str | Path,
    state_dir: str | Path,
    skill_name: str = "",
    backend: str = "model",
    gate_mode: str = "on",
    rubric_synthesis: str = "off",
    dry_run: bool = False,
) -> Any:
    """Run one sleep cycle synchronously (intended for ``asyncio.to_thread``)."""
    from openjiuwen.agent_evolving.checkpointing.evolution_store import EvolutionStore
    from openjiuwen.agent_evolving.skill_train.sleep import SleepConfig, run_sleep_cycle
    from openjiuwen.agent_evolving.skill_train.sleep.backend import build_backend

    traj_dir = Path(trajectory_dir).expanduser()
    if not traj_dir.exists():
        raise FileNotFoundError(f"trajectory dir not found: {traj_dir}")

    skills_dir = str(Path(skills_base_dir).expanduser())
    sleep_state = Path(state_dir).expanduser()
    sleep_state.mkdir(parents=True, exist_ok=True)

    target_client = None
    optimizer_client = None
    if backend == "model":
        _load_dotenv()
        client = _build_chat_client()
        target_client = client
        optimizer_client = client

    store = EvolutionStore(skills_dir)
    cfg = SleepConfig(
        trajectory_store_dir=str(traj_dir),
        skills_base_dir=skills_dir,
        skill_name=skill_name or "",
        skill_init="",
        trace_id=None,
        state_dir=str(sleep_state),
        staging_root="",
        backend=backend,
        rubric_synthesis=rubric_synthesis,
        gate_mode=gate_mode,
        progress=True,
    )
    return run_sleep_cycle(
        cfg,
        dry_run=dry_run,
        backend=build_backend(
            backend,
            target_client=target_client,
            optimizer_client=optimizer_client,
        ),
        target_client=target_client,
        optimizer_client=optimizer_client,
        evolution_store=store,
    )


class SkillSleepRunner:
    """Reset counter and launch at most one sleep cycle in the background."""

    def __init__(
        self,
        *,
        counter: SkillCallCounter,
        trajectory_dir: str | Path,
        skills_base_dir: str | Path,
        state_dir: str | Path,
        backend: str = "model",
        gate_mode: str = "on",
        rubric_synthesis: str = "off",
        on_task_created: Callable[[asyncio.Task], None] | None = None,
    ) -> None:
        self._counter = counter
        self._trajectory_dir = Path(trajectory_dir)
        self._skills_base_dir = Path(skills_base_dir)
        self._state_dir = Path(state_dir)
        self._backend = backend or "model"
        self._gate_mode = gate_mode or "on"
        self._rubric_synthesis = rubric_synthesis or "off"
        self._on_task_created = on_task_created
        self._lock = threading.Lock()
        self._inflight = False

    @property
    def inflight(self) -> bool:
        with self._lock:
            return self._inflight

    def try_start(self, skill_name: str) -> bool:
        """Reset *skill_name* counter and start sleep if no cycle is running.

        Returns True when a background task was scheduled.
        """
        name = str(skill_name or "").strip()
        if not name:
            return False
        with self._lock:
            if self._inflight:
                logger.info(
                    "[SkillSleepRunner] skip start for %s: sleep already in flight",
                    name,
                )
                return False
            self._inflight = True

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            with self._lock:
                self._inflight = False
            logger.warning(
                "[SkillSleepRunner] no running event loop; cannot start sleep for %s",
                name,
            )
            return False

        # Reset as soon as offline training is accepted for launch.
        self._counter.reset(name)
        logger.info(
            "[SkillSleepRunner] starting sleep for skill=%s trajectory=%s skills=%s",
            name,
            self._trajectory_dir,
            self._skills_base_dir,
        )

        task = loop.create_task(self._run_async(name), name=f"skill-sleep:{name}")
        if self._on_task_created is not None:
            try:
                self._on_task_created(task)
            except Exception:
                logger.debug(
                    "[SkillSleepRunner] on_task_created failed", exc_info=True
                )
        return True

    async def _run_async(self, skill_name: str) -> None:
        try:
            outcome = await asyncio.to_thread(
                run_sleep_cycle_sync,
                trajectory_dir=self._trajectory_dir,
                skills_base_dir=self._skills_base_dir,
                state_dir=self._state_dir,
                skill_name=skill_name,
                backend=self._backend,
                gate_mode=self._gate_mode,
                rubric_synthesis=self._rubric_synthesis,
            )
            report = getattr(outcome, "report", None)
            logger.info(
                "[SkillSleepRunner] sleep finished skill=%s night=%s accepted=%s tasks=%s",
                skill_name,
                getattr(outcome, "night", None),
                getattr(report, "accepted", None),
                getattr(report, "n_tasks", None),
            )
        except Exception:
            logger.warning(
                "[SkillSleepRunner] sleep failed skill=%s",
                skill_name,
                exc_info=True,
            )
        finally:
            with self._lock:
                self._inflight = False
