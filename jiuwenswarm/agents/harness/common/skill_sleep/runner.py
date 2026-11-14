# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Background skill_train sleep cycle launcher (non-blocking).

Concurrency: at most one sleep cycle per runner (``threading.Lock`` +
``_inflight``), per ``state_dir`` and per skills directory. The latter two are
portalocker file locks, so they also hold across runners in one process
(per-session adapters, hot toggle) and across worker processes on the same
host that share the directory (e.g. several sidecars writing one skills dir).

Failures: the call counter is snapshotted at start and only consumed after a
successful offline cycle (so mid-flight crashes keep the count). Consecutive
failures per skill back off exponentially (``skill_sleep_failures.json`` under
``state_dir``) with ERROR logs.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import portalocker

from jiuwenswarm.agents.harness.common.skill_sleep.counter import SkillCallCounter
from jiuwenswarm.common.config import get_skill_sleep_action

logger = logging.getLogger(__name__)

ACTION_OFF = "off"
ACTION_SUGGEST = "suggest"
ACTION_AUTO = "auto"
_SUGGEST_OPS = frozenset({"add", "replace", "delete"})
_SUGGEST_OP_TO_ACTION = {
    "add": "append",
    "replace": "replace",
    "delete": "delete",
}

_DEFAULT_ATTEMPT_TIMEOUT = 300.0
_DEFAULT_MAX_ATTEMPTS = 3
_INFLIGHT_LOCK_NAME = "skill_sleep.in_flight.lock"
_FAILURES_FILE_NAME = "skill_sleep_failures.json"
_FILE_LOCK_TIMEOUT_SEC = 5.0
_BACKOFF_BASE_SEC = 600.0
_BACKOFF_MAX_SEC = 24 * 3600.0
_FENCED_JSON_STAGES = frozenset({"sleep_reflect"})
_JSON_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class SleepModelSpec:
    """Snapshot of a model configuration used to build the sleep LLM client.

    Only configs are carried (never a live ``Model``): ``ChatLLMClient`` drives
    calls on its own background event loop, and a ``Model`` already bound to
    the server loop must not be shared across loops.
    """

    client_config: Any
    request_config: Any
    model_name: str


SkillsDirs = str | Path | Sequence[str | Path]


def _copy_config(cfg: Any) -> Any:
    model_copy = getattr(cfg, "model_copy", None)
    if callable(model_copy):
        return model_copy(deep=True)
    return copy.deepcopy(cfg)


def _normalize_skills_dirs(skills_dirs: SkillsDirs) -> list[Path]:
    """Return de-duplicated skills dirs, preserving registration order."""
    raw = [skills_dirs] if isinstance(skills_dirs, (str, Path)) else list(skills_dirs)
    dirs: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        key = str(path).lower() if os.name == "nt" else str(path)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)
    return dirs


def _isolated_client_config(cfg: Any) -> Any:
    """Copy *cfg* with the process-wide shared HTTP client disabled.

    ``ChatLLMClient`` runs each call on its own event loop, while the shared
    ``AsyncOpenAI`` cache is keyed without the loop, so reusing a client bound
    to the server loop fails with "bound to a different event loop".
    """
    isolated = _copy_config(cfg)
    if hasattr(isolated, "use_shared_llm_http_client"):
        isolated.use_shared_llm_http_client = False
    return isolated


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


def _unwrap_json_fence(text: str) -> str:
    """Return the first fenced JSON array/object in *text*, else *text* unchanged."""
    for match in _JSON_FENCE_RE.finditer(text or ""):
        body = match.group(1).strip()
        if body[:1] in ("[", "{"):
            return body
    return text


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _bullet_body(line: str) -> str:
    return _fold(line.strip().removeprefix("- "))


def _learned_lines(prompt: str) -> list[str]:
    try:
        from openjiuwen.agent_evolving.skill_train.sleep.memory import (
            current_learned_lines,
        )
    except ImportError:
        return []
    return [_fold(line) for line in current_learned_lines(prompt or "")]


