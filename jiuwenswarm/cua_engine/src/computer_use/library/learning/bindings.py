"""Invocation-local, read-only argument binding."""
import weakref
from .inspection import handles, reads
from ._util import clone, member, resolve, scheduled, drain

_bindingHandles = weakref.WeakSet()


async def resolveInput(computer, input, bindInput=None):
    data = clone(input)
    if bindInput is None: return data
    active, violation = True, None
    pending, targets = set(), {}

    def fail(message):
        nonlocal violation
        if violation is None: violation = message
        raise RuntimeError(message)

    class Binding:
        def __init__(self, target):
            object.__setattr__(self, "_target", target)
            targets[self] = target; _bindingHandles.add(self)
        def __getattr__(self, key):
            value = member(object.__getattribute__(self, "_target"), key)
            if not callable(value): return value
            def call(*args):
                if not active: fail("bindInput scope is closed; no SDK call dispatched.")
                if key in handles: return Binding(value(*args))
                if key not in reads: fail(f"bindInput is read-only: {key} is forbidden; no action dispatched.")
                return scheduled(lambda: value(*args), pending)
            return call
        def __setattr__(self, key, value): fail("bindInput cannot modify an SDK handle.")
        def __delattr__(self, key): fail("bindInput cannot modify an SDK handle.")

    error = None
    try: args = await resolve(bindInput(Binding(computer), *data))
    except BaseException as caught: error = caught
    if pending and violation is None: violation = "Await every bindInput SDK call; an operation is still running."
    active = False
    await drain(pending)
    if error is not None: raise error
    if violation is not None: raise RuntimeError(violation)
    if not isinstance(args, list): raise ValueError("bindInput must return an array of method arguments.")
    copied = {}

    def materialize(value):
        if isinstance(value, Binding) and value in targets: return targets[value]
        try:
            if value in _bindingHandles: raise ValueError("bindInput returned a handle from an earlier binding; resolve it with this invocation's computer.")
        except TypeError: pass  # Ordinary lists/dictionaries are not weak keys.
        if id(value) in copied: return copied[id(value)]
        if isinstance(value, (dict, list)):
            result = {} if isinstance(value, dict) else []
            copied[id(value)] = result
            if isinstance(value, dict):
                for key, item in value.items(): result[key] = materialize(item)
            else: result.extend(materialize(item) for item in value)
            return result
        return clone(value)
    return materialize(args)
