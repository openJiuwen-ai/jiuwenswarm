# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Component rules for the Gateway-owned application slots.

The vocabulary matches the browser component catalog, but this copy is the
Gateway contract: it carries the Form / Form.Item / Dropdown interaction
components a package needs for an application-slot view. The AgentServer copy
does not enable them, so the two files intentionally diverge here.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from jiuwenswarm.gateway.extensions_v2.webui.constants import MAX_LIST_ITEMS, MAX_PROP_DEPTH, MAX_TEXT_BYTES
from jiuwenswarm.gateway.extensions_v2.webui.icon_whitelist_generated import WEBUI_ICON_NAMES


def token(*values: str) -> dict[str, Any]:
    return {"kind": "token", "values": values}


def array(item: Any) -> dict[str, Any]:
    return {"kind": "array", "item": item}


def union(*choices: Any) -> dict[str, Any]:
    return {"kind": "union", "choices": choices}


def obj(fields: Mapping[str, Any], *required: str) -> dict[str, Any]:
    return {"kind": "object", "fields": fields, "required": required}


TEXT = {"kind": "string"}
LOCALIZED_TEXT = union(TEXT, obj({"zh": TEXT, "en": TEXT}, "zh", "en"))
BOOL = {"kind": "boolean"}
NUMBER = {"kind": "number"}
NIL = {"kind": "null"}
INTEGER = {"kind": "number", "integer": True, "min": 0}
SIZE = token("small", "medium", "large", "sm", "md", "lg")
COMPACT_SIZE = token("small", "medium", "sm", "md")
PRESET_COLORS = (
    "blue", "purple", "cyan", "green", "magenta", "pink", "red",
    "orange", "yellow", "volcano", "geekblue", "lime", "gold",
)
VARIANT = token("outlined", "borderless", "filled", "underlined")
INPUT_PROPS = {"disabled": BOOL, "ariaLabel": TEXT, "size": SIZE, "variant": VARIANT,
               "status": token("error", "warning")}
OPTION_VALUE = union(TEXT, NUMBER)
SELECTION = union(TEXT, NIL, array(TEXT))
NO_DATA = obj({})
VALUE_TEXT = obj({"value": TEXT}, "value")
FIELD_NAME = {"kind": "string", "minBytes": 1, "maxBytes": 200}
FORM_RULE = obj({
    "required": BOOL, "min": INTEGER, "max": INTEGER, "pattern": TEXT, "message": TEXT,
})
MENU_ITEM = union(
    obj({"type": token("item"), "key": TEXT, "label": union(TEXT, {"kind": "nodes"}),
         "disabled": BOOL, "danger": BOOL}, "key", "label"),
    obj({"type": token("divider"), "key": TEXT}, "type"),
)
DIMENSION = union(NUMBER, TEXT)
TREE_OPTION = obj({
    "key": TEXT, "value": TEXT, "title": TEXT, "disabled": BOOL, "disableCheckbox": BOOL,
    "selectable": BOOL, "checkable": BOOL, "children": array({"kind": "tree-option"}),
}, "value", "title")

_FILE_NODE: dict[str, Any] = obj({
    "key": TEXT, "title": TEXT, "path": TEXT, "kind": token("file", "directory"),
    "children": array({"kind": "lazy", "get": lambda: _FILE_NODE}),
    "loaded": BOOL, "truncated": BOOL,
}, "key", "title", "path", "kind")

_JSON_VALUE: dict[str, Any] = union(
    TEXT, BOOL, NUMBER, NIL,
    array({"kind": "lazy", "get": lambda: _JSON_VALUE}),
    {"kind": "record", "value": {"kind": "lazy", "get": lambda: _JSON_VALUE}},
)

