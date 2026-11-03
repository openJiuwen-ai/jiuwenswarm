"""Shared guards for lossless presentation, not native capability discovery."""
from ...native_semantics import TABLE_FIELDS
from ..presentation_policy import independent_actions
from ..tree import carries_value, disclosure, has_display_value, has_incomplete_children, is_dialog, unique

WRAPPERS = {"group", "cell", "column"}
REGIONS = {
    "window", "sheet", "group", "splitGroup", "scrollArea", "toolbar", "tabGroup", "outline", "list", "table",
    "webArea", "document"
}
CONTROLS = {
    "button", "checkBox", "radioButton", "switch", "textField", "textArea", "comboBox", "popUpButton", "menuButton",
    "link", "row"
}


def sources(node):
    result = []
    for attribute in ("name", "description", "value"):
        if not node.get(attribute):
            continue
        parts = [part for part in node['textSources'] if part['field'] == attribute]
        if attribute == "value" and not parts:
            result.append(dict(sourceRef=node["ref"], attribute=attribute, field="name", text=node["value"]))
        else:
            result.extend(parts)
    return result


def content(node):
    return unique(part["text"] for part in sources(node))


def protected_content(node):
    return bool("selectionRange" in node or node.get("selectedText") or node.get("actionsUnavailable")
                or node.get("placeholder")
                or (independent_actions(node) if node.get('_factBacked') else node.get("actions"))
                or node.get("busy") is True or has_incomplete_children(node) or "shortcut" in node or "menuMark" in node
                or "toggleState" in node
                or "disclosureLevel" in node or node.get("disclosedBy") or node.get("incompleteDisclosureRelation")
                or any(key in node for key in TABLE_FIELDS))


def independent(node):
    capabilities = node.get('capabilities', {})
    available = any(value == 'available' for key, value in capabilities.items()
                    if not (node.get('_factBacked') and node['role'] in WRAPPERS and key == 'focus'))
    return bool(available or (node.get("focusable") and node["role"] not in WRAPPERS) or node.get("labelTargets")
                or node.get("incompleteLabelRelation") or node.get("preserveGroup")
                or (node.get("subrole") and node["subrole"] != "hostingView") or "control" in node
                or node.get("titleRef") or carries_value(node) or node.get("help") or node.get("url")
                or (node.get("identifier") and not node.get("name") and not node.get("description")
                    and not node.get("value") and not node["children"]) or protected_content(node) or is_dialog(node)
                or node.get("modal"))


def same_blocking(a, b):
    return ",".join(a.get("blockedBy", [])) == ",".join(b.get("blockedBy", []))


def layout(node, ignore_focus=False, ignore_selected=False):
    return (node["role"] in WRAPPERS and not node.get("name") and not node.get("description")
            and not has_display_value(node) and not independent(node) and not node.get("pressable")
            and not node.get("selectable") and node.get("enabled") is not False
            and (ignore_selected or not node.get("selected")) and (ignore_focus or not node.get("focused"))
            and not node.get("expandable") and not node.get("submenu") and disclosure(node) is None)


def ordinary_control(node):
    return (node["role"] in CONTROLS or bool(node.get("pressable"))
            or bool(node.get('_factBacked') and node.get('capabilities', {}).get('click') == 'available'))


def secondary_actions(node):
    result = []
    for action in node.get("actions", []):
        name, caps = action["name"], node.get("capabilities")
        if name == "AXPress" and not node.get("selectable") and (caps.get("click") == "available"
                                                                 if caps is not None else ordinary_control(node)
                                                                 or node["role"] in ("menuItem", "menuBarItem")):
            continue
        primary_pick_action = name == "AXPick" and not node.get("selectable") and caps is None and node["role"] in (
            "menuItem", "menuBarItem")
        if primary_pick_action:
            continue
        primary_menu_action = (name == "AXShowMenu" and not node.get("selectable") and node.get("pressable")
                               and node["role"] in ("popUpButton", "menuButton", "comboBox")
                               and (caps is None or
                                    (caps.get("click") == "available" and not any(a["name"] == "AXPress"
                                                                                  for a in node.get("actions", [])))))
        if primary_menu_action:
            continue
        if name in ("AXExpand", "AXCollapse") and node.get("expandable") and caps is None:
            continue
        result.append(action)
    return result
