"""Bounded check presentation without invoking user getters or repr hooks."""
from ...protocol.primitives import utf16Length
from ._util import slice16

checkPresentationLimit = 16384
_missing = object()


def field(value, key, default=None):
    if isinstance(value, dict): return dict.get(value, key, default)
    if isinstance(value, list) and isinstance(key, int) and 0 <= key < list.__len__(value): return list.__getitem__(value, key)
    return default


def clip(text, limit): return text if utf16Length(text) <= limit else slice16(text, 0, max(0, limit-14)) + " … [truncated]"


def brief(value, limit=800):
    def safe(v, depth=0):
        if type(v) in (bool, int, float, type(None)): return repr(v)
        if isinstance(v, str): return repr(clip(v, 800))
        if depth >= 4: return "[…]" if isinstance(v, (list, tuple)) else "{…}"
        if isinstance(v, dict):
            items = list(dict.items(v))[:12]
            return "{" + ", ".join(safe(k, depth+1)+": "+safe(x, depth+1) for k, x in items) + (", …" if dict.__len__(v) > 12 else "") + "}"
        if isinstance(v, list): return "[" + ", ".join(safe(x, depth+1) for x in list.__getitem__(v, slice(0, 12))) + (", …" if list.__len__(v) > 12 else "") + "]"
        return "[Object not evaluated]"
    return clip(safe(value), limit)


def formatCheck(result):
    lines = [f"Learning check [{'passed' if field(result, 'passed') is True else 'failed'}] id={brief(field(result, 'id'), 100)} case={brief(field(result, 'caseId'), 160)}",
             f"terminalStage={brief(field(result, 'terminalStage'), 80)} candidateStarted={brief(field(result, 'candidateStarted'), 20)}"]
    for key in ("error", "failureOrigin", "subsequentFailure", "verification"):
        value = field(result, key, _missing)
        if value is not _missing: lines.append(f"{key}: {brief(value, 1600 if key == 'error' else 900)}")
    for key in ("before", "after", "returned"):
        value = field(result, key, _missing)
        if value is not _missing: lines.append(f"{key} (bounded): {brief(value)}")
    phases = field(field(result, "cost"), "byPhase", {})
    lines.append("Recorded SDK cost by phase (commands/reads/inputs): " + "; ".join(f"{phase}=" + "/".join(brief(field(cost, key), 20) for key in ("commands", "reads", "inputs")) for phase, cost in dict.items(phases) if isinstance(phase, str)))
    calls = field(result, "calls", [])
    failed = [i for i in range(list.__len__(calls)) if field(field(calls, i), "error", _missing) is not _missing] if isinstance(calls, list) else []
    origin = field(field(result, "failureOrigin"), "sdkCallIndex")
    failed.sort(key=lambda i: (i != origin, i))
    for index in failed[:3]:
        lines.append(f"Failed calls[{index}]: " + brief({key: field(field(calls, index), key) for key in ("phase", "method", "path", "args", "error")}, 1100))
    if len(failed) > 3: lines.append(f"{len(failed)-3} more failed calls in full evidence.")
    last = field(result, "lastObservation"); page = field(last, "result"); text = field(page, "text")
    if isinstance(text, str):
        lines.append(f"Historical last observation, NOT current UI: calls[{brief(field(last, 'callIndex'), 20)}].result; phase={brief(field(last, 'phase'), 80)} method={brief(field(last, 'method'), 80)}; laterCalls={brief(field(last, 'laterCalls'), 20)}.")
        lines.append("Scope: " + brief({key: field(last, key) for key in ("path", "args")}, 500))
        lines.append(f"Recorded textChars={brief(field(last, 'textChars'), 20)} receiptTruncated={brief(field(last, 'truncated'), 20)}; displayedTextTruncated={str(utf16Length(text) > 3000).lower()}; page=" + brief({key: field(page, key) for key in ("offset", "total", "next")}, 180) + ".")
        lines.append(clip(text, 3000))
    url = field(result, "evidence")
    evidence = f"\nFull evidence: {url if isinstance(url, str) else '[unavailable]'}\nRecorded check evidence only; not a new UI observation or proof of the full contract."
    if utf16Length(evidence) > checkPresentationLimit-256: raise ValueError("Check evidence URL exceeds presentation budget; inspect the returned evidence field.")
    return clip("\n".join(lines), checkPresentationLimit-utf16Length(evidence)) + evidence
