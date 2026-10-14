"""Recorded-prefix failure probes. These facades cannot access a desktop."""
from ...errors import ComputerUseError
from ..._json import dumps
from .bindings import handles, reads, resolveInput
from ._util import clone, equal, member, resolve


async def probeFailures(create, method, input, trace, bindInput=None, bindingTrace=()):
    recorded = [*bindingTrace, *trace]
    points = {}
    for index, call in enumerate(trace):
        if call["method"] not in reads:
            points.setdefault(call["method"], index)
            points[call["method"] + ":last"] = index
    results = []
    for at in sorted(set(points.values())):
        for code in ("execution_unknown", "ambiguous_target"):
            cursor = 0
            injected = continued = mismatch = rejected = False
            recent = {}

            class Facade:
                def __init__(self, path): self.path = path
                def __getattr__(self, key):
                    path = self.path
                    if key in handles: return lambda *args: Facade([*path, dict(method=key, args=list(args))])
                    async def call(*args):
                        nonlocal cursor, injected, continued, mismatch
                        args = list(args); lookup = dumps([path, key, args])
                        if injected:
                            if key not in reads: continued = True
                            if key in reads and lookup in recent: return clone(recent[lookup])
                            raise RuntimeError("Compile probe: stop after failed input; no desktop action was sent.")
                        original = recorded[cursor] if cursor < len(recorded) else None
                        if not original or original["method"] != key or not equal(original["path"], path) or not equal(original["args"], args):
                            mismatch = True
                            raise RuntimeError("Compile probe could not reproduce the observed call prefix.")
                        index = cursor; cursor += 1
                        if index == len(bindingTrace) + at:
                            injected = True
                            raise ComputerUseError(dict(code=code, target="Compile failure probe", effect="possible" if code == "execution_unknown" else "none",
                                detail="Injected error; no desktop action was sent.", next="stop and observe; do not try another input target"))
                        if "error" in original: raise original["error"]
                        if key in reads: recent[lookup] = original.get("result")
                        return clone(original.get("result"))
                    return call
            try:
                computer = Facade([]); api = create(computer)
                args = await resolveInput(computer, input, bindInput)
                if cursor != len(bindingTrace): raise RuntimeError("Input binding did not consume its recorded calls.")
                await resolve(member(api, method)(*args))
            except BaseException: rejected = True
            result = dict(at=at, method=trace[at]["method"], code=code, passed=injected and rejected and not continued and not mismatch)
            if continued: result["error"] = "Candidate attempted input after an uncertain or ambiguous action failure."
            elif mismatch or not injected: result["error"] = "Observed call prefix was not reproducible; failure behavior is unverified."
            elif not rejected: result["error"] = "Candidate swallowed the injected action failure and returned success."
            results.append(result)
    return results