# The same rule vocabulary as catalog/types.ts, without UI adapters or callbacks.
COMPONENT_RULES: dict[str, dict[str, Any]] = {
    "Box": {"props": {
        "direction": token("row", "column"), "gap": union(token("none", "sm", "md", "lg"), {"kind": "number", "min": 0}),
        "align": token("stretch", "start", "center", "end", "baseline"),
        "justify": token("start", "center", "end", "between", "around", "evenly"),
        "wrap": BOOL, "flex": {"kind": "number", "min": 0, "max": 1000},
        "width": DIMENSION, "height": DIMENSION, "minHeight": DIMENSION, "maxWidth": DIMENSION,
        "marginInline": DIMENSION, "padding": token("none", "sm", "md", "lg"),
        "backgroundImage": TEXT, "backgroundFit": token("cover", "contain"),
        "visibleFrom": token("sm", "md", "lg"),
    }, "children": True},
    "Text": {"props": {
        "value": LOCALIZED_TEXT, "type": token("secondary", "success", "warning", "danger"),
        "size": SIZE, "strong": BOOL, "italic": BOOL, "underline": BOOL, "delete": BOOL,
        "code": BOOL, "mark": BOOL, "keyboard": BOOL, "disabled": BOOL, "ellipsis": BOOL,
    }},
    "Button": {"props": {
        "openPanel": {"kind": "string", "maxBytes": 128},
        "value": LOCALIZED_TEXT, "type": token("default", "primary", "dashed", "link", "text"),
        "color": token("default", "primary", "danger", *PRESET_COLORS),
        "variant": token("outlined", "dashed", "solid", "filled", "text", "link"),
        "shape": token("default", "circle", "round", "square"),
        "size": SIZE, "danger": BOOL, "ghost": BOOL, "disabled": BOOL, "block": BOOL,
        "loading": union(BOOL, obj({"delay": NUMBER})), "href": TEXT, "target": token("_self", "_blank"),
        "icon": token(*WEBUI_ICON_NAMES), "iconPlacement": token("start", "end"), "aria-label": LOCALIZED_TEXT, "form": FIELD_NAME,
    }, "events": {"ui.click": {"kind": "record", "value": TEXT}}},
    "Input": {"props": {
        **INPUT_PROPS, "ariaLabel": LOCALIZED_TEXT, "value": TEXT, "defaultValue": TEXT, "placeholder": TEXT, "maxLength": INTEGER,
        "allowClear": BOOL, "showCount": BOOL, "type": token("text", "email", "tel", "url", "password"),
        "readOnly": BOOL, "autoComplete": TEXT,
    }, "events": {
        "ui.click": VALUE_TEXT, "ui.change": obj({"value": TEXT, "isComposing": BOOL}, "value", "isComposing"),
        "ui.press_enter": VALUE_TEXT,
    }},
    "Switch": {"props": {
        "checked": BOOL, "defaultChecked": BOOL, "loading": BOOL, "disabled": BOOL,
        "ariaLabel": LOCALIZED_TEXT, "size": COMPACT_SIZE, "checkedChildren": TEXT, "unCheckedChildren": TEXT,
    }, "events": {name: obj({"checked": BOOL}, "checked") for name in ("ui.click", "ui.change")}},
    "Select": {"props": {
        **INPUT_PROPS, "ariaLabel": LOCALIZED_TEXT, "options": array(obj({"value": OPTION_VALUE, "label": LOCALIZED_TEXT, "disabled": BOOL}, "value", "label")),
        "value": union(OPTION_VALUE, NIL, array(OPTION_VALUE)),
        "defaultValue": union(OPTION_VALUE, NIL, array(OPTION_VALUE)), "placeholder": TEXT, "allowClear": BOOL,
        "mode": token("multiple", "tags"), "maxCount": {"kind": "number", "integer": True, "min": 1},
        "maxTagCount": union(INTEGER, token("responsive")), "showSearch": BOOL,
    }, "required": ("options",), "events": {
        "ui.change": obj({"value": union(OPTION_VALUE, NIL, array(OPTION_VALUE))}, "value"),
        "ui.select": obj({"value": OPTION_VALUE}, "value"),
        "ui.search": obj({"searchValue": TEXT}, "searchValue"), "ui.clear": NO_DATA,
    }},
    "TreeSelect": {"props": {
        **INPUT_PROPS, "treeData": array(TREE_OPTION), "value": SELECTION, "defaultValue": SELECTION,
        "placeholder": TEXT, "allowClear": BOOL, "multiple": BOOL, "treeCheckable": BOOL,
        "showCheckedStrategy": token("SHOW_CHILD", "SHOW_PARENT", "SHOW_ALL"), "showSearch": BOOL,
        "treeDefaultExpandAll": BOOL, "treeDefaultExpandedKeys": array(TEXT), "maxTagCount": INTEGER,
    }, "required": ("treeData",), "events": {
        "ui.select": VALUE_TEXT, "ui.change": obj({"value": SELECTION}, "value"), "ui.clear": NO_DATA,
    }},
    "Slider": {"props": {
        "min": NUMBER, "max": NUMBER, "step": NUMBER, "value": NUMBER, "defaultValue": NUMBER,
        "disabled": BOOL, "ariaLabel": TEXT, "marks": {"kind": "record", "value": TEXT},
        "dots": BOOL, "included": BOOL, "reverse": BOOL, "orientation": token("horizontal", "vertical"),
    }, "events": {"ui.change": obj({"value": NUMBER}, "value")}},
    "Tabs": {"props": {
        "items": array(obj({"key": TEXT, "label": TEXT, "disabled": BOOL, "children": {"kind": "nodes"}}, "key", "label")),
        "activeKey": TEXT, "defaultActiveKey": TEXT, "type": token("line", "card"),
        "tabPlacement": token("top", "end", "start", "bottom"), "size": SIZE,
        "centered": BOOL, "destroyOnHidden": BOOL, "ariaLabel": TEXT,
    }, "required": ("items",), "events": {"ui.change": obj({"activeKey": TEXT}, "activeKey")}},
    "Empty": {"props": {"image": TEXT, "description": TEXT}},
    "Avatar": {"props": {
        "src": TEXT, "children": TEXT, "alt": TEXT,
        "gap": {"kind": "number", "min": 0, "max": 4096},
        "style": obj({
            "backgroundColor": {"kind": "string", "maxBytes": 9},
            "color": {"kind": "string", "maxBytes": 9},
            "fontSize": {"kind": "number", "min": 1, "max": 4096},
        }),
        "size": union(token("small", "medium", "large", "sm", "md", "lg", "default"),
                      {"kind": "number", "min": 1, "max": 4096}),
        "shape": token("circle", "square"),
    }},
    "Image": {"props": {
        "src": TEXT, "alt": TEXT, "width": union(NUMBER, TEXT), "height": union(NUMBER, TEXT),
        "fallback": TEXT, "preview": union(BOOL, obj({"src": TEXT}, "src")),
        "fit": token("contain", "cover"),
    }, "required": ("src",)},
    "Divider": {"props": {
        "children": TEXT, "orientation": token("horizontal", "vertical"), "variant": token("solid", "dashed", "dotted"),
        "titlePlacement": token("start", "center", "end"), "plain": BOOL, "size": SIZE,
    }, "children": True, "childComponents": ("Text",)},
    "Card": {"props": {
        "title": TEXT, "extra": TEXT, "cover": TEXT, "hoverable": BOOL,
        "size": COMPACT_SIZE, "variant": token("outlined", "borderless"),
    }, "children": True},
    "Tag": {"props": {
        "children": TEXT, "color": token("default", "success", "processing", "warning", "error"),
        "variant": token("filled", "solid", "outlined"), "disabled": BOOL,
        "href": TEXT, "target": token("_self", "_blank"),
    }},
    "Form": {"props": {
        "name": FIELD_NAME, "layout": token("vertical", "horizontal", "inline"), "disabled": BOOL,
        "ariaLabel": TEXT, "requiredMark": BOOL,
    }, "required": ("name",), "children": True, "childComponents": ("Form.Item",)},
    "Form.Item": {"props": {
        "name": FIELD_NAME, "label": TEXT, "rules": array(FORM_RULE),
    }, "required": ("name",), "children": True, "childComponents": ("Input",)},
    "Dropdown": {"props": {
        "trigger": token("click", "hover"), "disabled": BOOL,
        "ariaLabel": TEXT,
        "placement": token("bottomLeft", "bottom", "bottomRight", "topLeft", "top", "topRight",
                           "left", "leftTop", "leftBottom", "right", "rightTop", "rightBottom"),
        "align": obj({"points": array(token("tl", "tc", "tr", "cl", "cc", "cr", "bl", "bc", "br")),
                      "offset": array({"kind": "number", "min": -128, "max": 128})}, "points"),
        "autoFocus": BOOL, "destroyOnHidden": BOOL,
        "styles": obj({"root": obj({"width": {"kind": "number", "min": 0, "max": 4096}})}),
        "items": array(MENU_ITEM),
    }, "required": ("items",), "children": True, "events": {"ui.select": obj({"key": TEXT}, "key")}},
    "FileTree": {
        "props": {
            "treeData": array(_FILE_NODE), "selectedKey": union(TEXT, NIL), "expandedKeys": array(TEXT),
            "disabled": BOOL, "ariaLabel": TEXT, "emptyText": TEXT, "message": TEXT,
            "status": token("ready", "unavailable", "unsupported", "error"),
            "truncated": BOOL, "readFailed": BOOL,
        },
        "required": ("treeData",),
        "events": {
            "ui.select": obj({"key": TEXT}, "key"),
            "ui.change": obj({"key": TEXT, "expanded": BOOL, "expandedKeys": array(TEXT)}, "key", "expanded"),
        },
    },
    "FilePreview": {
        "props": {
            "file": union(NIL, obj({
                "name": TEXT, "path": TEXT, "mimeType": TEXT, "size": INTEGER, "content": TEXT,
                "downloadUrl": TEXT,
            }, "name", "path")),
            "emptyText": TEXT,
            "status": token("ready", "missing", "unsupported", "too_large", "forbidden", "unavailable", "error"),
        },
        "required": ("file",),
    },
    "Panel": {"props": {"title": {"kind": "string", "maxBytes": 200}}, "required": ("title",), "children": True},
    "Iframe": {"props": {
        "src": LOCALIZED_TEXT, "srcDoc": LOCALIZED_TEXT, "title": LOCALIZED_TEXT,
        "minWidth": {"kind": "number", "min": 0, "max": 4096},
    }, "required": ("title",)},
    "MediaSurface": {"props": {
        "kind": token("image", "video", "html"), "src": TEXT, "title": {"kind": "string", "maxBytes": 200},
        "poster": TEXT, "fit": token("cover", "contain"), "position": token("center", "top", "bottom", "left", "right"),
        "loop": BOOL, "muted": BOOL, "dim": {"kind": "number", "min": 0, "max": 0.9},
        "blur": {"kind": "number", "min": 0, "max": 40}, "opacity": {"kind": "number", "min": 0.1, "max": 1},
    }, "required": ("kind", "src", "title")},
    "ApplicationSurface": {"props": {
        "src": TEXT, "title": LOCALIZED_TEXT,
        "width": {"kind": "number", "integer": True, "min": 160, "max": 960},
        "height": {"kind": "number", "integer": True, "min": 120, "max": 960},
        "config": {"kind": "record", "value": _JSON_VALUE},
    }, "required": ("src", "title")},
    # Host-mediated import (S_10 section 6); the data is the host-issued upload id.
    "FileImport": {"props": {
        "label": union({"kind": "string", "minBytes": 1, "maxBytes": 200}, obj({
            "zh": {"kind": "string", "minBytes": 1, "maxBytes": 200},
            "en": {"kind": "string", "minBytes": 1, "maxBytes": 200},
        }, "zh", "en")), "accept": array(TEXT),
        "multiple": BOOL, "directory": BOOL, "disabled": BOOL,
    }, "required": ("label",), "events": {"ui.change": obj({"upload_id": TEXT}, "upload_id")}},
}

