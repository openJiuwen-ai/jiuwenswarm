import hashlib

from .._identity import WeakIdentityMap
from ..observation.formatting import json_string
from ..observation.image_caption import png_summary, render_image_caption
from ..protocol.captures import capture_metadata
from ..protocol.png import png_size

_sources = WeakIdentityMap()
_DELIVERY_REQUIRED = False
_DELIVERY_EPOCH = 0


def _hash(png):
    return hashlib.sha256(png).hexdigest()


def require_image_delivery():
    global _DELIVERY_REQUIRED
    _DELIVERY_REQUIRED = True


def image_delivery_required():
    return _DELIVERY_REQUIRED


def image_delivery_epoch():
    return _DELIVERY_EPOCH


def reset_image_delivery():
    global _DELIVERY_EPOCH
    _DELIVERY_EPOCH += 1


def register_image(png, source):
    _sources.set(png, dict(source=source, hash=_hash(png), metadata=capture_metadata(png)))
    size = png_size(png)
    png.display_summary = png_summary(size["width"], size["height"], len(png))


def image_metadata(png):
    known = _sources.get(png)
    return known["metadata"] if known is not None and known["hash"] == _hash(png) else None


def image_delivered(png, coordinates=None):
    known = _sources.get(png)
    if known is not None and known["hash"] == _hash(png) and known["source"].get("current",
                                                                                 lambda: True)() is not False:
        delivered = known["source"].get("delivered")
        if delivered is not None:
            if coordinates is not None:
                size = png_size(png)
                if coordinates.source_size != (size["width"], size["height"]):
                    raise ValueError("The delivered coordinate map does not belong to this source PNG.")
            if coordinates is None or coordinates.source_size == coordinates.delivered_size:
                delivered()
            else:
                delivered(coordinates)


def image_label(png, context_already_shown=False, coordinates=None):
    size = png_size(png)
    if coordinates is not None:
        size = dict(width=coordinates.delivered_size[0], height=coordinates.delivered_size[1])
    known = _sources.get(png)
    if known is not None and known["hash"] != _hash(png):
        _sources.delete(png)
        known = None
    source = known["source"] if known is not None else None
    if source is None:
        facts = dict(kind="unknown")
    elif source["kind"] == "desktop":
        facts = dict(kind="desktop", stale=source.get("current", lambda: True)() is False)
    elif source["kind"] == "overview":
        facts = dict(kind="overview", context=source.get("context"))
    else:
        context = source.get("context")
        if context is None:
            context = "App selector: " + json_string(source["selector"])
        stale = source.get("current", lambda: True)() is False
        identity = "Window reference: " + (source.get("window") if source.get("window") is not None else "unknown")
        if source.get("observed"):
            identity += "; observation=" + json_string(source["observed"]["id"])
        facts = dict(kind="window", context=context, identity=identity, stale=stale, omitContext=context_already_shown
                     and source.get("current", lambda: True)() is not False)
    if source is not None and known["metadata"] is not None:
        facts["stopReasons"] = known["metadata"].get("stopReasons", ())
    caption = render_image_caption(size["width"], size["height"], facts)
    return caption + ("\nUse this displayed image's pixels for coordinates." if coordinates is not None else "")
