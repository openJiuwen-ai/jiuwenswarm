from contextvars import ContextVar

_guard = None
_nested = ContextVar("computer_use_nested", default=False)
_revision = 0
_active = 0


def operation_checkpoint():
    captured, idle = _revision, _active == 0
    return lambda: idle and _active == 0 and _revision == captured


def set_execution_guard(guard):
    global _guard
    _guard = guard


async def operation(run):
    global _active, _revision
    if _nested.get():
        return await run()
    done = _guard() if _guard is not None else None
    _active += 1
    _revision += 1
    token = _nested.set(True)
    try:
        return await run()
    finally:
        _nested.reset(token)
        _active -= 1
        _revision += 1
        if done:
            done()