#: One ``accept`` entry, for example ``.zip``.
FILE_SUFFIX = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")
UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")


def finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return abs(value) <= 1.7976931348623157e308
    except OverflowError:
        return False


def check_value(rule: Mapping[str, Any], value: Any, *, depth: int = 0) -> bool:
    """Check one catalog value; render-node contents are walked by schema.py."""
    if depth > MAX_PROP_DEPTH:
        return False
    kind = rule["kind"]
    if kind == "tree-option":
        return check_value(TREE_OPTION, value, depth=depth)
    if kind == "lazy":
        return check_value(rule["get"](), value, depth=depth)
    if kind == "string":
        if not isinstance(value, str):
            return False
        size = len(value.encode("utf-8"))
        return size >= rule.get("minBytes", 0) and size <= rule.get("maxBytes", MAX_TEXT_BYTES)
    if kind == "boolean":
        return type(value) is bool
    if kind == "null":
        return value is None
    if kind == "number":
        return (finite_number(value) and (not rule.get("integer") or value % 1 == 0)
                and value >= rule.get("min", -math.inf) and value <= rule.get("max", math.inf))
    if kind == "token":
        return isinstance(value, str) and value in rule["values"]
    if kind == "union":
        return any(check_value(choice, value, depth=depth) for choice in rule["choices"])
    if kind in ("array", "nodes"):
        if not isinstance(value, (list, tuple)) or len(value) > MAX_LIST_ITEMS:
            return False
        if kind == "nodes":
            from jiuwenswarm.gateway.extensions_v2.webui.models import RenderNode

            return all(isinstance(item, RenderNode) for item in value)
        return all(check_value(rule["item"], item, depth=depth + 1) for item in value)
    if kind in ("object", "record"):
        if (not isinstance(value, Mapping) or len(value) > MAX_LIST_ITEMS
                or any(not isinstance(key, str) or len(key.encode("utf-8")) > MAX_TEXT_BYTES for key in value)):
            return False
        if kind == "record":
            return len(value) <= MAX_LIST_ITEMS and all(
                check_value(rule["value"], item, depth=depth + 1) for item in value.values()
            )
        fields = rule["fields"]
        return (not (set(value) - fields.keys()) and set(rule["required"]) <= value.keys()
                and all(check_value(fields[key], item, depth=depth + 1) for key, item in value.items()))
    return False


