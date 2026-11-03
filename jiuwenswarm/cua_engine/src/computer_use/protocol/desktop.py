from .captures import capture_metadata, decode_capture, decode_stamp
from .png import png_size
from .primitives import nonempty, record
from .schema import array, boolean, enum, number, obj, string

_name = string(min=1, max=4096)
_rect = obj(
    dict(x=number(min=0), y=number(min=0), width=number(min=0, exclusiveMin=True),
         height=number(min=0, exclusiveMin=True)))
_schema = obj(
    dict(mode=enum("shared", "exclusive"),
         canvas=obj(dict(width=number(integer=True, min=1, max=4096),
                         height=number(integer=True, min=1,
                                       max=4096))), frontmost=obj(dict(name=_name,
                                                                       selector=_name.optional())).nullable(),
         displays=array(obj(dict(name=_name, rect=_rect, captured=boolean(), error=_name.optional())), min=1, max=32),
         windows=array(obj(dict(app=_name, title=string(max=4096), selector=_name.optional(), rect=_rect)),
                       max=256), omittedWindows=number(integer=True, min=0), catalogError=_name.optional()))


def decode_desktop(value):
    result = _schema.parse(value)
    if len({d["name"] for d in result["displays"]}) != len(result["displays"]):
        raise ValueError("protocol: duplicate desktop display name")
    for item in result["displays"] + result["windows"]:
        rect = item["rect"]
        if rect["x"] + rect["width"] > result["canvas"]["width"] + \
                1e-6 or rect["y"] + rect["height"] > result["canvas"]["height"] + 1e-6:
            raise ValueError("protocol: desktop rectangle exceeds canvas")
    if any(d["captured"] == ("error" in d) for d in result["displays"]):
        raise ValueError("protocol: inconsistent display capture status")
    return result


def decode_desktop_observation(value):
    """Common pixel/catalogue contract; independent of transport version."""
    result = record(value, ["desktopRef", "observed", "scope", "observation", "screenshot"])
    nonempty(result["desktopRef"], "protocol desktopRef")
    nonempty(result["scope"], "protocol desktop scope")
    observed, observation = decode_stamp(result["observed"]), decode_desktop(result["observation"])
    screenshot = None if result["screenshot"] is None else decode_capture(result["screenshot"], "desktop",
                                                                          result["scope"], observed)
    inconsistent_screenshot = (
        (screenshot is not None) != any(d["captured"] for d in observation["displays"])
        or (screenshot is not None and
            (png_size(screenshot) != observation["canvas"] or capture_metadata(screenshot)["id"] == observed["id"])))
    if inconsistent_screenshot:
        raise ValueError("protocol: inconsistent desktop screenshot")
    return dict(desktopRef=result["desktopRef"], scope=result["scope"], observed=observed, observation=observation,
                screenshot=screenshot)
