"""Model-independent desktop stages, with explicit reads and single-use frames."""
import hashlib
import re
import sys
import uuid
from ..._identity import Record, WeakIdentityMap
from ..._json import dumps, utf8
from ...errors import ComputerUseError
from ...client.observation_data import observationData
from ...client.computer import selectWorkingWindow
from ...client.execution import eager
from ...protocol.schema import SchemaError
from .actions import bindActions
from .contract import stageTaskSchema, commandSchema
from .plan import assertPlannedCommand


def targetLabels(roots):
    targets = {}
    def remember(node):
        targets[node["ref"]] = dict(role=node["role"], **{key: node[key] for key in ("name", "description") if node.get(key)})
        for child in node["children"]: remember(child)
    for root in roots: remember(root)
    return targets


def modalDescendant(ref, parent, windows):
    if not parent or ref == parent: return False
    seen = set()
    while ref not in seen:
        seen.add(ref)
        window = next((window for window in windows if window["ref"] == ref), None)
        if window is None or window.get("modal") is not True or not window.get("parent"): return False
        if window["parent"] == parent: return True
        ref = window["parent"]
    return False


class DecisionSession:
    def __init__(self, app, task):
        self.app, self._task, self._view = app, stageTaskSchema.parse(task), app
        self._frame = self._identity = self._lastInputWindow = self._pendingRead = self._completedObservation = None
        self._refs, self._windows = frozenset(), frozenset()
        self._modalParents, self._targets, self._otherWindows = WeakIdentityMap(), {}, {}
        self._showNextImage = self._busy = self._ended = self._plannedDispatched = self._plannedInterrupted = False
        self._recent, self._history, self._shownImages = [], [], []

    @property
    def task(self): return self._task
    @property
    def plannedComplete(self): return self._plannedDispatched

    def advance(self, task):
        if self._busy or not (self._plannedDispatched or self._ended and self._recent and self._recent[-1]["kind"] == "finish"):
            raise RuntimeError("Only a completed decision stage can advance.")
        nextTask = stageTaskSchema.parse(task)
        if nextTask["app"] != self.task["app"]: raise ValueError("A workflow cannot change its bound application selector.")
        retained, self._completedObservation = self._completedObservation, None
        self._task, self._ended, self._plannedDispatched, self._frame = nextTask, False, False, None
        if retained is not None:
            frame = Record(**{**retained["frame"], "id": str(uuid.uuid4()), "task": self.task, "recent": list(self._recent), "history": list(self._history)})
            self._frame = frame
            return Record(frame=frame, images=retained["images"])
        return None

    @eager
    async def read(self):
        if self._busy or self._ended: raise RuntimeError("Decision session is busy or ended.")
        self._busy = True; self._frame = self._completedObservation = None; self._shownImages = []
        self._refs, self._windows = frozenset(), frozenset(); self._targets.clear(); self._otherWindows.clear()
        pending, self._pendingRead = self._pendingRead, None
        query = pending["query"] if pending else {}
        showImage, self._showNextImage = query.get("image", self._showNextImage), False
        if pending and pending.get("view") is not None: self._view = pending["view"]
        try:
            note = ""
            while True:
                try:
                    state = await pending["element"].read(dict(offset=query.get("offset", 0))) if pending and "element" in pending else await self._view.read(dict(full=True, **({"menuPath": query["menuPath"]} if query.get("menuPath") else {})))
                    break
                except Exception as error:
                    parent = self._modalParents.get(self._view)
                    if parent is None or pending and "element" in pending or query.get("menuPath") or not isinstance(error, ComputerUseError) or error.code != "stale_target": raise
                    self._view = parent
                    note = "\nObservation note: the selected modal became unavailable. Showing its previously observed parent working window; no input was replayed."
            data = observationData(state)
            if data is None: raise RuntimeError("Structured observation is unavailable; update the MCP and SDK together.")
            self._identity = data.get("identity")
            if pending and pending.get("view") is not None: self._lastInputWindow = None
            self._refs, self._windows = data["refs"], data["windows"]
            self._targets = targetLabels(data["roots"])
            identity = self._identity or {}; focused = identity.get("focusedWindow")
            if not (pending and "element" in pending) and not query.get("menuPath") and focused and focused != identity.get("current") and focused in self._windows:
                try:
                    view = self._view.window(focused); extra = await view.read(dict(full=True)); facts = observationData(extra)
                    if facts is None or facts.get("identity", {}).get("current") != focused: raise RuntimeError("Supplementary observation did not preserve the selected window identity.")
                    self._otherWindows[focused] = dict(view=view, refs=facts["refs"], targets=targetLabels(facts["roots"]))
                    relationship = "confirmed modal descendant of the working window" if modalDescendant(focused, identity.get("current"), identity.get("windows", [])) else "relationship to the working window is not established"
                    note += f"\n\nSeparately observed window {focused} (application-reported focus; {relationship}).\nTo act here, explicitly set window: {dumps(focused)} on the action batch. This observation supplies element refs only; its screenshot has not been shown. The working App is unchanged.\n"
                    if facts.get("instance"): note += "Observed App selector: " + dumps(facts["instance"]) + "\n"
                    note += extra.text
                except Exception as error:
                    if not isinstance(error, ComputerUseError) or error.effect != "none": raise
                    note += f"\nSupplementary window {focused} could not be read: {error.code}: {error.detail}. It has no input bindings in this frame."
            images = [image for image in (state.screenshot, state.overview) if image is not None] if showImage else []
            current = identity.get("current"); windows = identity.get("windows", [])
            evidence = hashlib.sha256(utf8(re.sub(r"^Observation: [^\n]*\n", "", state.text + note, flags=re.MULTILINE)))
            evidence.update(b"\0image\0" if state.screenshot is not None else b"\0no-image\0")
            if state.screenshot is not None: evidence.update(state.screenshot)
            frame = Record(id=str(uuid.uuid4()), task=self.task, observation=state.text+note, recent=list(self._recent), history=list(self._history),
                **({"app": data["instance"]} if data.get("instance") else {}),
                directKeysReady=bool(current and (not focused or focused == current) and (not self._lastInputWindow or self._lastInputWindow == current
                    or modalDescendant(self._lastInputWindow, current, windows) or modalDescendant(current, self._lastInputWindow, windows))),
                platform=sys.platform, imageShown=bool(showImage and state.screenshot is not None), evidence=evidence.hexdigest())
            self._frame, self._shownImages = frame, images
            return Record(frame=frame, images=images)
        finally: self._busy = False

    @eager
    async def execute(self, frameId, input):
        if self._busy or self._ended or self._frame is None or self._frame["id"] != frameId: raise RuntimeError("Stale decision frame; no operation dispatched. Read again.")
        if self._plannedInterrupted: raise RuntimeError("Prescribed input was interrupted after possible/completed effects. Observe and hand off; do not replay the sequence.")
        if self._plannedDispatched: raise RuntimeError("This stage's prescribed inputs were already dispatched. Advance or inspect; do not replay them.")
        frame, steps = self._frame, []
        self._frame = self._completedObservation = None; self._busy = True
        failedAction = None
        try:
            command = commandSchema.parse(input)
            if self.task.get("actions"): assertPlannedCommand(self.task["actions"], command)
            identity = self._identity or {}
            if "actions" in command:
                window = command.get("window")
                other = self._otherWindows.get(window) if window is not None and window != identity.get("current") else None
                if window is not None and window != identity.get("current") and other is None:
                    raise ComputerUseError(dict(code="stale_target", effect="none", target=window, detail="This window has no separate observation in the current frame.", next="Read the intended catalogue window before acting."))
                bound = bindActions(other["view"] if other else self._view, command["actions"], other["refs"] if other else self._refs, False if other else frame["imageShown"])
                self._showNextImage = command.get("image", any("x" in action or action["op"] == "drag" for action in command["actions"]))
                for item in bound:
                    failedAction = item.action
                    target = (other["targets"] if other else self._targets).get(item.action["ref"]) if "ref" in item.action else None
                    receipt = await item.run()
                    self._lastInputWindow = command.get("window", identity.get("current"))
                    step = dict(action=item.action, receipt=receipt, **({"target": target} if target else {}), **({"window": window} if window is not None else {}))
                    steps.append(step); self._history.append(step)
                result = dict(kind="action", steps=steps)
                if self.task.get("actions"):
                    remaining = self.task["actions"][len(steps):]; self._plannedDispatched = not remaining
                    if remaining: self._task = {**self.task, "actions": remaining}
            elif command["op"] == "read":
                window = command.get("window")
                if window is not None and window not in self._windows: raise ComputerUseError(dict(code="stale_target", effect="none", target=window, detail="Window reference is outside the current catalogue.", next="Read the current app and choose a listed task window."))
                current = window is None or window == identity.get("current")
                refs = self._refs if current else self._otherWindows.get(window, {}).get("refs", ())
                if "ref" in command and command["ref"] not in refs: raise ComputerUseError(dict(code="element_not_found", effect="none", target=command["ref"], detail="Reference is outside the selected window's current observation.", next="Read the intended window first; use only its observed references."))
                view = None
                if window is not None:
                    view = self._view if window == identity.get("current") else selectWorkingWindow(self._view, window)
                    if view is not self._view and modalDescendant(window, identity.get("current"), identity.get("windows", [])): self._modalParents.set(view, self._view)
                self._pendingRead = dict(query=command, **({"element": (view if view is not None else self._view).element(command["ref"])} if "ref" in command else {}), **({"view": view} if view is not None else {}))
                result = dict(kind="observe", query=command)
            else:
                self._ended = True
                result = dict(kind="finish", summary=command["summary"]) if command["op"] == "done" else dict(kind="handoff", reason=command["reason"])
                if command["op"] == "done": self._completedObservation = dict(frame=frame, images=self._shownImages)
        except Exception as error:
            if isinstance(error, ComputerUseError): failure = {key: getattr(error, key) for key in ("code", "effect", "target", "detail", "next")}
            else:
                detail = "; ".join(f"{'.'.join(map(str, issue['path'])) or 'command'}: {issue['message']}" for issue in error.issues) if isinstance(error, SchemaError) else str(error)
                failure = dict(code="execution_unknown" if failedAction else "invalid_request", effect="possible" if failedAction else "none", target=dumps(failedAction) if failedAction else "Decision instruction", detail=detail,
                    next="Observe before retrying; uncertain input must not be replayed." if failedAction else "Correct the instruction against a fresh observation. read/done/handoff are top-level commands, not actions.")
            result = dict(kind="error", steps=steps, **({"failedAction": failedAction} if failedAction else {}), error=failure)
            self._plannedInterrupted = bool(self.task.get("actions") and (steps or failure["effect"] != "none"))
        finally: self._busy = False
        self._recent.append(result); self._recent = self._recent[-6:]
        return Record(result)