def _unique(values: list[Any]) -> bool:
    return len(set(values)) == len(values)


def _form_rules_ok(rules: Any) -> bool:
    """Each rule must constrain the string and carry a usable message."""

    for rule in rules:
        if not isinstance(rule, Mapping) or not any(
            key in rule for key in ("required", "min", "max", "pattern")
        ):
            return False
        if "pattern" in rule:
            try:
                re.compile(rule["pattern"])
            except re.error:
                return False
        if "message" in rule and not rule["message"].strip():
            return False
        if "min" in rule and "max" in rule and rule["min"] > rule["max"]:
            return False
    return True


def _tree_options(items: list[Any] | tuple[Any, ...], *, depth: int = 1) -> list[Mapping[str, Any]]:
    if depth > 8 and items:
        raise ValueError("tree option depth exceeds 8")
    result = []
    for item in items:
        result.append(item)
        result.extend(_tree_options(item.get("children", ()), depth=depth + 1))
        if len(result) > 512:
            raise ValueError("too many tree options")
    return result


def _workspace_path_ok(value: str) -> bool:
    return (
        bool(value) and "\\" not in value and "\0" not in value
        and not value.startswith("/") and ":" not in value
        and all(part and part not in {".", ".."} for part in value.split("/"))
    )


def _file_tree_nodes(items: Any) -> dict[str, Mapping[str, Any]]:
    nodes: dict[str, Mapping[str, Any]] = {}
    paths: set[str] = set()

    def walk(entries: Any, depth: int, parent: str | None) -> None:
        if entries and depth > 8:
            raise ValueError("file tree is too deep")
        for item in entries:
            key, path = item["key"], item["path"]
            if (not key or key in nodes or not _workspace_path_ok(path) or path in paths
                    or (parent is not None and not path.startswith(f"{parent}/"))):
                raise ValueError("invalid file tree identities or paths")
            nodes[key] = item
            paths.add(path)
            if len(nodes) > 512:
                raise ValueError("file tree has too many nodes")
            if item["kind"] == "file" and (
                "children" in item or "loaded" in item or "truncated" in item
            ):
                raise ValueError("file nodes cannot declare directory fields")
            if item.get("loaded") is False and "children" in item:
                raise ValueError("unloaded directory has children")
            walk(item.get("children", ()), depth + 1, path)

    walk(items, 1, None)
    return nodes


