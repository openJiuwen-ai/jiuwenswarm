import weakref
from ..protocol.primitives import finite
from .images import imageDeliveryEpoch, imageDeliveryRequired, registerImage
from .diagnostics import localFailure


class DesktopStore:
    def __init__(self): self.reset()
    def reset(self): self._view = None; self._delivered = None
    def record(self, state):
        self._view, self._delivered = state, None
        if (png := state.get('screenshot')) is not None:
            # Registry callbacks must not retain their own image or owner.
            image, desktop = weakref.ref(png), weakref.ref(self)
            registerImage(png, dict(kind='desktop',
                current=lambda: desktop() is not None and image() is not None and desktop().current(image()),
                delivered=lambda: desktop().delivered(image()) if desktop() is not None and image() is not None else None))
    def current(self, png): return self._view is not None and self._view.get("screenshot") is png
    def observed(self, ref):
        if self._view is None or self._view['desktopRef'] != ref:
            raise localFailure('desktop observation changed', 'read the desktop again', 'stale_target', 'Desktop')
        return self._view
    def delivered(self, png):
        if self.current(png): self._delivered = dict(state=self._view, epoch=imageDeliveryEpoch())
    def target(self, coordinates=False):
        state = self._view
        if state is None: raise localFailure("desktop observation is missing or expired", "call Computer.read before input", "stale_target", "Desktop")
        if state["observation"]["mode"] != "exclusive":
            raise localFailure("desktop input requires exclusive mode", "use App/Element operations in shared mode; desktop mode is host configuration", "mode_unsupported", "Desktop")
        if coordinates:
            if state.get("screenshot") is None:
                raise localFailure("desktop observation has no PNG", "read the desktop and inspect capture availability", "input_unavailable", "Desktop")
            if imageDeliveryRequired() and (self._delivered is None or self._delivered["state"] is not state or self._delivered["epoch"] != imageDeliveryEpoch()):
                raise localFailure("current desktop image has not been returned to the model", "finish Computer.read() in this tool call, then choose desktop coordinates in the next call", "input_unavailable", "Desktop")
        return state["desktopRef"]
    def checkCoordinates(self, x, y):
        self.target(True)
        if not finite(x) or not finite(y) or x < 0 or y < 0:
            raise localFailure("coordinates must be finite, nonnegative numbers", "use pixels from Computer.read's desktop PNG")
        matches = [d for d in self._view["observation"]["displays"] if d["rect"]["x"] <= x < d["rect"]["x"] + d["rect"]["width"]
                   and d["rect"]["y"] <= y < d["rect"]["y"] + d["rect"]["height"]]
        if len(matches) != 1 or not matches[0]["captured"]:
            raise localFailure("coordinates are outside a captured display or in a display gap/overlap", "choose a point on one captured display in the desktop PNG")
