from .formatting import jsonString, numberString


def pngSummary(width, height, bytes):
    return f"PNG image ({numberString(width)}x{numberString(height)} px; {numberString(bytes)} bytes; use printImage(...) to display)"


def renderImageCaption(width, height, facts):
    return _baseCaption(width, height, facts) + "".join(
        "\nImage note: " + jsonString(reason) for reason in facts.get("stopReasons", ()))


def _baseCaption(width, height, facts):
    size = numberString(width) + "x" + numberString(height)
    kind = facts["kind"]
    if kind == "unknown":
        return f"Image: {size} px; source unknown; do not infer App.click coordinates."
    if kind == "desktop":
        return f"Image: desktop {size} px; not valid for App.click coordinates." + (" This image is stale; read the desktop again." if facts.get("stale") else " Origin=top-left; use its display layout and window catalogue.")
    if kind == "overview":
        return f"Image: app overview {size} px; context only, not App.click coordinates; use app.window(ref).read().\n" + (facts.get("context") or "")
    validity = "This image is stale; read this App and return its current screenshot before using coordinates." if facts.get("stale") else "Window PNG coordinates; printing does not refresh."
    return f"Image: window {size} px; origin=top-left\n{facts['identity']}" + ("" if facts.get("omitContext") else "\n" + facts["context"]) + "\n" + validity
