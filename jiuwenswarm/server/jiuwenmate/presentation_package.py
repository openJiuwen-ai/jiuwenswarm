"""Make a handed-off presentation open when its slide-size declaration disagrees with the page.

python-pptx keeps the blank template's ``screen4x3`` type after the page is
resized. PowerPoint and Keynote then refuse the package. A size within one
millimetre of a standard page is snapped to that page; any other mismatch is
marked ``custom`` and the page size is left as drawn.
"""

from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

_PRESENTATIONS = frozenset({".pptx", ".pptm"})
_MEMBER = "ppt/presentation.xml"
# Inches(13.333) is 305 EMU short of the 16:9 page. One millimetre is 36000 EMU.
_TOLERANCE = 36_000
_PRESETS = (
    ("screen4x3", 9_144_000, 6_858_000),
    ("screen16x9", 12_192_000, 6_858_000),
    ("screen16x10", 12_192_000, 7_620_000),
)
_SLIDE_SIZE = re.compile(r"<(?P<prefix>[A-Za-z0-9]+):sldSz\b(?P<attrs>[^>]*?)/>")
_ATTRIBUTE = re.compile(r"([A-Za-z_][\w:.-]*)=\"([^\"]*)\"")


def normalize_presentation_package(path: Path) -> bool:
    """Rewrite one presentation's slide-size declaration. Return whether the file changed."""
    if path.suffix.lower() not in _PRESENTATIONS or not path.is_file():
        return False
    try:
        with zipfile.ZipFile(path) as package:
            original = package.read(_MEMBER)
            infos = package.infolist()
            members = {info.filename: package.read(info.filename) for info in infos}
    except (OSError, zipfile.BadZipFile, KeyError):
        return False
    revised = _revise_slide_size(original)
    if revised == original:
        return False
    temporary = path.with_name(f".{path.name}.normalizing")
    try:
        with zipfile.ZipFile(temporary, "w") as package:
            for info in infos:
                data = revised if info.filename == _MEMBER else members[info.filename]
                replaced = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                replaced.compress_type = info.compress_type
                replaced.external_attr = info.external_attr
                package.writestr(replaced, data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _revise_slide_size(original: bytes) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        return original
    match = _SLIDE_SIZE.search(text)
    if match is None:
        return original
    attrs = dict(_ATTRIBUTE.findall(match.group("attrs")))
    try:
        width = int(attrs["cx"])
        height = int(attrs["cy"])
    except (KeyError, ValueError):
        return original
    declared = attrs.get("type")
    if declared == "custom":
        return original
    preset = _nearest_preset(width, height)
    if preset is None:
        if declared in {name for name, _, _ in _PRESETS}:
            attrs["type"] = "custom"
        else:
            return original
    else:
        name, preset_width, preset_height = preset
        if declared == name and width == preset_width and height == preset_height:
            return original
        attrs["cx"] = str(preset_width)
        attrs["cy"] = str(preset_height)
        attrs["type"] = name
    rendered = " ".join(f'{key}="{value}"' for key, value in attrs.items())
    tag = f"<{match.group('prefix')}:sldSz {rendered}/>"
    return (text[: match.start()] + tag + text[match.end() :]).encode("utf-8")


def _nearest_preset(width: int, height: int) -> tuple[str, int, int] | None:
    nearest: tuple[str, int, int] | None = None
    distance = _TOLERANCE + 1
    for name, preset_width, preset_height in _PRESETS:
        delta = abs(width - preset_width) + abs(height - preset_height)
        if delta < distance:
            nearest = (name, preset_width, preset_height)
            distance = delta
    return nearest if distance <= _TOLERANCE else None
