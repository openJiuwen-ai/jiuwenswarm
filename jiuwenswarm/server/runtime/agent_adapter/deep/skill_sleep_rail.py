"""SkillSleep rail 构建、会话遗忘与注册。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rails.skill_sleep_rail import SkillSleepRail
from jiuwenswarm.agents.harness.common.skill_sleep import (
    SkillCallCounter,
    SkillSleepRunner,
    SleepModelSpec,
)
from jiuwenswarm.common.config import (
    get_skill_sleep_call_threshold,
    get_skill_sleep_config,
)
from jiuwenswarm.common.utils import get_agent_skills_dir, get_agent_workspace_dir
from jiuwenswarm.server.runtime.agent_adapter.deep.evolution import (
    _on_evolution_watcher_done,
)
from jiuwenswarm.server.runtime.agent_adapter.deep.pure_helpers import (
    _push_skill_sleep_published,
    _push_skill_sleep_suggest,
    _resolve_skill_sleep_traces_dir,
)

logger = logging.getLogger(__name__)


def _resolve_skill_sleep_skills_dirs(adapter) -> list[Path]:
    """All registered skills dirs, so sleep finds each skill where it lives."""
    dirs = [Path(d) for d in adapter._resolve_skill_dirs() if str(d or "").strip()]
    return dirs or [get_agent_skills_dir()]


def _resolve_skill_sleep_state_dir(adapter, sleep_cfg: dict[str, Any]) -> Path:
    raw = str(sleep_cfg.get("state_dir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    root = Path(adapter._workspace_dir) if adapter._workspace_dir else get_agent_workspace_dir()
    return root / ".skill_sleep"


def _build_skill_sleep_rail(adapter, config: dict[str, Any]) -> SkillSleepRail | None:
    """Build SkillSleepRail; per-skill selfEvolution gating happens in the runner."""
    try:
        sleep_cfg = get_skill_sleep_config(config)
        state_dir = _resolve_skill_sleep_state_dir(adapter, sleep_cfg)
        counter = SkillCallCounter(state_dir / "call_counts.json")
        skills_dirs = _resolve_skill_sleep_skills_dirs(adapter)
        runner = SkillSleepRunner(
            counter=counter,
            trajectory_dir=_resolve_skill_sleep_traces_dir(),
            skills_base_dir=skills_dirs,
            state_dir=state_dir,
            backend=str(sleep_cfg.get("backend") or "model"),
            gate_mode=str(sleep_cfg.get("gate_mode") or "on"),
            rubric_synthesis=str(sleep_cfg.get("rubric_synthesis") or "off"),
            on_task_created=adapter._track_skill_sleep_task,
            model_provider=adapter._skill_sleep_model_spec,
            skills_dirs_provider=adapter._resolve_skill_sleep_skills_dirs,
            on_published=_push_skill_sleep_published,
            on_suggest=_push_skill_sleep_suggest,
        )
        rail = SkillSleepRail(
            counter=counter,
            runner=runner,
            call_threshold=get_skill_sleep_call_threshold(config),
            last_skills_path=state_dir / "last_skills.json",
        )
        logger.info(
            "[JiuWenSwarmDeepAdapter] SkillSleepRail create success "
            "threshold=%s trajectory=%s skills=%s state=%s",
            rail.call_threshold,
            _resolve_skill_sleep_traces_dir(),
            ";".join(str(d) for d in skills_dirs),
            state_dir,
        )
        traces_dir = _resolve_skill_sleep_traces_dir()
        if not traces_dir.exists():
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SkillSleepRail trajectory dir %s does not "
                "exist yet; sleep cycles are skipped until traces are exported "
                "(check agent_observability file exporter)",
                traces_dir,
            )
        return rail
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] SkillSleepRail create failed: %s",
            exc,
        )
        return None


def _skill_sleep_model_spec(adapter) -> SleepModelSpec | None:
    """Resolve the sleep-training model from the main conversation config.

    ``evolution.skill_sleep.model_name`` / ``model_tier`` override the
    adapter default. Returns None (env fallback) when no model is set.
    """
    if adapter._model is None:
        return None
    sleep_cfg = get_skill_sleep_config(adapter._config_cache)
    model, _ = adapter._resolve_model(
        model_name=str(sleep_cfg.get("model_name") or ""),
        model_tier=str(sleep_cfg.get("model_tier") or ""),
    )
    request_config = model.model_config
    model_name = str(
        getattr(request_config, "model_name", "")
        or getattr(request_config, "model", "")
        or adapter._default_model_name
        or ""
    )
    return SleepModelSpec(
        client_config=model.model_client_config,
        request_config=request_config,
        model_name=model_name,
    )


def _forget_skill_sleep_session(
    adapter, session_id: str, peer_adapter: "JiuWenSwarmDeepAdapter | None" = None
) -> None:
    """Drop SkillSleepRail per-session state held by this and *peer_adapter*."""
    if not session_id:
        return
    rails: dict[int, Any] = {}
    for rail in (
        getattr(adapter, "_skill_sleep_rail", None),
        getattr(peer_adapter, "_skill_sleep_rail", None),
    ):
        if rail is not None:
            rails[id(rail)] = rail
    for rail in rails.values():
        try:
            rail.forget_session(session_id)
        except Exception:
            logger.warning(
                "[JiuWenSwarmDeepAdapter] SkillSleepRail forget_session failed: "
                "session_id=%s",
                session_id,
                exc_info=True,
            )


def _track_skill_sleep_task(adapter, task: asyncio.Task) -> None:
    """Track background sleep tasks with evolution watcher set."""
    task.add_done_callback(lambda task: _on_evolution_watcher_done(adapter, task))
    adapter._evolution_watcher_tasks.add(task)


async def _reconcile_skill_sleep_rail(adapter) -> None:
    """Register SkillSleepRail when it is not mounted yet."""
    if adapter._instance is None or adapter._skill_sleep_rail is not None:
        return
    # Prefer instance method so monkeypatches on the adapter apply.
    rail = adapter._build_skill_sleep_rail(adapter._config_cache)
    if rail is None:
        logger.info(
            "[JiuWenSwarmDeepAdapter] SkillSleepRail build returned None; skip register"
        )
        return
    try:
        await adapter._instance.register_rail(rail)
    except Exception as exc:
        logger.warning(
            "[JiuWenSwarmDeepAdapter] SkillSleepRail register failed: %s",
            exc,
        )
        return
    adapter._skill_sleep_rail = rail
    logger.info("[JiuWenSwarmDeepAdapter] SkillSleepRail registered")
