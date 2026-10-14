"""Recorded attempts and text volume, never inferred UI success or token counts."""
from computer_use._json import loads as jsonLoads
import json
import re
from ..._json import dumps
from ...protocol.primitives import utf16Length

nativeKinds = {**dict.fromkeys(("readApp", "readDesktop", "readElementText", "listApps", "findElement"), "read"),
               **dict.fromkeys(("click", "scroll", "drag", "typeText", "paste", "pressKey", "clickElement", "openElementMenu",
                   "performElementAction", "pressKeyElement", "pasteElement", "focusElement", "typeTextElement"), "input")}
sdkReads = frozenset(("read", "readText", "listApps"))
sdkInputs = frozenset(("click", "scroll", "drag", "typeText", "paste", "pressKey", "openMenu", "performAction", "focus"))


def _empty():
    return dict(commands=0, reads=0, inputs=0, other=0, byMethod={},
        beforeFirstInput=dict(boundary="no-recorded-input", commands=0, reads=0, byMethod={}),
        observations=0, observationChars=0, failedCommands=[])


def _count(byMethod, method): byMethod[method] = byMethod.get(method, 0) + 1


def _command(cost, method, kind):
    prefix = cost["beforeFirstInput"]
    if prefix["boundary"] == "no-recorded-input":
        if kind == "input": prefix["boundary"] = "first-input"
        else:
            prefix["commands"] += 1
            if kind == "read": prefix["reads"] += 1
            _count(prefix["byMethod"], method)
    cost["commands"] += 1
    cost[{"read": "reads", "input": "inputs"}.get(kind, "other")] += 1
    _count(cost["byMethod"], method)


def _endpoints(cost, summary):
    cost.setdefault("firstObservation", summary); cost["lastObservation"] = summary


def _textSummary(text):
    summary = {}
    for key, pattern in (("id", r'^Observation: ("(?:[^"\\]|\\.)*")$'),
                         ("window", r'^Window: ("(?:[^"\\]|\\.)*"), App: '),
                         ("document", r'^Document: ("(?:[^"\\]|\\.)*")$')):
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            try:
                value = jsonLoads(match[1])
                if isinstance(value, str): summary[key] = value
            except ValueError: pass
    if "document" not in summary and re.search(r'^Document: unknown \(not reported\)$', text, re.MULTILINE): summary["document"] = None
    return summary


def _nativeSummary(event):
    observation = event.get("observation")
    if not isinstance(observation, dict): return None
    summary = {}; observed = event.get("observed")
    if isinstance(observed, dict) and isinstance(observed.get("id"), str): summary["id"] = observed["id"]
    if isinstance(observation.get("title"), str): summary["window"] = observation["title"]
    elif isinstance(observation.get("current"), str) and isinstance(observation.get("windows"), list):
        selected = next((w for w in observation["windows"] if isinstance(w, dict) and w.get("ref") == observation["current"]), None)
        if selected and isinstance(selected.get("title"), str): summary["window"] = selected["title"]
    if "document" in observation and (isinstance(observation["document"], str) or observation["document"] is None): summary["document"] = observation["document"]
    return summary


def summarizeExecution(relatedEvents):
    cost, starts, nativeById, native = _empty(), {}, {}, []
    for event in relatedEvents:
        if not isinstance(event, dict) or event.get("kind") != "native_observation": continue
        summary = _nativeSummary(event)
        if summary is None: continue
        native.append(summary)
        if "id" in summary: nativeById[summary["id"]] = summary
    def key(event):
        return dumps([event.get("traceId"), event["id"]]) if type(event.get("id")) in (str, int, float) else None
    for event in relatedEvents:
        if not isinstance(event, dict): continue
        method, kind = event.get("method"), event.get("kind")
        if kind == "connection": starts.clear()
        if kind == "command_start" and isinstance(method, str):
            _command(cost, method, nativeKinds.get(method, "other"))
            if (identity := key(event)) is not None: starts[identity] = dict(position=cost["commands"], method=method)
        if kind == "command_end" and event.get("status") == "error" and isinstance(method, str):
            start = starts.get(key(event))
            cost["failedCommands"].append(dict(position=start["position"] if start and start["method"] == method else None, method=method))
        if kind == "observation" and isinstance(event.get("text"), str):
            cost["observations"] += 1; cost["observationChars"] += utf16Length(event["text"])
            rendered = _textSummary(event["text"]); observed = event.get("observed")
            if isinstance(observed, dict) and isinstance(observed.get("id"), str): rendered["id"] = observed["id"]
            _endpoints(cost, {**rendered, **nativeById.get(rendered.get("id"), {})})
    if not cost["observations"]:
        for summary in native: _endpoints(cost, summary)
    return cost


def summarizeCalls(calls):
    total, phases, stages = _empty(), {}, {}
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("method"), str): continue
        method, phase = call["method"], call.get("phase") if isinstance(call.get("phase"), str) else "unknown"
        summaries = [total, phases.setdefault(phase, _empty())]
        if isinstance(call.get("stage"), str): summaries.append(stages.setdefault(call["stage"], _empty()))
        kind = "read" if method in sdkReads else "input" if method in sdkInputs else "other"
        for cost in summaries:
            _command(cost, method, kind)
            if "error" in call: cost["failedCommands"].append(dict(position=cost["commands"], method=method))
            result = call.get("result")
            if isinstance(result, dict) and isinstance(result.get("text"), str):
                cost["observations"] += 1; cost["observationChars"] += utf16Length(result["text"])
                if method == "read": _endpoints(cost, _textSummary(result["text"]))
    return dict(total=total, byPhase=phases, **({"byStage": stages} if stages else {}))