def _normalize_reflect_edits(
    text: str,
    prompt: str,
    *,
    preserve_body_ops: bool = False,
) -> str:
    """Rewrite replace/delete edits the sleep applier cannot execute.

    openjiuwen applies replace/delete only inside the learned region (legacy
    ``SKILL-TRAIN-SLEEP:LEARNED`` markers), while the reflect prompt invites
    anchors copied from the skill body. In auto mode such a replace becomes an
    ``add`` of its new lines (anchor line removed) and such a delete is dropped.
    Suggest mode keeps body replace/delete so evolutions.json can record 增删改.
    """
    if preserve_body_ops:
        return text
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return text
    items = payload.get("edits") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return text
    learned = _learned_lines(prompt)

    def _in_learned(anchor: str) -> bool:
        folded = _fold(anchor)
        return bool(folded) and any(folded in line for line in learned)

    out: list[Any] = []
    changed = False
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        op = str(item.get("op") or "add").strip().lower()
        anchor = str(item.get("anchor") or "")
        if op not in ("replace", "delete") or _in_learned(anchor or str(item.get("content") or "")):
            out.append(item)
            continue
        changed = True
        if op == "delete":
            logger.info("[SkillSleepRunner] drop body delete edit anchor=%r", anchor[:80])
            continue
        anchor_body = _bullet_body(anchor)
        kept = [
            line
            for line in str(item.get("content") or "").splitlines()
            if line.strip() and _bullet_body(line) != anchor_body
        ]
        if not kept:
            continue
        out.append({**item, "op": "add", "anchor": "", "content": "\n".join(kept)})
    if not changed:
        return text
    if isinstance(payload, dict):
        payload = {**payload, "edits": out}
    else:
        payload = out
    return json.dumps(payload, ensure_ascii=False)


class _ReflectNormalizingChatClient:
    """Delegate to a ``ChatLLMClient``, making reflect replies applicable.

    The sleep reflect prompt asks for a bare JSON array, but models often wrap
    it in a json code fence; openjiuwen's parser only unwraps fenced dicts, so
    a fenced array silently yields zero edits. Body-anchored replace/delete
    edits are rewritten by :func:`_normalize_reflect_edits` unless suggest mode
    preserves them for 增删改 suggestion records.
    """

    def __init__(self, inner: Any, *, preserve_body_ops: bool = False) -> None:
        self._inner = inner
        self._preserve_body_ops = preserve_body_ops

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def chat(self, **kwargs: Any) -> tuple[str, Any]:
        text, meta = self._inner.chat(**kwargs)
        if kwargs.get("stage") in _FENCED_JSON_STAGES and isinstance(text, str):
            text = _normalize_reflect_edits(
                _unwrap_json_fence(text),
                str(kwargs.get("user") or ""),
                preserve_body_ops=self._preserve_body_ops,
            )
        return text, meta


def _build_chat_client(
    spec: SleepModelSpec | None = None,
    *,
    preserve_body_ops: bool = False,
) -> Any:
    """Build the sleep LLM client from *spec*, or from env/.env when absent."""
    from openjiuwen.agent_evolving.skill_train.llm_client import (
        ChatLLMClient,
        make_llm_invoke_policy,
    )
    from openjiuwen.core.foundation.llm.model import Model

    if spec is not None:
        model = Model(
            model_client_config=_isolated_client_config(spec.client_config),
            model_config=spec.request_config,
        )
        model_name = spec.model_name
    else:
        model, model_name = _build_env_model()
    attempt_timeout = float(_env("LLM_ATTEMPT_TIMEOUT", default=str(_DEFAULT_ATTEMPT_TIMEOUT)))
    max_attempts = int(_env("LLM_MAX_ATTEMPTS", default=str(_DEFAULT_MAX_ATTEMPTS)))
    return _ReflectNormalizingChatClient(
        ChatLLMClient(
            llm=model,
            model=model_name,
            policy=make_llm_invoke_policy(
                attempt_timeout_secs=attempt_timeout,
                total_budget_secs=attempt_timeout * max_attempts + 30.0,
                max_attempts=max_attempts,
            ),
        ),
        preserve_body_ops=preserve_body_ops,
    )


