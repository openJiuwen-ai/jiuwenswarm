"""Independent observation and return grounding using inert recorded evidence."""
from collections.abc import Mapping
from ..._identity import Record
from ...protocol.validation import decodeResult
from .bindings import handles, reads, resolveInput
from ._util import clone, equal, member, resolve, scheduled, drain, errorText, slice16

unavailableText = "Compile probe: UI evidence is unavailable. This is not an observed application state."


def snapshotReadResult(method, result):
    if method == "listApps":
        if not isinstance(result, list): raise ValueError("Cannot record listApps: expected an application array.")
        return clone(result)
    if not isinstance(result, Mapping) or not isinstance(result.get("text"), str):
        raise ValueError(f"Cannot record {method}: expected a semantic text read result.")
    if method == "read": return Record(text=result["text"], screenshot=None, overview=None)
    if method == "readText": return Record(text=result["text"], **{k: result[k] for k in ("offset", "total", "next") if k in result})
    raise ValueError(f"Cannot record {method}: expected read, readText or listApps.")


def _describe(call):
    if call is None: return "end of recorded sequence"
    return slice16(repr({key: call[key] for key in ("path", "method", "args")}), 0, 640)


async def _run(observe, trace, unavailable, allowRecordedActions, subject, stopOnInvalid=False, bindingCalls=0):
    cursor = readCount = 0
    active, invalid = True, None
    pending = set()
    def fail(message):
        nonlocal invalid
        if invalid is None: invalid = message
        raise RuntimeError(message)
    class Facade:
        def __init__(self, path): self.path = path
        def __getattr__(self, key):
            path = self.path
            if key in handles: return lambda *args: Facade([*path, dict(method=key, args=clone(list(args)))])
            def call(*args):
                def invoke():
                    nonlocal cursor, readCount
                    if stopOnInvalid and invalid is not None: raise RuntimeError(invalid)
                    if not active: fail(f"{subject} tried to make a call after its probe finished.")
                    if key not in reads and not allowRecordedActions: fail(f"Observer attempted {key} in an inert read-only probe; no action dispatched.")
                    original = trace[cursor] if cursor < len(trace) else None
                    if not original or original["method"] != key or not equal(original["path"], path) or not equal(original["args"], list(args)):
                        fail(f"{subject} requested a call outside its recorded call sequence; the {'unavailable' if unavailable else 'replay'} probe is unverified at call {cursor + 1}. Expected {_describe(original)}; requested {_describe(dict(path=path, method=key, args=list(args)))}. No live UI call was made. Unrecorded fallback branches need separate evidence.")
                    cursor += 1
                    if "error" in original: raise original["error"]
                    if "result" not in original:
                        fail(f"Recorded {key} result is missing; {subject.lower()} cannot be replayed from this evidence.")
                    if key not in reads: return clone(original["result"])
                    if key == "listApps":
                        if not isinstance(original["result"], list): fail("Recorded listApps result is not an application array.")
                        return snapshotReadResult(key, original["result"])
                    result = original["result"]
                    if not isinstance(result, Mapping) or not isinstance(result.get("text"), str): fail(f"Recorded {key} result has no UI text.")
                    bindingCall = cursor <= bindingCalls
                    if not bindingCall: readCount += 1
                    if unavailable and not bindingCall:
                        return Record(text=unavailableText, offset=0, total=len(unavailableText), next=None) if key == "readText" else Record(text=unavailableText, screenshot=None, overview=None)
                    return snapshotReadResult(key, result)
                return scheduled(invoke, pending)
            return call
    value = error = None; rejected = False
    def bindingFinished():
        if cursor != bindingCalls: fail("Input binding did not consume its recorded calls.")
    try: value = await resolve(observe(Facade([]), bindingFinished))
    except BaseException as caught: rejected, error = True, caught
    if pending and invalid is None: invalid = f"{subject} left an unawaited call in its inert probe."
    active = False
    await drain(pending)
    if not unavailable and not rejected and cursor != len(trace) and invalid is None: invalid = f"{subject} did not consume its complete recorded call sequence."
    return Record(value=value, error=error, rejected=rejected, invalid=invalid, readCount=readCount, cursor=cursor)


async def _probeValue(observe, trace, observed, allowRecordedActions, bindingCalls=0):
    subject = "Candidate return" if allowRecordedActions else "Observer"
    replay = await _run(observe, trace, False, allowRecordedActions, subject, bindingCalls=bindingCalls)
    if replay.invalid or replay.rejected or not equal(replay.value, observed):
        return [dict(probe="replay", passed=False, reads=replay.readCount, error=replay.invalid or
            (f"{subject} could not replay its recorded evidence: {errorText(replay.error)}" if replay.rejected else f"{subject} did not reproduce the original value from the same recorded evidence."))]
    if not replay.readCount: return [dict(probe="replay", passed=False, reads=0, error=f"{subject} has no successful UI text read to validate.")]
    unavailable = await _run(observe, trace, True, allowRecordedActions, subject, bindingCalls=bindingCalls)
    unchanged = not unavailable.rejected and equal(unavailable.value, observed)
    error = unavailable.invalid or ("Unavailable-evidence probe did not reach a UI text read." if not unavailable.readCount else
        f"{subject} returned the same asserted value with all UI text unavailable; its assertion is not grounded in the recorded text." if unchanged else None)
    return [dict(probe="replay", passed=True, reads=replay.readCount), dict(probe="unavailable", passed=error is None, reads=unavailable.readCount, **({"error": error} if error else {}))]


async def probeObservation(observe, trace, observed):
    return await _probeValue(lambda computer, _: observe(computer), trace, observed, False)


async def probeReturn(create, method, input, trace, actualReturn, bindInput=None, bindingTrace=()):
    async def observe(computer, bindingFinished):
        api = create(computer); function = member(api, method)
        if not callable(function): raise ValueError(f"create(computer) must expose {method}().")
        args = await resolveInput(computer, input, bindInput)
        bindingFinished()
        return await resolve(function(*args))
    return await _probeValue(observe, [*bindingTrace, *trace], actualReturn, True, len(bindingTrace))


async def replayPreparation(prepare, trace, originalError):
    def unverified(error): return dict(diagnostic="unverified", consumed=0, recorded=len(trace), reads=0, error=error)
    if any("error" in call for call in trace): return unverified("Preparation contains a recorded call error whose original exception cannot be reproduced.")
    for index, call in enumerate(trace):
        if call["method"] != "readText": continue
        try: decodeResult("readElementText", call.get("result"))
        except Exception: return unverified(f"Recorded readText at call {index + 1} has missing or invalid text/pagination fields; preparation evidence is incomplete.")
    replay = await _run(lambda computer, _: prepare(computer), trace, False, True, "Preparation", True)
    reproduced = replay.rejected and replay.cursor == len(trace) and errorText(replay.error) == originalError
    return dict(diagnostic="unverified" if replay.invalid or (replay.rejected and not reproduced) else "reproduced-error" if reproduced else "completed",
                consumed=replay.cursor, recorded=len(trace), reads=replay.readCount,
                **({"error": replay.invalid or errorText(replay.error)} if replay.invalid or replay.rejected else {}))
