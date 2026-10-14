"""Stored candidate acceptance with fresh bindings and independent UI observers."""
import re
import traceback
from collections.abc import Mapping
from ..._json import dumps
from .bindings import handles, reads, resolveInput
from .inspection import inspectionComputer
from .probes import probeFailures
from .observation_probes import probeObservation, probeReturn, snapshotReadResult
from ._util import clone, equal, member, resolve, scheduled, drain, errorText, slice16


def checkCases(cases, requireComplete=True):
    minimum = 3 if requireComplete else 1
    if not isinstance(cases, list) or not minimum <= len(cases) <= 12:
        raise ValueError("Export cases: 3–12 cases, including two distinct positive inputs and one negative case." if requireComplete else
                         "Export cases: 1–12 valid cases; publication requires two distinct positive inputs and one negative case.")
    ids = set()
    for index, c in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(c, Mapping): raise ValueError(f"{where}: expected a case object.")
        identity = c.get("id")
        if not isinstance(identity, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity) is None:
            raise ValueError(f"{where}.id: {dumps(identity)} must use 1–80 letters, digits, underscores or hyphens; no dots or spaces.")
        if identity in ids: raise ValueError(f"{where}.id: duplicate {dumps(identity)}.")
        if c.get("kind") not in ("positive", "negative"): raise ValueError(f"{where}.kind: expected positive or negative.")
        if not isinstance(c.get("input"), list): raise ValueError(f"{where}.input: expected a fixed test-data array; bindInput maps it to method arguments when present.")
        if "bindInput" in c and not callable(c["bindInput"]): raise ValueError(f"{where}.bindInput: expected a function or omit it.")
        if "expected" not in c: raise ValueError(f"{where}.expected: supply the fixed expected UI value.")
        if "expectedReturn" in c and c["kind"] != "positive": raise ValueError(f"{where}.expectedReturn: only positive cases can assert a normal return value.")
        if c["kind"] == "negative" and not isinstance(c.get("expectedRejection"), (str, re.Pattern)):
            raise ValueError(f"{where}.expectedRejection: negative cases must supply an exact string or compiled regex matching the typed exception message.")
        if c["kind"] == "positive" and "expectedRejection" in c: raise ValueError(f"{where}.expectedRejection: only negative cases can assert a rejection.")
        if not callable(c.get("observe")): raise ValueError(f"{where}.observe: expected a function that independently reads UI.")
        if "prepare" in c and not callable(c["prepare"]): raise ValueError(f"{where}.prepare: expected a function or omit it.")
        ids.add(identity)
    if requireComplete and (len({dumps(c["input"]) for c in cases if c["kind"] == "positive"}) < 2 or not any(c["kind"] == "negative" for c in cases)):
        raise ValueError("Require distinct positive inputs and a negative precondition case.")