def _env_model_settings() -> tuple[dict[str, str], list[str]]:
    """Read env / ``.env`` model settings; returns (settings, missing labels)."""
    _load_dotenv()
    settings = {
        "provider": _env(
            "MODEL_PROVIDER", "OPTIMIZER_PROVIDER", "TARGET_PROVIDER", default="openai"
        ),
        "api_key": _env("API_KEY", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_API_KEY"),
        "api_base": _env("API_BASE", "OPENAI_COMPATIBLE_BASE_URL"),
        "model_name": _env("MODEL_NAME", "OPENAI_COMPATIBLE_MODEL", "OPTIMIZER_MODEL"),
    }
    required = (
        ("API_KEY / OPENAI_COMPATIBLE_API_KEY", "api_key"),
        ("API_BASE / OPENAI_COMPATIBLE_BASE_URL", "api_base"),
        ("MODEL_NAME / OPENAI_COMPATIBLE_MODEL", "model_name"),
    )
    missing = [label for label, key in required if not settings.get(key)]
    return settings, missing


def _build_env_model() -> tuple[Any, str]:
    from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
    from openjiuwen.core.foundation.llm.model import Model

    settings, missing = _env_model_settings()
    if missing:
        raise ValueError("Missing required environment variables: " + ", ".join(missing))
    model = Model(
        model_client_config=ModelClientConfig(
            client_provider=settings["provider"],
            api_key=settings["api_key"],
            api_base=settings["api_base"],
            use_shared_llm_http_client=False,
        ),
        model_config=ModelRequestConfig(model=settings["model_name"]),
    )
    return model, settings["model_name"]


def _skills_dir_lock_path(skills_dir: Path) -> Path:
    """Host-wide lock file keyed by the resolved skills directory."""
    path = skills_dir.expanduser()
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path.absolute())
    if os.name == "nt":
        resolved = resolved.lower()
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "jiuwenswarm-skill-sleep" / f"skills-{digest}.lock"


