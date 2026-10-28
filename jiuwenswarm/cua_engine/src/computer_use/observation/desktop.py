import math

from .formatting import json_string, number_string


def render_desktop(value):

    def box(rect):
        return "(" + ", ".join(
            number_string(math.floor(rect[k] * 100 + 0.5) / 100) for k in ("x", "y", "width", "height")) + ")"

    captured = sum(bool(d["captured"]) for d in value["displays"])
    front = value["frontmost"]
    lines = [
        f"Desktop: {value['mode']}; View: full", "Foreground: " +
        (json_string(front["name"]) + ("; app=" + json_string(front["selector"]) if front.get("selector") else "")
         if front is not None else "unknown"),
        (f"Screenshot: desktop {number_string(value['canvas']['width'])}x"
         f"{number_string(value['canvas']['height'])} px; origin=top-left")
        if captured else "Screenshot: unavailable; window catalogue only", "Displays (x,y,width,height in desktop PNG):"
    ]
    for d in value["displays"]:
        lines.append(f"  {json_string(d['name'])} {box(d['rect'])} " +
                     ("captured" if d["captured"] else "unavailable: " + json_string(d["error"])))
    if captured:
        lines.append("Display gaps and uncaptured regions are blank canvas, not interactive desktop.")
    lines.append("On-screen windows (rectangles may overlap; not element references):")
    for window in value["windows"]:
        lines.append("  " + json_string(window["app"]) + " — " +
                     (json_string(window["title"]) if window["title"] else "<untitled>") + " " + box(window["rect"]) +
                     (" app=" + json_string(window["selector"]) if window.get("selector") else ""))
    if not value["windows"]:
        lines.append("  Unknown" if value.get("catalogError") else "  None reported")
    if value["omittedWindows"]:
        lines.append((f"Window catalogue truncated: {number_string(value['omittedWindows'])} more "
                      f"windows; use the screenshot to inspect visible content."))
    if value.get("catalogError"):
        lines.append("Catalogue: " + json_string(value["catalogError"]))
    lines.append(('Desktop pixels are not App.click coordinates. Window catalogue and '
                  'display images were collected sequentially; re-observe if they '
                  'disagree.'))
    return "\n".join(lines)
