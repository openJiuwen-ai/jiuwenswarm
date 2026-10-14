import math
from .formatting import jsonString, numberString
from ..protocol.desktop import decodeDesktop


def renderDesktop(value):
    def box(rect):
        return "(" + ", ".join(numberString(math.floor(rect[k] * 100 + 0.5) / 100) for k in ("x", "y", "width", "height")) + ")"
    captured = sum(bool(d["captured"]) for d in value["displays"])
    front = value["frontmost"]
    lines = [f"Desktop: {value['mode']}; View: full",
             "Foreground: " + (jsonString(front["name"]) + ("; app=" + jsonString(front["selector"]) if front.get("selector") else "") if front is not None else "unknown"),
             f"Screenshot: desktop {numberString(value['canvas']['width'])}x{numberString(value['canvas']['height'])} px; origin=top-left" if captured else "Screenshot: unavailable; window catalogue only",
             "Displays (x,y,width,height in desktop PNG):"]
    for d in value["displays"]:
        lines.append(f"  {jsonString(d['name'])} {box(d['rect'])} " + ("captured" if d["captured"] else "unavailable: " + jsonString(d["error"])))
    if captured:
        lines.append("Display gaps and uncaptured regions are blank canvas, not interactive desktop.")
    lines.append("On-screen windows (rectangles may overlap; not element references):")
    for window in value["windows"]:
        lines.append("  " + jsonString(window["app"]) + " — " + (jsonString(window["title"]) if window["title"] else "<untitled>") + " " + box(window["rect"]) + (" app=" + jsonString(window["selector"]) if window.get("selector") else ""))
    if not value["windows"]:
        lines.append("  Unknown" if value.get("catalogError") else "  None reported")
    if value["omittedWindows"]:
        lines.append(f"Window catalogue truncated: {numberString(value['omittedWindows'])} more windows; use the screenshot to inspect visible content.")
    if value.get("catalogError"):
        lines.append("Catalogue: " + jsonString(value["catalogError"]))
    lines.append("Desktop pixels are not App.click coordinates. Window catalogue and display images were collected sequentially; re-observe if they disagree.")
    return "\n".join(lines)