class _FailureLedger:
    """Consecutive sleep failures per skill with exponential retry backoff."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def retry_at(self, skill_name: str) -> float:
        try:
            with self._locked():
                entry = self._read().get(skill_name) or {}
            return float(entry.get("next_retry_at") or 0.0)
        except Exception:
            logger.debug("[SkillSleepRunner] read %s failed", self._path, exc_info=True)
            return 0.0

    def record_failure(self, skill_name: str, error: str) -> tuple[int, float]:
        """Returns (consecutive failures, seconds until the next retry)."""
        try:
            with self._locked():
                data = self._read()
                entry = data.get(skill_name) or {}
                failures = int(entry.get("failures") or 0) + 1
                delay = min(_BACKOFF_BASE_SEC * (2 ** (failures - 1)), _BACKOFF_MAX_SEC)
                data[skill_name] = {
                    "failures": failures,
                    "next_retry_at": time.time() + delay,
                    "last_error": error[:500],
                }
                self._write(data)
            return failures, delay
        except Exception:
            logger.warning("[SkillSleepRunner] write %s failed", self._path, exc_info=True)
            return 0, 0.0

    def record_success(self, skill_name: str) -> None:
        try:
            with self._locked():
                data = self._read()
                if data.pop(skill_name, None) is not None:
                    self._write(data)
        except Exception:
            logger.warning("[SkillSleepRunner] write %s failed", self._path, exc_info=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), mode="a+", timeout=_FILE_LOCK_TIMEOUT_SEC):
            yield

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)


def run_sleep_cycle_sync(
    *,
    trajectory_dir: str | Path,
    skills_base_dir: SkillsDirs,
    state_dir: str | Path,
    skill_name: str = "",
    backend: str = "model",
    gate_mode: str = "on",
    rubric_synthesis: str = "off",
    dry_run: bool = False,
    model_spec: SleepModelSpec | None = None,
    action: str = ACTION_AUTO,
) -> tuple[Any, int]:
    """Run one sleep cycle synchronously (intended for ``asyncio.to_thread``).

    With ``backend="model"``, the LLM comes from *model_spec* when given,
    otherwise from env / ``.env`` (standalone usage).

    *skills_base_dir* may list several registered skills dirs; each skill is
    read from / written to the first dir that contains it.

    When *skill_name* is set, only tasks hinted to that skill are consolidated.
    *action* ``auto`` adopts a gate-accepted candidate as a new skill version;
    ``suggest`` never stages/adopts and instead appends the proposed edits
    (gate-accepted and gate-rejected) to ``evolutions.json`` as
    ``review_status="suggest"`` experiences.

    Returns ``(outcome, suggest_written)`` where *suggest_written* is the number
    of suggest experiences persisted (0 for auto / dry-run / no edits).
    """
    from openjiuwen.agent_evolving.checkpointing.evolution_store import EvolutionStore
    from openjiuwen.agent_evolving.skill_train.sleep import SleepConfig, run_sleep_cycle
    from openjiuwen.agent_evolving.skill_train.sleep.backend import build_backend

    traj_dir = Path(trajectory_dir).expanduser()
    if not traj_dir.exists():
        raise FileNotFoundError(f"trajectory dir not found: {traj_dir}")

    skills_dirs = [str(path) for path in _normalize_skills_dirs(skills_base_dir)]
    if not skills_dirs:
        raise ValueError("skills_base_dir is empty")
    sleep_state = Path(state_dir).expanduser()
    sleep_state.mkdir(parents=True, exist_ok=True)

    target_client = None
    optimizer_client = None
    if backend == "model":
        client = _build_chat_client(
            model_spec,
            preserve_body_ops=(action == ACTION_SUGGEST),
        )
        target_client = client
        optimizer_client = client

    store = EvolutionStore(skills_dirs)
    cfg = SleepConfig(
        trajectory_store_dir=str(traj_dir),
        skills_base_dir=";".join(skills_dirs),
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
    suggest = action == ACTION_SUGGEST
    seed_tasks = None
    consumed_max_ns = 0
    if skill_name:
        seed_tasks, consumed_max_ns = _mine_skill_tasks(
            cfg, skill_name, state_dir=sleep_state
        )
    outcome = run_sleep_cycle(
        cfg,
        dry_run=dry_run or suggest,
        backend=build_backend(
            backend,
            target_client=target_client,
            optimizer_client=optimizer_client,
        ),
        target_client=target_client,
        optimizer_client=optimizer_client,
        seed_tasks=seed_tasks,
        evolution_store=store,
    )
    if skill_name and consumed_max_ns > 0:
        _persist_skill_harvest_watermark(
            sleep_state, skill_name=skill_name, consumed_max_ns=consumed_max_ns
        )
    suggest_written = 0
    if suggest and not dry_run:
        suggest_written = _persist_suggestions(store, skill_name, outcome)
    return outcome, suggest_written


def _unpack_sleep_cycle_result(raw: Any) -> tuple[Any, int | None]:
    """Normalize ``run_sleep_cycle_sync`` return (or test mocks that return outcome only)."""
    if isinstance(raw, tuple) and len(raw) == 2:
        return raw[0], max(0, int(raw[1] or 0))
    return raw, None


def _persist_skill_harvest_watermark(
    state_dir: str | Path,
    *,
    skill_name: str,
    consumed_max_ns: int,
) -> None:
    """Advance per-skill harvest watermark after a successful offline cycle."""
    from openjiuwen.agent_evolving.skill_train.sleep.state import SleepState

    name = str(skill_name or "").strip()
    ns = max(int(consumed_max_ns or 0), 0)
    if not name or ns <= 0:
        return
    state = SleepState.load(Path(state_dir).expanduser() / "state.json")
    previous = state.get_skill_harvest_ns(name)
    if ns <= previous:
        return
    state.set_skill_harvest_ns(name, ns)
    state.save()
    logger.info(
        "[SkillSleepRunner] advanced harvest watermark skill=%s ns=%s",
        name,
        ns,
    )


def _mine_skill_tasks(
    cfg: Any,
    skill_name: str,
    *,
    state_dir: str | Path | None = None,
) -> tuple[list[Any], int]:
    """Harvest + mine trajectories for *skill_name*, respecting per-skill watermark.

    Returns ``(tasks, consumed_max_ns)``. ``consumed_max_ns`` is the harvest
    watermark to persist after a successful cycle (0 = do not advance).

    Watermark policy: only advance when ``mine_capped`` did **not** hit
    ``max_tasks_per_night``. Then use the max ``SessionDigest.max_start_ns``
    across the whole harvested window (fully scanned, including digests that
    yielded no task). If truncated, return 0 so unread traces are not skipped.
    """
    from openjiuwen.agent_evolving.skill_train.sleep.harvest import (
        harvest_otlp_trajectories,
    )
    from openjiuwen.agent_evolving.skill_train.sleep.mine import mine_capped
    from openjiuwen.agent_evolving.skill_train.sleep.state import SleepState

    name = str(skill_name or "").strip()
    since_ns = 0
    if state_dir is not None and name:
        state = SleepState.load(Path(state_dir).expanduser() / "state.json")
        since_ns = state.get_skill_harvest_ns(name)

    digests = harvest_otlp_trajectories(cfg, since_ns=since_ns)
    max_tasks = int(getattr(cfg, "max_tasks_per_night", 40) or 40)
    tasks, truncated = mine_capped(
        digests,
        max_tasks=max_tasks,
        val_fraction=cfg.val_fraction,
        test_fraction=cfg.test_fraction,
        seed=cfg.seed,
    )
    filtered = [
        task for task in tasks if (task.skill_hint or "").strip() == name
    ] if name else list(tasks)

    if truncated:
        logger.warning(
            "[SkillSleepRunner] skip harvest watermark skill=%s: "
            "mine hit max_tasks_per_night=%s (digests=%s tasks=%s skill_tasks=%s); "
            "unread traces kept for a later cycle",
            name or "*",
            max_tasks,
            len(digests),
            len(tasks),
            len(filtered),
        )
        return filtered, 0

    consumed_max_ns = 0
    for digest in digests:
        consumed_max_ns = max(
            consumed_max_ns, int(getattr(digest, "max_start_ns", 0) or 0)
        )
    return filtered, consumed_max_ns


def _suggestion_records(skill_name: str, outcome: Any) -> list[Any]:
    """Convert a dry-run outcome's edits into suggest-mode records.

    Suggestions are for human review, so gate-rejected and unmatched edits are
    kept too; ``gate=accepted|rejected|unmatched`` in the record context tells
    them apart. Edit ops map to patch actions: add→append, replace→replace,
    delete→delete.
    """
    from openjiuwen.agent_evolving.checkpointing.types import (
        EvolutionPatch,
        EvolutionRecord,
    )
    from openjiuwen.agent_evolving.signal.base import EvolutionTarget

    report = getattr(outcome, "report", None)
    if report is None:
        return []
    base_context = f"skill_sleep night={getattr(outcome, 'night', '')} tasks={report.n_tasks}"
    records = []
    seen: set[str] = set()
    for gate, edits in (
        ("accepted", getattr(report, "edits", None) or []),
        ("rejected", getattr(report, "rejected_edits", None) or []),
        ("unmatched", getattr(report, "unmatched_edits", None) or []),
    ):
        for edit in edits:
            op = (edit.op or "add").lower()
            if op not in _SUGGEST_OPS:
                logger.debug(
                    "[SkillSleepRunner] skip suggest edit skill=%s op=%s", skill_name, edit.op
                )
                continue
            content = (edit.content or "").strip()
            if op == "delete":
                content = content or (edit.anchor or "").strip()
            if not content:
                logger.debug(
                    "[SkillSleepRunner] skip suggest edit skill=%s op=%s empty content",
                    skill_name,
                    edit.op,
                )
                continue
            dedupe_key = f"{op}:{content}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rationale = (edit.rationale or "").strip()
            patch = EvolutionPatch(
                section="Instructions",
                action=_SUGGEST_OP_TO_ACTION[op],
                content=content,
                target=EvolutionTarget.BODY,
                summary=(rationale or content)[:100],
            )
            records.append(
                EvolutionRecord.make(
                    source="skill_sleep",
                    context=f"{base_context} gate={gate}",
                    change=patch,
                    summary=(rationale or content)[:100],
                    root_cause=rationale or None,
                    review_status=ACTION_SUGGEST,
                )
            )
    return records


def _persist_suggestions(store: Any, skill_name: str, outcome: Any) -> int:
    """Append suggest-mode experiences to ``evolutions.json`` (SKILL.md untouched)."""
    records = _suggestion_records(skill_name, outcome)
    if not records:
        return 0

    async def _append_all() -> int:
        written = 0
        for record in records:
            try:
                await store.append_record(skill_name, record, update_skill_md=False)
            except Exception:
                logger.warning(
                    "[SkillSleepRunner] append suggest record failed skill=%s id=%s",
                    skill_name,
                    record.id,
                    exc_info=True,
                )
                continue
            written += 1
        return written

    written = asyncio.run(_append_all())
    logger.info(
        "[SkillSleepRunner] saved %s suggest experience(s) for skill=%s",
        written,
        skill_name,
    )
    return written


PublishedCallback = Callable[..., Awaitable[None]]
"""``await cb(skill_name=, version=, session_id=, request_id=)`` after an auto adopt."""

SuggestCallback = Callable[..., Awaitable[None]]
"""``await cb(skill_name=, session_id=, request_id=)`` after suggest experiences are saved."""


class SkillSleepRunner:
    """Reset counter and launch at most one sleep cycle in the background."""

    def __init__(
        self,
        *,
        counter: SkillCallCounter,
        trajectory_dir: str | Path,
        skills_base_dir: SkillsDirs,
        state_dir: str | Path,
        backend: str = "model",
        gate_mode: str = "on",
        rubric_synthesis: str = "off",
        on_task_created: Callable[[asyncio.Task], None] | None = None,
        model_provider: Callable[[], SleepModelSpec | None] | None = None,
        skills_dirs_provider: Callable[[], SkillsDirs] | None = None,
        on_published: PublishedCallback | None = None,
        on_suggest: SuggestCallback | None = None,
    ) -> None:
        self._counter = counter
        self._model_provider = model_provider
        self._skills_dirs_provider = skills_dirs_provider
        self._on_published = on_published
        self._on_suggest = on_suggest
        self._trajectory_dir = Path(trajectory_dir)
        self._skills_dirs = _normalize_skills_dirs(skills_base_dir)
        self._state_dir = Path(state_dir)
        self._backend = backend or "model"
        self._gate_mode = gate_mode or "on"
        self._rubric_synthesis = rubric_synthesis or "off"
        self._on_task_created = on_task_created
        self._lock = threading.Lock()
        self._inflight = False
        self._held_locks: list[portalocker.Lock] = []
        self._failures = _FailureLedger(self._state_dir / _FAILURES_FILE_NAME)

    @property
    def inflight(self) -> bool:
        with self._lock:
            return self._inflight

    def current_skills_dirs(self) -> list[Path]:
        """Public accessor for rails: the currently registered skills dirs."""
        return self._current_skills_dirs()

    def _current_skills_dirs(self) -> list[Path]:
        """Re-resolve registered skills dirs (they may change after startup)."""
        if self._skills_dirs_provider is None:
            return list(self._skills_dirs)
        try:
            dirs = _normalize_skills_dirs(self._skills_dirs_provider())
        except Exception:
            logger.warning(
                "[SkillSleepRunner] skills_dirs_provider failed; using initial dirs",
                exc_info=True,
            )
            return list(self._skills_dirs)
        return dirs or list(self._skills_dirs)

    def _acquire_cross_process_locks(self, skills_dirs: Sequence[Path]) -> str:
        """Take the state-dir lock and one lock per skills dir without blocking.

        Returns "" on success, else the path of the lock held elsewhere.
        """
        for path in (
            self._state_dir / _INFLIGHT_LOCK_NAME,
            *(_skills_dir_lock_path(skills_dir) for skills_dir in skills_dirs),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = portalocker.Lock(str(path), mode="a+", timeout=0)
            try:
                lock.acquire()
            except portalocker.exceptions.LockException:
                self._release_cross_process_locks()
                return str(path)
            self._held_locks.append(lock)
        return ""

    def _release_cross_process_locks(self) -> None:
        held, self._held_locks = self._held_locks, []
        for lock in reversed(held):
            try:
                lock.release()
            except Exception:
                logger.debug(
                    "[SkillSleepRunner] cross-process lock release failed",
                    exc_info=True,
                )

    def _abort_start(self) -> None:
        self._release_cross_process_locks()
        with self._lock:
            self._inflight = False

    def _preflight(self, spec: SleepModelSpec | None) -> str:
        """Return why a cycle cannot run right now, or "" when it can."""
        traj_dir = self._trajectory_dir.expanduser()
        if not traj_dir.exists():
            return f"trajectory dir not found: {traj_dir}"
        if self._backend == "model" and spec is None:
            _settings, missing = _env_model_settings()
            if missing:
                return (
                    "no main-config model and missing environment variables: "
                    + ", ".join(missing)
                )
        return ""

    def _record_failure(self, skill_name: str, reason: str, *, exc_info: bool = False) -> None:
        failures, delay = self._failures.record_failure(skill_name, reason)
        logger.error(
            "[SkillSleepRunner] sleep failed skill=%s consecutive_failures=%s "
            "next_retry_in=%.0fs reason=%s",
            skill_name,
            failures,
            delay,
            reason,
            exc_info=exc_info,
        )

    def _resolve_model_spec(self) -> SleepModelSpec | None:
        """Snapshot the model on the caller's loop; None means env fallback."""
        if self._backend != "model" or self._model_provider is None:
            return None
        try:
            spec = self._model_provider()
            if spec is None:
                return None
            # Detach from adapter-owned configs so hot reload cannot mutate them.
            return SleepModelSpec(
                client_config=_copy_config(spec.client_config),
                request_config=_copy_config(spec.request_config),
                model_name=spec.model_name,
            )
        except Exception:
            logger.warning(
                "[SkillSleepRunner] model_provider failed; falling back to env",
                exc_info=True,
            )
            return None

    def try_start(self, skill_name: str, *, session_id: str = "") -> bool:
        """Snapshot *skill_name*'s count and start sleep if no cycle is running.

        Returns True when a background task was scheduled. The counter is left
        unchanged until the offline cycle succeeds (then ``consume`` of the
        snapshotted amount). A skill in failure backoff or failing preflight is
        not started. *session_id* routes published / suggest pushes.
        """
        name = str(skill_name or "").strip()
        if not name:
            return False
        retry_at = self._failures.retry_at(name)
        if retry_at > time.time():
            logger.debug(
                "[SkillSleepRunner] skip start for %s: failure backoff for %.0fs",
                name,
                retry_at - time.time(),
            )
            return False
        skills_dirs = self._current_skills_dirs()
        action = get_skill_sleep_action(name, skills_dirs=skills_dirs)
        if action == ACTION_OFF:
            self._counter.reset(name)
            logger.info(
                "[SkillSleepRunner] skip start for %s: selfEvolution is off or unregistered",
                name,
            )
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
            self._abort_start()
            logger.warning(
                "[SkillSleepRunner] no running event loop; cannot start sleep for %s",
                name,
            )
            return False

        held_elsewhere = self._acquire_cross_process_locks(skills_dirs)
        if held_elsewhere:
            self._abort_start()
            logger.info(
                "[SkillSleepRunner] skip start for %s: sleep already in flight "
                "(lock held: %s)",
                name,
                held_elsewhere,
            )
            return False

        spec = self._resolve_model_spec()
        problem = self._preflight(spec)
        if problem:
            self._abort_start()
            self._record_failure(name, problem)
            return False

        # Defer clear until offline experience generation succeeds.
        taken = self._counter.get(name)
        coro = self._run_async(
            name, spec, taken, action, skills_dirs, session_id=str(session_id or "").strip()
        )
        try:
            task = loop.create_task(coro, name=f"skill-sleep:{name}")
        except Exception:
            coro.close()
            self._abort_start()
            logger.warning(
                "[SkillSleepRunner] create_task failed for %s; inflight cleared",
                name,
                exc_info=True,
            )
            return False

        logger.info(
            "[SkillSleepRunner] starting sleep for skill=%s action=%s count=%s trajectory=%s "
            "skills=%s model=%s",
            name,
            action,
            taken,
            self._trajectory_dir,
            ";".join(str(path) for path in skills_dirs),
            f"main-config:{spec.model_name}" if spec is not None else "env",
        )

        if self._on_task_created is not None:
            try:
                self._on_task_created(task)
            except Exception:
                logger.debug(
                    "[SkillSleepRunner] on_task_created failed", exc_info=True
                )
        return True

    async def _run_async(
        self,
        skill_name: str,
        model_spec: SleepModelSpec | None = None,
        taken: int = 0,
        action: str = ACTION_AUTO,
        skills_dirs: Sequence[Path] | None = None,
        *,
        session_id: str = "",
    ) -> None:
        outcome = None
        suggest_written: int | None = None
        try:
            raw = await asyncio.to_thread(
                run_sleep_cycle_sync,
                trajectory_dir=self._trajectory_dir,
                skills_base_dir=list(skills_dirs or self._skills_dirs),
                state_dir=self._state_dir,
                skill_name=skill_name,
                backend=self._backend,
                gate_mode=self._gate_mode,
                rubric_synthesis=self._rubric_synthesis,
                model_spec=model_spec,
                action=action,
            )
            outcome, suggest_written = _unpack_sleep_cycle_result(raw)
            # Offline cycle returned: consume the pre-trigger count, keep any
            # increments accrued while sleep was running.
            if taken > 0:
                self._counter.consume(skill_name, taken)
            report = getattr(outcome, "report", None)
            logger.info(
                "[SkillSleepRunner] sleep finished skill=%s action=%s night=%s "
                "accepted=%s tasks=%s",
                skill_name,
                action,
                getattr(outcome, "night", None),
                getattr(report, "accepted", None),
                getattr(report, "n_tasks", None),
            )
            self._failures.record_success(skill_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._record_failure(skill_name, repr(exc), exc_info=True)
        finally:
            self._release_cross_process_locks()
            with self._lock:
                self._inflight = False
        if outcome is not None:
            if action == ACTION_SUGGEST:
                await self._notify_suggest(
                    skill_name,
                    outcome,
                    session_id,
                    suggest_written=suggest_written,
                )
            else:
                await self._notify_published(outcome, session_id)

    async def _notify_suggest(
        self,
        skill_name: str,
        outcome: Any,
        session_id: str,
        *,
        suggest_written: int | None = None,
    ) -> None:
        """Notify relay that suggest experiences were saved (best effort)."""
        if self._on_suggest is None:
            return
        if suggest_written is None:
            count = len(_suggestion_records(skill_name, outcome))
        else:
            count = max(0, suggest_written)
        if count <= 0:
            return
        name = str(skill_name or "").strip()
        if not name:
            return
        if not session_id:
            logger.warning(
                "[SkillSleepRunner] skip suggest push: skill=%s count=%s "
                "reason=no_session_context",
                name,
                count,
            )
            return
        request_id = f"skill-sleep-{getattr(outcome, 'night', '') or 'night'}"
        try:
            await self._on_suggest(
                skill_name=name,
                session_id=session_id,
                request_id=request_id,
            )
        except Exception:
            logger.warning(
                "[SkillSleepRunner] suggest push failed: skill=%s request_id=%s",
                name,
                request_id,
                exc_info=True,
            )

    async def _notify_published(self, outcome: Any, session_id: str) -> None:
        """Report each auto-adopted SemVer bump via *on_published* (best effort)."""
        if self._on_published is None:
            return
        request_id = f"skill-sleep-{getattr(outcome, 'night', '') or 'night'}"
        for adopted in getattr(outcome, "adopted_skills", None) or []:
            name = str(getattr(adopted, "skill_name", "") or "").strip()
            version = str(getattr(adopted, "new_version", "") or "").strip()
            if not name or not version or version == str(
                getattr(adopted, "previous_version", "") or ""
            ).strip():
                continue
            if not session_id:
                logger.warning(
                    "[SkillSleepRunner] skip published push: skill=%s version=%s "
                    "reason=no_session_context",
                    name,
                    version,
                )
                continue
            try:
                await self._on_published(
                    skill_name=name,
                    version=version,
                    session_id=session_id,
                    request_id=request_id,
                )
            except Exception:
                logger.warning(
                    "[SkillSleepRunner] published push failed: skill=%s version=%s",
                    name,
                    version,
                    exc_info=True,
                )