async def runCase(computer, create, method, c):
    phase = terminalStage = "initialize"
    candidateStarted = False
    calls, trace, bindingTrace, observerTrace, callErrors = [], [], [], [], []
    pending = set(); violations = 0; runFailure = None

    def failure(error, stage):
        matched = next((item for item in reversed(callErrors) if item[0] is error), None)
        sdkCallIndex = matched[1] if matched else None
        result = dict(error=errorText(error), origin=dict(stage=stage, kind="sdk-call" if matched else "stage-code",
            callCount=len(calls), lastCallIndex=len(calls)-1 if calls else None, sdkCallIndex=sdkCallIndex))
        if isinstance(error, BaseException): result["errorStack"] = slice16("".join(traceback.format_exception(error)), 0, 16000)
        return result

    def settled():
        if pending: raise RuntimeError("Await every UI call within its test stage; an operation is still running.")

    def observedCount():
        return sum(bool(call["phase"] == "observe" and "result" in call and (call["method"] == "read" or
            (call["method"] == "readText" and call["path"] and call["path"][-1]["method"] == "find"
             and (call["args"][0].get("offset", 0) if call["args"] and call["args"][0] is not None else 0) == 0))) for call in calls)

    class Facade:
        def __init__(self, target, path=()): self.target, self.path = target, list(path)
        def __getattr__(self, key):
            value = member(self.target, key)
            if not callable(value): return value
            path = self.path
            def call(*args):
                nonlocal violations
                if key in handles: return Facade(value(*args), [*path, dict(method=key, args=clone(list(args)))])
                if phase in ("initialize", "finished") or (phase in ("observe", "bind-input") or (phase == "run" and c["kind"] == "negative")) and key not in reads:
                    violations += 1
                    raise RuntimeError(f"{phase}: {key} is forbidden here; no action dispatched.")
                callIndex = len(calls)
                record = dict(phase=phase, path=path, method=key, args=list(args)); calls.append(record)
                replay = dict(path=path, method=key, args=clone(list(args))) if phase in ("run", "observe", "bind-input") else None
                if replay is not None: (observerTrace if phase == "observe" else bindingTrace if phase == "bind-input" else trace).append(replay)
                async def invoke():
                    try: result = await resolve(value(*args))
                    except BaseException as error:
                        if replay is not None: replay["error"] = error
                        record["error"] = errorText(error); callErrors.append((error, callIndex))
                        raise
                    semantic = snapshotReadResult(key, result) if key in reads else clone(result)
                    if replay is not None: replay["result"] = semantic
                    if key == "read": record["result"] = dict(text=semantic["text"])
                    elif key in reads: record["result"] = semantic
                    elif isinstance(result, Mapping) and "status" in result and "detail" in result:
                        record["result"] = dict(status=result["status"], detail=result["detail"])
                    return result
                return scheduled(invoke, pending)
            return call

    facade = Facade(computer); before = after = None
    try:
        api = create(facade); function = member(api, method)
        if not callable(function): raise ValueError(f"create(computer) must expose {method}().")
        phase = terminalStage = "prepare"
        if c.get("prepare"): await resolve(c["prepare"](facade))
        settled()
        phase = terminalStage = "bind-input"
        input = await resolveInput(facade, c["input"], c.get("bindInput")); settled()
        phase, terminalStage = "observe", "observe-before"
        before = await resolve(c["observe"](facade)); settled()
        beforeReads = observedCount()
        freshError = "Observer must independently read fresh UI text before and after: use read() or find(...).readText() from offset 0; a captured snapshot or package result is insufficient."
        if not beforeReads: raise RuntimeError(freshError)
        phase = "finished"
        beforeChecks = await probeObservation(c["observe"], observerTrace, before)
        beforeFailure = next((check for check in beforeChecks if not check["passed"]), None)
        if beforeFailure:
            return dict(passed=False, terminalStage=terminalStage, candidateStarted=candidateStarted, before=before, after=after, calls=calls,
                        observationChecks=beforeChecks, error="Before observation probe failed: " + beforeFailure.get("error", "Observer could not validate the recorded state."))
        if violations: raise RuntimeError("Candidate or observer attempted forbidden input/initialization, even if it caught the error.")
        observerTrace.clear(); phase = terminalStage = "run"; candidateStarted = True
        rejected = False; thrown = returned = None
        try: returned = await resolve(function(*input))
        except BaseException as error: rejected, thrown, runFailure = True, error, failure(error, "run")
        settled()
        phase, terminalStage = "observe", "observe-after"
        after = await resolve(c["observe"](facade)); settled()
        if violations: raise RuntimeError("Candidate or observer attempted forbidden input/initialization, even if it caught the error.")
        if observedCount() <= beforeReads: raise RuntimeError(freshError)
        terminalStage = "verification"
        if c["kind"] == "positive" and rejected: raise thrown
        if c["kind"] == "negative" and (not rejected or not equal(before, after)): raise RuntimeError("Negative case must reject before input and preserve the observed state.")
        if c["kind"] == "negative":
            expected = c.get("expectedRejection")
            matches = errorText(thrown) == expected if isinstance(expected, str) else expected.search(errorText(thrown)) is not None if isinstance(expected, re.Pattern) else False
            if not matches: raise AssertionError(f"Rejection assertion failed: expected {expected!r}, observed {dumps(errorText(thrown))}.")
        if not equal(after, c["expected"]): raise AssertionError(f"UI assertion failed: expected {dumps(c['expected'])}, observed {dumps(after)}.")
        returnChecked = "expectedReturn" in c
        if returnChecked and not equal(returned, c["expectedReturn"]): raise AssertionError(f"Return assertion failed: expected {dumps(c['expectedReturn'])}, returned {dumps(returned)}.")
        phase = "finished"
        stateChanged, inputCalls = not equal(before, after), sum(call["method"] not in reads for call in trace)
        observationChecks = await probeObservation(c["observe"], observerTrace, after)
        returnChecks = await probeReturn(create, method, c["input"], trace, returned, c.get("bindInput"), bindingTrace) if returnChecked and not (stateChanged and inputCalls > 0) else []
        faultChecks = await probeFailures(create, method, c["input"], trace, c.get("bindInput"), bindingTrace) if c["kind"] == "positive" else []
        failed = next((check for check in [*observationChecks, *returnChecks, *faultChecks] if not check["passed"]), None)
        verification = dict(stateChanged=stateChanged, beforeMatchedExpected=equal(before, c["expected"]), inputCalls=inputCalls, returnChecked=returnChecked,
            positiveEvidence=None if c["kind"] != "positive" or failed else "observed-change" if stateChanged and inputCalls > 0 else "observed-return" if returnChecked else "repeat-only")
        return dict(passed=not failed, terminalStage=terminalStage if failed else "finished", candidateStarted=candidateStarted,
            before=before, after=after, verification=verification, rejection=errorText(thrown) if rejected else None,
            calls=calls, faultChecks=faultChecks, observationChecks=observationChecks,
            **(dict(returned=returned, returnChecks=returnChecks) if returnChecked else {}),
            **(dict(rejectionOrigin=runFailure["origin"]) if runFailure and c["kind"] == "negative" else {}), **({"error": failed.get("error")} if failed else {}))
    except BaseException as error:
        later = failure(error, terminalStage)
        primary = runFailure if c["kind"] == "positive" and runFailure else later
        return dict(passed=False, terminalStage=terminalStage, candidateStarted=candidateStarted, error=primary["error"],
            **({"errorStack": primary["errorStack"]} if "errorStack" in primary else {}), failureOrigin=primary["origin"],
            **({"subsequentFailure": later} if runFailure and c["kind"] == "positive" and terminalStage != "verification" else {}),
            **(dict(rejection=runFailure["error"], rejectionOrigin=runFailure["origin"], **({"rejectionErrorStack": runFailure["errorStack"]} if "errorStack" in runFailure else {})) if runFailure and c["kind"] == "negative" else {}),
            before=before, after=after, calls=calls)
    finally:
        phase = "finished"
        await drain(pending)
