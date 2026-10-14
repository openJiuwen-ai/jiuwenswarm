import hashlib
from .._identity import WeakIdentityMap
from ..protocol.png import pngSize
from ..protocol.captures import captureMetadata
from ..observation.image_caption import pngSummary, renderImageCaption
from ..observation.formatting import jsonString

_sources = WeakIdentityMap()
_deliveryRequired = False
_deliveryEpoch = 0


def _hash(png): return hashlib.sha256(png).hexdigest()


def requireImageDelivery():
    global _deliveryRequired
    _deliveryRequired = True


def imageDeliveryRequired(): return _deliveryRequired
def imageDeliveryEpoch(): return _deliveryEpoch


def resetImageDelivery():
    global _deliveryEpoch
    _deliveryEpoch += 1


def registerImage(png, source):
    _sources.set(png, dict(source=source, hash=_hash(png), metadata=captureMetadata(png)))
    size = pngSize(png)
    png._displaySummary = pngSummary(size["width"], size["height"], len(png))


def imageMetadata(png):
    known = _sources.get(png)
    return known["metadata"] if known is not None and known["hash"] == _hash(png) else None


def imageDelivered(png):
    known = _sources.get(png)
    if known is not None and known["hash"] == _hash(png) and known["source"].get("current", lambda: True)() is not False:
        known["source"].get("delivered", lambda: None)()


def imageLabel(png, contextAlreadyShown=False):
    size = pngSize(png)
    known = _sources.get(png)
    if known is not None and known["hash"] != _hash(png):
        _sources.delete(png)
        known = None
    source = known["source"] if known is not None else None
    if source is None: facts = dict(kind="unknown")
    elif source["kind"] == "desktop": facts = dict(kind="desktop", stale=source.get("current", lambda: True)() is False)
    elif source["kind"] == "overview": facts = dict(kind="overview", context=source.get("context"))
    else:
        context = source.get("context")
        if context is None: context = "App selector: " + jsonString(source["selector"])
        stale = source.get("current", lambda: True)() is False
        identity = "Window reference: " + (source.get("window") if source.get("window") is not None else "unknown")
        if source.get("observed"): identity += "; observation=" + jsonString(source["observed"]["id"])
        facts = dict(kind="window", context=context, identity=identity, stale=stale,
                     omitContext=contextAlreadyShown and source.get("current", lambda: True)() is not False)
    if source is not None and known["metadata"] is not None:
        facts["stopReasons"] = known["metadata"].get("stopReasons", ())
    return renderImageCaption(size["width"], size["height"], facts)
