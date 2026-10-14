"""Compose only declared parts of a control; retain independent targets."""
from ...protocol.primitives import utf16Length
from ...native_semantics import TABLE_FIELDS
from ..tree import disclosure, isDialog, hasIncompleteChildren, valuePreviewLimit
from .policy import layout, sameBlocking, sources, ordinaryControl
from .labels import passiveLabel


def merge_editors(state, node, scope):
    if node.get('control', {}).get('kind') == 'textInput':
        node['children'] = [c for child in node['children'] for c in compactInput(state, child, node)]


def merge_control_labels(state, node, scope):
    if scope.in_table or not ordinaryControl(node):
        return
    if not node.get('name'):
        if 'control' in node:
            parts = []
            for member in node['control']['members']:
                source = state.nodes.get(member['ref'])
                if member['part'] == 'label' and source is not None and utf16Length(source.get('value', '')) <= valuePreviewLimit:
                    parts.extend(sources(source))
        else:
            parts = [part for child in node['children'] for part in (labelParts(state, child, node) or [])]
        if parts:
            state.adopt(node, parts, 'name')
    node['children'] = [c for child in node['children'] for c in compactLabel(state, child, node)]


def memberLayout(state, node, target, ignoreFocus=False, ignoreSelected=False):
    if (node.get('_factBacked') and node['role'] == 'cell' and target['role'] == 'row'
            and state.memberOf(node, target)):
        # A single-column outline/list item's cell is a presentation part,
        # not a spreadsheet cell. Keep its coordinates/focus as source facts.
        # The control-label pass excludes table rows and multi-cell records.
        if any(node.get(key, {}).get('length', 1) != 1 for key in ('rowRange', 'columnRange')):
            return False
        candidate = {key: value for key, value in node.items() if key not in ('rowRange', 'columnRange', 'index')}
        if candidate.get('titleRef') in state.nodes and state.memberOf(state.nodes[candidate['titleRef']], target):
            candidate.pop('titleRef')
        return layout(candidate, ignoreFocus, ignoreSelected)
    return layout(node, ignoreFocus, ignoreSelected)

def labelParts(state, node, target):
    if (target["role"] == "row" and node["role"] == "cell" and node.get("titleRef") and state.ancestorOf(node["ref"], node["titleRef"])
            and not node.get("incompleteLabelRelation") and not node.get("valueExpected") and "value" not in node
            and sameBlocking(node, target) and not node.get("modal") and not isDialog(node)):
        title = state.nodes.get(node["titleRef"])
        if (title is not None and not title.get("incompleteLabelRelation") and sameBlocking(title, target)
                and len(state.targets.get(title["ref"], {})) == 1 and node["ref"] in state.targets[title["ref"]]
                and utf16Length(title.get("value", "")) <= valuePreviewLimit):
            return sources(title)
    if "capabilities" in target and not state.memberOf(node, target):
        return None
    if (node["role"] == "staticText" and not any(node.get(k) for k in ("pressable", "selectable", "focusable", "valueExpected", "incompleteLabelRelation", "labelTargets"))
            and not node["children"] and "partialReason" not in node and utf16Length(node.get("value", "")) <= valuePreviewLimit):
        return sources(node)
    if (node["role"] == "image" and not any(node.get(k) for k in ("pressable", "selectable", "focusable", "actions", "actionsUnavailable"))
            and not node["children"] and "partialReason" not in node):
        return []
    if not memberLayout(state, node, target, True, True):
        return None
    result = []
    for child in node["children"]:
        parts = labelParts(state, child, target)
        if parts is None:
            return None
        result.extend(parts)
    return result

def compactLabel(state, node, target):
    if "capabilities" in target and not state.memberOf(node, target):
        return [node]
    if passiveLabel(state, node, target) and not node["children"] and state.represented(node, target):
        state.fold(node, target, "label")
        return []
    declaredLayout = any(m["ref"] == node["ref"] and m["part"] == "layout" for m in target.get("control", {}).get("members", []))
    if ((not declaredLayout and labelParts(state, node, target) is None) or not memberLayout(state, node, target, True, True) or state.keep(node) or not sameBlocking(node, target)):
        return [node]
    repeatedSelection = node["role"] == "cell" and target["role"] == "row" and node.get("selected") is True and target.get("selected") is True
    internalFocus = node["role"] == "cell" and ordinaryControl(target) and not (node["ref"] == state.context.get("focusedRef") and node.get("focused") is False)
    node["children"] = [c for child in node["children"] for c in compactLabel(state, child, target)]
    if memberLayout(state, node, target, internalFocus, repeatedSelection) and (node["ref"] != state.context.get("focusedRef") or internalFocus):
        state.fold(node, target, "layout")
        return node["children"]
    return [node]

def compactInput(state, node, owner):
    member = next((m for m in owner.get("control", {}).get("members", []) if m["ref"] == node["ref"]), None)
    if member is None or "control" in node or state.keep(node) or not sameBlocking(node, owner):
        return [node]
    node["children"] = [c for child in node["children"] for c in compactInput(state, child, owner)]
    ownerText = {owner.get("name"), owner.get("description"), owner.get("value")}
    if (any(node.get(k) and node[k] not in ownerText for k in ("name", "description"))
            or ("value" in node and node["value"] != owner.get("value"))
            or ("value" in node and bool(node.get("valueIsNumber")) != bool(owner.get("valueIsNumber")))
            or (node.get("valueExpected") and "value" not in node)
            or (node.get("selectable") and not owner.get("selectable"))
            or (node.get("pressable") and not owner.get("pressable") and node.get("capabilities", {}).get("click") != owner.get("capabilities", {}).get("click"))
            or any(v == "available" and owner.get("capabilities", {}).get(k) != "available" for k, v in node.get("capabilities", {}).items())
            or any(node.get(k) for k in ("actions", "actionsUnavailable", "help", "url"))
            or (node.get("placeholder") and node["placeholder"] != owner.get("placeholder"))
            or hasIncompleteChildren(node) or any(node.get(k) for k in ("labelTargets", "incompleteLabelRelation", "preserveGroup", "subrole", "modal"))
            or ("selected" in node and node["selected"] != owner.get("selected"))
            or (node.get("enabled") is False and owner.get("enabled") is not False)
            or node.get("expandable") or node.get("submenu") or disclosure(node) is not None
            or "shortcut" in node or "menuMark" in node or any(key in node for key in TABLE_FIELDS)
            or (node["ref"] == state.context.get("focusedRef") and node.get("focused") is False)):
        return [node]
    if "selectionRange" in node or "selectedText" in node:
        if ("value" not in owner or node.get("value") != owner["value"]
                or ("selectionRange" in owner and owner["selectionRange"] != node.get("selectionRange"))
                or ("selectedText" in owner and owner["selectedText"] != node.get("selectedText"))):
            return [node]
        if "selectionRange" in node:
            owner["selectionRange"] = dict(node["selectionRange"])
        if "selectedText" in node:
            owner["selectedText"] = node["selectedText"]
    state.fold(node, owner, "editor" if member["part"] == "editor" else "layout")
    return node["children"]
