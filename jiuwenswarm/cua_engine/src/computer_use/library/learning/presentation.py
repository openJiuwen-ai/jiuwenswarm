"""Reorder verified historical headers only; canonical evidence is unchanged."""
import re
from ..._json import dumps
from ...observation.observation import compactOverview, windowContext
from ...protocol.observation import decodeObservation
from ...protocol.primitives import safeInteger

purpose = "Historical presentation only: whole lines reordered; original journal and observation state unchanged."


def _display(text, native):
    if native is None: return None
    try: observation = decodeObservation(native["observation"])
    except Exception: return None
    if "\r" in text: return None
    lines = text.split("\n"); context = windowContext(observation["windows"], observation.get("current"))
    if lines[:len(context)] != context: return None
    treeAt = next((i for i, line in enumerate(lines) if re.match(r"^Tree: (?:full|diff|unchanged|unavailable)(?: |$)", line)), -1)
    overview = compactOverview(observation["overviewText"]).split("\n") if observation.get("overviewText") else []
    directory = len(overview) == 3 and re.fullmatch(r"(?:Overview: [0-9]+x[0-9]+ px; context only \(not click coordinates\)\.|Overview: app layout [0-9]+x[0-9]+ px; context only; use app\.window\(ref\)\.read\(\) for click coordinates\.(?: Missing image content for windows: [0-9]+(?:, [0-9]+)*\.)?)", overview[0]) is not None \
        and overview[1].startswith("Overview windows (x,y,width,height): ") \
        and overview[2] == "Overview bounds: full windows in overview pixels, possibly outside image; display coverage excludes gaps, not occlusion."
    if overview and not directory and not (len(overview) == 1 and re.fullmatch(r"Overview: unavailable \(.+\)", overview[0])): return None
    overviewAt = treeAt - len(overview)
    header = lines[len(context):overviewAt] if overviewAt >= 0 else []
    if overviewAt <= len(context) or not header or not re.match(r"^(?:Window: |App: )", header[0]) \
        or not all(re.match(r"^(?:Window: |App: |Observation: |Document: |Application menu: )", line) for line in header) \
        or lines[overviewAt:treeAt] != overview: return None
    moved = {i for i, line in enumerate(context) if line.startswith(("Windows: ", "Window documents: "))}
    if directory: moved.update(range(overviewAt, treeAt))
    trailing = bool(lines and lines[-1] == "")
    if trailing: lines.pop()
    reordered = "\n".join([*(line for i, line in enumerate(lines) if i not in moved), *(line for i, line in enumerate(lines) if i in moved)]) + ("\n" if trailing else "")
    if reordered == text: return None
    return dict(text=reordered, metadata=dict(purpose=purpose, order="current-window-first", currentWindow=observation.get("current")))


def _key(event):
    observed = event.get("observed")
    if safeInteger(event.get("executionId")) and isinstance(event.get("scope"), str) and event["scope"] and isinstance(observed, dict) and isinstance(observed.get("id"), str) and observed["id"]:
        return dumps([event["executionId"], event["scope"], observed["id"]])
    return None


def sourcePresentation(evidence, events):
    natives = {}
    for event in events:
        if event.get("kind") != "native_observation": continue
        key = _key(event)
        if key is not None: natives[key] = None if key in natives else event
    result = []
    for event in evidence:
        view = _display(event["text"], natives.get(_key(event))) if event.get("kind") == "observation" and isinstance(event.get("text"), str) else None
        result.append({**event, "text": view["text"], "presentation": view["metadata"]} if view else event)
    return result
