import weakref

from ..protocol.primitives import finite
from .diagnostics import local_failure
from .images import image_delivery_epoch, image_delivery_required, register_image


class DesktopStore:

    def __init__(self):
        self.reset()

    def reset(self):
        self._view = None
        self._delivered = None

    def record(self, state):
        self._view, self._delivered = state, None
        png = state.get('screenshot')
        if png is not None:
            # Registry callbacks must not retain their own image or owner.
            image, desktop = weakref.ref(png), weakref.ref(self)
            register_image(
                png,
                dict(
                    kind='desktop',
                    current=lambda: desktop() is not None and image() is not None and desktop().current(image()),
                    delivered=lambda coordinates=None: desktop().delivered(image(), coordinates)
                    if desktop() is not None and image() is not None else None))

    def current(self, png):
        return self._view is not None and self._view.get("screenshot") is png

    def observed(self, ref):
        if self._view is None or self._view['desktopRef'] != ref:
            raise local_failure('desktop observation changed', 'read the desktop again', 'stale_target', 'Desktop')
        return self._view

    def delivered(self, png, coordinates=None):
        if self.current(png):
            self._delivered = dict(state=self._view, epoch=image_delivery_epoch(), coordinates=coordinates)

    def map_parameters(self, operation, parameters):
        self.target(True)
        coordinates = self._delivered.get("coordinates") if self._delivered is not None else None
        return coordinates.source_parameters(operation, parameters) if coordinates is not None else parameters

    def target(self, coordinates=False):
        state = self._view
        if state is None:
            raise local_failure("desktop observation is missing or expired", "call Computer.read before input",
                                "stale_target", "Desktop")
        if state["observation"]["mode"] != "exclusive":
            raise local_failure("desktop input requires exclusive mode",
                                "use App/Element operations in shared mode; desktop mode is host configuration",
                                "mode_unsupported", "Desktop")
        if coordinates:
            if state.get("screenshot") is None:
                raise local_failure("desktop observation has no PNG",
                                    "read the desktop and inspect capture availability", "input_unavailable", "Desktop")
            image_not_delivered = image_delivery_required() and (self._delivered is None
                                                                 or self._delivered["state"] is not state
                                                                 or self._delivered["epoch"] != image_delivery_epoch())
            if image_not_delivered:
                raise local_failure(
                    "current desktop image has not been returned to the model",
                    "finish Computer.read() in this tool call, then choose desktop coordinates in the next call",
                    "input_unavailable", "Desktop")
        return state["desktopRef"]

    def check_coordinates(self, x, y):
        self.target(True)
        coordinates = self._delivered.get("coordinates") if self._delivered is not None else None
        if coordinates is not None:
            x, y = coordinates.source_point((x, y))
        invalid_coordinates = not finite(x) or not finite(y) or x < 0 or y < 0
        if invalid_coordinates:
            raise local_failure("coordinates must be finite, nonnegative numbers",
                                "use pixels from Computer.read's desktop PNG")
        matches = []
        for display in self._view["observation"]["displays"]:
            rect = display["rect"]
            if rect["x"] <= x < rect["x"] + rect["width"] and rect["y"] <= y < rect["y"] + rect["height"]:
                matches.append(display)
        if len(matches) != 1 or not matches[0]["captured"]:
            raise local_failure("coordinates are outside a captured display or in a display gap/overlap",
                                "choose a point on one captured display in the desktop PNG")