def slider_value_allowed(props: Mapping[str, Any], value: Any) -> bool:
    minimum, maximum, step = props.get("min", 0), props.get("max", 100), props.get("step", 1)
    if not finite_number(value) or not minimum <= value <= maximum or step <= 0:
        return False
    if props.get("dots"):
        return value in (minimum, maximum) or value in [float(key) for key in props.get("marks", {})]
    steps = (value - minimum) / step
    return finite_number(steps) and abs(steps - round(steps)) <= 1e-7


def valid_url(value: str, *, iframe: bool = False) -> bool:
    """Accept host-relative or HTTP(S) resources, with stricter iframe authorities."""
    if not value.strip():
        return False
    source = value.strip()
    if iframe:
        if re.search(r"[\x00-\x1f\x7f\\]", value) or source.startswith("///"):
            return False
        if re.match(r"^[a-z][a-z0-9+.-]*:", source, re.I) and not re.match(r"^https?://", source, re.I):
            return False
        authority = re.match(r"^(?:https?:)?//([^/?#]*)", source, re.I)
        if authority and (not authority[1] or re.search(r"[\s%@<>^|]", authority[1])):
            return False
    try:
        parsed = urlsplit(urljoin("https://webui.invalid/", source.replace("\\", "/")))
        parsed.port  # Validate the optional port while accepting port zero.
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and not re.search(r"[\s<>^|]", parsed.netloc)
                and (not iframe or not (parsed.username or parsed.password)))
    except ValueError:
        return False


MEDIA_URL_PREFIX = "/extension-assets/"
_MEDIA_PATH_CHARS = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@%/]*")
# Printable ASCII without the space and the backslash.
_MEDIA_FORBIDDEN = re.compile(r"[^\x21-\x5b\x5d-\x7e]")
_MEDIA_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MEDIA_BAD_SEGMENT = re.compile(r"[/\\\x00-\x1f\x7f]")


