# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Component rules for the Gateway-owned application slots.

The vocabulary matches the browser component catalog, but this copy is the
Gateway contract: it carries the Form / Form.Item / Dropdown interaction
components a package needs for an application-slot view. The AgentServer copy
does not enable them, so the two files intentionally diverge here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

from jiuwenswarm.gateway.extensions_v2.webui.constants import MAX_LIST_ITEMS, MAX_PROP_DEPTH, MAX_TEXT_BYTES


def token(*values: str) -> dict[str, Any]:
    return {"kind": "token", "values": values}


def array(item: Any) -> dict[str, Any]:
    return {"kind": "array", "item": item}


def union(*choices: Any) -> dict[str, Any]:
    return {"kind": "union", "choices": choices}


def obj(fields: Mapping[str, Any], *required: str) -> dict[str, Any]:
    return {"kind": "object", "fields": fields, "required": required}


TEXT = {"kind": "string"}
BOOL = {"kind": "boolean"}
NUMBER = {"kind": "number"}
NIL = {"kind": "null"}
INTEGER = {"kind": "number", "integer": True, "min": 0}
SIZE = token("small", "medium", "large")
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
    obj({"type": token("item"), "key": FIELD_NAME, "label": union(TEXT, {"kind": "nodes"}),
         "disabled": BOOL, "danger": BOOL}, "key", "label"),
    obj({"type": token("divider"), "key": FIELD_NAME}, "type"),
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
        "value": TEXT, "variant": token("default", "primary", "secondary", "danger", "ghost"),
        "tone": token("neutral", "info", "success", "warning", "danger"),
        "size": token("sm", "md", "lg"), "weight": token("regular", "medium", "bold"),
        "max_lines": NUMBER, "monospace": BOOL,
    }},
    "Button": {"props": {
        "label": TEXT, "variant": token("default", "primary", "secondary", "danger", "ghost"),
        "size": token("sm", "md", "lg"), "disabled": BOOL,
        # Name of a Form in the same render tree. When present the browser
        # validates that form and sends its field values with ``ui.click``.
        "form": FIELD_NAME,
    }, "events": {"ui.click": {"kind": "record", "value": TEXT}}},
    "Input": {"props": {
        **INPUT_PROPS, "value": TEXT, "defaultValue": TEXT, "placeholder": TEXT, "maxLength": INTEGER,
        "allowClear": BOOL, "showCount": BOOL, "type": token("text", "email", "tel", "url", "password"),
        "readOnly": BOOL, "autoComplete": TEXT,
    }, "events": {
        "ui.click": VALUE_TEXT, "ui.change": obj({"value": TEXT, "isComposing": BOOL}, "value", "isComposing"),
        "ui.press_enter": VALUE_TEXT,
    }},
    "Switch": {"props": {
        "checked": BOOL, "defaultChecked": BOOL, "loading": BOOL, "disabled": BOOL,
        "ariaLabel": TEXT, "size": token("medium", "small"), "checkedChildren": TEXT, "unCheckedChildren": TEXT,
    }, "events": {name: obj({"checked": BOOL}, "checked") for name in ("ui.click", "ui.change")}},
    "Select": {"props": {
        **INPUT_PROPS, "options": array(obj({"value": OPTION_VALUE, "label": TEXT, "disabled": BOOL}, "value", "label")),
        "value": union(OPTION_VALUE, NIL, array(OPTION_VALUE)),
        "defaultValue": union(OPTION_VALUE, NIL, array(OPTION_VALUE)), "placeholder": TEXT, "allowClear": BOOL,
        "mode": token("multiple", "tags"), "maxCount": {"kind": "number", "integer": True, "min": 1},
        "maxTagCount": union(INTEGER, token("responsive")), "showSearch": BOOL,
    }, "required": ("options",), "events": {
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
        "url": TEXT, "alt": TEXT,
        "size": union(token("small", "default", "large"), {"kind": "number", "min": 1, "max": 4096}),
        "shape": token("circle", "square"),
    }, "required": ("url",)},
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
        "size": token("medium", "small"), "variant": token("outlined", "borderless"),
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
            }, "name", "path", "content")),
            "emptyText": TEXT,
            "status": token("ready", "missing", "unsupported", "too_large", "forbidden", "unavailable", "error"),
        },
        "required": ("file",),
    },
    "Panel": {"props": {"title": {"kind": "string", "maxBytes": 200}}, "required": ("title",), "children": True},
    "Iframe": {"props": {"src": TEXT, "title": {"kind": "string", "maxBytes": 200}}, "required": ("src", "title")},
}


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
    if component in ("Panel", "Iframe") and not props["title"].strip():
        return "title is required"
    url_fields = {"Avatar": ("url",), "Card": ("cover",), "Tag": ("href",), "Image": ("src", "fallback"), "Iframe": ("src",)}
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
