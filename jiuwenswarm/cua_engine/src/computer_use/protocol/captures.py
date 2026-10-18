"""Capture identities are object-local; never recovered from equal PNG bytes."""
import weakref

from .png import PNG as PNG
from .png import decode_png, png_size
from .primitives import frozen
from .schema import array, enum, identity, number, obj, string

_interval = dict(startedAt=number(min=0), completedAt=number(min=0))
_stamp = obj(dict(id=identity, **_interval))
_capture = obj(
    dict(id=identity, kind=enum("window", "overview", "desktop"), scope=identity, **_interval,
         width=number(integer=True, min=0, exclusiveMin=True), height=number(integer=True, min=0, exclusiveMin=True),
         png=string(), stopReasons=array(string(nonblank=True, max=4096), max=16).optional()))
_captures = {}


def decode_stamp(value):
    stamp = _stamp.parse(value)
    if stamp["completedAt"] < stamp["startedAt"]:
        raise ValueError("protocol: reversed observation interval")
    return frozen(stamp)


def decode_capture(value, kind, scope=None, observed=None):
    metadata = _capture.parse(value)
    base64 = metadata.pop("png")
    if "stopReasons" in metadata:
        metadata["stopReasons"] = tuple(metadata["stopReasons"])
    if metadata["kind"] != kind or (scope is not None and metadata["scope"] != scope):
        raise ValueError("protocol: image belongs to a different scope or kind")
    outside_observation = metadata["completedAt"] < metadata["startedAt"] or (
        observed is not None and
        (metadata["startedAt"] < observed["startedAt"] or metadata["completedAt"] > observed["completedAt"]))
    if outside_observation:
        raise ValueError("protocol: image capture is outside its observation interval")
    png = decode_png(base64)
    size = png_size(png)
    if metadata["width"] != size["width"] or metadata["height"] != size["height"]:
        raise ValueError("protocol: image dimensions do not match PNG")
    key = id(png)

    def release(ref):
        item = _captures.get(key)
        if item is not None and item[0] is ref:
            _captures.pop(key, None)

    _captures[key] = (weakref.ref(png, release), frozen(metadata))
    return png


def capture_metadata(png):
    item = _captures.get(id(png))
    return item[1] if item is not None and item[0]() is png else None