def valid_media_url(value: str) -> bool:
    """Accept only site-relative package or data asset paths for MediaSurface.

    The browser copy in ``catalog/components/mediaSurface.ts`` applies the same
    steps so both sides reach the same verdict.
    """
    if _MEDIA_FORBIDDEN.search(value):
        return False
    path = re.split(r"[?#]", value, maxsplit=1)[0]
    if not path.startswith(MEDIA_URL_PREFIX) or not _MEDIA_PATH_CHARS.fullmatch(path) or _MEDIA_BAD_ESCAPE.search(path):
        return False
    segments = path[len(MEDIA_URL_PREFIX):].split("/")
    if len(segments) < 3:
        return False
    for segment in segments:
        try:
            decoded = unquote(segment, errors="strict")
        except UnicodeDecodeError:
            return False
        if not segment or decoded in (".", "..") or _MEDIA_BAD_SEGMENT.search(decoded):
            return False
    return True


def _dimension_allowed(value: Any, *, allow_auto: bool) -> bool:
    if finite_number(value):
        return 0 <= value <= 4096
    if value == "auto":
        return allow_auto
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(px|%)", value) if isinstance(value, str) else None
    return match is not None and float(match[1]) <= (100 if match[2] == "%" else 4096)


def validate_props(component: str, props: Mapping[str, Any]) -> str | None:
    """Validate types, required fields and dependent component properties."""
    rule = COMPONENT_RULES[component]
    if not set(rule.get("required", ())) <= props.keys():
        return "required component prop missing"
    for key, value in props.items():
        if key not in rule["props"] or not check_value(rule["props"][key], value):
            return f"invalid {component}.{key}"
    if component in ("Form", "Form.Item") and not props["name"].strip():
        return "field name is required"
    if component == "Form.Item" and not _form_rules_ok(props.get("rules", ())):
        return "invalid Form.Item.rules"
    if component == "Form.Item" and props["name"] in {"__proto__", "constructor", "prototype"}:
        return "invalid field name"
    if component == "Button" and "form" in props and not props["form"].strip():
        return "form reference is required"
    if component == "Button":
        label = props.get("value", "")
        labels = label.values() if isinstance(label, Mapping) else (label,)
        accessible = props.get("aria-label", "")
        accessible_labels = accessible.values() if isinstance(accessible, Mapping) else (accessible,)
        if props.get("icon") and not all(text.strip() for text in labels) and not all(text.strip() for text in accessible_labels):
            return "icon-only buttons require a non-empty aria-label"
        if "target" in props and "href" not in props:
            return "button target requires href"
    if component == "Dropdown":
        items = props["items"]
        keys = [item["key"] for item in items if "key" in item]
        if not items or not _unique(keys) or any(not key.strip() for key in keys) or any(
            item.get("type") != "divider" and not item["label"] for item in items
        ) or any(isinstance(item.get("label"), str) and not item["label"].strip() for item in items):
            return "invalid menu items"
        align = props.get("align")
        if align and (len(align["points"]) != 2 or "offset" in align and len(align["offset"]) != 2):
            return "invalid menu alignment"
    if component == "Box":
        for key in ("width", "height", "minHeight", "maxWidth", "marginInline"):
            if key in props and not _dimension_allowed(props[key], allow_auto=key not in ("minHeight", "maxWidth")):
                return "invalid Box dimension"
        if "backgroundImage" in props and not valid_url(props["backgroundImage"]):
            return "invalid background URL"
    if component == "Input" and "maxLength" in props:
        if any(len(props[key].encode("utf-16-le")) // 2 > props["maxLength"]
               for key in ("value", "defaultValue") if key in props):
            return "initial text exceeds maxLength"
    if component == "Select":
        values = [item["value"] for item in props["options"]]
        value_type = str if props.get("mode") == "tags" else (str if values and isinstance(values[0], str) else float)
        if (not _unique(values) or any(value == "" or (isinstance(value, str)) != (value_type is str) for value in values)):
            return "invalid option values"
        for key in ("value", "defaultValue"):
            if key in props and not _selection_allowed(props[key], values, "mode" in props,
                                                      tags=props.get("mode") == "tags", maximum=props.get("maxCount")):
                return "invalid selection"
    if component == "TreeSelect":
        try:
            options = _tree_options(props["treeData"])
        except ValueError as exc:
            return str(exc)
        values = [item["value"] for item in options]
        keys = [item.get("key", item["value"]) for item in options]
        if not all(values) or not all(keys) or not _unique(values) or not _unique(keys):
            return "invalid tree identities"
        if props.get("treeCheckable") and props.get("multiple") is False:
            return "treeCheckable conflicts with multiple=false"
        multiple = props.get("treeCheckable", False) or props.get("multiple", False)
        if any(key in props and not _selection_allowed(props[key], values, multiple) for key in ("value", "defaultValue")):
            return "invalid tree selection"
        expanded = props.get("treeDefaultExpandedKeys", [])
        if not _unique(expanded) or any(key not in keys for key in expanded) or (expanded and props.get("treeDefaultExpandAll")):
            return "invalid tree expansion"
    if component == "Button" and "openPanel" in props and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", props["openPanel"],
    ) is None:
        return "invalid panel instance key"
    if component == "Slider":
        minimum, maximum, step = props.get("min", 0), props.get("max", 100), props.get("step", 1)
        if maximum <= minimum or step <= 0 or not finite_number((maximum - minimum) / step):
            return "invalid slider interval"
        count = (maximum - minimum) / step
        if abs(count - round(count)) > 1e-7:
            return "step must divide interval"
        try:
            marks = [float(key) if key.strip() else math.nan for key in props.get("marks", {})]
        except ValueError:
            return "invalid slider marks"
        if not _unique(marks) or any(not finite_number(mark) or not minimum <= mark <= maximum for mark in marks):
            return "invalid slider marks"
        if any(key in props and not slider_value_allowed(props, props[key]) for key in ("value", "defaultValue")):
            return "invalid slider value"
    if component == "Tabs":
        items = props["items"]
        keys = [item["key"] for item in items]
        if not all(keys) or not _unique(keys):
            return "invalid tab keys"
        enabled = [item["key"] for item in items if not item.get("disabled")]
        if any(key in props and props[key] not in enabled for key in ("activeKey", "defaultActiveKey")):
            return "invalid active tab"
    if component == "FileTree":
        try:
            nodes = _file_tree_nodes(props["treeData"])
        except ValueError as exc:
            return str(exc)
        selected = props.get("selectedKey")
        if selected is not None and nodes.get(selected, {}).get("kind") != "file":
            return "unknown selected file"
        expanded = props.get("expandedKeys", [])
        if not _unique(expanded) or any(nodes.get(key, {}).get("kind") != "directory" for key in expanded):
            return "unknown expanded directory"
    if component == "FilePreview":
        file = props["file"]
        if file is not None and (not file["name"].strip() or not _workspace_path_ok(file["path"])):
            return "invalid preview file"
        if file is not None and "content" not in file and "downloadUrl" not in file:
            return "preview content or URL is required"
        if file is not None and "downloadUrl" in file and not valid_url(file["downloadUrl"]):
            return "invalid preview URL"
    if component in ("Panel", "MediaSurface") and not props["title"].strip():
        return "title is required"
    if component == "FileImport":
        labels = props["label"].values() if isinstance(props["label"], Mapping) else (props["label"],)
        if not all(label.strip() for label in labels):
            return "label is required"
        accept = props.get("accept")
        if accept is not None and (not 1 <= len(accept) <= 16 or not all(FILE_SUFFIX.fullmatch(item) for item in accept)
                                   or not _unique([item.lower() for item in accept])):
            return "invalid FileImport accept"
        if props.get("directory") and ("accept" in props or "multiple" in props):
            return "directory import does not take accept or multiple"
    if component == "MediaSurface" and any(
        key in props and not valid_media_url(props[key]) for key in ("src", "poster")
    ):
        return "invalid media URL"
    if component == "ApplicationSurface":
        titles = [props["title"]] if isinstance(props["title"], str) else props["title"].values()
        if any(not title.strip() or len(title.encode("utf-8")) > 200 for title in titles):
            return "invalid ApplicationSurface title"
        if not valid_media_url(props["src"]):
            return "invalid ApplicationSurface URL"
        if len(json.dumps(props.get("config", {}), ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16 * 1024:
            return "ApplicationSurface config exceeds 16 KiB"
    if component == "Iframe":
        title = props["title"]
        titles = [title] if isinstance(title, str) else list(title.values())
        if any(not value.strip() or len(value.encode("utf-8")) > 200 for value in titles):
            return "invalid iframe title"
        if ("src" in props) == ("srcDoc" in props):
            return "Iframe requires exactly one document source"
        document = props.get("srcDoc")
        if document is not None:
            documents = [document] if isinstance(document, str) else list(document.values())
            if any(not value.strip() for value in documents):
                return "iframe document is required"
        source = props.get("src")
        if source is not None:
            sources = [source] if isinstance(source, str) else source.values()
            if any(not valid_url(value, iframe=True) for value in sources):
                return "invalid iframe URL"
    if component == "Avatar":
        style = props.get("style", {})
        if any(key in style and re.fullmatch(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", style[key]) is None
               for key in ("backgroundColor", "color")):
            return "Avatar colors must use hexadecimal notation"
    url_fields = {"Button": ("href",), "Avatar": ("src",), "Card": ("cover",), "Tag": ("href",), "Image": ("src", "fallback")}
    if any(key in props and not valid_url(props[key], iframe=component == "Iframe") for key in url_fields.get(component, ())):
        return "invalid resource URL"
    if component == "Image":
        if isinstance(props.get("preview"), Mapping) and not valid_url(props["preview"]["src"]):
            return "invalid preview URL"
        for key in ("width", "height"):
            if key not in props or props[key] == "auto":
                continue
            value = props[key]
            if finite_number(value) and 0 <= value <= 4096:
                continue
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(px|%)", value) if isinstance(value, str) else None
            if match is None or float(match[1]) > (100 if match[2] == "%" else 4096):
                return "invalid image dimension"
    if component == "Empty" and "image" in props and props["image"] not in ("default", "simple") and not valid_url(props["image"]):
        return "invalid Empty image"
    if component == "Divider" and props.get("orientation") == "vertical" and props.get("children"):
        return "vertical Divider cannot have a title"
    return None


def _selection_allowed(value: Any, values: list[Any], multiple: bool, *, tags: bool = False, maximum: int | None = None) -> bool:
    if multiple != isinstance(value, (list, tuple)):
        return False
    selected = list(value) if multiple else ([] if value is None else [value])
    return (_unique(selected) and (maximum is None or not multiple or len(selected) <= maximum)
            and all((isinstance(item, str) and bool(item)) if tags else item in values for item in selected))


def validate_event_data(component: str, event: str, data: Mapping[str, Any], props: Mapping[str, Any]) -> bool:
    """Check event data before business handlers, including mutable selection values."""
    rule = COMPONENT_RULES.get(component, {}).get("events", {}).get(event)
    if rule is None or not check_value(rule, data) or props.get("disabled") or props.get("loading"):
        return False
    if component == "Button":
        return "form" in props or not data
    if component == "Dropdown":
        return any(item.get("type") != "divider" and not item.get("disabled") and item["key"] == data["key"]
                   for item in props["items"])
    if component == "Input":
        return (not (props.get("readOnly") and event != "ui.click")
                and ("maxLength" not in props or len(data["value"].encode("utf-16-le")) // 2 <= props["maxLength"]))
    if component == "Slider":
        return slider_value_allowed(props, data["value"])
    if component == "Tabs":
        return any(item["key"] == data["activeKey"] and not item.get("disabled") for item in props["items"])
    if component in ("Select", "TreeSelect"):
        if event == "ui.clear":
            return props.get("allowClear", False)
        if event == "ui.search":
            return props.get("showSearch", "mode" in props)
        options = props["options"] if component == "Select" else _tree_options(props["treeData"])
        value = data["value"]
        if component == "Select" and event == "ui.change":
            initial = props.get("value", props.get("defaultValue"))
            retained = initial if isinstance(initial, (list, tuple)) else ([] if initial is None else [initial])
            values = [item["value"] for item in options if not item.get("disabled") or item["value"] in retained]
            if value is None and not props.get("allowClear"):
                return False
            if not _selection_allowed(value, values, "mode" in props,
                                      tags=props.get("mode") == "tags", maximum=props.get("maxCount")):
                return False
            selected = value if isinstance(value, (list, tuple)) else ([] if value is None else [value])
            return not any(item.get("disabled") and item["value"] in selected and item["value"] not in retained
                           for item in options)
        if component == "Select" and props.get("mode") == "tags":
            return isinstance(value, str) and bool(value)
        values = [item["value"] for item in options if not item.get("disabled")
                  and (item.get("checkable", True) and not item.get("disableCheckbox")
                       if component == "TreeSelect" and props.get("treeCheckable")
                       else item.get("selectable", True))]
        if component == "TreeSelect" and event == "ui.change":
            return _selection_allowed(value, values, props.get("treeCheckable", False) or props.get("multiple", False))
        return any(type(value) is type(item) and value == item or finite_number(value) and finite_number(item) and value == item
                   for item in values)
    if component == "FileImport":
        return bool(UPLOAD_ID.fullmatch(data["upload_id"]))
    if component == "FileTree":
        if props.get("status") not in (None, "ready"):
            return False
        try:
            nodes = _file_tree_nodes(props["treeData"])
        except ValueError:
            return False
        item = nodes.get(data.get("key"))
        if event == "ui.select":
            return item is not None and item["kind"] == "file"
        if event != "ui.change" or item is None or item["kind"] != "directory":
            return False
        if "expandedKeys" in data:
            expanded = data["expandedKeys"]
            if not _unique(expanded) or any(nodes.get(key, {}).get("kind") != "directory" for key in expanded):
                return False
            if (data["key"] in expanded) != data["expanded"]:
                return False
        return True
    return True
