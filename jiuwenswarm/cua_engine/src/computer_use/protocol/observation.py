"""Bounded platform-neutral observation validation, before any projection."""
from .primitives import safeInteger
from .schema import array, boolean, count, enum, number, obj, ref, role, string, union, unknown

_strings = ("roleDescription", "name", "description", "value", "identifier", "selectedText", "shortcut",
            "help", "url", "placeholder", "partialReason", "subrole", "menuMark")
_booleans = ("enabled", "selected", "expanded", "disclosing", "expandable", "focused", "pressable", "selectable",
             "submenu", "focusable", "incompleteLabelRelation", "linkedLabel", "preserveGroup", "valueExpected", "modal", "valueIsNumber")
_counts = ("childrenRead", "childCount", "childrenStart", "nextChildOffset", "childrenEnd")
_availability = enum("available", "unavailable", "unknown")
_node = obj({
    "ref": ref, "role": role, "children": array(unknown()),
    **{k: string().optional() for k in _strings},
    **{k: boolean().optional() for k in _booleans},
    **{k: count.optional() for k in _counts},
    "titleRef": ref.optional(), "labelTargets": array(ref).optional(), "editorRef": ref.optional(),
    "ownedModals": array(ref).optional(), "blockedBy": array(ref).optional(),
    "childOmissions": array(string()).optional(),
    "selectionRange": obj(dict(location=count, length=count)).optional(),
    "actions": array(obj(dict(name=string(min=1), description=string().optional()))).optional(),
    "actionsUnavailable": boolean().optional(), "disclosureLevel": count.optional(),
    "disclosedBy": ref.optional(), "incompleteDisclosureRelation": boolean().optional(),
    "capabilities": obj({k: _availability for k in ("click", "typeText", "replaceText", "focus")}).optional(),
    "control": obj(dict(kind=enum("item", "button", "textInput"),
                        members=array(obj(dict(ref=ref, part=enum("label", "layout", "editor"))), max=64))).optional(),
})
_context = obj(dict(app=string().optional(), focusedRef=union(ref, enum("")).optional(),
                    focusAncestorRefs=array(ref).optional(), menuQuery=boolean().optional(),
                    includeBackground=boolean().optional(), subtreeRef=ref.optional(), inheritedBlocks=array(ref).optional()))
_window = obj(dict(ref=ref, title=string(), kind=string().optional(), parent=ref.optional(),
                   modal=boolean().optional(), onScreen=boolean().optional(), document=string(min=1).optional()))
_positive_count = number(integer=True, min=0, exclusiveMin=True, max=9_007_199_254_740_991)
_observation = obj(dict(root=unknown(), context=_context, windows=array(_window), current=ref.nullable(),
                        title=string().optional(), document=string(min=1).nullable().optional(), unavailable=string().optional(),
                        screenshot=obj(dict(width=_positive_count, height=_positive_count)).optional(), screenshotError=string().optional(),
                        overviewText=string().optional(), notes=array(string()).optional(),
                        sharedFocus=obj(dict(refs=array(ref), outside=count)).optional(),
                        applicationMenu=obj(dict(mainWindow=ref.nullable(), focusedWindow=ref.nullable())).optional(),
                        menuPath=array(string()).optional(), commandRef=ref.optional()))


def decodeObservation(value):
    observation = _observation.parse(value)
    seen = set()
    node_count = 0

    def decode(raw, depth):
        nonlocal node_count
        node_count += 1
        if node_count > 8192 or depth > 128:
            raise ValueError("protocol: observation tree exceeds node/depth limit")
        parsed = _node.parse(raw)
        if parsed["ref"] in seen:
            raise ValueError("protocol: duplicate node reference")
        seen.add(parsed["ref"])
        selection = parsed.get("selectionRange")
        if selection is not None and not safeInteger(selection["location"] + selection["length"]):
            raise ValueError("protocol: invalid selection range")
        if "childCount" in parsed and "childrenRead" in parsed and parsed["childrenRead"] > parsed["childCount"]:
            raise ValueError("protocol: invalid child coverage")
        parsed["children"] = [decode(child, depth + 1) for child in parsed["children"]]
        return parsed

    windows = set()
    for window in observation["windows"]:
        if window["ref"] in windows:
            raise ValueError("protocol: duplicate window reference")
        windows.add(window["ref"])
    if observation["current"] is not None and observation["current"] not in windows:
        raise ValueError("protocol: unknown current window")
    if "applicationMenu" in observation:
        for window in observation["applicationMenu"].values():
            if window is not None and window not in windows:
                raise ValueError("protocol: unknown application menu window")
    raw = observation.get("root")
    root = None if raw is None and "root" in observation else decode(raw, 0)
    nodes, parents, owners = {}, {}, {}

    def index(node):
        nodes[node["ref"]] = node
        for child in node["children"]:
            parents[child["ref"]] = node["ref"]
            index(child)

    if root is not None:
        index(root)
    boundaries = ("window", "sheet", "table", "document", "webArea")
    for node in nodes.values():
        for member in node.get("control", {}).get("members", []):
            ref_value = member["ref"]
            if ref_value not in nodes or ref_value in owners or ref_value == node["ref"]:
                raise ValueError("protocol: invalid control member")
            child = nodes[ref_value]
            if ((member["part"] == "label" and child["role"] not in ("staticText", "heading"))
                    or (member["part"] == "layout" and child["role"] not in ("group", "unknown", "cell"))
                    or (member["part"] == "editor" and node["control"]["kind"] != "textInput") ):
                raise ValueError("protocol: invalid control member semantics")
            parent = parents.get(ref_value)
            while parent is not None and parent != node["ref"]:
                ancestor = nodes[parent]
                if ancestor["role"] in boundaries or "control" in ancestor:
                    break
                parent = parents.get(parent)
            if parent != node["ref"]:
                raise ValueError("protocol: control member crosses its containment scope")
            if child["role"] in boundaries:
                raise ValueError("protocol: invalid control member role")
            owners[ref_value] = node["ref"]
    return {**observation, "root": root}
