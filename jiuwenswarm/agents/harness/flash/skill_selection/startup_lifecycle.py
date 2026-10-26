"""Flash-owned preparation scopes across the SDK's reusable worker tasks.

This module schedules optional background work; it is independent of diagnostic
logging. Wrappers are installed on one Flash instance, never on SDK classes or
shared model clients. A model boundary closes the lease; finally closes it again
on every exit path, including cancellation and streams closed before a model.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import logging

from .execution import get_startup_gate

logger = logging.getLogger(__name__)


@dataclass
class Preparation:
    request_id: str
    lease: object

    def close(self):
        self.lease.close()


_current: ContextVar[Preparation | None] = ContextVar(
    "flash_skill_startup_preparation", default=None
)


@contextmanager
def bind(preparation):
    token = _current.set(preparation)
    try:
        yield preparation
    finally:
        _current.reset(token)


@contextmanager
def preparing(request_id=""):
    """Nested facade/session creation shares the lease for the same request."""
    current = _current.get()
    request_id = str(request_id or "")
    if current is not None and (not request_id or current.request_id == request_id):
        yield current
        return
    preparation = Preparation(request_id, get_startup_gate().startup())
    with bind(preparation):
        try:
            yield preparation
        finally:
            preparation.close()


def bind_instance(instance, request_preparations):
    """Carry the actual request lease through both long-lived SDK workers."""
    if getattr(instance, "flash_preparation_bound", False):
        return
    instance.flash_preparation_bound = True
    task_preparations = {}
    instance.flash_preparation_tasks = task_preparations

    def wrap(name, factory):
        original = getattr(instance, name, None)
        if callable(original):
            setattr(instance, name, factory(original))
        else:
            logger.warning("[SkillSelection] preparation hook unavailable: %s", name)

    def round_wrapper(original):
        @wraps(original)
        async def execute_round(work, *args, **kwargs):
            preparation = request_preparations.get(getattr(work, "request_id", None))
            # An unrelated/background task must not retain the context which
            # happened to create the SDK supervisor.
            with bind(preparation):
                try:
                    return await original(work, *args, **kwargs)
                finally:
                    if preparation is not None:
                        preparation.close()
        return execute_round

    def run_wrapper(original):
        @wraps(original)
        async def run_round(work, task_id, session, *args, **kwargs):
            preparation = _current.get()
            if preparation is not None:
                task_preparations[task_id] = preparation
            try:
                return await original(work, task_id, session, *args, **kwargs)
            finally:
                task_preparations.pop(task_id, None)
        return run_round

    def setup_wrapper(original):
        @wraps(original)
        async def setup(*args, **kwargs):
            result = await original(*args, **kwargs)
            scheduler = getattr(result[1], "task_scheduler", None)
            if scheduler is not None and not getattr(scheduler, "flash_preparation_bound", False):
                execute = getattr(scheduler, "execute_task", None)
                if callable(execute):
                    @wraps(execute)
                    async def execute_task(task_id, session, *a, **kw):
                        preparation = task_preparations.get(task_id)
                        with bind(preparation):
                            return await execute(task_id, session, *a, **kw)
                    scheduler.execute_task = execute_task
                    scheduler.flash_preparation_bound = True
                else:
                    logger.warning("[SkillSelection] preparation scheduler hook unavailable")
            return result
        return setup

    wrap("_execute_round", round_wrapper)
    wrap("run_one_round", run_wrapper)
    wrap("_setup_task_loop", setup_wrapper)
    react = getattr(instance, "react_agent", None)
    apply_kwargs = getattr(react, "_apply_llm_call_kwargs", None)
    if callable(apply_kwargs):
        @wraps(apply_kwargs)
        def before_model(*args, **kwargs):
            result = apply_kwargs(*args, **kwargs)
            # SDK calls this after BEFORE_MODEL_CALL rails, context-window
            # preparation and KV bookkeeping, immediately before invoke/stream.
            preparation = _current.get()
            if preparation is not None:
                preparation.close()
            return result
        # Keep SDK-version-specific binding dynamic, like the instance hooks above.
        setattr(react, "_apply_llm_call_kwargs", before_model)
    else:
        logger.warning("[SkillSelection] preparation model boundary unavailable")
